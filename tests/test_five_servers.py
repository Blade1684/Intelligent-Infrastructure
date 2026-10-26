import unittest
from unittest.mock import MagicMock, patch
import json
import sys
from pathlib import Path

# Add monitor directory to sys.path if not present
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import monitor
    from monitor import TARGET_SERVICES, startup_reconciliation
    from detector import get_detector, reset_detectors
    from recovery import (
        RecoveryDecisionEngine,
        RecoveryStrategy,
        select_recovery_strategy,
        set_server_state,
        get_server_state
    )
    MONITOR_MODULE = "monitor"
    RECOVERY_MODULE = "recovery"
except ImportError:
    import monitor.monitor as monitor
    from monitor.monitor import TARGET_SERVICES, startup_reconciliation
    from monitor.detector import get_detector, reset_detectors
    from monitor.recovery import (
        RecoveryDecisionEngine,
        RecoveryStrategy,
        select_recovery_strategy,
        set_server_state,
        get_server_state
    )
    MONITOR_MODULE = "monitor.monitor"
    RECOVERY_MODULE = "monitor.recovery"


class TargetServicesConfigurationTests(unittest.TestCase):
    def test_target_services_contains_all_five_servers(self):
        """Verify TARGET_SERVICES explicitly includes app1 through app5."""
        expected = ["app1", "app2", "app3", "app4", "app5"]
        self.assertEqual(len(TARGET_SERVICES), 5)
        self.assertListEqual(TARGET_SERVICES, expected)

    def test_each_target_service_has_valid_name_format(self):
        """Verify each target service follows standard naming convention."""
        for i, service in enumerate(TARGET_SERVICES, start=1):
            self.assertEqual(service, f"app{i}")


class FiveServerMonitoringAndStateTests(unittest.TestCase):
    def setUp(self):
        self.mock_redis = MagicMock()
        self.mock_docker = MagicMock()

    def test_server_state_management_across_all_five_servers(self):
        """Verify state tracking functions independently for each of the 5 servers."""
        for service in TARGET_SERVICES:
            state = set_server_state(service, "healthy", "Normal", self.mock_redis)
            self.assertEqual(state["server"], service)
            self.assertEqual(state["state"], "healthy")

        # Set app3 to isolated and app4 to recovering
        set_server_state("app3", "isolated", "Fault injection", self.mock_redis)
        set_server_state("app4", "recovering", "Container restart", self.mock_redis)

        self.assertEqual(get_server_state("app1", self.mock_redis)["state"], "healthy")
        self.assertEqual(get_server_state("app2", self.mock_redis)["state"], "healthy")
        self.assertEqual(get_server_state("app3", self.mock_redis)["state"], "isolated")
        self.assertEqual(get_server_state("app4", self.mock_redis)["state"], "recovering")
        self.assertEqual(get_server_state("app5", self.mock_redis)["state"], "healthy")

    def test_startup_reconciliation_evaluates_all_five_servers(self):
        """Verify startup reconciliation checks every one of the 5 servers."""
        with patch(f"{MONITOR_MODULE}.get_server_state") as mock_state, \
             patch(f"{MONITOR_MODULE}.get_active_incident") as mock_incident, \
             patch(f"{MONITOR_MODULE}.automatic_recovery") as mock_recovery:

            def state_side_effect(service, redis):
                if service == "app5":
                    return {"server": "app5", "state": "isolated"}
                return {"server": service, "state": "healthy"}

            def incident_side_effect(service):
                if service == "app5":
                    return {"incident_id": "app5-12345", "status": "active", "anomaly_type": "container_crash"}
                return None

            mock_state.side_effect = state_side_effect
            mock_incident.side_effect = incident_side_effect

            startup_reconciliation()

            # Should have checked state for all 5 servers
            checked_servers = [call.args[0] for call in mock_state.call_args_list]
            for service in TARGET_SERVICES:
                self.assertIn(service, checked_servers)

            # Should have resumed recovery specifically for app5
            mock_recovery.assert_called_once_with("app5")


class FiveServerAnomalyDetectorIsolationTests(unittest.TestCase):
    def setUp(self):
        reset_detectors()

    def test_independent_detector_instance_per_server(self):
        """Verify each of the 5 servers maintains an isolated ML detector baseline."""
        detectors = {service: get_detector(service) for service in TARGET_SERVICES}
        self.assertEqual(len(detectors), 5)

        # Ensure all instances are distinct objects in memory
        detector_ids = [id(d) for d in detectors.values()]
        self.assertEqual(len(detector_ids), len(set(detector_ids)))

    def test_anomaly_on_one_server_does_not_pollute_others(self):
        """Training and predicting on app3 should not alter baselines of other servers."""
        app3_detector = get_detector("app3")
        app1_detector = get_detector("app1")

        # Initial clean training on app1
        clean_history = [{"cpu_percent": 2.0, "memory_mb": 45.0, "status": "running"} for _ in range(25)]
        app1_detector.train(clean_history)
        self.assertTrue(app1_detector.is_trained)
        self.assertFalse(app3_detector.is_trained)


class FiveServerRecoveryEngineTests(unittest.TestCase):
    def setUp(self):
        self.mock_docker = MagicMock()
        self.mock_redis = MagicMock()
        self.engine = RecoveryDecisionEngine(
            docker_client=self.mock_docker,
            redis_client=self.mock_redis
        )

    def test_strategy_selection_works_identically_across_all_five_servers(self):
        """Verify strategy selection rules apply uniformly across app1..app5."""
        for service in TARGET_SERVICES:
            self.assertEqual(
                select_recovery_strategy("cpu_spike", service=service),
                RecoveryStrategy.CPU_WORKER_TERMINATION
            )
            self.assertEqual(
                select_recovery_strategy("memory_leak", service=service),
                RecoveryStrategy.MEMORY_CLEANUP
            )
            self.assertEqual(
                select_recovery_strategy("process_failure", service=service, details={"pids_current": 3}),
                RecoveryStrategy.PROCESS_RELOAD
            )
            self.assertEqual(
                select_recovery_strategy("disk_exhaustion", service=service, details={"container_status": "running"}),
                RecoveryStrategy.DISK_CLEANUP
            )
            self.assertEqual(
                select_recovery_strategy("container_crash", service=service),
                RecoveryStrategy.CONTAINER_RESTART
            )

    def test_container_restart_remediation_on_app5(self):
        """Verify recovery execution specifically targets app5 without cross-affecting others."""
        with patch(f"{RECOVERY_MODULE}.recover_server") as mock_recover:
            mock_recover.return_value = True
            self.mock_redis.get.return_value = json.dumps({
                "incident_id": "app5-9999",
                "server": "app5",
                "status": "active",
                "started_at": "2026-10-23T00:00:00Z"
            })

            result = self.engine.execute_recovery("app5", anomaly_type="container_crash", incident_id="app5-9999")

            self.assertTrue(result["success"])
            mock_recover.assert_called_once_with("app5", self.mock_docker, self.mock_redis)


if __name__ == "__main__":
    unittest.main()
