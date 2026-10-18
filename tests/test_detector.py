import sys
import types
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    # Tests mock outbound HTTP; this only lets host-side unit tests import
    # the monitor module when dependencies are installed solely in Docker.
    sys.modules["requests"] = types.SimpleNamespace(RequestException=Exception)

from detector import (
    detect_service_failure,
    detect_disk_anomaly,
    detect_latency_anomaly,
    AnomalyDetectorML,
    detect_cpu_anomaly,
    detect_memory_anomaly,
    get_detector,
    reset_detectors
)
from recovery import get_server_state, recover_server, set_server_state


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class ServiceFailureDetectionTests(unittest.TestCase):
    def test_exited_container_is_a_crash_incident(self):
        result = detect_service_failure({"status": "exited"})
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "container_crash")

    def test_dead_or_paused_container_is_a_crash_incident(self):
        for st in ("dead", "paused", "created"):
            result = detect_service_failure({"status": st})
            self.assertTrue(result["anomaly"])
            self.assertEqual(result["type"], "container_crash")

    def test_missing_container_is_container_missing(self):
        result = detect_service_failure({"status": "not_found"})
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "container_missing")

    def test_running_but_unreachable_service_is_a_network_incident(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "health_error": "Connection refused"
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "network_failure")

    def test_network_timeout_is_network_failure(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "health_error": "HTTPSConnectionPool: Read timed out."
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "network_failure")

    def test_healthy_service_is_not_an_anomaly(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": True
        })
        self.assertFalse(result["anomaly"])


class ProcessFailureDetectionTests(unittest.TestCase):
    def test_running_container_with_zero_pids_is_process_failure(self):
        result = detect_service_failure({
            "status": "running",
            "pids_current": 0,
            "service_available": False
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "process_failure")
        self.assertIn("active PIDs: 0", result["reason"])

    def test_http_500_internal_server_error_is_process_failure(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "status_code": 500,
            "health_error": "Health endpoint returned HTTP 500"
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "process_failure")
        self.assertEqual(result["status_code"], 500)

    def test_http_503_service_unavailable_is_process_failure(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "status_code": 503,
            "health_error": "Health endpoint returned HTTP 503"
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "process_failure")
        self.assertEqual(result["status_code"], 503)

    def test_explicit_worker_crashed_error_is_process_failure(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "health_error": "Application worker process crashed unexpectedly"
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "process_failure")


class DiskAnomalyDetectionTests(unittest.TestCase):
    def test_disk_usage_below_threshold_is_normal(self):
        result = detect_disk_anomaly({"disk_percent": 35.0})
        self.assertFalse(result["anomaly"])
        self.assertEqual(result["reason"], "Normal disk usage")

    def test_disk_usage_exceeding_default_threshold_is_anomaly(self):
        result = detect_disk_anomaly({"disk_percent": 88.5})
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "disk_exhaustion")
        self.assertEqual(result["current_disk_percent"], 88.5)
        self.assertEqual(result["threshold_percent"], 85.0)

    def test_disk_usage_custom_configurable_threshold(self):
        # Exceeds custom 70% threshold
        result1 = detect_disk_anomaly({"disk_percent": 75.0}, threshold_percent=70.0)
        self.assertTrue(result1["anomaly"])
        self.assertEqual(result1["type"], "disk_exhaustion")

        # Does not exceed custom 80% threshold
        result2 = detect_disk_anomaly({"disk_percent": 75.0}, threshold_percent=80.0)
        self.assertFalse(result2["anomaly"])

    def test_disk_usage_safely_handles_empty_or_invalid_metrics(self):
        self.assertFalse(detect_disk_anomaly({})["anomaly"])
        self.assertFalse(detect_disk_anomaly(None)["anomaly"])
        self.assertFalse(detect_disk_anomaly({"disk_percent": 0.0})["anomaly"])


class LatencyAnomalyDetectionTests(unittest.TestCase):
    def test_normal_response_latency_is_not_an_anomaly(self):
        history = [
            {"status": "running", "latency_ms": 35.0}
            for _ in range(10)
        ]
        result = detect_latency_anomaly(history, "app1")
        self.assertFalse(result["anomaly"])
        self.assertEqual(result["reason"], "Normal latency")

    def test_single_transient_spike_is_ignored_by_sustained_gating(self):
        """Single transient spike should not cause false positive incidents."""
        history = [
            {"status": "running", "latency_ms": 25.0}
            for _ in range(10)
        ]
        # Append 1 single latency spike (850ms)
        history.append({"status": "running", "latency_ms": 850.0})
        result = detect_latency_anomaly(history, "app1", threshold_ms=500.0, sustained_count=3)
        self.assertFalse(result["anomaly"])

    def test_sustained_abnormal_latency_triggers_anomaly(self):
        """Sustained abnormal latency exceeding threshold for 3 consecutive checks triggers anomaly."""
        history = [
            {"status": "running", "latency_ms": 25.0}
            for _ in range(5)
        ]
        sustained_spikes = [
            {"status": "running", "latency_ms": 620.0},
            {"status": "running", "latency_ms": 580.0},
            {"status": "running", "latency_ms": 710.0}
        ]
        history.extend(sustained_spikes)
        result = detect_latency_anomaly(history, "app1", threshold_ms=500.0, sustained_count=3)
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "high_latency")
        self.assertEqual(result["current_latency_ms"], 710.0)
        self.assertAlmostEqual(result["average_latency_ms"], 636.67, delta=1.0)
        self.assertEqual(result["sustained_count"], 3)

    def test_custom_latency_threshold_and_sustained_count(self):
        history = [
            {"status": "running", "latency_ms": 120.0},
            {"status": "running", "latency_ms": 130.0}
        ]
        result = detect_latency_anomaly(history, "app1", threshold_ms=100.0, sustained_count=2)
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["threshold_ms"], 100.0)
        self.assertEqual(result["sustained_count"], 2)

    def test_insufficient_samples_does_not_trigger_anomaly(self):
        history = [
            {"status": "running", "latency_ms": 999.0}
        ]
        result = detect_latency_anomaly(history, "app1", threshold_ms=500.0, sustained_count=3)
        self.assertFalse(result["anomaly"])
        self.assertEqual(result["reason"], "Insufficient latency samples")


class RecoveryTests(unittest.TestCase):
    def test_state_survives_memory_reset_when_redis_is_available(self):
        redis_client = FakeRedis()
        set_server_state("app1", "isolated", "test", redis_client)
        self.assertEqual(
            get_server_state("app1", redis_client)["state"], "isolated"
        )

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", side_effect=[False, True])
    def test_recovery_retries_until_application_health_is_verified(
        self, health_check, _sleep
    ):
        container = Mock()
        container.status = "running"
        docker_client = Mock()
        docker_client.containers.list.return_value = [container]
        redis_client = FakeRedis()

        self.assertTrue(recover_server("app1", docker_client, redis_client))
        self.assertEqual(health_check.call_count, 2)
        self.assertEqual(
            get_server_state("app1", redis_client)["state"], "healthy"
        )

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=False)
    def test_failed_recovery_is_marked_failed_after_retry_limit(
        self, _health_check, _sleep
    ):
        container = Mock()
        container.status = "running"
        docker_client = Mock()
        docker_client.containers.list.return_value = [container]
        redis_client = FakeRedis()

        self.assertFalse(recover_server("app1", docker_client, redis_client))
        state = get_server_state("app1", redis_client)
        self.assertEqual(state["state"], "failed")
        self.assertIn("2 attempts", state["reason"])


class AnomalyDetectorContaminationTests(unittest.TestCase):
    def setUp(self):
        reset_detectors()

    def test_initial_model_warmup(self):
        """Warm-up: Model with fewer than min_samples (20) reports not_trained."""
        detector = AnomalyDetectorML(min_samples=20)
        few_samples = [
            {"cpu_percent": 2.0, "memory_mb": 45.0, "status": "running"}
            for _ in range(15)
        ]
        self.assertFalse(detector.train(few_samples))
        self.assertFalse(detector.is_trained)

        is_anom, reason = detector.predict({"cpu_percent": 5.0, "memory_mb": 45.0})
        self.assertFalse(is_anom)
        self.assertEqual(reason, "not_trained")

        result = detect_cpu_anomaly(few_samples, "app1")
        self.assertFalse(result["anomaly"])

    def test_normal_metric_samples(self):
        """Normal metrics train the model and do not trigger false positive anomalies."""
        detector = AnomalyDetectorML(min_samples=20)
        normal_samples = [
            {"cpu_percent": float(i % 5 + 3), "memory_mb": float(40 + i % 6), "status": "running"}
            for i in range(25)
        ]
        self.assertTrue(detector.train(normal_samples))
        self.assertTrue(detector.is_trained)

        is_anom, reason = detector.predict({"cpu_percent": 6.5, "memory_mb": 44.0})
        self.assertFalse(is_anom)
        self.assertEqual(reason, "normal")

    def test_sudden_cpu_spikes(self):
        """Sudden CPU spike is immediately flagged as anomalous and classified as cpu_spike."""
        normal_samples = [
            {"cpu_percent": 4.0 + (i % 3), "memory_mb": 45.0, "status": "running"}
            for i in range(25)
        ]
        history = list(normal_samples)
        spike = {"cpu_percent": 88.0, "memory_mb": 45.0, "status": "running"}
        history.append(spike)

        result = detect_cpu_anomaly(history, "app1")
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "cpu_spike")
        self.assertEqual(result["current_cpu"], 88.0)

    def test_sustained_abnormal_cpu_usage(self):
        """Sustained abnormal CPU usage does not contaminate the baseline or adapt away."""
        detector = AnomalyDetectorML(min_samples=20)
        normal_samples = [
            {"cpu_percent": 5.0 + (i % 2), "memory_mb": 45.0, "status": "running"}
            for i in range(25)
        ]
        detector.train(normal_samples)
        baseline_cpu_mean = detector.means[0]
        self.assertAlmostEqual(baseline_cpu_mean, 5.5, delta=1.5)

        # Simulate 50 monitoring cycles with sustained abnormal CPU (95%)
        sustained_abnormal = [
            {"cpu_percent": 95.0, "memory_mb": 45.0, "status": "running"}
            for _ in range(50)
        ]

        # Feed sustained abnormal history repeatedly
        for _ in range(20):
            detector.train(sustained_abnormal)

        # The baseline mean MUST NOT adapt to 95%
        self.assertAlmostEqual(detector.means[0], baseline_cpu_mean, delta=2.0)

        # Anomaly prediction MUST still flag the 95% CPU as anomalous
        is_anom, reason = detector.predict({"cpu_percent": 95.0, "memory_mb": 45.0})
        self.assertTrue(is_anom)
        self.assertIn("spike", reason)

    def test_gradual_memory_growth(self):
        """Gradual memory growth is detected when exceeding threshold and does not inflate baseline."""
        detector = get_detector("app2")
        normal_samples = [
            {"cpu_percent": 5.0, "memory_mb": 40.0 + (i % 4), "status": "running"}
            for i in range(25)
        ]
        detector.train(normal_samples)

        # Gradually increase memory up to severe leak
        growing_memory = [
            {"cpu_percent": 5.0, "memory_mb": 42.0 + (i * 4.0), "status": "running"}
            for i in range(25)
        ]
        leak_sample = {"cpu_percent": 5.0, "memory_mb": 140.0, "status": "running"}
        all_samples = normal_samples + growing_memory + [leak_sample]

        result = detect_memory_anomaly(all_samples, "app2")
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "memory_leak")

        detector_app2 = get_detector("app2")
        self.assertLess(detector_app2.means[1], 70.0)

    def test_exclusion_of_confirmed_anomalous_samples_from_training(self):
        """Mixed batches with confirmed anomalies cleanly exclude the anomalous samples from training."""
        detector = AnomalyDetectorML(min_samples=20)
        normal_samples = [
            {"cpu_percent": 5.0, "memory_mb": 45.0, "status": "running"}
            for _ in range(25)
        ]
        detector.train(normal_samples)

        mixed_batch = [
            {"cpu_percent": 5.0, "memory_mb": 45.0, "status": "running"}
            for _ in range(20)
        ] + [
            {"cpu_percent": 99.0, "memory_mb": 300.0, "status": "running"}
            for _ in range(30)
        ]

        detector.train(mixed_batch)

        for s in detector.clean_history:
            self.assertLessEqual(s[0], 40.0)
            self.assertLessEqual(s[1], 150.0)

        self.assertAlmostEqual(detector.means[0], 5.0, delta=1.0)
        self.assertAlmostEqual(detector.means[1], 45.0, delta=2.0)


if __name__ == "__main__":
    unittest.main()
