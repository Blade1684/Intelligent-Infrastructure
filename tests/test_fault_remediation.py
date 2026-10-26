"""
tests/test_fault_remediation.py
--------------------------------
Comprehensive test suite for Phase 5.3: Intelligent Fault Remediation
and Automated Infrastructure Recovery.

Covers all 25 required test cases:
1. CPU targeted recovery succeeds.
2. CPU targeted recovery fails and escalates appropriately.
3. Memory cleanup succeeds.
4. Memory cleanup fails and escalates appropriately.
5. Process recovery succeeds.
6. Process recovery failure escalates.
7. Disk cleanup succeeds.
8. Disk cleanup safety protections remain effective.
9. Container restart succeeds.
10. Container restart fails.
11. Missing container is handled without a false success.
12. Unsupported external network failure is not repeatedly restarted.
13. Health verification prevents false recovery.
14. Duplicate recovery attempts are suppressed.
15. Concurrent recovery attempts on the same server are prevented.
16. Different servers can recover independently.
17. Failed recovery is recorded correctly.
18. Recovery cooldown works.
19. Restart-loop prevention works.
20. Traffic isolation and recovery coordinate correctly.
21. Minimum eligible backend safeguard remains effective.
22. HAProxy failure does not crash the recovery engine.
23. Redis failure does not crash the recovery engine.
24. Automatic recovery can be disabled safely.
25. Existing Phase 1–5.2 functionality remains operational.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Ensure monitor/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))

from recovery import (
    RecoveryDecisionEngine,
    RecoveryStrategy,
    is_safe_disk_cleanup_path,
    select_recovery_strategy
)
from traffic_manager import TrafficManager


class FakeRedis:
    """Mock Redis client supporting string key-value, lists, and deletions."""
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)

    def rpush(self, key, value):
        if key not in self.data or not isinstance(self.data[key], list):
            self.data[key] = []
        self.data[key].append(value)

    def lpush(self, key, value):
        if key not in self.data or not isinstance(self.data[key], list):
            self.data[key] = []
        self.data[key].insert(0, value)

    def lrange(self, key, start, end):
        lst = self.data.get(key, [])
        if not isinstance(lst, list):
            return []
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    def ltrim(self, key, start, end):
        lst = self.data.get(key, [])
        if isinstance(lst, list):
            if end == -1:
                self.data[key] = lst[start:]
            else:
                self.data[key] = lst[start:end + 1]


class TestFaultRemediationPhase53(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.name = "test-app3"
        self.container_mock.status = "running"
        self.docker_mock.containers.list.return_value = [self.container_mock]

        self.mock_traffic_mgr = Mock()
        self.mock_traffic_mgr.isolate_server.return_value = {"action": "TRAFFIC_ISOLATED", "server": "app3"}
        self.mock_traffic_mgr.restore_server.return_value = {"action": "BACKEND_RESTORED", "server": "app3"}
        self.mock_traffic_mgr.get_server_status.return_value = {"state": "NORMAL", "weight": 100}

        self.engine = RecoveryDecisionEngine(
            docker_client=self.docker_mock,
            redis_client=self.redis,
            traffic_manager=self.mock_traffic_mgr,
            config={"cooldown_seconds": 0.0}
        )

    def tearDown(self):
        with self.engine._lock:
            self.engine._active_recoveries.clear()

    # 1. CPU targeted recovery succeeds
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_01_cpu_targeted_recovery_succeeds(self, mock_health, mock_post, mock_sleep):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"processes_stopped": 1})
        self.container_mock.stats.return_value = {
            "cpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000, "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 90}, "system_cpu_usage": 900}
        }
        res = self.engine.execute_recovery("app3", anomaly_type="cpu_spike", incident_id="inc-cpu-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "cpu_worker_terminated")
        self.assertFalse(res.get("escalated", False))

    # 2. CPU targeted recovery fails and escalates appropriately
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.recover_server", return_value=True)
    def test_02_cpu_targeted_recovery_fails_and_escalates(self, mock_restart, mock_post, mock_sleep):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"processes_stopped": 0})
        res = self.engine.execute_recovery("app3", anomaly_type="cpu_spike", incident_id="inc-cpu-2")
        self.assertTrue(res["success"])
        self.assertTrue(res.get("escalated", False))
        self.assertEqual(res["action"], "escalated_container_restart")

    # 3. Memory cleanup succeeds
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_03_memory_cleanup_succeeds(self, mock_health, mock_post, mock_sleep):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"message": "cleared"})
        self.container_mock.stats.return_value = {
            "memory_stats": {"usage": 40 * 1024 * 1024}
        }
        res = self.engine.execute_recovery("app2", anomaly_type="memory_leak", incident_id="inc-mem-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "memory_stress_cleared")
        self.assertEqual(res["memory_mb"], 40.0)

    # 4. Memory cleanup fails and escalates appropriately
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.recover_server", return_value=True)
    def test_04_memory_cleanup_fails_and_escalates(self, mock_restart, mock_post, mock_sleep):
        mock_post.return_value = Mock(status_code=500)
        res = self.engine.execute_recovery("app2", anomaly_type="memory_leak", incident_id="inc-mem-2")
        self.assertTrue(res["success"])
        self.assertTrue(res.get("escalated", False))
        self.assertEqual(res["action"], "escalated_container_restart")

    # 5. Process recovery succeeds
    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_05_process_recovery_succeeds(self, mock_health, mock_sleep):
        self.container_mock.exec_run.return_value = Mock(exit_code=0)
        res = self.engine.execute_recovery("app4", anomaly_type="process_failure", details={"pids_current": 3}, incident_id="inc-proc-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "gunicorn_worker_reloaded")

    # 6. Process recovery failure escalates
    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=False)
    @patch("recovery.recover_server", return_value=True)
    def test_06_process_recovery_failure_escalates(self, mock_restart, mock_health, mock_sleep):
        self.container_mock.exec_run.return_value = Mock(exit_code=0)
        res = self.engine.execute_recovery("app4", anomaly_type="process_failure", details={"pids_current": 3}, incident_id="inc-proc-2")
        self.assertTrue(res["success"])
        self.assertTrue(res.get("escalated", False))
        self.assertEqual(res["action"], "escalated_container_restart")

    # 7. Disk cleanup succeeds
    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_07_disk_cleanup_succeeds(self, mock_health, mock_sleep):
        find_mock = Mock(output=b"/tmp/test_disk_fill/disk_stress_demo.dat\n", exit_code=0)
        calc_mock = Mock(output=b"20480\n", exit_code=0)
        del_mock = Mock(output=b"", exit_code=0)
        verify_mock = Mock(output=b"0\n", exit_code=0)
        self.container_mock.exec_run.side_effect = [find_mock, calc_mock, del_mock, verify_mock]

        res = self.engine.execute_recovery("app5", anomaly_type="disk_exhaustion", incident_id="inc-disk-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "safe_temporary_file_cleanup")
        self.assertEqual(res["reclaimed_mb"], 20.0)

    # 8. Disk cleanup safety protections remain effective
    def test_08_disk_cleanup_safety_protections(self):
        self.assertFalse(is_safe_disk_cleanup_path("/etc/passwd"))
        self.assertFalse(is_safe_disk_cleanup_path("/app/app.py"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/../../etc/shadow"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/critical_system_file.so"))
        self.assertTrue(is_safe_disk_cleanup_path("/tmp/test_disk_fill/disk_stress_42.dat"))
        self.assertTrue(is_safe_disk_cleanup_path("/tmp/test_disk_fill/.metadata.json"))

    # 9. Container restart succeeds
    @patch("recovery.recover_server", return_value=True)
    def test_09_container_restart_succeeds(self, mock_restart):
        res = self.engine.execute_recovery("app1", anomaly_type="container_crash", incident_id="inc-rst-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "container_restart")

    # 10. Container restart fails
    @patch("recovery.recover_server", return_value=False)
    def test_10_container_restart_fails(self, mock_restart):
        res = self.engine.execute_recovery("app1", anomaly_type="container_crash", incident_id="inc-rst-2")
        self.assertFalse(res["success"])
        self.assertIn("failed after", res["reason"])

    # 11. Missing container is handled without a false success
    def test_11_missing_container_handled_without_false_success(self):
        self.docker_mock.containers.list.return_value = []
        res = self.engine.execute_recovery("ghost_app", anomaly_type="container_missing", incident_id="inc-ghost")
        self.assertFalse(res["success"])
        self.assertEqual(res["action"], "awaiting_operator")
        self.assertIn("No automated recovery available", res["reason"])

    # 12. Unsupported external network failure is not repeatedly restarted
    def test_12_unsupported_external_network_failure_not_restarted(self):
        res = self.engine.execute_recovery(
            "app1",
            anomaly_type="network_failure",
            details={"network_scope": "external_partition"},
            incident_id="inc-net-ext"
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["action"], "awaiting_operator")
        self.assertIn("External network partition", res["reason"])

    # 13. Health verification prevents false recovery
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.requests.get")
    def test_13_health_verification_prevents_false_recovery(self, mock_get, mock_post, mock_sleep):
        # Latency reset returns 200, but health verification returns 500
        mock_post.return_value = Mock(status_code=200)
        mock_get.return_value = Mock(status_code=500, json=lambda: {"status": "unhealthy"})

        res = self.engine.remediate_high_latency("app1", incident_id="inc-lat-fail")
        self.assertFalse(res["success"])
        self.assertTrue(res["escalate"])
        self.assertIn("verification failed", res["reason"])

    # 14. Duplicate recovery attempts are suppressed
    def test_14_duplicate_recovery_attempts_suppressed(self):
        with self.engine._lock:
            self.engine._active_recoveries.add("app2")
        res = self.engine.execute_recovery("app2", anomaly_type="cpu_spike")
        self.assertFalse(res["success"])
        self.assertIn("already in progress", res["reason"])

    # 15. Concurrent recovery attempts on the same server are prevented
    def test_15_concurrent_recovery_same_server_prevented(self):
        with self.engine._lock:
            self.engine._active_recoveries.add("app3")
        self.assertTrue(self.engine.is_recovery_in_progress("app3"))
        with self.engine._lock:
            self.engine._active_recoveries.discard("app3")
        self.assertFalse(self.engine.is_recovery_in_progress("app3"))

    # 16. Different servers can recover independently
    @patch("recovery.time.sleep")
    @patch("recovery.requests.post")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_16_different_servers_recover_independently(self, mock_health, mock_post, mock_sleep):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"processes_stopped": 1, "message": "cleared"})
        self.container_mock.stats.return_value = {"cpu_stats": {}, "precpu_stats": {}, "memory_stats": {"usage": 1024}}

        res1 = self.engine.execute_recovery("app1", anomaly_type="cpu_spike")
        res2 = self.engine.execute_recovery("app2", anomaly_type="memory_leak")
        self.assertTrue(res1["success"])
        self.assertTrue(res2["success"])

    # 17. Failed recovery is recorded correctly
    @patch("recovery.recover_server", return_value=False)
    def test_17_failed_recovery_recorded_correctly(self, mock_restart):
        self.redis.set("incident:app4", json.dumps({
            "incident_id": "inc-4-fail",
            "server": "app4",
            "status": "active"
        }))
        res = self.engine.execute_recovery("app4", anomaly_type="container_crash", incident_id="inc-4-fail")
        self.assertFalse(res["success"])
        persisted = json.loads(self.redis.get("incident:app4"))
        self.assertEqual(persisted["status"], "escalated")
        self.assertEqual(persisted["recovery_state"], "recovery_failed")
        self.assertEqual(persisted["recovery_result"], "failed")

    # 18. Recovery cooldown works
    def test_18_recovery_cooldown_works(self):
        engine_cd = RecoveryDecisionEngine(
            docker_client=self.docker_mock,
            redis_client=self.redis,
            config={"cooldown_seconds": 15.0}
        )
        engine_cd._last_recovery_time["app1"] = time.time()
        res = engine_cd.execute_recovery("app1", anomaly_type="cpu_spike")
        self.assertFalse(res["success"])
        self.assertEqual(res.get("status"), "cooldown")
        self.assertIn("in recovery cooldown", res["reason"])

    # 19. Restart-loop prevention works
    def test_19_restart_loop_prevention_works(self):
        now = time.time()
        # Preload 3 restarts in window
        self.engine._restart_history["app1"] = [now - 10, now - 20, now - 30]
        res = self.engine.remediate_container_restart("app1", incident_id="inc-loop")
        self.assertFalse(res["success"])
        self.assertEqual(res["action"], "restart_suppressed")
        self.assertIn("Restart loop protection triggered", res["reason"])

    # 20. Traffic isolation and recovery coordinate correctly
    @patch("recovery.recover_server", return_value=True)
    def test_20_traffic_isolation_and_recovery_coordinate(self, mock_restart):
        res = self.engine.execute_recovery("app3", anomaly_type="container_crash", incident_id="inc-coord")
        self.assertTrue(res["success"])
        self.mock_traffic_mgr.isolate_server.assert_called_once_with("app3", reason="Pre-recovery isolation for container_crash")
        self.mock_traffic_mgr.restore_server.assert_called_once_with("app3", force=False)

    # 21. Minimum eligible backend safeguard remains effective
    def test_21_minimum_eligible_backend_safeguard(self):
        hp_client_mock = Mock()
        hp_client_mock.get_backends_status.return_value = {
            "app1": {"status": "UP", "is_up": True},
            "app2": {"status": "MAINT", "is_up": False},
            "app3": {"status": "MAINT", "is_up": False},
            "app4": {"status": "MAINT", "is_up": False},
            "app5": {"status": "MAINT", "is_up": False}
        }
        tm = TrafficManager(target_services=["app1", "app2", "app3", "app4", "app5"], haproxy_client=hp_client_mock, redis_client=self.redis)
        # Attempt to isolate app1 (which is the last UP eligible backend)
        res = tm.isolate_server("app1", reason="Test fault")
        self.assertEqual(res["action"], "ACTION_REJECTED")
        self.assertIn("Minimum eligible backend safeguard", res["reason"])
        # HAProxy set_server_maintenance should NOT have been called
        hp_client_mock.set_server_maintenance.assert_not_called()

    # 22. HAProxy failure does not crash the recovery engine
    @patch("recovery.recover_server", return_value=True)
    def test_22_haproxy_failure_does_not_crash_recovery_engine(self, mock_restart):
        broken_tm = Mock()
        broken_tm.isolate_server.side_effect = RuntimeError("HAProxy socket connection refused")
        broken_tm.restore_server.side_effect = RuntimeError("HAProxy socket error")
        engine = RecoveryDecisionEngine(
            docker_client=self.docker_mock,
            redis_client=self.redis,
            traffic_manager=broken_tm
        )
        res = engine.execute_recovery("app1", anomaly_type="container_crash")
        self.assertTrue(res["success"])

    # 23. Redis failure does not crash the recovery engine
    @patch("recovery.recover_server", return_value=True)
    def test_23_redis_failure_does_not_crash_recovery_engine(self, mock_restart):
        broken_redis = Mock()
        broken_redis.get.side_effect = ConnectionError("Redis cluster unreachable")
        broken_redis.set.side_effect = ConnectionError("Redis write failed")
        broken_redis.lpush.side_effect = ConnectionError("Redis list push failed")

        engine = RecoveryDecisionEngine(
            docker_client=self.docker_mock,
            redis_client=broken_redis
        )
        res = engine.execute_recovery("app1", anomaly_type="container_crash")
        self.assertTrue(res["success"])

    # 24. Automatic recovery can be disabled safely
    def test_24_automatic_recovery_can_be_disabled_safely(self):
        self.engine.disable()
        self.assertFalse(self.engine.enabled)
        res = self.engine.execute_recovery("app1", anomaly_type="cpu_spike")
        self.assertFalse(res["success"])
        self.assertEqual(res.get("status"), "disabled")
        self.assertIn("Automatic recovery is disabled", res["reason"])

        # Manual execution with force=True bypasses the disabled gate
        with patch.object(self.engine, "remediate_cpu_spike", return_value={"success": True, "action": "forced"}):
            forced_res = self.engine.execute_recovery("app1", anomaly_type="cpu_spike", force=True)
            self.assertTrue(forced_res["success"])

    # 25. Existing Phase 1–5.2 functionality remains operational
    def test_25_existing_functionality_remains_operational(self):
        # Strategy selection backward compatibility
        self.assertEqual(select_recovery_strategy("cpu_spike"), RecoveryStrategy.CPU_WORKER_TERMINATION)
        self.assertEqual(select_recovery_strategy("memory_leak"), RecoveryStrategy.MEMORY_CLEANUP)
        self.assertEqual(select_recovery_strategy("process_failure"), RecoveryStrategy.PROCESS_RELOAD)
        self.assertEqual(select_recovery_strategy("disk_exhaustion"), RecoveryStrategy.DISK_CLEANUP)
        self.assertEqual(select_recovery_strategy("high_latency"), RecoveryStrategy.CONTAINER_RESTART)
        self.assertEqual(select_recovery_strategy("network_failure"), RecoveryStrategy.CONTAINER_RESTART)
        self.assertEqual(select_recovery_strategy("container_missing"), RecoveryStrategy.UNSUPPORTED)

        # Status and configuration APIs
        cfg = self.engine.get_config()
        self.assertIn("enabled", cfg)
        self.assertIn("cooldown_seconds", cfg)
        self.assertIn("max_restarts_per_window", cfg)

        status = self.engine.get_status()
        self.assertIn("servers", status)
        self.assertEqual(len(status["servers"]), 5)
        self.assertIn("app1", status["servers"])
        self.assertIn("app5", status["servers"])

    # 26. Last eligible backend targeted remediation proceeds non-disruptively
    def test_26_last_eligible_backend_targeted_remediation_proceeds_safely(self):
        # Isolation rejected by TrafficManager because app1 is the last eligible backend
        self.mock_traffic_mgr.isolate_server.return_value = {
            "success": False,
            "action": "ACTION_REJECTED",
            "reason": "Minimum eligible backend safeguard rejected pre-recovery isolation"
        }
        self.mock_traffic_mgr.count_healthy_eligible.return_value = 1

        with patch.object(self.engine, "remediate_cpu_spike", return_value={"success": True, "action": "cpu_worker_terminated"}):
            res = self.engine.execute_recovery("app1", anomaly_type="cpu_spike", incident_id="inc-last-1")
            self.assertTrue(res["success"])
            self.assertEqual(res["action"], "cpu_worker_terminated")

    # 27. Last eligible backend disruptive container restart suppressed
    def test_27_last_eligible_backend_disruptive_restart_suppressed(self):
        self.mock_traffic_mgr.isolate_server.return_value = {
            "success": False,
            "action": "ACTION_REJECTED",
            "reason": "Minimum eligible backend safeguard rejected pre-recovery isolation"
        }
        self.mock_traffic_mgr.count_healthy_eligible.return_value = 1

        # Anomaly requires container restart, but disruptive restart must be suppressed
        res = self.engine.execute_recovery("app1", anomaly_type="container_crash", incident_id="inc-last-2")
        self.assertFalse(res["success"])
        self.assertEqual(res["action"], "awaiting_operator")
        self.assertIn("Disruptive container restart suppressed", res["reason"])

    # 28. Last eligible backend escalation to container restart suppressed
    def test_28_last_eligible_backend_escalation_suppressed(self):
        self.mock_traffic_mgr.isolate_server.return_value = {
            "success": False,
            "action": "ACTION_REJECTED",
            "reason": "Minimum eligible backend safeguard rejected pre-recovery isolation"
        }
        self.mock_traffic_mgr.count_healthy_eligible.return_value = 1

        # Targeted remediation fails and requests escalation
        with patch.object(self.engine, "remediate_disk_exhaustion", return_value={"success": False, "escalate": True, "reason": "No files"}):
            res = self.engine.execute_recovery("app1", anomaly_type="disk_exhaustion", incident_id="inc-last-3")
            self.assertFalse(res["success"])
            self.assertEqual(res["action"], "awaiting_operator")
            self.assertIn("Escalated container restart suppressed", res["reason"])


if __name__ == "__main__":
    unittest.main()
