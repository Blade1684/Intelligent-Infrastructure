import sys
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

# Add paths to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from overload_detector import (
        OverloadState,
        RollingBaseline,
        TrendPredictor,
        ServerOverloadDetector,
        TrafficOverloadEngine,
        DEFAULT_CONFIG
    )
except ImportError:
    from monitor.overload_detector import (
        OverloadState,
        RollingBaseline,
        TrendPredictor,
        ServerOverloadDetector,
        TrafficOverloadEngine,
        DEFAULT_CONFIG
    )


class TestRollingBaseline:
    """Tests 13 & 14: Baseline learning, warm-up, stability, and anti-corruption."""

    def test_insufficient_baseline_history_reports_warming_up(self):
        baseline = RollingBaseline(min_samples=10)
        # Add only 3 clean samples
        for _ in range(3):
            baseline.add_sample(cpu=12.0, memory_mb=45.0, latency_ms=15.0)

        assert not baseline.is_trained()
        stats = baseline.get_stats()
        assert stats["status"] == "warming_up"
        assert stats["sample_count"] == 3
        assert stats["cpu_mean"] is None

    def test_baseline_learning_when_sufficient_samples_provided(self):
        baseline = RollingBaseline(min_samples=5)
        for i in range(6):
            baseline.add_sample(cpu=15.0 + i, memory_mb=40.0 + i, latency_ms=20.0 + i)

        assert baseline.is_trained()
        stats = baseline.get_stats()
        assert stats["status"] == "ready"
        assert stats["sample_count"] == 6
        assert 15.0 <= stats["cpu_mean"] <= 21.0
        assert stats["cpu_std"] > 0

    def test_baseline_stability_rejects_contaminated_samples(self):
        baseline = RollingBaseline(min_samples=5)
        # Add 5 normal clean samples
        for _ in range(5):
            baseline.add_sample(cpu=10.0, memory_mb=45.0, latency_ms=12.0)

        # Attempt to add contaminated samples (e.g. during fault injection or overload)
        res_cpu = baseline.add_sample(cpu=88.0, memory_mb=45.0, latency_ms=12.0)
        res_mem = baseline.add_sample(cpu=10.0, memory_mb=180.0, latency_ms=12.0)
        res_lat = baseline.add_sample(cpu=10.0, memory_mb=45.0, latency_ms=500.0)
        res_err = baseline.add_sample(cpu=10.0, memory_mb=45.0, latency_ms=12.0, error_rate_pct=10.0)

        assert res_cpu is False
        assert res_mem is False
        assert res_lat is False
        assert res_err is False

        # Verify baseline sample count remained 5 and wasn't polluted
        assert len(baseline.samples) == 5
        stats = baseline.get_stats()
        assert stats["cpu_mean"] == 10.0


class TestTrendPrediction:
    """Tests 15 & 16: Early-warning prediction with sufficient and insufficient data."""

    def test_prediction_with_insufficient_data(self):
        # Only 3 samples provided
        now = time.time()
        time_series = [(now - 20, 20.0), (now - 10, 25.0), (now, 30.0)]
        res = TrendPredictor.estimate_trend(time_series, min_samples=6)

        assert res["prediction_status"] == "insufficient_data"
        assert res["confidence"] == "insufficient_data"

        time_to_t, conf = TrendPredictor.predict_time_to_threshold(res, threshold=65.0)
        assert time_to_t is None
        assert conf == "insufficient_data"

    def test_prediction_with_sufficient_data_and_clear_trend(self):
        # 8 samples with consistent growth rate of 2.0% per second
        now = time.time()
        time_series = [(now + i * 2, 20.0 + i * 4.0) for i in range(8)]
        res = TrendPredictor.estimate_trend(time_series, min_samples=6)

        assert res["prediction_status"] == "ready"
        assert res["trend"] == "increasing"
        assert res["slope_per_second"] > 0
        assert res["r_squared"] >= 0.9

        # Predict time to reach 65.0 from last value (48.0)
        # Expected remaining is 17.0, with slope approx 2.0/s -> ~8.5 seconds
        time_to_t, conf = TrendPredictor.predict_time_to_threshold(res, threshold=65.0)
        assert time_to_t is not None
        assert 5.0 <= time_to_t <= 15.0
        assert conf == "high"


class TestServerOverloadClassification:
    """Covers specific overload states, correlation rules, and debounce stability."""

    def test_1_normal_traffic_with_healthy_resources(self):
        detector = ServerOverloadDetector("app1")
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 8.5, "memory_mb": 42.0, "latency_ms": 12.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 2.0, "active_users": 0, "backend_rps": 0.4, "backend_pct": 20.0,
            "p95_ms": 15.0, "error_rate_pct": 0.0
        }
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.NORMAL
        assert "nominal" in eval_res["reason"].lower()

    def test_2_high_traffic_with_healthy_resources_is_not_overload(self):
        detector = ServerOverloadDetector("app1")
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 28.0, "memory_mb": 50.0, "latency_ms": 18.0, "pids_current": 3
        }
        # High RPS (45 RPS, 20 users) handled cleanly
        traffic = {
            "global_rps": 45.0, "active_users": 20, "backend_rps": 9.0, "backend_pct": 20.0,
            "p95_ms": 25.0, "p99_ms": 40.0, "error_rate_pct": 0.0
        }
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.HIGH_TRAFFIC_HEALTHY
        assert "successfully" in eval_res["reason"].lower()

    def test_3_and_10_brief_cpu_spike_does_not_trigger_confirmed_overload(self):
        detector = ServerOverloadDetector("app1")
        # 1-cycle CPU spike
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 75.0, "memory_mb": 45.0, "latency_ms": 250.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 5.0, "active_users": 1, "backend_rps": 1.0, "backend_pct": 20.0,
            "p95_ms": 250.0, "error_rate_pct": 0.0
        }
        # First cycle: debounced (not confirmed overload yet)
        eval_1 = detector.evaluate(metric, traffic)
        assert eval_1["state"] != OverloadState.OVERLOAD

    def test_4_sustained_high_cpu_with_increasing_latency_triggers_overload(self):
        detector = ServerOverloadDetector("app1")
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 72.0, "memory_mb": 55.0, "latency_ms": 350.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 15.0, "active_users": 10, "backend_rps": 3.0, "backend_pct": 20.0,
            "p95_ms": 360.0, "error_rate_pct": 0.5
        }
        # Cycle 1: initial detection
        detector.evaluate(metric, traffic)
        # Cycle 2: confirmed sustained overload
        eval_2 = detector.evaluate(metric, traffic)
        assert eval_2["state"] == OverloadState.OVERLOAD
        assert "overload confirmed" in eval_2["reason"].lower()

    def test_5_gradually_increasing_memory_consumption(self):
        detector = ServerOverloadDetector("app1")
        # Memory at warning level (110MB)
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 15.0, "memory_mb": 115.0, "latency_ms": 20.0, "pids_current": 3
        }
        traffic = {"global_rps": 2.0, "backend_rps": 0.4, "backend_pct": 20.0, "p95_ms": 20.0}
        detector.evaluate(metric, traffic)
        eval_2 = detector.evaluate(metric, traffic)
        assert eval_2["state"] == OverloadState.EARLY_WARNING
        assert "memory" in eval_2["reason"].lower()

    def test_6_high_p95_and_p99_latency_triggers_warning_or_overload(self):
        detector = ServerOverloadDetector("app1")
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 30.0, "memory_mb": 50.0, "latency_ms": 220.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 20.0, "backend_rps": 4.0, "backend_pct": 20.0,
            "p95_ms": 240.0, "p99_ms": 450.0, "error_rate_pct": 0.0
        }
        detector.evaluate(metric, traffic)
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.EARLY_WARNING
        assert "latency" in eval_res["reason"].lower()

    def test_7_increasing_http_error_rate(self):
        detector = ServerOverloadDetector("app1")
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 25.0, "memory_mb": 45.0, "latency_ms": 40.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 25.0, "backend_rps": 5.0, "backend_pct": 20.0,
            "p95_ms": 50.0, "error_rate_pct": 3.5  # between 2% and 5% -> early warning
        }
        detector.evaluate(metric, traffic)
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.EARLY_WARNING
        assert "error" in eval_res["reason"].lower()

    def test_8_uneven_backend_request_distribution(self):
        detector = ServerOverloadDetector("app3")
        # Server 3 getting 60% of total traffic with elevated CPU
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 48.0, "memory_mb": 50.0, "latency_ms": 120.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 30.0, "backend_rps": 18.0, "backend_pct": 60.0,
            "p95_ms": 130.0, "error_rate_pct": 0.0
        }
        detector.evaluate(metric, traffic)
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.EARLY_WARNING
        assert "disproportionate" in eval_res["reason"].lower()

    def test_9_sudden_traffic_spike_handling(self):
        detector = ServerOverloadDetector("app1")
        # Traffic suddenly spikes from 0 to 60 RPS, but resources are completely normal
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 18.0, "memory_mb": 45.0, "latency_ms": 20.0, "pids_current": 3
        }
        traffic = {
            "global_rps": 60.0, "active_users": 25, "backend_rps": 12.0, "backend_pct": 20.0,
            "p95_ms": 25.0, "error_rate_pct": 0.0
        }
        eval_res = detector.evaluate(metric, traffic)
        # Should be classified as HIGH_TRAFFIC_HEALTHY, not an overload
        assert eval_res["state"] == OverloadState.HIGH_TRAFFIC_HEALTHY

    def test_11_multiple_correlated_anomalies_triggers_overload(self):
        detector = ServerOverloadDetector("app2")
        # High CPU + High Memory + High Latency
        metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 70.0, "memory_mb": 150.0, "latency_ms": 320.0, "pids_current": 4
        }
        traffic = {
            "global_rps": 20.0, "backend_rps": 4.0, "backend_pct": 20.0,
            "p95_ms": 340.0, "error_rate_pct": 1.0
        }
        detector.evaluate(metric, traffic)
        eval_res = detector.evaluate(metric, traffic)
        assert eval_res["state"] == OverloadState.OVERLOAD

    def test_backend_down_distinguished_from_resource_overload(self):
        detector = ServerOverloadDetector("app4")
        metric = {
            "status": "exited", "service_available": False, "status_code": None,
            "cpu_percent": 0.0, "memory_mb": 0.0, "latency_ms": None
        }
        eval_res = detector.evaluate(metric, {})
        assert eval_res["state"] == OverloadState.OFFLINE
        assert "not resource overload" in eval_res["reason"].lower()

    def test_17_and_19_recovery_to_normal_with_hysteresis_and_cooldown(self):
        detector = ServerOverloadDetector("app1")
        # 1. Place in overload
        overload_metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 75.0, "memory_mb": 50.0, "latency_ms": 350.0, "pids_current": 3
        }
        traffic = {"global_rps": 10.0, "backend_rps": 2.0, "backend_pct": 20.0, "p95_ms": 350.0}
        detector.evaluate(overload_metric, traffic)
        eval_ovl = detector.evaluate(overload_metric, traffic)
        assert eval_ovl["state"] == OverloadState.OVERLOAD

        # 2. Metrics drop to clean values (CPU: 12%, lat: 15ms)
        clean_metric = {
            "status": "running", "service_available": True, "status_code": 200,
            "cpu_percent": 12.0, "memory_mb": 42.0, "latency_ms": 15.0, "pids_current": 3
        }
        clean_traffic = {"global_rps": 2.0, "backend_rps": 0.4, "backend_pct": 20.0, "p95_ms": 15.0, "error_rate_pct": 0.0}

        # Cycle 1 clean: hysteresis prevents immediate flip to NORMAL
        eval_c1 = detector.evaluate(clean_metric, clean_traffic)
        assert eval_c1["state"] == OverloadState.OVERLOAD

        # Cycle 2 clean: still waiting for 3 clean cycles
        eval_c2 = detector.evaluate(clean_metric, clean_traffic)
        assert eval_c2["state"] == OverloadState.OVERLOAD

        # Cycle 3 clean: confirmed recovery to NORMAL
        eval_c3 = detector.evaluate(clean_metric, clean_traffic)
        assert eval_c3["state"] == OverloadState.NORMAL


class TestClusterOverloadEngine:
    """Covers cluster aggregation, missing locust metrics, and incident deduplication."""

    def test_12_missing_or_unavailable_locust_metrics_handled_gracefully(self):
        engine = TrafficOverloadEngine(target_services=["app1", "app2", "app3", "app4", "app5"])
        server_metrics = {
            f"app{i}": {
                "status": "running", "service_available": True, "status_code": 200,
                "cpu_percent": 10.0, "memory_mb": 45.0, "latency_ms": 15.0, "pids_current": 3
            } for i in range(1, 6)
        }
        # Locust metrics is None (locust stopped or idle)
        res = engine.evaluate_cluster(server_metrics, locust_metrics=None)
        assert res["cluster_state"] == OverloadState.NORMAL
        assert res["traffic"]["locust_active"] is False
        assert res["traffic"]["global_rps"] == 0.0
        assert len(res["servers"]) == 5

    def test_18_duplicate_incident_suppression_in_redis(self):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No incident initially

        engine = TrafficOverloadEngine(
            target_services=["app1", "app2"],
            redis_client=mock_redis
        )

        server_metrics = {
            "app1": {
                "status": "running", "service_available": True, "status_code": 200,
                "cpu_percent": 75.0, "memory_mb": 50.0, "latency_ms": 350.0, "pids_current": 3
            },
            "app2": {
                "status": "running", "service_available": True, "status_code": 200,
                "cpu_percent": 10.0, "memory_mb": 40.0, "latency_ms": 12.0, "pids_current": 3
            }
        }
        traffic = {"global_rps": 10.0, "p95_ms": 350.0}

        # Cycle 1: starts early warning
        engine.evaluate_cluster(server_metrics, locust_metrics={"traffic": traffic, "response_times_ms": {"p95": 350.0}})
        # Cycle 2: confirmed overload
        res = engine.evaluate_cluster(server_metrics, locust_metrics={"traffic": traffic, "response_times_ms": {"p95": 350.0}})

        assert res["servers"]["app1"]["state"] == OverloadState.OVERLOAD
        # Verify set was called to record incident
        assert mock_redis.set.called

        # In Cycle 3, mock_redis indicates incident already active on app1 (and none on app2)
        mock_redis.get.side_effect = lambda k: '{"incident_id": "ovl-app1-123", "state": "OVERLOAD"}' if "app1" in k else None
        mock_redis.lpush.reset_mock()

        engine.evaluate_cluster(server_metrics, locust_metrics={"traffic": traffic, "response_times_ms": {"p95": 350.0}})
        # lpush to overload:history should NOT be called again because incident is already active on app1 (deduplication)
        assert not mock_redis.lpush.called


class TestOverloadMonitoringApi:
    """Tests the new REST endpoints added in monitor.py for Phase 5.1."""

    @pytest.fixture(autouse=True)
    def setup_client(self):
        try:
            import monitor
            self.app = monitor.app
        except ImportError:
            from monitor import monitor
            self.app = monitor.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_api_get_overload_config(self):
        resp = self.client.get("/overload/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cpu_warning_threshold" in data
        assert "cpu_overload_threshold" in data
        assert "latency_warning_threshold" in data
        assert data["cpu_overload_threshold"] == 65.0

    def test_api_get_overload_status(self):
        resp = self.client.get("/overload/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cluster_state" in data
        assert "servers" in data

    def test_api_get_server_overload_valid(self):
        resp = self.client.get("/overload/servers/app1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "state" in data
        assert "reason" in data

    def test_api_get_server_overload_unknown_404(self):
        resp = self.client.get("/overload/servers/unknown_srv")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "error" in data

    def test_api_get_overload_predictions(self):
        resp = self.client.get("/overload/predictions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "predictions" in data
        assert "app1" in data["predictions"]

    def test_api_get_overload_history(self):
        resp = self.client.get("/overload/history?limit=10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "history" in data
        assert isinstance(data["history"], list)

