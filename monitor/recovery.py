import json
import os
import threading
import time
from datetime import datetime, timezone

import requests


SERVER_STATES = {}
RECOVERY_ATTEMPTS = 2
RECOVERY_STABILIZATION_SECONDS = 8
RECOVERY_RETRY_DELAY_SECONDS = 2
HEALTH_CHECK_TIMEOUT = 2


def record_event(service, event_type, details=None, redis_client=None):
    """Keep a bounded operational audit trail alongside incident records."""
    event = {
        "server": service,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details or {}
    }
    if redis_client and hasattr(redis_client, "rpush"):
        try:
            redis_client.rpush(f"events:{service}", json.dumps(event))
            if hasattr(redis_client, "ltrim"):
                redis_client.ltrim(f"events:{service}", -50, -1)
        except Exception as err:
            print(f"Error logging event to Redis: {err}")
    return event


def calculate_cpu_percent(stats):
    """Calculate CPU usage percentage from raw Docker container stats."""
    if not stats or not isinstance(stats, dict):
        return 0.0
    cpu_stats = stats.get("cpu_stats", {})
    precpu_stats = stats.get("precpu_stats", {})
    cpu_usage = cpu_stats.get("cpu_usage", {})
    precpu_usage = precpu_stats.get("cpu_usage", {})
    cpu_delta = cpu_usage.get("total_usage", 0) - precpu_usage.get("total_usage", 0)
    system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
    online_cpus = cpu_stats.get("online_cpus", 1)

    if system_delta > 0 and cpu_delta > 0:
        return round((cpu_delta / system_delta) * online_cpus * 100, 2)
    return 0.0


def set_server_state(service, state, reason=None, redis_client=None):
    """Store operational state in memory and Redis for monitor-restart resilience."""
    server_state = {
        "server": service,
        "state": state,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    SERVER_STATES[service] = server_state
    if redis_client and hasattr(redis_client, "set"):
        try:
            redis_client.set(f"server_state:{service}", json.dumps(server_state))
        except Exception as err:
            print(f"Error persisting server state to Redis: {err}")

    print(f"\nSERVER STATE CHANGED\nServer: {service}\nState: {state}")
    if reason:
        print(f"Reason: {reason}")
    return server_state


def get_server_state(service, redis_client=None):
    if redis_client and hasattr(redis_client, "get"):
        try:
            persisted_state = redis_client.get(f"server_state:{service}")
            if persisted_state:
                return json.loads(persisted_state)
        except Exception:
            pass
    return SERVER_STATES.get(service, {
        "server": service,
        "state": "healthy",
        "reason": None
    })


def get_container(service, docker_client):
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.service={service}"}
    )
    if not containers:
        raise RuntimeError(f"Container not found: {service}")
    return containers[0]


def isolate_server(service, anomaly_type, docker_client, redis_client=None, stop_container=True):
    set_server_state(
        service, "isolated", f"Detected {anomaly_type}", redis_client
    )
    record_event(service, "server_isolated", {
        "anomaly_type": anomaly_type,
        "stop_container": stop_container
    }, redis_client)

    if stop_container:
        try:
            container = get_container(service, docker_client)
            container.reload()
            print(f"\nISOLATING SERVER: {service}")
            if container.status == "running":
                print(f"Stopping container: {container.name}")
                container.stop()
            print(f"SERVER {service} ISOLATED")
        except Exception as error:
            set_server_state(service, "failed", f"Isolation failed: {error}", redis_client)
            print(f"Isolation error: {error}")
    else:
        print(f"\nSERVER {service} MARKED ISOLATED (in-place remediation pending)")


def application_is_healthy(service):
    """Verify the actual Flask endpoint, not merely the Docker process."""
    try:
        response = requests.get(
            f"http://{service}:5000/health", timeout=HEALTH_CHECK_TIMEOUT
        )
        return response.status_code == 200 and response.json().get("status") == "healthy"
    except Exception:
        return False


def recover_server(service, docker_client, redis_client=None):
    """Restart and verify a service, retrying before declaring escalation-worthy failure."""
    last_error = "Application health check failed"

    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        try:
            set_server_state(
                service,
                "recovering",
                f"Automatic recovery attempt {attempt} of {RECOVERY_ATTEMPTS}",
                redis_client
            )
            container = get_container(service, docker_client)
            container.reload()
            if container.status != "running":
                print(f"\nStarting container: {container.name}")
                container.start()
            elif hasattr(container, "restart"):
                print(f"\nRestarting container: {container.name}")
                container.restart()

            print(f"Waiting for {service} to stabilize...")
            time.sleep(RECOVERY_STABILIZATION_SECONDS)
            container.reload()
            if container.status == "running" and application_is_healthy(service):
                set_server_state(
                    service,
                    "healthy",
                    "Recovery verified by application health endpoint",
                    redis_client
                )
                print(f"\nSERVER {service} RECOVERED SUCCESSFULLY")
                return True

            last_error = f"Container status={container.status}; health check failed"
        except (Exception, requests.RequestException, ValueError) as error:
            last_error = str(error)
            print(f"Recovery attempt {attempt} error: {error}")

        if attempt < RECOVERY_ATTEMPTS:
            time.sleep(RECOVERY_RETRY_DELAY_SECONDS)

    set_server_state(
        service,
        "failed",
        f"Recovery failed after {RECOVERY_ATTEMPTS} attempts: {last_error}",
        redis_client
    )
    return False


# ============================================================
# INTELLIGENT RECOVERY DECISION ENGINE
# ============================================================

class RecoveryStrategy:
    CPU_WORKER_TERMINATION = "cpu_worker_termination"
    MEMORY_CLEANUP = "memory_cleanup"
    PROCESS_RELOAD = "process_reload"
    DISK_CLEANUP = "disk_cleanup"
    CONTAINER_RESTART = "container_restart"
    LATENCY_RESET = "latency_reset"
    UNSUPPORTED = "unsupported"


APPROVED_DISK_CLEANUP_DIRS = ["/tmp/test_disk_fill"]
PROHIBITED_DIRECTORIES = ["/", "/app", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/proc", "/root", "/sys", "/usr", "/var"]

RESTART_WINDOW_SECONDS = 300
MAX_RESTARTS_PER_WINDOW = 3
RECOVERY_COOLDOWN_SECONDS = 15.0


def is_safe_disk_cleanup_path(file_path, approved_dir="/tmp/test_disk_fill"):
    """
    Safety check: ensures target path is strictly inside approved_dir,
    prevents path traversal (no '..'), refuses prohibited directories,
    and only matches approved test artifact prefix.
    """
    try:
        resolved_dir = os.path.realpath(approved_dir)
        resolved_file = os.path.realpath(file_path)
        if resolved_file == resolved_dir:
            return False
        for prohibited in PROHIBITED_DIRECTORIES:
            if os.path.realpath(prohibited) == resolved_file:
                return False
        common = os.path.commonpath([resolved_dir, resolved_file])
        if common != resolved_dir:
            return False
        basename = os.path.basename(resolved_file)
        if not (basename.startswith("disk_stress_") or basename == ".metadata.json"):
            return False
        return True
    except Exception:
        return False


def select_recovery_strategy(anomaly_type, service=None, details=None):
    """
    Central strategy selection mapping each anomaly type to a tailored remediation action.
    Separates decision strategy selection from execution.
    """
    details = details or {}
    if details.get("target_strategy"):
        return details["target_strategy"]

    if anomaly_type == "cpu_spike":
        return RecoveryStrategy.CPU_WORKER_TERMINATION

    if anomaly_type == "memory_leak":
        return RecoveryStrategy.MEMORY_CLEANUP

    if anomaly_type == "process_failure":
        # If the container has zero active PIDs, there is no running process to reload.
        # Fall back directly to container restart.
        pids_current = details.get("pids_current")
        if pids_current is not None and pids_current <= 0:
            return RecoveryStrategy.CONTAINER_RESTART
        return RecoveryStrategy.PROCESS_RELOAD

    if anomaly_type == "disk_exhaustion":
        # If the container is not running, in-place disk cleanup cannot be performed.
        # Fall back directly to container restart.
        container_status = details.get("container_status")
        if container_status is not None and container_status != "running":
            return RecoveryStrategy.CONTAINER_RESTART
        return RecoveryStrategy.DISK_CLEANUP

    if anomaly_type == "high_latency":
        if details.get("target_strategy") == "latency_reset" or details.get("controlled_latency") is True or details.get("cause") == "controlled_latency":
            return RecoveryStrategy.LATENCY_RESET
        return RecoveryStrategy.CONTAINER_RESTART

    if anomaly_type == "network_failure":
        if details.get("network_scope") == "external_partition" or details.get("is_external_partition") is True or details.get("cause") == "external_partition":
            return RecoveryStrategy.UNSUPPORTED
        return RecoveryStrategy.CONTAINER_RESTART

    if anomaly_type == "container_crash":
        return RecoveryStrategy.CONTAINER_RESTART

    if anomaly_type == "container_missing":
        return RecoveryStrategy.UNSUPPORTED

    return RecoveryStrategy.CONTAINER_RESTART


class RecoveryDecisionEngine:
    """
    Coordinates anomaly-specific remediation, health verification, and escalation.
    Prevents simultaneous recovery runs against the same service.
    """
    _active_recoveries = set()
    _lock = threading.Lock()

    def __init__(self, docker_client=None, redis_client=None, traffic_manager=None, haproxy_client=None, config=None):
        self.docker_client = docker_client
        self.redis_client = redis_client
        self.traffic_manager = traffic_manager
        self.haproxy_client = haproxy_client
        self.enabled = True
        self.cooldown_seconds = 0.0
        self.max_attempts = RECOVERY_ATTEMPTS
        self.stabilization_seconds = RECOVERY_STABILIZATION_SECONDS
        self._last_recovery_time = {}
        self._restart_history = {}
        self._incident_history = []
        if config and isinstance(config, dict):
            self.update_config(config)

    def is_recovery_in_progress(self, service):
        with self._lock:
            return service in self._active_recoveries

    def enable(self):
        self.enabled = True
        return {"enabled": True}

    def disable(self):
        self.enabled = False
        return {"enabled": False}

    def get_config(self):
        return {
            "enabled": self.enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "max_attempts": self.max_attempts,
            "stabilization_seconds": self.stabilization_seconds,
            "max_restarts_per_window": MAX_RESTARTS_PER_WINDOW,
            "restart_window_seconds": RESTART_WINDOW_SECONDS
        }

    def update_config(self, cfg):
        if "enabled" in cfg:
            self.enabled = bool(cfg["enabled"])
        if "cooldown_seconds" in cfg:
            self.cooldown_seconds = float(cfg["cooldown_seconds"])
        if "max_attempts" in cfg:
            self.max_attempts = int(cfg["max_attempts"])
        if "stabilization_seconds" in cfg:
            self.stabilization_seconds = float(cfg["stabilization_seconds"])
        return self.get_config()

    def _get_active_incident_data(self, service):
        if self.redis_client and hasattr(self.redis_client, "get"):
            try:
                raw = self.redis_client.get(f"incident:{service}")
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return None

    def _update_incident_state(self, service, incident_id, updates):
        incident = self._get_active_incident_data(service) or {
            "server": service,
            "incident_id": incident_id,
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        incident.update(updates)
        incident["last_updated_at"] = datetime.now(timezone.utc).isoformat()
        if self.redis_client and hasattr(self.redis_client, "set"):
            try:
                self.redis_client.set(f"incident:{service}", json.dumps(incident))
            except Exception as err:
                print(f"Error updating incident state in Redis: {err}")
        return incident

    def _get_recent_restarts(self, service):
        now = time.time()
        timestamps = self._restart_history.get(service, [])
        valid = [t for t in timestamps if now - t < RESTART_WINDOW_SECONDS]
        self._restart_history[service] = valid
        return valid

    def get_status(self):
        all_incidents = self.get_history(limit=100)
        total = len(all_incidents)
        recovered = [inc for inc in all_incidents if inc.get("recovery_state") in ("recovered", "RECOVERED") or inc.get("status") == "resolved"]
        failed = [inc for inc in all_incidents if inc.get("recovery_state") in ("recovery_failed", "RECOVERY_FAILED", "AWAITING_OPERATOR") or inc.get("status") == "escalated"]

        mttrs = [inc.get("mttr_seconds") for inc in recovered if inc.get("mttr_seconds") is not None]
        avg_mttr = round(sum(mttrs) / len(mttrs), 2) if mttrs else 0.0

        services = ["app1", "app2", "app3", "app4", "app5"]
        server_status = {}
        for srv in services:
            active_inc = self._get_active_incident_data(srv)
            srv_state = get_server_state(srv, self.redis_client)
            last_rec = self._last_recovery_time.get(srv, None)

            traffic_state = "NORMAL"
            if self.traffic_manager:
                try:
                    ts = self.traffic_manager.get_server_status(srv)
                    traffic_state = ts.get("state", "NORMAL") if ts else "NORMAL"
                except Exception:
                    pass

            is_anchor = False
            if self.traffic_manager and hasattr(self.traffic_manager, "count_healthy_eligible"):
                try:
                    c = self.traffic_manager.count_healthy_eligible()
                    if isinstance(c, (int, float)):
                        is_anchor = (c <= 1 and traffic_state not in ("EXCLUDED", "CRITICAL"))
                except Exception:
                    is_anchor = False
            if active_inc and active_inc.get("traffic_action") == "ACTION_REJECTED":
                is_anchor = True

            server_status[srv] = {
                "server": srv,
                "operational_state": srv_state.get("state", "healthy"),
                "traffic_state": traffic_state,
                "recovery_in_progress": srv in self._active_recoveries,
                "active_incident": active_inc,
                "last_recovery_time": last_rec,
                "restart_count_in_window": len(self._get_recent_restarts(srv)),
                "is_last_eligible": is_anchor
            }

        return {
            "enabled": self.enabled,
            "active_recoveries": list(self._active_recoveries),
            "total_incidents": total,
            "in_progress_count": len(self._active_recoveries),
            "recovered_count": len(recovered),
            "failed_count": len(failed),
            "average_mttr_seconds": avg_mttr,
            "servers": server_status
        }

    def get_server_status(self, service):
        status = self.get_status()
        srv_data = status["servers"].get(service, {
            "server": service,
            "operational_state": "unknown",
            "traffic_state": "UNKNOWN",
            "recovery_in_progress": False,
            "active_incident": None,
            "last_recovery_time": None,
            "restart_count_in_window": 0
        })
        history = self.get_history(limit=20)
        srv_data["recent_history"] = [h for h in history if h.get("server") == service]
        return srv_data

    def get_history(self, limit=50):
        records = []
        if self.redis_client and hasattr(self.redis_client, "lrange"):
            try:
                raw_list = self.redis_client.lrange("recovery_history", 0, limit - 1)
                for item in raw_list:
                    try:
                        records.append(json.loads(item))
                    except Exception:
                        pass
                for srv in ["app1", "app2", "app3", "app4", "app5"]:
                    srv_raw = self.redis_client.lrange(f"closed_incidents:{srv}", 0, 20)
                    for item in srv_raw:
                        try:
                            parsed = json.loads(item)
                            if not any(r.get("incident_id") == parsed.get("incident_id") for r in records):
                                records.append(parsed)
                        except Exception:
                            pass
            except Exception:
                pass

        for inc in self._incident_history:
            if not any(r.get("incident_id") == inc.get("incident_id") for r in records):
                records.append(inc)

        def sort_key(item):
            return item.get("recovered_at") or item.get("escalated_at") or item.get("started_at") or item.get("detected_at") or ""

        records.sort(key=sort_key, reverse=True)
        return records[:limit]

    def remediate_cpu_spike(self, service, incident_id=None):
        """
        Safely stop registered CPU stress worker process, verify CPU drops below 40%,
        and confirm application health before resolving.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.CPU_WORKER_TERMINATION,
            "action": "terminate_cpu_stress_worker",
            "started_at": started_at
        }, self.redis_client)

        try:
            resp = requests.post(f"http://{service}:5000/stress/cpu/stop", timeout=5)
            if resp.status_code != 200:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"CPU stop endpoint returned HTTP {resp.status_code}"
                }
            data = resp.json()
            stopped = data.get("processes_stopped", 0)
            if stopped <= 0:
                # No registered stress process was active; unsafe to guess PID, escalate
                return {
                    "success": False,
                    "escalate": True,
                    "reason": "No registered CPU stress workers active"
                }
        except Exception as error:
            return {
                "success": False,
                "escalate": True,
                "reason": f"Failed to invoke CPU stop endpoint: {error}"
            }

        completed_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_completed", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.CPU_WORKER_TERMINATION,
            "action": "cpu_worker_terminated",
            "processes_stopped": stopped,
            "completed_at": completed_at
        }, self.redis_client)

        # Allow CPU utilization to settle
        time.sleep(2)
        try:
            container = get_container(service, self.docker_client)
            stats = container.stats(stream=False)
            cpu_percent = calculate_cpu_percent(stats)
        except Exception:
            cpu_percent = 0.0

        is_healthy = application_is_healthy(service)
        if cpu_percent < 40.0 and is_healthy:
            record_event(service, "recovery_verification_succeeded", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.CPU_WORKER_TERMINATION,
                "cpu_percent": cpu_percent,
                "threshold_percent": 40.0,
                "health_status": "healthy"
            }, self.redis_client)
            return {
                "success": True,
                "escalate": False,
                "action": "cpu_worker_terminated",
                "cpu_percent": cpu_percent
            }
        else:
            record_event(service, "recovery_verification_failed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.CPU_WORKER_TERMINATION,
                "cpu_percent": cpu_percent,
                "health_status": "healthy" if is_healthy else "unhealthy",
                "reason": "CPU utilization remains elevated or health check failed"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": True,
                "reason": f"Post-remediation verification failed (CPU: {cpu_percent}%, Healthy: {is_healthy})"
            }

    def remediate_memory_leak(self, service, incident_id=None):
        """
        Safely clear allocated demonstration memory blocks, verify memory drops below threshold,
        and confirm application health before resolving.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.MEMORY_CLEANUP,
            "action": "clear_memory_stress_allocation",
            "started_at": started_at
        }, self.redis_client)

        try:
            resp = requests.post(f"http://{service}:5000/stress/memory/clear", timeout=5)
            if resp.status_code != 200:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"Memory clear endpoint returned HTTP {resp.status_code}"
                }
        except Exception as error:
            return {
                "success": False,
                "escalate": True,
                "reason": f"Failed to invoke memory clear endpoint: {error}"
            }

        completed_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_completed", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.MEMORY_CLEANUP,
            "action": "memory_stress_cleared",
            "completed_at": completed_at
        }, self.redis_client)

        # Allow memory allocation to settle
        time.sleep(2)
        try:
            container = get_container(service, self.docker_client)
            stats = container.stats(stream=False)
            usage_mb = round(stats.get("memory_stats", {}).get("usage", 0) / (1024 * 1024), 2)
        except Exception:
            usage_mb = 0.0

        is_healthy = application_is_healthy(service)
        if usage_mb < 70.0 and is_healthy:
            record_event(service, "recovery_verification_succeeded", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.MEMORY_CLEANUP,
                "memory_mb": usage_mb,
                "threshold_mb": 70.0,
                "health_status": "healthy"
            }, self.redis_client)
            return {
                "success": True,
                "escalate": False,
                "action": "memory_stress_cleared",
                "memory_mb": usage_mb
            }
        else:
            record_event(service, "recovery_verification_failed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.MEMORY_CLEANUP,
                "memory_mb": usage_mb,
                "health_status": "healthy" if is_healthy else "unhealthy",
                "reason": f"Memory usage ({usage_mb} MB) remains above threshold or health check failed"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": True,
                "reason": f"Post-remediation verification failed (Memory: {usage_mb} MB, Healthy: {is_healthy})"
            }

    def remediate_process_failure(self, service, incident_id=None):
        """
        Send graceful SIGHUP to Gunicorn master (PID 1) to recycle application workers,
        and verify health endpoint recovers to HTTP 200.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.PROCESS_RELOAD,
            "action": "reload_gunicorn_worker",
            "started_at": started_at
        }, self.redis_client)

        try:
            container = get_container(service, self.docker_client)
            container.reload()
            if container.status != "running":
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"Container status is {container.status}; cannot reload process"
                }

            # In slim containers, kill is a shell builtin. Execute via sh -c or fallback to SIGHUP.
            exec_res = container.exec_run(["sh", "-c", "kill -HUP 1"])
            exit_code = exec_res.exit_code if hasattr(exec_res, "exit_code") else exec_res[0]
            if exit_code != 0:
                try:
                    container.kill(signal="SIGHUP")
                    exit_code = 0
                except Exception:
                    pass

            if exit_code != 0:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"kill -HUP 1 returned non-zero exit code {exit_code}"
                }
        except Exception as error:
            return {
                "success": False,
                "escalate": True,
                "reason": f"Process reload command failed: {error}"
            }

        completed_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_completed", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.PROCESS_RELOAD,
            "action": "gunicorn_worker_reloaded",
            "completed_at": completed_at
        }, self.redis_client)

        # Allow Gunicorn worker to fork and initialize
        time.sleep(3)
        is_healthy = application_is_healthy(service)
        if is_healthy:
            record_event(service, "recovery_verification_succeeded", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.PROCESS_RELOAD,
                "health_status": "healthy"
            }, self.redis_client)
            return {
                "success": True,
                "escalate": False,
                "action": "gunicorn_worker_reloaded"
            }
        else:
            record_event(service, "recovery_verification_failed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.PROCESS_RELOAD,
                "reason": "Application health check failed after Gunicorn worker reload"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": True,
                "reason": "Application remains unhealthy after Gunicorn worker reload"
            }

    def remediate_disk_exhaustion(self, service, incident_id=None):
        """
        Remediate disk exhaustion by inspecting approved temporary directory,
        identifying safe eligible files, calculating reclaimable space,
        safely removing them, and verifying disk usage reduction and application health.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.DISK_CLEANUP,
            "action": "safe_temporary_file_cleanup",
            "started_at": started_at
        }, self.redis_client)

        try:
            container = get_container(service, self.docker_client)
            container.reload()
            if container.status != "running":
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"Container status is {container.status}; cannot perform disk cleanup"
                }

            # 1. Identify candidate files inside approved directory
            list_cmd = ["sh", "-c", "find /tmp/test_disk_fill -maxdepth 1 -type f 2>/dev/null"]
            list_res = container.exec_run(list_cmd)
            raw_output = list_res.output.decode("utf-8").strip() if hasattr(list_res, "output") else str(list_res[1] if isinstance(list_res, (list, tuple)) and len(list_res) > 1 else "")
            candidate_files = [line.strip() for line in raw_output.splitlines() if line.strip()]

            # 2. Strict path validation and traversal prevention
            safe_files = [f for f in candidate_files if is_safe_disk_cleanup_path(f, "/tmp/test_disk_fill")]

            if not safe_files:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": "No eligible temporary files found in approved cleanup locations"
                }

            # 3. Calculate reclaimable space before deletion
            calc_cmd = ["sh", "-c", "du -k /tmp/test_disk_fill/disk_stress_* 2>/dev/null | awk '{sum+=$1} END {print sum}'"]
            calc_res = container.exec_run(calc_cmd)
            calc_str = calc_res.output.decode("utf-8").strip() if hasattr(calc_res, "output") else ""
            reclaimable_kb = 0.0
            if calc_str and calc_str.isdigit():
                reclaimable_kb = float(calc_str)
            reclaimable_mb = round(reclaimable_kb / 1024.0, 2)

            # 4. Remove only explicitly approved temporary test files
            del_cmd = ["sh", "-c", "rm -f /tmp/test_disk_fill/disk_stress_* /tmp/test_disk_fill/.metadata.json"]
            del_res = container.exec_run(del_cmd)
            del_code = del_res.exit_code if hasattr(del_res, "exit_code") else del_res[0]
            if del_code != 0:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"Cleanup removal command returned exit code {del_code}"
                }

        except Exception as error:
            return {
                "success": False,
                "escalate": True,
                "reason": f"Disk cleanup failed: {error}"
            }

        completed_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_completed", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.DISK_CLEANUP,
            "action": "safe_temporary_file_cleanup",
            "reclaimed_mb": reclaimable_mb,
            "files_removed": len(safe_files),
            "physical_disk_note": "Targeted cleanup removes controlled demonstration artifacts in approved test directory. Physical host disk capacity exhaustion cannot be resolved by application test file deletion.",
            "completed_at": completed_at
        }, self.redis_client)

        # 5. Post-cleanup verification delay
        time.sleep(2)

        # 6. Verify that temporary stress file is gone and application is healthy
        verify_cmd = ["sh", "-c", "ls -1 /tmp/test_disk_fill/disk_stress_* 2>/dev/null | wc -l"]
        try:
            v_res = container.exec_run(verify_cmd)
            v_output = v_res.output.decode("utf-8").strip() if hasattr(v_res, "output") else str(v_res[1] if isinstance(v_res, (list, tuple)) and len(v_res) > 1 else "0")
            v_count = int(v_output) if v_output.isdigit() else 0
        except Exception:
            v_count = 0

        is_healthy = application_is_healthy(service)

        if v_count == 0 and is_healthy:
            record_event(service, "recovery_verification_succeeded", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.DISK_CLEANUP,
                "reclaimed_mb": reclaimable_mb,
                "health_status": "healthy"
            }, self.redis_client)
            return {
                "success": True,
                "escalate": False,
                "action": "safe_temporary_file_cleanup",
                "reclaimed_mb": reclaimable_mb,
                "physical_disk_note": "Targeted cleanup removes controlled demonstration artifacts in approved test directory. Physical host disk capacity exhaustion cannot be resolved by application test file deletion."
            }
        else:
            reason = f"Temporary files remaining ({v_count})" if v_count > 0 else "Application health check failed"
            record_event(service, "recovery_verification_failed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.DISK_CLEANUP,
                "reason": f"Post-cleanup verification failed: {reason}"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": True,
                "reason": f"Post-cleanup verification failed: {reason}"
            }

    def remediate_high_latency(self, service, incident_id=None):
        """
        Targeted remediation for high latency: reset controlled latency injection,
        wait for stabilization, and verify latency drops below 150ms and HTTP 200.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.LATENCY_RESET,
            "action": "reset_latency_injection",
            "started_at": started_at
        }, self.redis_client)

        try:
            resp = requests.post(f"http://{service}:5000/stress/latency/reset", timeout=5)
            if resp.status_code != 200:
                return {
                    "success": False,
                    "escalate": True,
                    "reason": f"Latency reset endpoint returned HTTP {resp.status_code}"
                }
        except Exception as error:
            return {
                "success": False,
                "escalate": True,
                "reason": f"Failed to invoke latency reset endpoint: {error}"
            }

        completed_at = datetime.now(timezone.utc).isoformat()
        record_event(service, "remediation_action_completed", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.LATENCY_RESET,
            "action": "latency_stress_reset",
            "completed_at": completed_at
        }, self.redis_client)

        time.sleep(2)

        try:
            t0 = time.monotonic()
            h_resp = requests.get(f"http://{service}:5000/health", timeout=2)
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            is_healthy = (h_resp.status_code == 200 and h_resp.json().get("status") == "healthy")
        except Exception:
            elapsed_ms = 9999.0
            is_healthy = False

        if elapsed_ms < 150.0 and is_healthy:
            record_event(service, "recovery_verification_succeeded", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.LATENCY_RESET,
                "latency_ms": elapsed_ms,
                "threshold_ms": 150.0,
                "health_status": "healthy"
            }, self.redis_client)
            return {
                "success": True,
                "escalate": False,
                "action": "latency_stress_reset",
                "latency_ms": elapsed_ms
            }
        else:
            record_event(service, "recovery_verification_failed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.LATENCY_RESET,
                "latency_ms": elapsed_ms,
                "health_status": "healthy" if is_healthy else "unhealthy",
                "reason": f"Latency ({elapsed_ms}ms) remains above threshold or health check failed"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": True,
                "reason": f"Post-remediation verification failed (Latency: {elapsed_ms}ms, Healthy: {is_healthy})"
            }

    def remediate_network_failure(self, service, incident_id=None, details=None):
        """
        Evaluate network failure scope: discriminate between external network partition
        and local container network failure. Never repeatedly restart containers for external partitions.
        """
        details = details or {}
        if details.get("network_scope") == "external_partition" or details.get("is_external_partition") is True or details.get("unsupported"):
            record_event(service, "recovery_skipped_external_partition", {
                "incident_id": incident_id,
                "reason": "External network partition cannot be resolved by container restart"
            }, self.redis_client)
            return {
                "success": False,
                "escalate": False,
                "reason": "External network partition detected; container restart suppressed. Awaiting operator intervention.",
                "action": "awaiting_operator"
            }

        return self.remediate_container_restart(service, incident_id)

    def remediate_container_restart(self, service, incident_id=None, is_escalation=False):
        """
        Fallback container restart utilizing the existing robust recover_server workflow
        with restart-loop protection and container missing check.
        """
        recent = self._get_recent_restarts(service)
        if len(recent) >= MAX_RESTARTS_PER_WINDOW:
            record_event(service, "restart_loop_prevented", {
                "incident_id": incident_id,
                "restarts_in_window": len(recent),
                "window_seconds": RESTART_WINDOW_SECONDS
            }, self.redis_client)
            return {
                "success": False,
                "escalate": False,
                "reason": f"Restart loop protection triggered: {len(recent)} restarts in {RESTART_WINDOW_SECONDS}s. Container restart suppressed.",
                "action": "restart_suppressed"
            }

        if self.docker_client:
            try:
                get_container(service, self.docker_client)
            except Exception as e:
                record_event(service, "container_missing_restart_aborted", {
                    "incident_id": incident_id,
                    "error": str(e)
                }, self.redis_client)
                return {
                    "success": False,
                    "escalate": False,
                    "reason": f"Container {service} is missing from Docker daemon. Manual operator recreation required.",
                    "action": "container_missing"
                }

        started_at = datetime.now(timezone.utc).isoformat()
        action_name = "escalated_container_restart" if is_escalation else "container_restart"
        record_event(service, "remediation_action_started", {
            "incident_id": incident_id,
            "strategy": RecoveryStrategy.CONTAINER_RESTART,
            "action": action_name,
            "is_escalation": is_escalation,
            "started_at": started_at
        }, self.redis_client)

        self._restart_history.setdefault(service, []).append(time.time())

        success = recover_server(service, self.docker_client, self.redis_client)
        completed_at = datetime.now(timezone.utc).isoformat()

        if success:
            record_event(service, "remediation_action_completed", {
                "incident_id": incident_id,
                "strategy": RecoveryStrategy.CONTAINER_RESTART,
                "action": action_name,
                "completed_at": completed_at
            }, self.redis_client)
            return {
                "success": True,
                "escalated": is_escalation,
                "action": action_name
            }
        else:
            return {
                "success": False,
                "escalated": is_escalation,
                "reason": f"Container restart failed after {RECOVERY_ATTEMPTS} attempts"
            }

    def _close_incident(self, service, strategy, outcome):
        """Mark active incident resolved, compute MTTR, and archive to closed_incidents."""
        incident = self._get_active_incident_data(service) or {}
        incident["status"] = "resolved"
        incident["recovery_state"] = "recovered"
        incident["recovery_result"] = "success"
        incident["recovered_at"] = datetime.now(timezone.utc).isoformat()
        incident["recovery_completed_at"] = incident["recovered_at"]
        incident["recovery_strategy"] = strategy
        incident["recovery_action"] = outcome.get("action")
        incident["remediation_action"] = outcome.get("action")
        incident["escalated"] = outcome.get("escalated", False)
        if "started_at" in incident:
            try:
                start_dt = datetime.fromisoformat(incident["started_at"])
                end_dt = datetime.fromisoformat(incident["recovered_at"])
                incident["mttr_seconds"] = round((end_dt - start_dt).total_seconds(), 2)
            except Exception:
                pass

        if self.redis_client and hasattr(self.redis_client, "lpush"):
            try:
                self.redis_client.lpush(f"closed_incidents:{service}", json.dumps(incident))
                if hasattr(self.redis_client, "ltrim"):
                    self.redis_client.ltrim(f"closed_incidents:{service}", 0, 99)
                self.redis_client.lpush("recovery_history", json.dumps(incident))
                if hasattr(self.redis_client, "ltrim"):
                    self.redis_client.ltrim("recovery_history", 0, 99)
            except Exception as err:
                print(f"Error closing incident in Redis: {err}")

        self._incident_history.append(dict(incident))

        if self.redis_client and hasattr(self.redis_client, "delete"):
            try:
                self.redis_client.delete(f"incident:{service}")
            except Exception as err:
                print(f"Error deleting incident in Redis: {err}")

    def _escalate_incident(self, service, strategy, outcome):
        """Update active incident status to escalated with failure reason."""
        incident = self._get_active_incident_data(service) or {
            "server": service,
            "incident_id": f"{service}-{int(time.time())}"
        }
        final_state = "awaiting_operator" if outcome.get("action") in ("awaiting_operator", "container_missing", "restart_suppressed") else "recovery_failed"
        incident["status"] = "escalated"
        incident["recovery_state"] = final_state
        incident["recovery_result"] = "failed"
        incident["escalated_at"] = datetime.now(timezone.utc).isoformat()
        incident["recovery_completed_at"] = incident["escalated_at"]
        incident["failure_reason"] = outcome.get("reason", "Recovery failed")
        incident["recovery_strategy"] = strategy

        if self.redis_client and hasattr(self.redis_client, "set"):
            try:
                self.redis_client.set(f"incident:{service}", json.dumps(incident))
            except Exception as err:
                print(f"Error escalating incident in Redis: {err}")

        if self.redis_client and hasattr(self.redis_client, "lpush"):
            try:
                self.redis_client.lpush(f"closed_incidents:{service}", json.dumps(incident))
                self.redis_client.lpush("recovery_history", json.dumps(incident))
            except Exception:
                pass

        self._incident_history.append(dict(incident))

    def execute_recovery(self, service, anomaly_type=None, details=None, incident_id=None, force=False):
        """
        Central entrypoint executing the complete Phase 5.3 self-healing workflow:
        DETECTED -> TRAFFIC_PROTECTED -> RECOVERY_IN_PROGRESS -> VERIFICATION -> RECOVERED / RECOVERY_FAILED
        """
        details = details or {}
        now = time.time()
        with self._lock:
            if service in self._active_recoveries:
                print(f"[RecoveryEngine] Recovery already running for {service}, ignoring duplicate request.")
                return {"success": False, "reason": "Recovery already in progress"}
            self._active_recoveries.add(service)

        try:
            if not self.enabled and not force:
                print(f"[RecoveryEngine] Automatic recovery is disabled; skipping recovery for {service}.")
                return {
                    "success": False,
                    "reason": "Automatic recovery is disabled",
                    "status": "disabled"
                }

            last_rec = self._last_recovery_time.get(service, 0)
            if not force and self.cooldown_seconds > 0 and (now - last_rec) < self.cooldown_seconds:
                remaining = round(self.cooldown_seconds - (now - last_rec), 1)
                print(f"[RecoveryEngine] Server {service} in recovery cooldown ({remaining}s remaining).")
                return {
                    "success": False,
                    "reason": f"Server {service} in recovery cooldown ({remaining}s remaining)",
                    "status": "cooldown"
                }
            self._last_recovery_time[service] = now

            if not incident_id:
                active_inc = self._get_active_incident_data(service)
                if active_inc:
                    incident_id = active_inc.get("incident_id")
                    if not anomaly_type:
                        anomaly_type = active_inc.get("anomaly_type")
                    if not details and active_inc.get("details"):
                        details = active_inc.get("details")
                else:
                    incident_id = f"{service}-{int(now)}"

            # STEP 1: DETECTED
            self._update_incident_state(service, incident_id, {
                "recovery_state": "DETECTED",
                "status": "active",
                "anomaly_type": anomaly_type,
                "details": details
            })

            # STEP 2: TRAFFIC_PROTECTED
            traffic_action = "none"
            if self.traffic_manager:
                try:
                    iso_res = self.traffic_manager.isolate_server(service, reason=f"Pre-recovery isolation for {anomaly_type}")
                    traffic_action = iso_res.get("action", "unknown")
                except Exception as t_err:
                    print(f"[RecoveryEngine] TrafficManager isolation warning: {t_err}")

            self._update_incident_state(service, incident_id, {
                "recovery_state": "TRAFFIC_PROTECTED",
                "traffic_action": traffic_action
            })

            # STEP 3: RECOVERY_IN_PROGRESS
            strategy = select_recovery_strategy(anomaly_type, service, details)
            self._update_incident_state(service, incident_id, {
                "recovery_state": "RECOVERY_IN_PROGRESS",
                "recovery_strategy": strategy
            })
            record_event(service, "strategy_selected", {
                "incident_id": incident_id,
                "anomaly_type": anomaly_type,
                "strategy": strategy
            }, self.redis_client)

            # Check if this backend is the last eligible backend in HAProxy routing
            is_last_eligible = False
            if self.traffic_manager and hasattr(self.traffic_manager, "count_healthy_eligible"):
                try:
                    c = self.traffic_manager.count_healthy_eligible()
                    if isinstance(c, (int, float)) and c <= 1:
                        is_last_eligible = True
                except Exception:
                    pass
            if traffic_action == "ACTION_REJECTED":
                is_last_eligible = True

            if anomaly_type == "high_latency":
                result = self.remediate_high_latency(service, incident_id)
            elif strategy == RecoveryStrategy.CPU_WORKER_TERMINATION:
                result = self.remediate_cpu_spike(service, incident_id)
            elif strategy == RecoveryStrategy.MEMORY_CLEANUP:
                result = self.remediate_memory_leak(service, incident_id)
            elif strategy == RecoveryStrategy.PROCESS_RELOAD:
                result = self.remediate_process_failure(service, incident_id)
            elif strategy == RecoveryStrategy.DISK_CLEANUP:
                result = self.remediate_disk_exhaustion(service, incident_id)
            elif anomaly_type == "network_failure":
                result = self.remediate_network_failure(service, incident_id, details)
            elif strategy == RecoveryStrategy.UNSUPPORTED:
                result = {
                    "success": False,
                    "escalate": False,
                    "reason": f"No automated recovery available for {anomaly_type}",
                    "action": "awaiting_operator"
                }
            elif strategy == RecoveryStrategy.CONTAINER_RESTART:
                if is_last_eligible and not force:
                    record_event(service, "disruptive_restart_suppressed_last_backend", {
                        "incident_id": incident_id,
                        "strategy": RecoveryStrategy.CONTAINER_RESTART,
                        "reason": f"Disruptive container restart suppressed: {service} is the last eligible backend in HAProxy cluster. Marked AWAITING_OPERATOR."
                    }, self.redis_client)
                    result = {
                        "success": False,
                        "escalate": False,
                        "action": "awaiting_operator",
                        "reason": f"Disruptive container restart suppressed: {service} is the last eligible backend in HAProxy cluster. Marked AWAITING_OPERATOR to prevent cluster blackout."
                    }
                else:
                    result = self.remediate_container_restart(service, incident_id)
            else:
                result = {
                    "success": False,
                    "escalate": False,
                    "reason": f"No automated recovery available for {anomaly_type}"
                }

            # STEP 4: ESCALATION (if primary strategy failed and escalation is permitted)
            if not result.get("success") and result.get("escalate"):
                if result.get("action") == "awaiting_operator":
                    result["escalate"] = False
                elif is_last_eligible and not force:
                    record_event(service, "disruptive_restart_suppressed_last_backend", {
                        "incident_id": incident_id,
                        "strategy": RecoveryStrategy.CONTAINER_RESTART,
                        "reason": f"Escalated container restart suppressed: {service} is the last eligible backend in HAProxy cluster. Marked AWAITING_OPERATOR."
                    }, self.redis_client)
                    result = {
                        "success": False,
                        "escalate": False,
                        "action": "awaiting_operator",
                        "reason": f"Escalated container restart suppressed: {service} is the last eligible backend in HAProxy cluster. Marked AWAITING_OPERATOR to prevent cluster blackout."
                    }
                else:
                    record_event(service, "recovery_escalated", {
                        "incident_id": incident_id,
                        "from_strategy": strategy,
                        "to_strategy": RecoveryStrategy.CONTAINER_RESTART,
                        "reason": result.get("reason")
                    }, self.redis_client)
                    self._update_incident_state(service, incident_id, {
                        "recovery_state": "RECOVERY_IN_PROGRESS",
                        "recovery_strategy": RecoveryStrategy.CONTAINER_RESTART,
                        "escalation_reason": result.get("reason"),
                        "attempts": 2
                    })
                    result = self.remediate_container_restart(service, incident_id, is_escalation=True)

            # STEP 5: VERIFICATION & OUTCOME
            self._update_incident_state(service, incident_id, {
                "recovery_state": "VERIFICATION"
            })

            if result.get("success"):
                set_server_state(
                    service,
                    "healthy",
                    f"Recovered via {result.get('action')}",
                    self.redis_client
                )
                record_event(service, "recovery_succeeded", {
                    "incident_id": incident_id,
                    "strategy": strategy,
                    "action": result.get("action"),
                    "escalated": result.get("escalated", False)
                }, self.redis_client)
                self._close_incident(service, strategy, result)

                # Backend re-admission via TrafficManager if safety criteria are met
                if self.traffic_manager:
                    try:
                        self.traffic_manager.restore_server(service, force=False)
                    except Exception as rm_err:
                        print(f"[RecoveryEngine] Backend re-admission notice: {rm_err}")
            else:
                final_state = "awaiting_operator" if result.get("action") in ("awaiting_operator", "container_missing", "restart_suppressed") else "recovery_failed"
                set_server_state(
                    service,
                    "failed",
                    result.get("reason", "Recovery failed"),
                    self.redis_client
                )
                record_event(service, "recovery_failed", {
                    "incident_id": incident_id,
                    "strategy": strategy,
                    "reason": result.get("reason"),
                    "final_state": final_state
                }, self.redis_client)
                self._escalate_incident(service, strategy, result)

            return result

        finally:
            with self._lock:
                self._active_recoveries.discard(service)

