"""
tests/test_app.py
-----------------
Unit tests for the latency injection feature in app/app.py.

These tests run inside the monitor container (where pytest is installed)
by inserting the app/ directory onto sys.path and stubbing out the two
dependencies that would otherwise require live connections at import time:
  - redis        (Redis client constructed at module level in app.py)
  - flask_session (Session(app) called at module level)

No real network connections are made; all I/O is exercised through
Flask's built-in test client.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ----------------------------------------------------------------
# Make app/ importable from inside the monitor container.
# When tests are run from the project root the path is one level up.
# ----------------------------------------------------------------
_APP_DIR = str(Path(__file__).resolve().parents[1] / "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)


# ----------------------------------------------------------------
# Stub redis and flask_session before app.py is imported.
#
# redis.Redis() is called at module level in app.py to build the
# session store.  The stub returns a MagicMock whose .ping() method
# returns True so /health passes its Redis connectivity check.
#
# flask_session.Session is called with the Flask app object at module
# level; the stub turns that into a no-op.
# ----------------------------------------------------------------
_redis_instance = MagicMock()
_redis_instance.ping.return_value = True

_redis_stub = MagicMock()
_redis_stub.Redis.return_value = _redis_instance
_redis_stub.RedisError = Exception          # must be a real exception class

_flask_session_stub = MagicMock()

with patch.dict(
    "sys.modules",
    {
        "redis": _redis_stub,
        "flask_session": _flask_session_stub,
    },
):
    import app as _app_module   # noqa: E402  (import not at top intentional)

_flask_app = _app_module.app
_flask_app.config["TESTING"] = True


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _reset_injection():
    """Force injection state back to the clean/inactive baseline."""
    with _app_module._latency_lock:
        _app_module._latency_injection["delay_ms"] = 0.0
        _app_module._latency_injection["expires_at"] = 0.0
    with _app_module._process_failure_lock:
        _app_module._process_failure_injection["active"] = False
        _app_module._process_failure_injection["expires_at"] = 0.0
        _app_module._process_failure_injection["reason"] = ""
    if hasattr(_app_module, "reset_disk"):
        try:
            with _flask_app.test_request_context():
                _app_module.reset_disk()
        except Exception:
            pass


# ================================================================
# Test classes
# ================================================================

class HealthEndpointWithoutInjectionTests(unittest.TestCase):
    """Health endpoint behaves normally when no injection is active."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_health_returns_200_and_healthy_status(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "healthy")

    def test_health_response_includes_server_field(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIn("server", data)

    def test_health_responds_quickly_without_injection(self):
        start = time.perf_counter()
        self.client.get("/health")
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed_ms, 200)


class LatencyInjectionActivationTests(unittest.TestCase):
    """POST /stress/latency correctly sets the delay state."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_valid_delay_returns_200_and_confirms_settings(self):
        resp = self.client.post(
            "/stress/latency",
            json={"delay_ms": 800, "duration_s": 10}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["delay_ms"], 800.0)
        self.assertEqual(data["duration_s"], 10)
        self.assertIn("message", data)
        self.assertIn("server", data)

    def test_zero_ms_delay_is_accepted(self):
        resp = self.client.post("/stress/latency", json={"delay_ms": 0})
        self.assertEqual(resp.status_code, 200)

    def test_maximum_boundary_delay_is_accepted(self):
        resp = self.client.post(
            "/stress/latency",
            json={"delay_ms": 5000, "duration_s": 5}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["delay_ms"], 5000.0)

    def test_delay_via_query_string_is_accepted(self):
        resp = self.client.post("/stress/latency?delay_ms=600&duration_s=5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["delay_ms"], 600.0)


class LatencyInjectionValidationTests(unittest.TestCase):
    """Invalid inputs are rejected with HTTP 400."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_missing_delay_ms_is_rejected(self):
        resp = self.client.post("/stress/latency", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_negative_delay_is_rejected(self):
        resp = self.client.post("/stress/latency", json={"delay_ms": -50})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(">=", resp.get_json()["error"])

    def test_delay_above_maximum_is_rejected(self):
        resp = self.client.post("/stress/latency", json={"delay_ms": 5001})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("<=", resp.get_json()["error"])

    def test_non_numeric_string_delay_is_rejected(self):
        resp = self.client.post("/stress/latency", json={"delay_ms": "fast"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())


class HealthEndpointWithInjectionTests(unittest.TestCase):
    """Health endpoint applies the configured delay when injection is active."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_health_delays_by_approximately_configured_amount(self):
        self.client.post(
            "/stress/latency",
            json={"delay_ms": 150, "duration_s": 10}
        )
        start = time.perf_counter()
        resp = self.client.get("/health")
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(elapsed_ms, 140)   # 10 ms tolerance

    def test_health_response_format_is_preserved_with_injection(self):
        self.client.post("/stress/latency", json={"delay_ms": 50, "duration_s": 5})
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["status"], "healthy")
        self.assertIn("server", data)


class LatencyResetTests(unittest.TestCase):
    """POST /stress/latency/reset immediately clears injected delay."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_reset_returns_200_with_message_and_server(self):
        resp = self.client.post("/stress/latency/reset")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("message", data)
        self.assertIn("server", data)

    def test_reset_clears_active_injection_state(self):
        self.client.post("/stress/latency", json={"delay_ms": 800, "duration_s": 60})
        self.client.post("/stress/latency/reset")
        self.assertEqual(_app_module.get_active_latency_ms(), 0.0)

    def test_health_responds_quickly_after_reset(self):
        self.client.post("/stress/latency", json={"delay_ms": 800, "duration_s": 30})
        self.client.post("/stress/latency/reset")

        start = time.perf_counter()
        self.client.get("/health")
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertLess(elapsed_ms, 200)

    def test_reset_on_already_inactive_injection_is_safe(self):
        resp = self.client.post("/stress/latency/reset")
        self.assertEqual(resp.status_code, 200)


class AutomaticExpiryTests(unittest.TestCase):
    """Injected delay expires automatically after the configured window."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_injection_is_active_immediately_after_activation(self):
        self.client.post("/stress/latency", json={"delay_ms": 300, "duration_s": 5})
        self.assertGreater(_app_module.get_active_latency_ms(), 0)

    def test_injection_expires_after_duration_elapses(self):
        self.client.post("/stress/latency", json={"delay_ms": 300, "duration_s": 1})
        self.assertGreater(_app_module.get_active_latency_ms(), 0)

        time.sleep(1.2)

        self.assertEqual(_app_module.get_active_latency_ms(), 0.0)

    def test_health_responds_quickly_after_expiry(self):
        self.client.post("/stress/latency", json={"delay_ms": 300, "duration_s": 1})
        time.sleep(1.2)

        start = time.perf_counter()
        resp = self.client.get("/health")
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertEqual(resp.status_code, 200)
        self.assertLess(elapsed_ms, 200)


class ProcessFailureInjectionTests(unittest.TestCase):
    """Controlled worker failure simulation produces HTTP 500 and can be reset."""

    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        _reset_injection()

    def test_process_crash_returns_200_and_reports_pids(self):
        resp = self.client.post("/stress/process/crash", json={"duration_s": 30})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("message", data)
        self.assertIn("worker_pid", data)
        self.assertIn("master_pid", data)

    def test_health_returns_500_with_worker_failure_active(self):
        self.client.post("/stress/process/crash", json={"duration_s": 30})
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 500)
        data = resp.get_json()
        self.assertEqual(data["status"], "unhealthy")
        self.assertIn("crashed unexpectedly", data["error"])

    def test_process_reset_restores_health(self):
        self.client.post("/stress/process/crash", json={"duration_s": 30})
        resp_crash = self.client.get("/health")
        self.assertEqual(resp_crash.status_code, 500)

        resp_reset = self.client.post("/stress/process/reset")
        self.assertEqual(resp_reset.status_code, 200)

        resp_health = self.client.get("/health")
        self.assertEqual(resp_health.status_code, 200)
        self.assertEqual(resp_health.get_json()["status"], "healthy")

    def test_process_crash_auto_expires(self):
        self.client.post("/stress/process/crash", json={"duration_s": 1})
        self.assertTrue(_app_module.is_worker_failure_active())
        time.sleep(1.2)
        self.assertFalse(_app_module.is_worker_failure_active())
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)


class DiskStressInjectionTests(unittest.TestCase):
    def setUp(self):
        self.client = _flask_app.test_client()
        _reset_injection()

    def tearDown(self):
        self.client.post("/stress/disk/reset")

    def test_disk_fill_creates_file_in_dedicated_directory(self):
        resp = self.client.post("/stress/disk/fill", json={"size_mb": 2, "duration_s": 30})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["size_mb"], 2)
        self.assertIn("disk_stress_", data["file"])

        resp_status = self.client.get("/stress/disk/status")
        self.assertEqual(resp_status.status_code, 200)
        status_data = resp_status.get_json()
        self.assertTrue(status_data["active"])
        self.assertGreaterEqual(status_data["total_mb"], 2.0)

    def test_disk_fill_enforces_bounds(self):
        resp = self.client.post("/stress/disk/fill", json={"size_mb": 500})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("between", resp.get_json()["error"])

        resp_neg = self.client.post("/stress/disk/fill", json={"size_mb": 0})
        self.assertEqual(resp_neg.status_code, 400)

    def test_disk_reset_clears_test_artifacts(self):
        self.client.post("/stress/disk/fill", json={"size_mb": 2})
        resp_reset = self.client.post("/stress/disk/reset")
        self.assertEqual(resp_reset.status_code, 200)
        reset_data = resp_reset.get_json()
        self.assertGreaterEqual(reset_data["reclaimed_mb"], 2.0)

        resp_status = self.client.get("/stress/disk/status")
        status_data = resp_status.get_json()
        self.assertFalse(status_data["active"])
        self.assertEqual(status_data["total_mb"], 0.0)


if __name__ == "__main__":
    unittest.main()
