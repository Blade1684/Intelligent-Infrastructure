import time
import json
import numpy as np
from datetime import datetime, timezone

# ============================================================
# CLASSIFICATION STATES
# ============================================================
class OverloadState:
    NORMAL = "NORMAL"
    HIGH_TRAFFIC_HEALTHY = "HIGH_TRAFFIC_HEALTHY"
    EARLY_WARNING = "EARLY_WARNING"
    OVERLOAD = "OVERLOAD"
    CRITICAL = "CRITICAL"
    OFFLINE = "OFFLINE"

# ============================================================
# CONFIGURABLE DEFAULT THRESHOLDS
# ============================================================
DEFAULT_CONFIG = {
    # Traffic thresholds
    "high_traffic_rps_threshold": 8.0,
    "high_traffic_users_threshold": 5,
    "traffic_spike_multiplier": 2.5,
    "uneven_distribution_pct_threshold": 35.0,  # Expected is 20% for 5 servers

    # CPU thresholds (%)
    "cpu_warning_threshold": 45.0,
    "cpu_overload_threshold": 65.0,
    "cpu_critical_threshold": 85.0,
    "cpu_recovery_threshold": 40.0,

    # Memory thresholds (MB)
    "memory_warning_threshold": 100.0,
    "memory_overload_threshold": 140.0,
    "memory_critical_threshold": 220.0,
    "memory_recovery_threshold": 85.0,

    # Latency thresholds (ms)
    "latency_warning_threshold": 150.0,
    "latency_overload_threshold": 300.0,
    "latency_critical_threshold": 1000.0,
    "latency_recovery_threshold": 100.0,
    "latency_baseline_multiplier_warning": 1.5,
    "latency_baseline_multiplier_overload": 2.5,

    # Error rate thresholds (%)
    "error_rate_warning_threshold": 2.0,
    "error_rate_overload_threshold": 5.0,
    "error_rate_critical_threshold": 15.0,

    # Stability / Debounce windows
    "min_consecutive_anomalous": 2,      # Requires 2 cycles to confirm early warning or overload
    "min_consecutive_normal_recovery": 3, # Requires 3 clean cycles to confirm return to normal
    "min_baseline_samples": 10,          # Minimum samples to consider baseline trained
    "max_baseline_samples": 100,         # Maximum rolling samples stored in baseline
    "min_prediction_samples": 6,         # Minimum points required for linear trend prediction
    "prediction_horizon_seconds": 300.0  # Maximum lookahead for time-to-threshold prediction
}


class RollingBaseline:
    """
    Maintains a clean rolling baseline of nominal operating metrics for a server or cluster.
    Strictly filters out anomalous or contaminated samples (e.g. during fault injection or overload)
    to prevent baseline corruption.
    """
    def __init__(self, min_samples=10, max_samples=100):
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.samples = []  # list of dicts: {cpu, memory_mb, latency_ms, rps, timestamp}

    def is_sample_clean(self, cpu, memory_mb, latency_ms, error_rate_pct=0.0):
        """
        Guards baseline from accepting contaminated measurements.
        """
        if cpu is not None and float(cpu) > 42.0:
            return False
        if memory_mb is not None and float(memory_mb) > 85.0:
            return False
        if latency_ms is not None and float(latency_ms) > 150.0:
            return False
        if error_rate_pct is not None and float(error_rate_pct) > 2.0:
            return False
        return True

    def add_sample(self, cpu, memory_mb, latency_ms, rps=0.0, error_rate_pct=0.0):
        if not self.is_sample_clean(cpu, memory_mb, latency_ms, error_rate_pct):
            return False

        sample = {
            "cpu": float(cpu or 0.0),
            "memory_mb": float(memory_mb or 0.0),
            "latency_ms": float(latency_ms or 0.0),
            "rps": float(rps or 0.0),
            "timestamp": time.time()
        }
        self.samples.append(sample)
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)
        return True

    def is_trained(self):
        return len(self.samples) >= self.min_samples

    def get_stats(self):
        if not self.is_trained():
            return {
                "status": "warming_up",
                "sample_count": len(self.samples),
                "required_samples": self.min_samples,
                "cpu_mean": None, "cpu_std": None,
                "memory_mean": None, "memory_std": None,
                "latency_mean": None, "latency_std": None,
                "rps_mean": None, "rps_std": None
            }

        cpus = [s["cpu"] for s in self.samples]
        mems = [s["memory_mb"] for s in self.samples]
        lats = [s["latency_ms"] for s in self.samples]
        rpss = [s["rps"] for s in self.samples]

        return {
            "status": "ready",
            "sample_count": len(self.samples),
            "cpu_mean": round(float(np.mean(cpus)), 2),
            "cpu_std": max(1.0, round(float(np.std(cpus)), 2)),
            "memory_mean": round(float(np.mean(mems)), 2),
            "memory_std": max(1.5, round(float(np.std(mems)), 2)),
            "latency_mean": round(float(np.mean(lats)), 2),
            "latency_std": max(2.0, round(float(np.std(lats)), 2)),
            "rps_mean": round(float(np.mean(rpss)), 2),
            "rps_std": max(0.5, round(float(np.std(rpss)), 2))
        }


class TrendPredictor:
    """
    Performs deterministic linear slope estimation on time-series telemetry.
    Predicts time to reach warning or overload thresholds only when statistically supportable.
    Returns 'insufficient_data' when history is inadequate.
    """
    @staticmethod
    def estimate_trend(time_series, min_samples=6, max_horizon=300.0):
        """
        time_series: list of (timestamp_epoch, metric_value) sorted by timestamp ascending.
        Returns prediction dictionary.
        """
        if not time_series or len(time_series) < min_samples:
            return {
                "prediction_status": "insufficient_data",
                "trend": "unknown",
                "slope_per_second": 0.0,
                "estimated_seconds_to_threshold": None,
                "confidence": "insufficient_data",
                "sample_count": len(time_series) if time_series else 0,
                "reason": f"Need at least {min_samples} observation points"
            }

        t_vals = np.array([pt[0] for pt in time_series])
        y_vals = np.array([pt[1] for pt in time_series])

        # Normalize time to relative seconds
        t_rel = t_vals - t_vals[0]
        duration = t_rel[-1]
        if duration < 1.0:  # all samples in same sub-second
            return {
                "prediction_status": "insufficient_data",
                "trend": "unknown",
                "slope_per_second": 0.0,
                "estimated_seconds_to_threshold": None,
                "confidence": "insufficient_data",
                "sample_count": len(time_series),
                "reason": "Observation window too brief"
            }

        # Linear regression slope: y = m*t + c
        try:
            slope, intercept = np.polyfit(t_rel, y_vals, 1)
        except Exception as e:
            return {
                "prediction_status": "error",
                "trend": "unknown",
                "slope_per_second": 0.0,
                "estimated_seconds_to_threshold": None,
                "confidence": "low",
                "error": str(e)
            }

        current_val = float(y_vals[-1])
        slope_per_sec = round(float(slope), 4)

        # Correlation coefficient (R^2)
        y_pred = slope * t_rel + intercept
        ss_tot = np.sum((y_vals - np.mean(y_vals)) ** 2)
        ss_res = np.sum((y_vals - y_pred) ** 2)
        r2 = round(float(1.0 - (ss_res / ss_tot)), 3) if ss_tot > 1e-6 else 0.0

        if slope_per_sec > 0.05:
            trend_str = "increasing"
        elif slope_per_sec < -0.05:
            trend_str = "decreasing"
        else:
            trend_str = "stable"

        return {
            "prediction_status": "ready",
            "trend": trend_str,
            "slope_per_second": slope_per_sec,
            "r_squared": r2,
            "current_value": round(current_val, 2),
            "sample_count": len(time_series),
            "window_duration_seconds": round(float(duration), 1)
        }

    @staticmethod
    def predict_time_to_threshold(trend_result, threshold, max_horizon=300.0):
        if trend_result.get("prediction_status") != "ready":
            return None, "insufficient_data"

        current_val = trend_result.get("current_value", 0.0)
        slope = trend_result.get("slope_per_second", 0.0)
        r2 = trend_result.get("r_squared", 0.0)

        # If already at or above threshold
        if current_val >= threshold:
            return 0.0, "high"

        # If not increasing toward threshold
        if slope <= 0.01:
            return None, "stable"

        time_to_t = (threshold - current_val) / slope
        if time_to_t > max_horizon:
            return None, "low"

        # Confidence based on R^2 and sample size
        if r2 >= 0.75 and trend_result.get("sample_count", 0) >= 8:
            confidence = "high"
        elif r2 >= 0.4:
            confidence = "medium"
        else:
            confidence = "low"

        return round(float(time_to_t), 1), confidence


class ServerOverloadDetector:
    """
    Stateful detector for a single server backend.
    Maintains rolling metrics, baseline, debouncing counters, hysteresis,
    and predictive trend trackers.
    """
    def __init__(self, server_name, config=None):
        self.server_name = server_name
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)

        self.baseline = RollingBaseline(
            min_samples=self.config["min_baseline_samples"],
            max_samples=self.config["max_baseline_samples"]
        )

        # Current state
        self.current_state = OverloadState.NORMAL
        self.previous_state = OverloadState.NORMAL
        self.state_since = time.time()
        self.consecutive_anomalous_cycles = 0
        self.consecutive_normal_cycles = 0

        # Time series history for trend prediction: list of (epoch, value)
        self.cpu_history = []
        self.mem_history = []
        self.lat_history = []

        # Latest evaluation details
        self.last_evaluation = {}
        self.active_incident = None

    def _update_history(self, cpu, mem, lat):
        now = time.time()
        max_history = 20

        if cpu is not None:
            self.cpu_history.append((now, float(cpu)))
            if len(self.cpu_history) > max_history:
                self.cpu_history.pop(0)

        if mem is not None:
            self.mem_history.append((now, float(mem)))
            if len(self.mem_history) > max_history:
                self.mem_history.pop(0)

        if lat is not None:
            self.lat_history.append((now, float(lat)))
            if len(self.lat_history) > max_history:
                self.lat_history.pop(0)

    def evaluate(self, metric, traffic_stats=None):
        """
        Evaluates a single server's telemetry against multi-metric correlation rules,
        accounting for baseline, debouncing, and trend analysis.
        
        metric: {
            "status": "running",
            "service_available": True,
            "status_code": 200,
            "cpu_percent": 12.5,
            "memory_mb": 45.2,
            "disk_percent": 22.0,
            "latency_ms": 15.0,
            "pids_current": 3
        }
        traffic_stats: {
            "global_rps": 15.0,
            "active_users": 5,
            "backend_rps": 3.0,
            "backend_pct": 20.0,
            "locust_state": "running"
        }
        """
        if not metric or not isinstance(metric, dict):
            return self._build_evaluation(OverloadState.NORMAL, "Invalid metric data", {})

        traffic_stats = traffic_stats or {}
        container_status = metric.get("status", "running")
        service_available = metric.get("service_available", True)
        status_code = metric.get("status_code", 200)

        # Distinguish backend crash/down from resource overload
        if container_status != "running":
            self.current_state = OverloadState.OFFLINE
            return self._build_evaluation(
                OverloadState.OFFLINE,
                f"Container is {container_status} (backend offline, not resource overload)",
                {"is_offline": True, "container_status": container_status}
            )

        cpu = float(metric.get("cpu_percent", 0.0) or 0.0)
        mem = float(metric.get("memory_mb", 0.0) or 0.0)
        lat = float(metric.get("latency_ms", 0.0) or 0.0) if metric.get("latency_ms") is not None else 0.0
        pids = int(metric.get("pids_current", 1) or 1)
        disk = float(metric.get("disk_percent", 0.0) or 0.0)

        global_rps = float(traffic_stats.get("global_rps", 0.0) or 0.0)
        active_users = int(traffic_stats.get("active_users", 0) or 0)
        backend_rps = float(traffic_stats.get("backend_rps", 0.0) or 0.0)
        backend_pct = float(traffic_stats.get("backend_pct", 20.0) or 20.0)
        p95_lat = float(traffic_stats.get("p95_ms", lat) or lat)
        p99_lat = float(traffic_stats.get("p99_ms", lat) or lat)
        err_rate = float(traffic_stats.get("error_rate_pct", 0.0) or 0.0)

        # Update time series and clean baseline
        self._update_history(cpu, mem, lat)
        self.baseline.add_sample(cpu, mem, lat, rps=backend_rps, error_rate_pct=err_rate)
        b_stats = self.baseline.get_stats()

        # Multi-signal indicator evaluation
        contributing_signals = []
        is_cpu_high = cpu >= self.config["cpu_warning_threshold"]
        is_cpu_overloaded = cpu >= self.config["cpu_overload_threshold"]
        is_cpu_critical = cpu >= self.config["cpu_critical_threshold"]

        is_mem_high = mem >= self.config["memory_warning_threshold"]
        is_mem_overloaded = mem >= self.config["memory_overload_threshold"]
        is_mem_critical = mem >= self.config["memory_critical_threshold"]

        # Backend latency isolates individual healthy servers from cluster-wide latency
        effective_lat = max(lat, p95_lat) if lat >= (self.config["latency_warning_threshold"] * 0.5) else lat
        is_lat_high = (effective_lat >= self.config["latency_warning_threshold"])
        is_lat_overloaded = (effective_lat >= self.config["latency_overload_threshold"])
        is_lat_critical = (effective_lat >= self.config["latency_critical_threshold"])

        is_err_high = err_rate >= self.config["error_rate_warning_threshold"]
        is_err_overloaded = err_rate >= self.config["error_rate_overload_threshold"]
        is_err_critical = err_rate >= self.config["error_rate_critical_threshold"]

        is_high_traffic = (global_rps >= self.config["high_traffic_rps_threshold"] or active_users >= self.config["high_traffic_users_threshold"])
        is_uneven = (backend_pct >= self.config["uneven_distribution_pct_threshold"])

        # Determine raw instantaneous state candidate
        raw_state = OverloadState.NORMAL
        reason = "Operating within normal nominal thresholds"

        # 1. Check for CRITICAL
        if is_cpu_critical or is_mem_critical or is_lat_critical or is_err_critical:
            raw_state = OverloadState.CRITICAL
            crit_reasons = []
            if is_cpu_critical: crit_reasons.append(f"CPU critical ({cpu}%)")
            if is_mem_critical: crit_reasons.append(f"Memory critical ({mem}MB)")
            if is_lat_critical: crit_reasons.append(f"Latency critical (P95: {p95_lat}ms)")
            if is_err_critical: crit_reasons.append(f"Error rate critical ({err_rate}%)")
            reason = "Severe resource saturation or failure: " + ", ".join(crit_reasons)
            contributing_signals.extend(crit_reasons)

        # 2. Check for OVERLOAD (Multi-metric correlation required!)
        elif (is_cpu_overloaded and is_lat_high) or \
             (is_mem_overloaded and (is_lat_high or is_err_high)) or \
             (is_lat_overloaded and is_err_overloaded) or \
             (is_cpu_overloaded and is_high_traffic and is_lat_high):
            raw_state = OverloadState.OVERLOAD
            reasons = []
            if is_cpu_overloaded: reasons.append(f"high CPU ({cpu}%)")
            if is_mem_overloaded: reasons.append(f"high memory ({mem}MB)")
            if is_lat_high: reasons.append(f"degraded response latency (P95: {p95_lat}ms)")
            if is_err_high: reasons.append(f"elevated error rate ({err_rate}%)")
            reason = "Infrastructure overload confirmed by correlated metrics: " + " combined with ".join(reasons)
            contributing_signals.extend(reasons)

        # 3. Check for EARLY WARNING
        elif (is_cpu_high and is_lat_high) or \
             (is_cpu_overloaded and not is_lat_high) or \
             is_mem_high or \
             is_mem_overloaded or \
             is_lat_high or \
             is_err_high or \
             (is_uneven and (is_cpu_high or is_lat_high)):
            raw_state = OverloadState.EARLY_WARNING
            reasons = []
            if is_cpu_high: reasons.append(f"increasing CPU ({cpu}%)")
            if is_mem_high or is_mem_overloaded: reasons.append(f"rising memory ({mem}MB)")
            if is_lat_high: reasons.append(f"increasing latency ({lat}ms, P95: {p95_lat}ms)")
            if is_err_high: reasons.append(f"request errors detected ({err_rate}%)")
            if is_uneven: reasons.append(f"disproportionate traffic share ({backend_pct}%)")
            reason = "Early warning: server approaching operating limits (" + ", ".join(reasons) + ")"
            contributing_signals.extend(reasons)

        # 4. Check for HIGH TRAFFIC, HEALTHY (Must not be treated as overload!)
        elif is_high_traffic and not is_cpu_high and not is_mem_high and not is_lat_high and not is_err_high:
            raw_state = OverloadState.HIGH_TRAFFIC_HEALTHY
            reason = f"High traffic handled successfully (RPS: {global_rps}, users: {active_users}, CPU: {cpu}%, P95: {p95_lat}ms)"

        # 5. Otherwise NORMAL
        else:
            raw_state = OverloadState.NORMAL
            reason = "All metrics within nominal baseline range"

        # Apply Debouncing and Hysteresis
        confirmed_state = self._apply_debouncing(raw_state, cpu, mem, lat, err_rate)

        # Predictive Trend Analysis
        prediction = self._compute_predictions()

        return self._build_evaluation(
            confirmed_state,
            reason,
            {
                "raw_candidate_state": raw_state,
                "contributing_signals": contributing_signals,
                "metrics": {
                    "cpu_percent": cpu,
                    "memory_mb": mem,
                    "disk_percent": disk,
                    "latency_ms": lat,
                    "pids_current": pids,
                    "status_code": status_code,
                    "global_rps": global_rps,
                    "backend_rps": backend_rps,
                    "backend_pct": backend_pct,
                    "active_users": active_users,
                    "p95_latency_ms": p95_lat,
                    "p99_latency_ms": p99_lat,
                    "error_rate_pct": err_rate
                },
                "baseline": b_stats,
                "prediction": prediction
            }
        )

    def _apply_debouncing(self, raw_state, cpu, mem, lat, err_rate):
        """
        Prevents transient spikes from creating false alarms.
        Requires consecutive anomalous cycles to enter EARLY_WARNING/OVERLOAD,
        and consecutive normal cycles to recover to NORMAL.
        """
        # If raw candidate is anomalous
        if raw_state in {OverloadState.EARLY_WARNING, OverloadState.OVERLOAD, OverloadState.CRITICAL}:
            self.consecutive_normal_cycles = 0
            self.consecutive_anomalous_cycles += 1

            # Critical transitions immediately
            if raw_state == OverloadState.CRITICAL:
                self.current_state = OverloadState.CRITICAL
                return self.current_state

            # Overload requires min_consecutive_anomalous cycles
            if raw_state == OverloadState.OVERLOAD:
                if self.consecutive_anomalous_cycles >= self.config["min_consecutive_anomalous"] or self.current_state == OverloadState.OVERLOAD:
                    self.current_state = OverloadState.OVERLOAD
                else:
                    # Filtered as transient spike; keep existing state or early warning
                    self.current_state = OverloadState.EARLY_WARNING if self.current_state == OverloadState.NORMAL else self.current_state
                return self.current_state

            # Early warning transitions to provide proactive visibility
            if raw_state == OverloadState.EARLY_WARNING:
                self.current_state = OverloadState.EARLY_WARNING
                return self.current_state

        elif raw_state == OverloadState.HIGH_TRAFFIC_HEALTHY:
            self.consecutive_anomalous_cycles = 0
            self.consecutive_normal_cycles += 1
            self.current_state = OverloadState.HIGH_TRAFFIC_HEALTHY
            return self.current_state

        else:  # raw_state == NORMAL
            self.consecutive_anomalous_cycles = 0
            self.consecutive_normal_cycles += 1

            # Hysteresis: only return from OVERLOAD/EARLY_WARNING to NORMAL after recovery window
            if self.current_state in {OverloadState.OVERLOAD, OverloadState.EARLY_WARNING, OverloadState.CRITICAL}:
                is_below_recovery_thresholds = (
                    cpu < self.config["cpu_recovery_threshold"] and
                    mem < self.config["memory_recovery_threshold"] and
                    lat < self.config["latency_recovery_threshold"] and
                    err_rate < 1.0
                )
                if self.consecutive_normal_cycles >= self.config["min_consecutive_normal_recovery"] and is_below_recovery_thresholds:
                    self.current_state = OverloadState.NORMAL
            else:
                self.current_state = OverloadState.NORMAL

            return self.current_state

    def _compute_predictions(self):
        """
        Runs trend prediction on CPU, memory, and latency histories.
        """
        cpu_trend = TrendPredictor.estimate_trend(self.cpu_history, min_samples=self.config["min_prediction_samples"])
        mem_trend = TrendPredictor.estimate_trend(self.mem_history, min_samples=self.config["min_prediction_samples"])
        lat_trend = TrendPredictor.estimate_trend(self.lat_history, min_samples=self.config["min_prediction_samples"])

        cpu_tt, cpu_conf = TrendPredictor.predict_time_to_threshold(
            cpu_trend, self.config["cpu_overload_threshold"], self.config["prediction_horizon_seconds"]
        )
        mem_tt, mem_conf = TrendPredictor.predict_time_to_threshold(
            mem_trend, self.config["memory_overload_threshold"], self.config["prediction_horizon_seconds"]
        )
        lat_tt, lat_conf = TrendPredictor.predict_time_to_threshold(
            lat_trend, self.config["latency_overload_threshold"], self.config["prediction_horizon_seconds"]
        )

        # Select primary predictive indicator
        primary = None
        if cpu_tt is not None and (primary is None or cpu_tt < primary["time_to_threshold_seconds"]):
            primary = {
                "metric": "cpu_percent",
                "current_value": cpu_trend.get("current_value"),
                "threshold": self.config["cpu_overload_threshold"],
                "trend": cpu_trend.get("trend"),
                "time_to_threshold_seconds": cpu_tt,
                "confidence": cpu_conf
            }
        if mem_tt is not None and (primary is None or mem_tt < primary["time_to_threshold_seconds"]):
            primary = {
                "metric": "memory_mb",
                "current_value": mem_trend.get("current_value"),
                "threshold": self.config["memory_overload_threshold"],
                "trend": mem_trend.get("trend"),
                "time_to_threshold_seconds": mem_tt,
                "confidence": mem_conf
            }
        if lat_tt is not None and (primary is None or lat_tt < primary["time_to_threshold_seconds"]):
            primary = {
                "metric": "latency_ms",
                "current_value": lat_trend.get("current_value"),
                "threshold": self.config["latency_overload_threshold"],
                "trend": lat_trend.get("trend"),
                "time_to_threshold_seconds": lat_tt,
                "confidence": lat_conf
            }

        return {
            "cpu_trend": cpu_trend,
            "memory_trend": mem_trend,
            "latency_trend": lat_trend,
            "primary_prediction": primary or {
                "prediction_status": "insufficient_data" if cpu_trend.get("prediction_status") == "insufficient_data" else "stable",
                "trend": "stable",
                "time_to_threshold_seconds": None,
                "confidence": "insufficient_data" if cpu_trend.get("prediction_status") == "insufficient_data" else "stable"
            }
        }

    def _build_evaluation(self, state, reason, details):
        eval_result = {
            "server": self.server_name,
            "state": state,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details": details,
            "recommended_action": self._get_recommended_action(state, details)
        }
        self.last_evaluation = eval_result
        return eval_result

    def _get_recommended_action(self, state, details):
        """
        Returns informational diagnosis and operational recommendations (no automatic action in Phase 5.1).
        """
        if state == OverloadState.CRITICAL:
            return "Critical saturation detected: investigate process starvation or high error rate; prepare for traffic shedding."
        elif state == OverloadState.OVERLOAD:
            return "Overload confirmed: observe server resource trend; consider rate limiting or HAProxy weight reduction in Phase 5.2."
        elif state == OverloadState.EARLY_WARNING:
            return "Early warning: server approaching threshold limits; monitor latency trend and traffic distribution."
        elif state == OverloadState.HIGH_TRAFFIC_HEALTHY:
            return "Normal operations: high traffic handled successfully within nominal latency bounds; no action required."
        elif state == OverloadState.OFFLINE:
            return "Server is offline or stopped; active health check exclusion in effect."
        return "Operating nominal; baseline tracking active."


class TrafficOverloadEngine:
    """
    Cluster-wide Intelligent Traffic Overload Detection and Classification Engine.
    Coordinates individual server detectors, cluster metrics aggregation,
    Redis state persistence, and incident deduplication.
    """
    def __init__(self, target_services=None, redis_client=None, load_test_manager=None, haproxy_client=None):
        self.target_services = target_services or ["app1", "app2", "app3", "app4", "app5"]
        self.redis_client = redis_client
        self.load_test_manager = load_test_manager
        self.haproxy_client = haproxy_client
        self.detectors = {srv: ServerOverloadDetector(srv) for srv in self.target_services}
        self.last_cluster_status = {}

    def get_detector(self, service):
        if service not in self.detectors:
            self.detectors[service] = ServerOverloadDetector(service)
        return self.detectors[service]

    def reset_detectors(self):
        self.detectors = {srv: ServerOverloadDetector(srv) for srv in self.target_services}

    def update_config(self, new_config):
        for detector in self.detectors.values():
            detector.config.update(new_config)

    def evaluate_cluster(self, server_metrics_dict, locust_metrics=None, haproxy_stats=None):
        """
        Evaluates all monitored servers and produces a unified cluster overload status.
        server_metrics_dict: dict mapping service_name -> metric dictionary
        locust_metrics: live stats from Locust if available
        haproxy_stats: backends dictionary from HAProxyClient
        """
        # Extract traffic signals
        locust_active = False
        global_rps = 0.0
        active_users = 0
        p95_ms = 0.0
        p99_ms = 0.0
        avg_lat_ms = 0.0
        err_rate_pct = 0.0
        total_reqs = 0
        distribution_counts = {f"Server-{i}": 0 for i in range(1, len(self.target_services) + 1)}
        distribution_pcts = {f"Server-{i}": 0.0 for i in range(1, len(self.target_services) + 1)}

        if locust_metrics and isinstance(locust_metrics, dict):
            traffic = locust_metrics.get("traffic", {})
            r_times = locust_metrics.get("response_times_ms", {})
            errors = locust_metrics.get("errors", {})
            dist = locust_metrics.get("backend_distribution", {})

            locust_state = traffic.get("state", "idle")
            locust_active = (locust_state == "running" or active_users > 0)
            if locust_active:
                global_rps = float(traffic.get("current_rps", 0.0) or traffic.get("total_rps", 0.0) or 0.0)
            else:
                global_rps = 0.0
            active_users = int(traffic.get("active_users", 0) or 0)
            total_reqs = int(traffic.get("total_requests", 0) or 0)

            avg_lat_ms = float(r_times.get("avg", 0.0) or 0.0)
            p95_ms = float(r_times.get("p95", 0.0) or 0.0)
            p99_ms = float(r_times.get("p99", 0.0) or 0.0)
            err_rate_pct = float(errors.get("error_rate_pct", 0.0) or 0.0)

            if dist.get("percentages"):
                distribution_pcts = dist.get("percentages")
            if dist.get("counts"):
                distribution_counts = dist.get("counts")

        # Map canonical server names (e.g. app1 -> Server-1)
        srv_name_map = {f"app{i}": f"Server-{i}" for i in range(1, len(self.target_services) + 1)}

        server_evaluations = {}
        for srv in self.target_services:
            canonical_name = srv_name_map.get(srv, srv)
            srv_metric = server_metrics_dict.get(srv, {})

            srv_traffic = {
                "global_rps": global_rps,
                "active_users": active_users,
                "backend_rps": round(global_rps * (distribution_pcts.get(canonical_name, 20.0) / 100.0), 2),
                "backend_pct": distribution_pcts.get(canonical_name, 20.0),
                "p95_ms": p95_ms,
                "p99_ms": p99_ms,
                "avg_lat_ms": avg_lat_ms,
                "error_rate_pct": err_rate_pct,
                "locust_active": locust_active
            }

            detector = self.get_detector(srv)
            srv_eval = detector.evaluate(srv_metric, srv_traffic)
            server_evaluations[srv] = srv_eval

        # Aggregate cluster classification
        counts = {
            OverloadState.NORMAL: 0,
            OverloadState.HIGH_TRAFFIC_HEALTHY: 0,
            OverloadState.EARLY_WARNING: 0,
            OverloadState.OVERLOAD: 0,
            OverloadState.CRITICAL: 0,
            OverloadState.OFFLINE: 0
        }

        for ev in server_evaluations.values():
            st = ev.get("state", OverloadState.NORMAL)
            counts[st] = counts.get(st, 0) + 1

        if counts[OverloadState.CRITICAL] > 0:
            cluster_state = OverloadState.CRITICAL
            cluster_reason = f"{counts[OverloadState.CRITICAL]} server(s) experiencing critical resource saturation or error rate"
        elif counts[OverloadState.OVERLOAD] > 0:
            cluster_state = OverloadState.OVERLOAD
            cluster_reason = f"{counts[OverloadState.OVERLOAD]} server(s) experiencing confirmed performance overload"
        elif counts[OverloadState.EARLY_WARNING] > 0:
            cluster_state = OverloadState.EARLY_WARNING
            cluster_reason = f"{counts[OverloadState.EARLY_WARNING]} server(s) showing early warning signs of degradation"
        elif counts[OverloadState.HIGH_TRAFFIC_HEALTHY] > 0 or (global_rps >= 8.0 and counts[OverloadState.NORMAL] > 0):
            cluster_state = OverloadState.HIGH_TRAFFIC_HEALTHY
            cluster_reason = f"High traffic successfully served across {counts[OverloadState.HIGH_TRAFFIC_HEALTHY] + counts[OverloadState.NORMAL]} backends within nominal latency"
        else:
            cluster_state = OverloadState.NORMAL
            cluster_reason = "Cluster operating within normal nominal parameters"

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster_state": cluster_state,
            "cluster_reason": cluster_reason,
            "state_counts": counts,
            "traffic": {
                "global_rps": global_rps,
                "active_users": active_users,
                "total_requests": total_reqs,
                "avg_latency_ms": avg_lat_ms,
                "p95_latency_ms": p95_ms,
                "p99_latency_ms": p99_ms,
                "error_rate_pct": err_rate_pct,
                "locust_active": locust_active,
                "distribution_pcts": distribution_pcts
            },
            "servers": server_evaluations
        }

        self.last_cluster_status = result

        # Record and manage incidents in Redis
        self._record_cluster_status(result)

        return result

    def _record_cluster_status(self, cluster_status):
        if not self.redis_client:
            return

        try:
            # 1. Update live status cache
            self.redis_client.set("overload:status", json.dumps(cluster_status))

            # 2. Check and record server incidents (deduplicated)
            for srv, ev in cluster_status.get("servers", {}).items():
                st = ev.get("state")
                existing_inc = self.redis_client.get(f"overload:incident:{srv}")

                if st in {OverloadState.EARLY_WARNING, OverloadState.OVERLOAD, OverloadState.CRITICAL}:
                    if not existing_inc:
                        # Create new diagnostic overload incident
                        inc_data = {
                            "incident_id": f"ovl-{srv}-{int(time.time())}",
                            "server": srv,
                            "state": st,
                            "severity": "critical" if st == OverloadState.CRITICAL else ("high" if st == OverloadState.OVERLOAD else "medium"),
                            "detected_at": ev.get("timestamp"),
                            "reason": ev.get("reason"),
                            "metrics": ev.get("details", {}).get("metrics", {}),
                            "recommended_action": ev.get("recommended_action"),
                            "lifecycle": "detected_informational"
                        }
                        self.redis_client.set(f"overload:incident:{srv}", json.dumps(inc_data))
                        self.redis_client.lpush("overload:history", json.dumps(inc_data))
                        self.redis_client.ltrim("overload:history", 0, 49)
                elif st == OverloadState.NORMAL:
                    if existing_inc:
                        # Close existing incident
                        inc_obj = json.loads(existing_inc)
                        inc_obj["lifecycle"] = "cleared"
                        inc_obj["cleared_at"] = datetime.now(timezone.utc).isoformat()
                        self.redis_client.delete(f"overload:incident:{srv}")
                        self.redis_client.lpush("overload:history", json.dumps(inc_obj))
                        self.redis_client.ltrim("overload:history", 0, 49)

        except Exception as e:
            # Graceful Redis failure handling
            pass

    def get_live_status(self):
        if self.redis_client:
            try:
                cached = self.redis_client.get("overload:status")
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        return self.last_cluster_status or {"cluster_state": OverloadState.NORMAL, "servers": {}}

    def get_history(self, limit=20):
        if self.redis_client:
            try:
                records = self.redis_client.lrange("overload:history", 0, limit - 1)
                return [json.loads(r) for r in records]
            except Exception:
                pass
        return []
