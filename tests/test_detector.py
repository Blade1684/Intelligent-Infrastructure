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

from detector import detect_service_failure
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

    def test_running_but_unreachable_service_is_a_network_incident(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": False,
            "health_error": "Connection refused"
        })
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["type"], "network_failure")

    def test_healthy_service_is_not_an_anomaly(self):
        result = detect_service_failure({
            "status": "running",
            "service_available": True
        })
        self.assertFalse(result["anomaly"])


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


if __name__ == "__main__":
    unittest.main()
