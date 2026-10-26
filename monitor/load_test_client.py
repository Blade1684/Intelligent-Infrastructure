import os
import time
import json
import threading
from datetime import datetime, timezone
import requests

DEFAULT_LOCUST_URL = os.getenv("LOCUST_URL", "http://locust:8089")

PROFILES = {
    "normal": {
        "name": "Normal Load",
        "description": "Low volume baseline testing",
        "user_count": 5,
        "spawn_rate": 1,
        "duration_seconds": 30
    },
    "moderate": {
        "name": "Moderate Load",
        "description": "Medium concurrency traffic",
        "user_count": 20,
        "spawn_rate": 4,
        "duration_seconds": 30
    },
    "heavy": {
        "name": "Heavy Load",
        "description": "High concurrency stress testing",
        "user_count": 50,
        "spawn_rate": 10,
        "duration_seconds": 30
    }
}

MAX_USERS = 100
MAX_SPAWN_RATE = 20
MAX_DURATION_SECONDS = 300
MIN_DURATION_SECONDS = 5


class LoadTestManager:
    """
    Coordinates Locust load testing sessions:
    manages lifecycle, safety limits, live metric collection, and history persistence.
    """

    def __init__(self, locust_url=None, redis_client=None):
        self.locust_url = (locust_url or DEFAULT_LOCUST_URL).rstrip("/")
        self.redis_client = redis_client
        self._lock = threading.Lock()
        self._timer = None
        self.active_session = None

    def get_profiles(self):
        return PROFILES

    def validate_parameters(self, params):
        profile_key = params.get("profile", "normal").lower()
        if profile_key in PROFILES:
            defaults = PROFILES[profile_key]
            user_count = int(params.get("user_count", defaults["user_count"]))
            spawn_rate = int(params.get("spawn_rate", defaults["spawn_rate"]))
            duration_s = int(params.get("duration_seconds", defaults["duration_seconds"]))
        else:
            profile_key = "custom"
            user_count = int(params.get("user_count", 10))
            spawn_rate = int(params.get("spawn_rate", 2))
            duration_s = int(params.get("duration_seconds", 30))

        if user_count < 1 or user_count > MAX_USERS:
            raise ValueError(f"user_count must be between 1 and {MAX_USERS}")
        if spawn_rate < 1 or spawn_rate > MAX_SPAWN_RATE:
            raise ValueError(f"spawn_rate must be between 1 and {MAX_SPAWN_RATE}")
        if duration_s < MIN_DURATION_SECONDS or duration_s > MAX_DURATION_SECONDS:
            raise ValueError(f"duration_seconds must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS}")

        return {
            "profile": profile_key,
            "user_count": user_count,
            "spawn_rate": spawn_rate,
            "duration_seconds": duration_s,
            "host": params.get("host", "http://haproxy:80")
        }

    def start_test(self, params):
        with self._lock:
            if self.active_session and self.active_session.get("status") in {"starting", "running"}:
                raise RuntimeError("A load test is already in progress.")

            config = self.validate_parameters(params)
            session_id = f"loadtest-{int(time.time())}"
            started_at = datetime.now(timezone.utc).isoformat()

            # 1. Reset Locust stats
            try:
                requests.post(f"{self.locust_url}/stats/reset", timeout=3)
            except Exception as e:
                # Still try to continue or warn
                pass

            # 2. Trigger swarm
            try:
                swarm_resp = requests.post(
                    f"{self.locust_url}/swarm",
                    data={
                        "user_count": config["user_count"],
                        "spawn_rate": config["spawn_rate"],
                        "host": config["host"]
                    },
                    timeout=5
                )
                if swarm_resp.status_code != 200:
                    raise RuntimeError(f"Locust returned HTTP {swarm_resp.status_code}: {swarm_resp.text}")
            except Exception as err:
                raise RuntimeError(f"Failed to communicate with Locust: {err}")

            self.active_session = {
                "session_id": session_id,
                "status": "running",
                "config": config,
                "started_at": started_at,
                "start_time_monotonic": time.monotonic(),
                "duration_seconds": config["duration_seconds"],
                "target_host": config["host"],
                "metrics": {}
            }

            # 3. Schedule auto-stop timer
            self._timer = threading.Timer(config["duration_seconds"], self._auto_stop_callback, args=[session_id])
            self._timer.daemon = True
            self._timer.start()

            return dict(self.active_session)

    def _auto_stop_callback(self, session_id):
        with self._lock:
            if not self.active_session or self.active_session.get("session_id") != session_id:
                return
            if self.active_session.get("status") != "running":
                return
            print(f"[LoadTestManager] Duration expired for {session_id}, auto-stopping.")
        self.stop_test(final_status="completed")

    def stop_test(self, final_status="stopped"):
        with self._lock:
            if not self.active_session or self.active_session.get("status") not in {"running", "starting"}:
                return self.active_session or {"status": "idle"}

            if self._timer:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

            # Command Locust to stop generating traffic
            try:
                requests.post(f"{self.locust_url}/stop", timeout=3)
            except Exception as err:
                try:
                    requests.get(f"{self.locust_url}/stop", timeout=3)
                except Exception:
                    print(f"[LoadTestManager] Error stopping Locust: {err}")

            # Collect final metrics snapshot
            final_metrics = self.get_live_metrics_unlocked()
            completed_at = datetime.now(timezone.utc).isoformat()
            duration_actual = round(time.monotonic() - self.active_session["start_time_monotonic"], 2)

            self.active_session["status"] = final_status
            self.active_session["completed_at"] = completed_at
            self.active_session["duration_actual_seconds"] = duration_actual
            self.active_session["final_metrics"] = final_metrics

            # Archive to Redis history
            self._archive_session(self.active_session)

            result = dict(self.active_session)
            return result

    def get_status(self):
        with self._lock:
            if not self.active_session:
                return {"status": "idle"}

            elapsed = round(time.monotonic() - self.active_session.get("start_time_monotonic", time.monotonic()), 1)
            duration = self.active_session.get("duration_seconds", 30)
            remaining = max(0.0, round(duration - elapsed, 1))

            live_stats = self.get_live_metrics_unlocked()

            return {
                "session_id": self.active_session.get("session_id"),
                "status": self.active_session.get("status"),
                "profile": self.active_session.get("config", {}).get("profile"),
                "config": self.active_session.get("config", {}),
                "started_at": self.active_session.get("started_at"),
                "completed_at": self.active_session.get("completed_at"),
                "elapsed_seconds": elapsed,
                "remaining_seconds": remaining,
                "live_metrics": live_stats
            }

    def get_live_metrics_unlocked(self):
        """Fetch current metrics from Locust API and distribution endpoint."""
        try:
            req_resp = requests.get(f"{self.locust_url}/stats/requests", timeout=2)
            if req_resp.status_code != 200:
                return self._empty_metrics()
            req_data = req_resp.json()

            # Query backend distribution
            dist_data = {}
            try:
                d_resp = requests.get(f"{self.locust_url}/stats/distribution", timeout=2)
                if d_resp.status_code == 200:
                    dist_data = d_resp.json()
            except Exception:
                pass

            return self.normalize_locust_stats(req_data, dist_data)
        except Exception as err:
            return self._empty_metrics(error=str(err))

    @staticmethod
    def normalize_locust_stats(locust_json, dist_json=None):
        """Extract and structure all required performance, latency, error, and distribution metrics."""
        stats_list = locust_json.get("stats", [])
        aggregated = next((s for s in stats_list if s.get("name") == "Aggregated"), {})

        total_requests = int(aggregated.get("num_requests", 0))
        num_failures = int(aggregated.get("num_failures", 0))
        successful_requests = max(0, total_requests - num_failures)
        current_rps = round(float(locust_json.get("current_rps", 0.0) or aggregated.get("current_rps", 0.0)), 2)
        total_rps = round(float(locust_json.get("total_rps", 0.0) or aggregated.get("total_rps", 0.0) or current_rps), 2)

        avg_latency = round(float(aggregated.get("avg_response_time", 0.0)), 2)
        min_latency = round(float(aggregated.get("min_response_time", 0.0)), 2)
        max_latency = round(float(aggregated.get("max_response_time", 0.0)), 2)
        median_latency = round(float(aggregated.get("median_response_time", 0.0)), 2)

        percentiles = locust_json.get("current_response_time_percentiles", {})
        p50 = percentiles.get("response_time_percentile_0.5") or median_latency
        p95 = percentiles.get("response_time_percentile_0.95") or aggregated.get("response_time_percentile_0.95", 0.0)
        p99 = percentiles.get("response_time_percentile_0.99") or aggregated.get("response_time_percentile_0.99", 0.0)

        # Error metrics
        fail_ratio = float(locust_json.get("fail_ratio", 0.0)) * 100
        if fail_ratio == 0.0 and total_requests > 0 and num_failures > 0:
            fail_ratio = (num_failures / total_requests) * 100
        fail_ratio = round(fail_ratio, 2)
        errors = locust_json.get("errors", [])

        # Backend distribution
        distribution = {}
        percentages = {}
        if dist_json and "distribution" in dist_json:
            distribution = dist_json.get("distribution", {})
            percentages = dist_json.get("percentages", {})
        else:
            # Map canonical server names if present
            for i in range(1, 6):
                sname = f"Server-{i}"
                distribution[sname] = 0
                percentages[sname] = 0.0

        return {
            "traffic": {
                "total_requests": total_requests,
                "successful_requests": successful_requests,
                "failed_requests": num_failures,
                "current_rps": current_rps,
                "total_rps": total_rps,
                "rps": total_rps,
                "active_users": int(locust_json.get("user_count", 0)),
                "state": locust_json.get("state", "ready")
            },
            "response_times_ms": {
                "avg": avg_latency,
                "min": min_latency,
                "max": max_latency,
                "median": median_latency,
                "p50": round(float(p50 or 0.0), 2),
                "p95": round(float(p95 or 0.0), 2),
                "p99": round(float(p99 or 0.0), 2)
            },
            "errors": {
                "error_count": num_failures,
                "error_rate_pct": fail_ratio,
                "http_error_count": num_failures,
                "http_error_rate_pct": fail_ratio,
                "connection_failures": 0,
                "timeout_failures": 0,
                "error_details": errors
            },
            "backend_distribution": {
                "counts": distribution,
                "percentages": percentages,
                "total_recorded": sum(distribution.values())
            }
        }

    def _empty_metrics(self, error=None):
        return {
            "traffic": {
                "total_requests": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "current_rps": 0.0,
                "total_rps": 0.0,
                "active_users": 0,
                "state": "unreachable" if error else "ready"
            },
            "response_times_ms": {
                "avg": 0.0, "min": 0.0, "max": 0.0, "median": 0.0,
                "p50": 0.0, "p95": 0.0, "p99": 0.0
            },
            "errors": {
                "error_count": 0,
                "error_rate_pct": 0.0,
                "error_details": [{"error": error}] if error else []
            },
            "backend_distribution": {
                "counts": {f"Server-{i}": 0 for i in range(1, 6)},
                "percentages": {f"Server-{i}": 0.0 for i in range(1, 6)},
                "total_recorded": 0
            }
        }

    def _archive_session(self, session_data):
        if not self.redis_client or not hasattr(self.redis_client, "lpush"):
            return
        try:
            # Prepare clean serializable summary
            summary = {
                "session_id": session_data.get("session_id"),
                "profile": session_data.get("config", {}).get("profile"),
                "config": session_data.get("config"),
                "status": session_data.get("status"),
                "started_at": session_data.get("started_at"),
                "completed_at": session_data.get("completed_at"),
                "duration_actual_seconds": session_data.get("duration_actual_seconds"),
                "metrics": session_data.get("final_metrics")
            }
            self.redis_client.lpush("load_test:history", json.dumps(summary))
            if hasattr(self.redis_client, "ltrim"):
                self.redis_client.ltrim("load_test:history", 0, 49)
        except Exception as e:
            print(f"[LoadTestManager] Error archiving session to Redis: {e}")

    def get_history(self, limit=20):
        if not self.redis_client or not hasattr(self.redis_client, "lrange"):
            return []
        try:
            records = self.redis_client.lrange("load_test:history", 0, limit - 1)
            return [json.loads(r) for r in records]
        except Exception as e:
            print(f"[LoadTestManager] Error fetching history from Redis: {e}")
            return []
