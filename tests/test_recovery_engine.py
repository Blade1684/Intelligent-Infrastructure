"""
tests/test_recovery_engine.py
-----------------------------
Unit tests for the Intelligent Recovery Decision Engine.
Tests strategy selection, CPU worker termination, memory cleanup,
process-level recovery (Gunicorn reload), fallback escalation,
incident closure/escalation, concurrency gating, and isolation.
"""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Ensure monitor/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = types.SimpleNamespace(RequestException=Exception)

from recovery import (
    RecoveryDecisionEngine,
    RecoveryStrategy,
    get_server_state,
    is_safe_disk_cleanup_path,
    isolate_server,
    recover_server,
    select_recovery_strategy
)


class FakeRedis:
    """Mock Redis client supporting strings, lists, and deletions for unit tests."""
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


# ============================================================
# 1. STRATEGY SELECTION TESTS
# ============================================================

class StrategySelectionTests(unittest.TestCase):
    def test_cpu_spike_selects_cpu_worker_termination(self):
        strat = select_recovery_strategy("cpu_spike")
        self.assertEqual(strat, RecoveryStrategy.CPU_WORKER_TERMINATION)

    def test_memory_leak_selects_memory_cleanup(self):
        strat = select_recovery_strategy("memory_leak")
        self.assertEqual(strat, RecoveryStrategy.MEMORY_CLEANUP)

    def test_process_failure_with_running_container_selects_process_reload(self):
        strat = select_recovery_strategy("process_failure", details={"pids_current": 3, "status_code": 500})
        self.assertEqual(strat, RecoveryStrategy.PROCESS_RELOAD)

    def test_process_failure_with_zero_pids_selects_container_restart(self):
        strat = select_recovery_strategy("process_failure", details={"pids_current": 0})
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)

    def test_container_crash_selects_container_restart(self):
        strat = select_recovery_strategy("container_crash")
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)

    def test_network_failure_selects_container_restart(self):
        strat = select_recovery_strategy("network_failure")
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)

    def test_high_latency_selects_container_restart(self):
        strat = select_recovery_strategy("high_latency")
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)

    def test_disk_exhaustion_selects_disk_cleanup(self):
        strat = select_recovery_strategy("disk_exhaustion")
        self.assertEqual(strat, RecoveryStrategy.DISK_CLEANUP)

    def test_disk_exhaustion_with_stopped_container_selects_container_restart(self):
        strat = select_recovery_strategy("disk_exhaustion", details={"container_status": "exited"})
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)

    def test_container_missing_selects_unsupported(self):
        strat = select_recovery_strategy("container_missing")
        self.assertEqual(strat, RecoveryStrategy.UNSUPPORTED)

    def test_unknown_anomaly_defaults_to_container_restart(self):
        strat = select_recovery_strategy("unknown_hardware_glitch")
        self.assertEqual(strat, RecoveryStrategy.CONTAINER_RESTART)


# ============================================================
# 2. CPU RECOVERY TESTS
# ============================================================

class CpuRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.name = "test-app1"
        self.container_mock.status = "running"
        self.docker_mock.containers.list.return_value = [self.container_mock]

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    @patch("recovery.requests.post")
    def test_cpu_worker_termination_success(self, mock_post, mock_health, mock_sleep):
        """CPU worker is terminated, CPU drops below 40%, and health passes."""
        resp_mock = Mock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"message": "CPU stress stopped", "processes_stopped": 1}
        mock_post.return_value = resp_mock

        # Stats return low CPU (e.g., 5.0%)
        self.container_mock.stats.return_value = {
            "cpu_stats": {"cpu_usage": {"total_usage": 1050}, "system_cpu_usage": 10000, "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 1000}, "system_cpu_usage": 9000}
        }

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="cpu_spike", incident_id="inc-101")

        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["action"], "cpu_worker_terminated")
        self.assertFalse(outcome.get("escalated", False))
        self.assertEqual(get_server_state("app1", self.redis)["state"], "healthy")

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=True)
    @patch("recovery.requests.post")
    def test_cpu_worker_termination_fails_and_triggers_container_restart(
        self, mock_post, mock_restart, mock_sleep
    ):
        """When no registered worker is stopped, escalate to container restart."""
        resp_mock = Mock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"message": "CPU stress stopped", "processes_stopped": 0}
        mock_post.return_value = resp_mock

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="cpu_spike", incident_id="inc-102")

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)


# ============================================================
# 3. MEMORY RECOVERY TESTS
# ============================================================

class MemoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.status = "running"
        self.docker_mock.containers.list.return_value = [self.container_mock]

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    @patch("recovery.requests.post")
    def test_memory_cleanup_success(self, mock_post, mock_health, mock_sleep):
        """Memory clear endpoint succeeds, memory drops below 70MB, health passes."""
        resp_mock = Mock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"message": "Allocated demonstration memory cleared"}
        mock_post.return_value = resp_mock

        # Stats return low memory (45 MB)
        self.container_mock.stats.return_value = {
            "memory_stats": {"usage": 45 * 1024 * 1024}
        }

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="memory_leak", incident_id="inc-201")

        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["action"], "memory_stress_cleared")
        self.assertFalse(outcome.get("escalated", False))
        self.assertEqual(get_server_state("app1", self.redis)["state"], "healthy")

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=True)
    @patch("recovery.requests.post", side_effect=Exception("Connection refused"))
    def test_memory_cleanup_fails_and_triggers_container_restart(
        self, mock_post, mock_restart, mock_sleep
    ):
        """When memory cleanup endpoint is unreachable, escalate to container restart."""
        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="memory_leak", incident_id="inc-202")

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)


# ============================================================
# 4. PROCESS RECOVERY TESTS
# ============================================================

class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.status = "running"
        self.container_mock.kill.side_effect = Exception("kill fallback error")
        self.docker_mock.containers.list.return_value = [self.container_mock]

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_process_failure_selects_process_recovery_and_succeeds(
        self, mock_health, mock_sleep
    ):
        """kill -HUP 1 reloads Gunicorn worker, health passes, incident resolves."""
        exec_mock = Mock()
        exec_mock.exit_code = 0
        self.container_mock.exec_run.return_value = exec_mock

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="process_failure",
            details={"pids_current": 3, "status_code": 500},
            incident_id="inc-301"
        )

        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["action"], "gunicorn_worker_reloaded")
        self.assertFalse(outcome.get("escalated", False))
        self.container_mock.exec_run.assert_called_with(["sh", "-c", "kill -HUP 1"])

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=True)
    def test_process_recovery_failure_triggers_container_restart(
        self, mock_restart, mock_sleep
    ):
        """When kill -HUP 1 fails (non-zero exit code), escalate to container restart."""
        exec_mock = Mock()
        exec_mock.exit_code = 1
        self.container_mock.exec_run.return_value = exec_mock

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="process_failure",
            details={"pids_current": 3, "status_code": 500},
            incident_id="inc-302"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=False)
    @patch("recovery.recover_server", return_value=True)
    def test_process_recovery_health_failure_triggers_container_restart(
        self, mock_restart, mock_health, mock_sleep
    ):
        """When kill -HUP 1 succeeds but health verification fails, escalate to container restart."""
        exec_mock = Mock()
        exec_mock.exit_code = 0
        self.container_mock.exec_run.return_value = exec_mock

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="process_failure",
            details={"pids_current": 3, "status_code": 500},
            incident_id="inc-303"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)


# ============================================================
# 5. INCIDENT CLOSURE AND ESCALATION TESTS
# ============================================================

class IncidentClosureAndEscalationTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.status = "running"
        self.docker_mock.containers.list.return_value = [self.container_mock]

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    @patch("recovery.requests.post")
    def test_successful_verification_closes_incident(self, mock_post, mock_health, mock_sleep):
        resp_mock = Mock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"message": "Allocated demonstration memory cleared"}
        mock_post.return_value = resp_mock
        self.container_mock.stats.return_value = {"memory_stats": {"usage": 40 * 1024 * 1024}}

        # Seed active incident
        active_inc = {
            "server": "app1",
            "incident_id": "inc-401",
            "status": "active",
            "anomaly_type": "memory_leak",
            "started_at": "2026-10-19T00:00:00+00:00"
        }
        self.redis.set("incident:app1", json.dumps(active_inc))

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="memory_leak", incident_id="inc-401")

        self.assertTrue(outcome["success"])
        # Active incident should be deleted
        self.assertIsNone(self.redis.get("incident:app1"))
        # Closed incident should be stored in closed_incidents:app1
        closed_list = self.redis.lrange("closed_incidents:app1", 0, -1)
        self.assertEqual(len(closed_list), 1)
        closed_inc = json.loads(closed_list[0])
        self.assertEqual(closed_inc["status"], "resolved")
        self.assertEqual(closed_inc["remediation_action"], "memory_stress_cleared")
        self.assertIn("mttr_seconds", closed_inc)

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=False)
    @patch("recovery.requests.post", side_effect=Exception("Failed"))
    def test_failed_verification_does_not_close_incident_and_escalates(
        self, mock_post, mock_restart, mock_sleep
    ):
        active_inc = {
            "server": "app1",
            "incident_id": "inc-402",
            "status": "active",
            "anomaly_type": "memory_leak",
            "started_at": "2026-10-19T00:00:00+00:00"
        }
        self.redis.set("incident:app1", json.dumps(active_inc))

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery("app1", anomaly_type="memory_leak", incident_id="inc-402")

        self.assertFalse(outcome["success"])
        # Active incident should NOT be deleted; status should be escalated
        persisted = json.loads(self.redis.get("incident:app1"))
        self.assertEqual(persisted["status"], "escalated")
        self.assertIn("failure_reason", persisted)
        # Server state should be failed
        self.assertEqual(get_server_state("app1", self.redis)["state"], "failed")


# ============================================================
# 5. DISK RECOVERY TESTS
# ============================================================

class DiskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.name = "test-app1"
        self.container_mock.status = "running"
        self.container_mock.kill.side_effect = Exception("kill fallback error")
        self.docker_mock.containers.list.return_value = [self.container_mock]

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    def test_disk_cleanup_success(self, mock_health, mock_sleep):
        """Eligible test files found, space calculated, safely deleted, and verified."""
        find_mock = Mock(output=b"/tmp/test_disk_fill/disk_stress_1.dat\n/tmp/test_disk_fill/.metadata.json\n", exit_code=0)
        calc_mock = Mock(output=b"51200\n", exit_code=0)
        del_mock = Mock(output=b"", exit_code=0)
        verify_mock = Mock(output=b"0\n", exit_code=0)

        self.container_mock.exec_run.side_effect = [find_mock, calc_mock, del_mock, verify_mock]

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="disk_exhaustion",
            details={"current_disk_percent": 92.0, "threshold_percent": 85.0},
            incident_id="inc-disk-001"
        )

        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["action"], "safe_temporary_file_cleanup")
        self.assertEqual(outcome["reclaimed_mb"], 50.0)
        self.assertFalse(outcome.get("escalated", False))

        # Verify events in Redis
        events = [json.loads(e) for e in self.redis.lrange("events:app1", 0, -1)]
        event_types = [e.get("event_type") for e in events]
        self.assertIn("strategy_selected", event_types)
        self.assertIn("remediation_action_started", event_types)
        self.assertIn("remediation_action_completed", event_types)
        self.assertIn("recovery_verification_succeeded", event_types)
        self.assertIn("recovery_succeeded", event_types)

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=True)
    def test_disk_cleanup_no_eligible_files_escalates_to_container_restart(self, mock_restart, mock_sleep):
        """When no eligible temporary files exist, escalate to container restart."""
        find_mock = Mock(output=b"", exit_code=0)
        self.container_mock.exec_run.return_value = find_mock

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="disk_exhaustion",
            details={"current_disk_percent": 92.0},
            incident_id="inc-disk-002"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)

    @patch("recovery.time.sleep")
    @patch("recovery.recover_server", return_value=True)
    def test_disk_cleanup_deletion_failure_escalates_to_container_restart(self, mock_restart, mock_sleep):
        """When cleanup command returns non-zero exit code, escalate to container restart."""
        find_mock = Mock(output=b"/tmp/test_disk_fill/disk_stress_1.dat\n", exit_code=0)
        calc_mock = Mock(output=b"10240\n", exit_code=0)
        del_mock = Mock(output=b"Permission denied\n", exit_code=1)

        self.container_mock.exec_run.side_effect = [find_mock, calc_mock, del_mock]

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="disk_exhaustion",
            details={"current_disk_percent": 92.0},
            incident_id="inc-disk-003"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")
        self.assertTrue(mock_restart.called)

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=True)
    @patch("recovery.recover_server", return_value=True)
    def test_disk_cleanup_files_remaining_escalates_to_container_restart(self, mock_restart, mock_health, mock_sleep):
        """When files remain after deletion, verification fails and escalates to restart."""
        find_mock = Mock(output=b"/tmp/test_disk_fill/disk_stress_1.dat\n", exit_code=0)
        calc_mock = Mock(output=b"10240\n", exit_code=0)
        del_mock = Mock(output=b"", exit_code=0)
        verify_mock = Mock(output=b"2\n", exit_code=0)

        self.container_mock.exec_run.side_effect = [find_mock, calc_mock, del_mock, verify_mock]

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="disk_exhaustion",
            details={"current_disk_percent": 92.0},
            incident_id="inc-disk-004"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")

    @patch("recovery.time.sleep")
    @patch("recovery.application_is_healthy", return_value=False)
    @patch("recovery.recover_server", return_value=True)
    def test_disk_cleanup_health_failure_escalates_to_container_restart(self, mock_restart, mock_health, mock_sleep):
        """When application health check fails after cleanup, escalate to container restart."""
        find_mock = Mock(output=b"/tmp/test_disk_fill/disk_stress_1.dat\n", exit_code=0)
        calc_mock = Mock(output=b"10240\n", exit_code=0)
        del_mock = Mock(output=b"", exit_code=0)
        verify_mock = Mock(output=b"0\n", exit_code=0)

        self.container_mock.exec_run.side_effect = [find_mock, calc_mock, del_mock, verify_mock]

        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        outcome = engine.execute_recovery(
            "app1",
            anomaly_type="disk_exhaustion",
            details={"current_disk_percent": 92.0},
            incident_id="inc-disk-005"
        )

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome.get("escalated"))
        self.assertEqual(outcome["action"], "escalated_container_restart")

    def test_path_traversal_prevention(self):
        """Confirm that directory escape attempts (../) are rejected."""
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/../etc/passwd"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/../../app/app.py"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/../var/log/syslog"))
        self.assertTrue(is_safe_disk_cleanup_path("/tmp/test_disk_fill/disk_stress_123.dat"))
        self.assertTrue(is_safe_disk_cleanup_path("/tmp/test_disk_fill/.metadata.json"))

    def test_protection_of_non_approved_directories(self):
        """Confirm that files in non-approved directories are strictly rejected."""
        self.assertFalse(is_safe_disk_cleanup_path("/app/disk_stress_foo.dat"))
        self.assertFalse(is_safe_disk_cleanup_path("/etc/disk_stress_foo.dat"))
        self.assertFalse(is_safe_disk_cleanup_path("/var/lib/docker/disk_stress.dat"))
        self.assertFalse(is_safe_disk_cleanup_path("/root/disk_stress.dat"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/disk_stress_outside.dat"))
        self.assertFalse(is_safe_disk_cleanup_path("/tmp/test_disk_fill/arbitrary_user_file.txt"))


# ============================================================
# 6. CONCURRENCY AND ISOLATION TESTS
# ============================================================

class ConcurrencyAndIsolationTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.docker_mock = Mock()
        self.container_mock = Mock()
        self.container_mock.status = "running"
        self.docker_mock.containers.list.return_value = [self.container_mock]

    def test_duplicate_concurrent_recovery_attempts_are_prevented(self):
        engine = RecoveryDecisionEngine(docker_client=self.docker_mock, redis_client=self.redis)
        # Simulate active recovery on app1
        with engine._lock:
            engine._active_recoveries.add("app1")

        try:
            outcome = engine.execute_recovery("app1", anomaly_type="cpu_spike")
            self.assertFalse(outcome["success"])
            self.assertIn("already in progress", outcome["reason"])
        finally:
            with engine._lock:
                engine._active_recoveries.discard("app1")

    def test_isolate_server_without_stop_preserves_container_running(self):
        isolate_server("app1", "cpu_spike", self.docker_mock, self.redis, stop_container=False)
        self.assertEqual(get_server_state("app1", self.redis)["state"], "isolated")
        self.assertFalse(self.container_mock.stop.called)

    def test_isolate_server_with_stop_calls_container_stop(self):
        isolate_server("app1", "container_crash", self.docker_mock, self.redis, stop_container=True)
        self.assertEqual(get_server_state("app1", self.redis)["state"], "isolated")
        self.assertTrue(self.container_mock.stop.called)


if __name__ == "__main__":
    unittest.main()
