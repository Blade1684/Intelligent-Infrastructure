import os
import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest

# ============================================================
# CONFIGURABLE DETECTION THRESHOLDS
# ============================================================
DEFAULT_DISK_THRESHOLD_PERCENT = float(os.getenv("DISK_THRESHOLD_PERCENT", "85.0"))
DEFAULT_LATENCY_THRESHOLD_MS = float(os.getenv("LATENCY_THRESHOLD_MS", "500.0"))
DEFAULT_LATENCY_SUSTAINED_COUNT = int(os.getenv("LATENCY_SUSTAINED_COUNT", "3"))


def detect_service_failure(metric):
    """
    Classify container and application-health failures before trend analysis.
    Distinguishes container crashes, process failures, and network unreachable events.
    """
    if not metric or not isinstance(metric, dict):
        return {"anomaly": False, "reason": "Invalid metric"}

    status = metric.get("status")

    if status in {"exited", "dead", "created", "paused"}:
        return {
            "anomaly": True,
            "type": "container_crash",
            "reason": f"Container status is {status}",
            "container_status": status
        }

    if status == "not_found":
        return {
            "anomaly": True,
            "type": "container_missing",
            "reason": "Container is not present"
        }

    if status == "running":
        # 1. Process check inside container: PID count zero while container status is running
        pids_current = metric.get("pids_current")
        if pids_current is not None and pids_current <= 0:
            return {
                "anomaly": True,
                "type": "process_failure",
                "reason": "Application process terminated inside container (active PIDs: 0)",
                "container_status": status,
                "pids_current": pids_current
            }

        # 2. Service availability check
        if not metric.get("service_available", False):
            status_code = metric.get("status_code")
            health_error = str(metric.get("health_error", ""))

            # Application returned 5xx server error or degraded HTTP status
            if status_code is not None and status_code >= 500:
                return {
                    "anomaly": True,
                    "type": "process_failure",
                    "reason": f"Application process error: HTTP {status_code} ({health_error or 'Internal Server Error'})",
                    "status_code": status_code,
                    "container_status": status
                }

            if status_code is not None and status_code != 200:
                return {
                    "anomaly": True,
                    "type": "process_failure",
                    "reason": f"Application returned unexpected status: HTTP {status_code}",
                    "status_code": status_code,
                    "container_status": status
                }

            # Explicit process failure indication in error message
            if "process" in health_error.lower() or "worker" in health_error.lower() or "crashed" in health_error.lower():
                return {
                    "anomaly": True,
                    "type": "process_failure",
                    "reason": f"Process failure: {health_error}",
                    "container_status": status
                }

            # Otherwise socket / network connectivity issue (connection refused, timeout, host unreachable)
            return {
                "anomaly": True,
                "type": "network_failure",
                "reason": health_error or "Network health probe unreachable or timed out",
                "container_status": status
            }

    return {"anomaly": False, "reason": "Service is available"}


def detect_disk_anomaly(metric, threshold_percent=None):
    """
    Detects unusually high container storage usage against a configurable threshold.
    """
    if not metric or not isinstance(metric, dict):
        return {"anomaly": False, "reason": "Invalid metric"}

    threshold = threshold_percent if threshold_percent is not None else DEFAULT_DISK_THRESHOLD_PERCENT
    disk_percent = float(metric.get("disk_percent", 0.0))

    if disk_percent >= threshold:
        return {
            "anomaly": True,
            "type": "disk_exhaustion",
            "current_disk_percent": disk_percent,
            "threshold_percent": threshold,
            "reason": f"Container disk usage ({disk_percent}%) exceeds threshold of {threshold}%"
        }

    return {"anomaly": False, "reason": "Normal disk usage"}


def detect_latency_anomaly(history, service=None, threshold_ms=None, sustained_count=None):
    """
    Detects sustained abnormal application response latency across consecutive health checks.
    Single transient spikes are ignored to prevent false positives.
    """
    if not history:
        return {"anomaly": False, "reason": "No metric history"}

    threshold = threshold_ms if threshold_ms is not None else DEFAULT_LATENCY_THRESHOLD_MS
    count_required = sustained_count if sustained_count is not None else DEFAULT_LATENCY_SUSTAINED_COUNT

    valid_samples = [
        s for s in history
        if isinstance(s, dict)
        and s.get("status") == "running"
        and s.get("latency_ms") is not None
    ]

    if len(valid_samples) < count_required:
        return {"anomaly": False, "reason": "Insufficient latency samples"}

    recent_window = valid_samples[-count_required:]
    all_exceed = all(float(s.get("latency_ms", 0.0)) >= threshold for s in recent_window)

    if all_exceed:
        latest_latency = float(recent_window[-1].get("latency_ms", 0.0))
        avg_latency = round(sum(float(s.get("latency_ms", 0.0)) for s in recent_window) / len(recent_window), 2)
        return {
            "anomaly": True,
            "type": "high_latency",
            "current_latency_ms": latest_latency,
            "average_latency_ms": avg_latency,
            "threshold_ms": threshold,
            "sustained_count": count_required,
            "reason": f"Sustained response latency (avg {avg_latency}ms, latest {latest_latency}ms) exceeded threshold of {threshold}ms for {count_required} consecutive checks"
        }

    return {"anomaly": False, "reason": "Normal latency"}


class AnomalyDetectorML:
    def __init__(self, contamination=0.1, min_samples=20, max_samples=100):
        self.model = IsolationForest(n_estimators=100, contamination=contamination, random_state=42)
        self.is_trained = False
        self.feature_names = ['cpu_percent', 'memory_mb']
        self.means = []
        self.stds = []
        self.clean_history = []
        self.min_samples = min_samples
        self.max_samples = max_samples

    def _extract_samples(self, history):
        """Extracts numerical [cpu_percent, memory_mb] pairs from history."""
        samples = []
        for s in history:
            if isinstance(s, dict):
                if s.get('status', 'running') == 'running':
                    samples.append([float(s.get('cpu_percent', 0.0)), float(s.get('memory_mb', 0.0))])
            elif isinstance(s, (list, tuple)) and len(s) >= 2:
                samples.append([float(s[0]), float(s[1])])
        return samples

    def _is_sample_anomalous(self, sample):
        """
        Determines whether a candidate sample is abnormal and must be excluded from baseline training.
        """
        cpu, mem = sample[0], sample[1]

        # Domain guard: High CPU (> 40%) is never normal baseline
        if cpu > 40.0:
            return True

        if not self.is_trained:
            # During cold start / warm-up, reject extreme outliers
            if mem > 85.0:
                return True
            return False

        # If trained, compare against established clean baseline:
        # 1. Memory leak threshold: > 1.5x baseline mean (minimum 70MB threshold)
        mem_threshold = max(self.means[1] * 1.5, 70.0)
        if mem > mem_threshold:
            return True

        # 2. Statistical Z-score deviation check (> 3.0 std dev)
        z_cpu = abs(cpu - self.means[0]) / self.stds[0]
        z_mem = abs(mem - self.means[1]) / self.stds[1]
        if z_cpu > 3.0 or z_mem > 3.0:
            return True

        # 3. Isolation Forest outlier check
        data = pd.DataFrame([[cpu, mem]], columns=self.feature_names)
        if self.model.predict(data)[0] == -1:
            if z_cpu > 2.5 or z_mem > 2.5:
                return True

        return False

    def _fit(self, clean_samples):
        """Fits Isolation Forest and computes baseline statistics on confirmed clean samples."""
        df = pd.DataFrame(clean_samples, columns=self.feature_names)
        self.model.fit(df)
        self.means = df.mean().values
        # Enforce minimum std floor (1.5% CPU, 2.0MB memory) to prevent false positives when variance is near zero
        min_stds = np.array([1.5, 2.0])
        self.stds = np.maximum(df.std().values, min_stds)
        self.is_trained = True

    def train(self, history):
        """
        Trains or updates the model while strictly excluding anomalous samples
        from the baseline dataset to prevent contamination.
        """
        raw_samples = self._extract_samples(history)
        if not raw_samples:
            return False

        if not self.is_trained:
            # Warm-up phase: only admit clean samples
            clean_candidates = [s for s in raw_samples if not self._is_sample_anomalous(s)]
            if len(clean_candidates) < self.min_samples:
                return False
            self.clean_history = clean_candidates[-self.max_samples:]
            self._fit(self.clean_history)
            return True
        else:
            # Model is trained: only admit confirmed non-anomalous samples
            clean_from_history = [s for s in raw_samples if not self._is_sample_anomalous(s)]

            if len(clean_from_history) >= self.min_samples:
                # Fresh batch of clean samples
                self.clean_history = clean_from_history[-self.max_samples:]
                self._fit(self.clean_history)
                return True
            elif len(clean_from_history) > 0 and len(self.clean_history) > 0:
                # Maintain clean history sliding window
                for s in clean_from_history:
                    self.clean_history.append(s)
                self.clean_history = self.clean_history[-self.max_samples:]
                if len(self.clean_history) >= self.min_samples:
                    self._fit(self.clean_history)
                return True
            else:
                # All samples in history were anomalous!
                # Do NOT pollute baseline; preserve existing clean baseline.
                return False

    def predict(self, current_metrics):
        """Predicts if current metrics are anomalous using hybrid approach."""
        if not self.is_trained:
            return False, "not_trained"

        if isinstance(current_metrics, dict):
            metrics = [float(current_metrics.get('cpu_percent', 0.0)), float(current_metrics.get('memory_mb', 0.0))]
        elif isinstance(current_metrics, (list, tuple)) and len(current_metrics) >= 2:
            metrics = [float(current_metrics[0]), float(current_metrics[1])]
        else:
            return False, "invalid_metrics"

        # 1. Z-score check (major deviations)
        for i, val in enumerate(metrics):
            z = (val - self.means[i]) / self.stds[i]
            if abs(z) > 5.0:
                return True, "z_score_spike"

        # 2. Domain check for sustained memory leak
        mem_threshold = max(self.means[1] * 1.5, 70.0)
        if metrics[1] > mem_threshold:
            return True, "memory_growth_anomaly"

        # 3. Isolation Forest prediction with safety guard
        data = pd.DataFrame([metrics], columns=self.feature_names)
        if self.model.predict(data)[0] == -1:
            if any(abs((val - self.means[i]) / self.stds[i]) > 2.5 for i, val in enumerate(metrics)):
                return True, "isolation_forest_anomaly"

        return False, "normal"

# Keep instances per service
_detectors = {}

def get_detector(service):
    if service not in _detectors:
        _detectors[service] = AnomalyDetectorML()
    return _detectors[service]

def reset_detectors():
    """Reset detectors dictionary (useful for isolated unit testing)."""
    global _detectors
    _detectors = {}

def detect_cpu_anomaly(history, service=None):
    """
    Legacy wrapper for CPU compatibility in monitor.py.
    """
    if not history or not service:
        return {"anomaly": False, "reason": "No history or service"}

    detector = get_detector(service)
    detector.train(history) # Train on clean samples from history

    latest = history[-1]
    is_anom, reason = detector.predict(latest)

    if is_anom and latest.get('cpu_percent', 0) > 40:
        return {
            "anomaly": True,
            "type": "cpu_spike",
            "current_cpu": latest.get('cpu_percent'),
            "reason": f"ML Detection: {reason}"
        }
    return {"anomaly": False, "reason": "Normal"}

def detect_memory_anomaly(history, service=None):
    """
    Legacy wrapper for Memory compatibility in monitor.py.
    """
    if not history or not service:
        return {"anomaly": False, "reason": "No history or service"}

    detector = get_detector(service)
    detector.train(history)

    latest = history[-1]
    is_anom, reason = detector.predict(latest)

    mem_thresh = max(detector.means[1] * 1.5, 70.0) if detector.is_trained else 70.0
    if is_anom and latest.get('memory_mb', 0) > mem_thresh:
        return {
            "anomaly": True,
            "type": "memory_leak",
            "current_memory": latest.get('memory_mb'),
            "reason": f"ML Detection: {reason}"
        }
    return {"anomaly": False, "reason": "Normal"}
