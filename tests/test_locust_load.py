import unittest
from unittest.mock import MagicMock, patch
import json
import sys
from pathlib import Path

# Add paths to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import app as flask_app
from load_test_client import (
    LoadTestManager,
    PROFILES,
    MAX_USERS,
    MAX_DURATION_SECONDS,
    MAX_SPAWN_RATE
)
MONITOR_MODULE = "monitor"


class LocustConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_root = Path(__file__).resolve().parents[1]
        compose_path = project_root / "docker-compose.yml"
        with open(compose_path, "r", encoding="utf-8") as f:
            cls.compose_text = f.read()

        locustfile_path = project_root / "locust" / "locustfile.py"
        with open(locustfile_path, "r", encoding="utf-8") as f:
            cls.locustfile_text = f.read()

        locustconf_path = project_root / "locust" / "locust.conf"
        with open(locustconf_path, "r", encoding="utf-8") as f:
            cls.locustconf_text = f.read()

    def test_locust_service_in_docker_compose(self):
        """Verify Locust service exists in docker-compose.yml with proper ports, pinned version and network."""
        self.assertIn("locust:", self.compose_text)
        self.assertIn("locustio/locust:2.47.0", self.compose_text)
        self.assertIn("8089:8089", self.compose_text)
        self.assertIn("infrastructure-network", self.compose_text)
        self.assertIn("http://haproxy:80", self.locustconf_text)

    def test_locustfile_endpoints_covered(self):
        """Verify locustfile covers realistic endpoints and not just /health."""
        self.assertIn('GET /', self.locustfile_text)
        self.assertIn('GET /health', self.locustfile_text)
        self.assertIn('POST /login', self.locustfile_text)
        self.assertIn('GET /session', self.locustfile_text)
        self.assertIn('POST /work', self.locustfile_text)

    def test_locustfile_tracks_backend_distribution(self):
        """Verify locustfile records backend responses for distribution tracking."""
        self.assertIn("record_backend_hit", self.locustfile_text)
        self.assertIn("/stats/distribution", self.locustfile_text)


class LoadTestManagerValidationTests(unittest.TestCase):
    def setUp(self):
        self.manager = LoadTestManager(locust_url="http://mock-locust:8089", redis_client=None)

    def test_default_profiles_exist(self):
        """Verify normal, moderate, and heavy profiles exist with valid configurations."""
        self.assertIn("normal", PROFILES)
        self.assertIn("moderate", PROFILES)
        self.assertIn("heavy", PROFILES)

        for name, profile in PROFILES.items():
            self.assertGreater(profile["user_count"], 0)
            self.assertLessEqual(profile["user_count"], MAX_USERS)
            self.assertGreater(profile["duration_seconds"], 0)
            self.assertLessEqual(profile["duration_seconds"], MAX_DURATION_SECONDS)

    def test_validate_parameters_profile(self):
        """Verify valid profile returns appropriate user_count, spawn_rate and duration."""
        cfg = self.manager.validate_parameters({"profile": "normal"})
        self.assertEqual(cfg["user_count"], PROFILES["normal"]["user_count"])
        self.assertEqual(cfg["spawn_rate"], PROFILES["normal"]["spawn_rate"])
        self.assertEqual(cfg["duration_seconds"], PROFILES["normal"]["duration_seconds"])

    def test_validate_parameters_custom_valid(self):
        """Verify custom valid parameters are accepted."""
        cfg = self.manager.validate_parameters({
            "profile": "custom",
            "user_count": 25,
            "spawn_rate": 5,
            "duration_seconds": 45
        })
        self.assertEqual(cfg["user_count"], 25)
        self.assertEqual(cfg["spawn_rate"], 5)
        self.assertEqual(cfg["duration_seconds"], 45)

    def test_validate_parameters_rejects_exceeded_users(self):
        """Verify virtual users exceeding safety limit are rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.validate_parameters({"user_count": MAX_USERS + 1})
        self.assertIn("user_count must be between", str(ctx.exception))

    def test_validate_parameters_rejects_zero_or_negative_users(self):
        """Verify zero or negative virtual users are rejected."""
        with self.assertRaises(ValueError):
            self.manager.validate_parameters({"user_count": 0})
        with self.assertRaises(ValueError):
            self.manager.validate_parameters({"user_count": -5})

    def test_validate_parameters_rejects_exceeded_duration(self):
        """Verify test duration exceeding safety limit is rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.validate_parameters({"duration_seconds": MAX_DURATION_SECONDS + 1})
        self.assertIn("duration_seconds must be between", str(ctx.exception))

    def test_validate_parameters_rejects_exceeded_spawn_rate(self):
        """Verify spawn rate exceeding safety limit is rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.validate_parameters({"spawn_rate": MAX_SPAWN_RATE + 1})
        self.assertIn("spawn_rate must be between", str(ctx.exception))


class LoadTestManagerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.manager = LoadTestManager(locust_url="http://mock-locust:8089", redis_client=None)

    @patch("load_test_client.requests.post")
    @patch("load_test_client.requests.get")
    def test_start_and_stop_lifecycle(self, mock_get, mock_post):
        """Verify starting transitions state to running and stop transitions to completed."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.text = '{"success": true}'
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"stats": [], "state": "running"}
        mock_get.return_value = mock_get_resp

        # Start
        result = self.manager.start_test({"profile": "normal"})
        self.assertEqual(result["status"], "running")
        self.assertIsNotNone(self.manager.active_session)

        # Concurrency check: starting again while running must raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            self.manager.start_test({"profile": "moderate"})
        self.assertIn("already in progress", str(ctx.exception))

        # Stop
        stop_result = self.manager.stop_test()
        self.assertEqual(stop_result["status"], "stopped")

    @patch("load_test_client.requests.post")
    @patch("load_test_client.requests.get")
    def test_locust_api_failure_handled_gracefully(self, mock_get, mock_post):
        """Verify network/API errors talking to Locust are caught gracefully."""
        mock_post.side_effect = Exception("Connection refused")

        with self.assertRaises(RuntimeError) as ctx:
            self.manager.start_test({"profile": "normal"})
        self.assertIn("Failed to communicate with Locust", str(ctx.exception))

    @patch("load_test_client.requests.get")
    def test_metrics_normalization(self, mock_get):
        """Verify metrics from Locust and distribution endpoint are formatted correctly."""
        mock_locust_stats = {
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 250,
                    "num_failures": 5,
                    "current_rps": 25.5,
                    "avg_response_time": 42.1,
                    "min_response_time": 10.0,
                    "max_response_time": 185.0,
                    "median_response_time": 35.0,
                    "ninetieth_response_time": 75.0
                }
            ],
            "total_rps": 25.5,
            "user_count": 10,
            "state": "running"
        }

        mock_distribution = {
            "distribution": {
                "Server-1": 50,
                "Server-2": 50,
                "Server-3": 50,
                "Server-4": 50,
                "Server-5": 50
            },
            "percentages": {
                "Server-1": 20.0,
                "Server-2": 20.0,
                "Server-3": 20.0,
                "Server-4": 20.0,
                "Server-5": 20.0
            },
            "total": 250
        }

        def mock_get_side_effect(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/stats/distribution" in url:
                resp.json.return_value = mock_distribution
            else:
                resp.json.return_value = mock_locust_stats
            return resp

        mock_get.side_effect = mock_get_side_effect
        
        metrics = self.manager.get_live_metrics_unlocked()
        
        traffic = metrics["traffic"]
        self.assertEqual(traffic["total_requests"], 250)
        self.assertEqual(traffic["successful_requests"], 245)
        self.assertEqual(traffic["failed_requests"], 5)
        self.assertEqual(traffic["rps"], 25.5)
        self.assertEqual(traffic["active_users"], 10)

        errors = metrics["errors"]
        self.assertEqual(errors["http_error_count"], 5)
        self.assertEqual(errors["http_error_rate_pct"], 2.0)

        backend_dist = metrics["backend_distribution"]
        self.assertEqual(backend_dist["counts"]["Server-1"], 50)
        self.assertEqual(backend_dist["percentages"]["Server-1"], 20.0)


class MonitorLoadTestApiTests(unittest.TestCase):
    def setUp(self):
        self.app = flask_app.test_client()

    def test_get_profiles_endpoint(self):
        """GET /load-test/profiles returns profile details."""
        resp = self.app.get("/load-test/profiles")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("normal", data)
        self.assertIn("moderate", data)
        self.assertIn("heavy", data)

    def test_get_status_endpoint(self):
        """GET /load-test/status returns status."""
        resp = self.app.get("/load-test/status")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("status", data)

    def test_post_start_validation_error(self):
        """POST /load-test/start rejects invalid parameters with 400."""
        resp = self.app.post(
            "/load-test/start",
            data=json.dumps({"user_count": 9999}),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_metrics_retrieval(self):
        """GET /load-test/metrics returns metric dictionary structure."""
        resp = self.app.get("/load-test/metrics")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("traffic", data)
        self.assertIn("response_times_ms", data)
        self.assertIn("errors", data)
        self.assertIn("backend_distribution", data)


if __name__ == "__main__":
    unittest.main()
