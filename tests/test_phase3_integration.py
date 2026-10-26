import unittest
from unittest.mock import MagicMock, patch
import json
import sys
from pathlib import Path

# Add monitor directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import monitor
    from monitor import (
        TARGET_SERVICES,
        create_incident,
        get_active_incident,
        analyze_service,
        app as flask_app
    )
    from detector import (
        detect_cpu_anomaly,
        detect_memory_anomaly,
        detect_disk_anomaly,
        detect_service_failure,
        detect_latency_anomaly,
        get_detector,
        reset_detectors
    )
    from recovery import (
        RecoveryDecisionEngine,
        RecoveryStrategy,
        select_recovery_strategy,
        set_server_state,
        get_server_state
    )
    from haproxy_client import HAProxyClient
    MONITOR_MODULE = "monitor"
except ImportError:
    import monitor.monitor as monitor
    from monitor.monitor import (
        TARGET_SERVICES,
        create_incident,
        get_active_incident,
        analyze_service,
        app as flask_app
    )
    from monitor.detector import (
        detect_cpu_anomaly,
        detect_memory_anomaly,
        detect_disk_anomaly,
        detect_service_failure,
        detect_latency_anomaly,
        get_detector,
        reset_detectors
    )
    from monitor.recovery import (
        RecoveryDecisionEngine,
        RecoveryStrategy,
        select_recovery_strategy,
        set_server_state,
        get_server_state
    )
    from monitor.haproxy_client import HAProxyClient
    MONITOR_MODULE = "monitor.monitor"


class FiveServerIndependentDetectionTests(unittest.TestCase):
    def setUp(self):
        reset_detectors()

    def test_cpu_anomaly_detected_specifically_on_app3(self):
        """Verify CPU spike is detected on app3 without triggering anomalies on app1, app2, app4, app5."""
        # Clean history for all servers
        clean_hist = [{"cpu_percent": 5.0, "memory_mb": 40.0, "status": "running"} for _ in range(25)]
        for s in TARGET_SERVICES:
            det = get_detector(s)
            det.train(clean_hist)

        # Spiked history only for app3
        app3_spiked_hist = list(clean_hist) + [{"cpu_percent": 88.0, "memory_mb": 40.0, "status": "running"}]
        app3_res = detect_cpu_anomaly(app3_spiked_hist, service="app3")
        self.assertTrue(app3_res["anomaly"])
        self.assertEqual(app3_res["type"], "cpu_spike")

        # Other servers with normal latest sample
        for other in ["app1", "app2", "app4", "app5"]:
            normal_hist = list(clean_hist) + [{"cpu_percent": 6.0, "memory_mb": 40.0, "status": "running"}]
            res = detect_cpu_anomaly(normal_hist, service=other)
            self.assertFalse(res["anomaly"], f"False positive anomaly on {other}")

    def test_disk_anomaly_detected_on_app4(self):
        """Verify disk exhaustion detection triggers on app4 when exceeding threshold."""
        metric_normal = {"disk_percent": 45.0, "status": "running"}
        metric_anom = {"disk_percent": 92.5, "status": "running"}

        self.assertFalse(detect_disk_anomaly(metric_normal)["anomaly"])
        res = detect_disk_anomaly(metric_anom, threshold_percent=85.0)
        self.assertTrue(res["anomaly"])
        self.assertEqual(res["type"], "disk_exhaustion")
        self.assertEqual(res["current_disk_percent"], 92.5)
        self.assertEqual(res["threshold_percent"], 85.0)

    def test_process_failure_detected_on_app2(self):
        """Verify application 500 error is classified as process_failure."""
        metric_fail = {
            "status": "running",
            "service_available": False,
            "status_code": 500,
            "health_error": "Internal Server Error",
            "pids_current": 1
        }
        res = detect_service_failure(metric_fail)
        self.assertTrue(res["anomaly"])
        self.assertEqual(res["type"], "process_failure")
        self.assertEqual(res["status_code"], 500)


class IncidentLifecycleAndDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.mock_redis = MagicMock()
        self.mock_redis.get.return_value = None

    def test_incident_record_contains_required_phase3_fields(self):
        """Verify create_incident records severity, baseline threshold, metric value, and initial state."""
        anomaly = {
            "type": "cpu_spike",
            "current_cpu": 85.0,
            "threshold_percent": 40.0,
            "reason": "Sustained high load"
        }

        with patch(f"{MONITOR_MODULE}.redis_client", self.mock_redis), \
             patch(f"{MONITOR_MODULE}.get_active_incident", return_value=None):
            inc = create_incident("app3", anomaly)

            self.assertEqual(inc["server"], "app3")
            self.assertEqual(inc["anomaly_type"], "cpu_spike")
            self.assertEqual(inc["recovery_state"], "detected")
            self.assertEqual(inc["status"], "active")
            self.assertEqual(inc["severity"], "high")
            self.assertEqual(inc["current_metric_value"], 85.0)
            self.assertEqual(inc["threshold_used"], 40.0)
            self.assertIn("metric_details", inc)

    def test_critical_severity_for_outage_events(self):
        """Verify container_crash, process_failure, and network_failure get critical severity."""
        for crash_type in ["container_crash", "process_failure", "network_failure"]:
            anomaly = {"type": crash_type, "status_code": 500}
            with patch(f"{MONITOR_MODULE}.redis_client", self.mock_redis), \
                 patch(f"{MONITOR_MODULE}.get_active_incident", return_value=None):
                inc = create_incident("app1", anomaly)
                self.assertEqual(inc["severity"], "critical")

    def test_deduplication_prevents_multiple_incidents_for_same_server(self):
        """Verify ongoing anomaly does not create duplicate incidents."""
        existing = {
            "incident_id": "app2-12345",
            "server": "app2",
            "status": "active",
            "anomaly_type": "memory_leak"
        }
        anomaly = {"type": "memory_leak", "current_memory": 150.0}

        with patch(f"{MONITOR_MODULE}.get_active_incident", return_value=existing):
            inc = create_incident("app2", anomaly)
            self.assertEqual(inc["incident_id"], "app2-12345")
            self.mock_redis.set.assert_not_called()


class TargetedRecoveryAndFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        self.mock_docker = MagicMock()
        self.mock_redis = MagicMock()
        self.engine = RecoveryDecisionEngine(
            docker_client=self.mock_docker,
            redis_client=self.mock_redis
        )

    def test_targeted_recovery_only_acts_on_specified_server(self):
        """Remediating app4 must not issue restart or stop commands to app1, app2, app3, app5."""
        with patch("recovery.recover_server") as mock_recover:
            mock_recover.return_value = True
            self.mock_redis.get.return_value = json.dumps({
                "incident_id": "app4-5555",
                "server": "app4",
                "status": "active"
            })

            result = self.engine.execute_recovery("app4", anomaly_type="container_crash", incident_id="app4-5555")
            self.assertTrue(result["success"])
            mock_recover.assert_called_once_with("app4", self.mock_docker, self.mock_redis)

    def test_failed_recovery_is_marked_recovery_failed_avoiding_restart_loops(self):
        """When recovery actions fail, incident transitions to escalated/recovery_failed."""
        with patch("recovery.recover_server") as mock_recover:
            mock_recover.return_value = False
            self.mock_redis.get.return_value = json.dumps({
                "incident_id": "app2-7777",
                "server": "app2",
                "status": "active",
                "started_at": "2026-10-23T00:00:00Z"
            })

            result = self.engine.execute_recovery("app2", anomaly_type="container_crash", incident_id="app2-7777")
            self.assertFalse(result["success"])

            # Verify escalated incident record was stored in Redis
            saved_calls = [c for c in self.mock_redis.set.call_args_list if "incident:app2" in str(c)]
            self.assertTrue(len(saved_calls) > 0)
            last_saved = json.loads(saved_calls[-1][0][1])
            self.assertEqual(last_saved["status"], "escalated")
            self.assertEqual(last_saved["recovery_state"], "recovery_failed")
            self.assertEqual(last_saved["recovery_result"], "failed")
            self.assertIn("failure_reason", last_saved)


class HAProxyIntegrationEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.app = flask_app.test_client()

    def test_servers_endpoint_returns_haproxy_status_for_all_five(self):
        """Verify /servers includes haproxy_status, haproxy_eligible, and haproxy_available."""
        mock_hp_status = {
            f"app{i}": {"status": "UP", "is_up": True, "check_code": 200, "check_status": "L7OK"}
            for i in range(1, 6)
        }
        with patch(f"{MONITOR_MODULE}.haproxy_client.get_backends_status", return_value=mock_hp_status):
            res = self.app.get("/servers")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn("servers", data)
            self.assertEqual(len(data["servers"]), 5)
            self.assertTrue(data.get("haproxy_available"))
            for s in data["servers"]:
                self.assertEqual(s["haproxy_status"], "UP")
                self.assertTrue(s["haproxy_eligible"])
                self.assertEqual(s["haproxy_check_code"], 200)

    def test_haproxy_status_endpoint(self):
        """Verify /haproxy/status returns operational backend mapping."""
        mock_hp_status = {
            "app1": {"status": "UP", "is_up": True},
            "app2": {"status": "DOWN", "is_up": False}
        }
        with patch(f"{MONITOR_MODULE}.haproxy_client.get_backends_status", return_value=mock_hp_status):
            res = self.app.get("/haproxy/status")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "healthy")
            self.assertIn("app1", data["backends"])
            self.assertIn("app2", data["backends"])


if __name__ == "__main__":
    unittest.main()
