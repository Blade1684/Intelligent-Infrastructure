"""
Phase 5.4 Test Suite: Predictive Failure Prevention and Early Warning System
Validates all 32 test cases specified in the Phase 5.4 requirements.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from prediction_engine import (
        PredictionEngine,
        PredictiveState,
        EvaluationOutcome,
        MetricTrendAnalyzer,
        DEFAULT_PREDICTION_CONFIG
    )
except ImportError:
    from monitor.prediction_engine import (
        PredictionEngine,
        PredictiveState,
        EvaluationOutcome,
        MetricTrendAnalyzer,
        DEFAULT_PREDICTION_CONFIG
    )


class TestPhase54PredictionEngine(unittest.TestCase):
    def setUp(self):
        self.config = dict(DEFAULT_PREDICTION_CONFIG)
        self.config["min_samples"] = 10
        self.config["min_samples_reliable"] = 10
        self.config["min_samples_preliminary"] = 5
        self.config["min_samples_traffic_action"] = 10
        self.config["forecast_horizon_seconds"] = 300.0
        self.config["high_risk_horizon_seconds"] = 120.0
        self.config["saturation_horizon_seconds"] = 60.0
        self.config["min_r_squared"] = 0.65

        self.engine = PredictionEngine(
            target_services=["app1", "app2", "app3", "app4", "app5"],
            redis_client=None,
            traffic_manager=None,
            config=self.config
        )

    # 1. Stable CPU trend returns NORMAL
    def test_01_stable_cpu_trend_returns_normal(self):
        # 10 samples around 30% CPU, flat
        now = time.time()
        ts = [(now - (10 - i) * 3, 30.0 + (i % 2) * 0.2) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.NORMAL)
        self.assertIn(res["trend"], ("stable", "decreasing"))
        self.assertIsNone(res["time_to_warning_seconds"])

    # 2. Increasing CPU trend generates EARLY_WARNING
    def test_02_increasing_cpu_trend_generates_early_warning(self):
        # Starts at 40%, increases by 0.15% per second. Warning is 70%.
        # In 10 samples over 30s: 40% -> 44.5%.
        # Slope = ~0.15%/s. Time to reach 70% from 44.5% = (70 - 44.5) / 0.15 = ~170s.
        # 170s <= 300s horizon -> EARLY_WARNING!
        now = time.time()
        ts = [(now - (10 - i) * 3, 40.0 + i * 3 * 0.15) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.EARLY_WARNING)
        self.assertIsNotNone(res["time_to_warning_seconds"])
        self.assertLessEqual(res["time_to_warning_seconds"], 300.0)

    # 3. Rapid CPU increase predicts critical threshold (HIGH_RISK or PREDICTED_SATURATION)
    def test_03_rapid_cpu_increase_predicts_critical_threshold(self):
        # Starts at 65%, increases rapidly by 0.5% per second.
        # Critical threshold is 85%. Current = 65 + 9*3*0.5 = 78.5%.
        # Time to 85% = (85 - 78.5) / 0.5 = 13.0 seconds <= 60s -> PREDICTED_SATURATION
        now = time.time()
        ts = [(now - (10 - i) * 3, 65.0 + i * 3 * 0.5) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.PREDICTED_SATURATION)
        self.assertLessEqual(res["time_to_critical_seconds"], 60.0)

    # 4. Increasing memory consumption is detected
    def test_04_increasing_memory_consumption_detected(self):
        # Memory starts at 70MB, increases by 0.25 MB/s. Warning is 120MB.
        # In 10 samples over 30s: 70 -> 76.75MB.
        # Time to 120MB = (120 - 76.75) / 0.25 = 173s <= 300s -> EARLY_WARNING
        now = time.time()
        ts = [(now - (10 - i) * 3, 70.0 + i * 3 * 0.25) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "memory_mb", self.config["thresholds"]["memory_mb"], self.config)
        self.assertEqual(res["status"], PredictiveState.EARLY_WARNING)
        self.assertIsNotNone(res["time_to_warning_seconds"])

    # 5. Stable memory usage does not trigger a warning
    def test_05_stable_memory_usage_does_not_trigger_warning(self):
        now = time.time()
        ts = [(now - (10 - i) * 3, 45.0 + (i % 2) * 0.1) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "memory_mb", self.config["thresholds"]["memory_mb"], self.config)
        self.assertEqual(res["status"], PredictiveState.NORMAL)
        self.assertIsNone(res["time_to_warning_seconds"])

    # 6. Increasing latency is detected
    def test_06_increasing_latency_detected(self):
        # Latency increases from 50ms up toward 200ms warning
        now = time.time()
        ts = [(now - (10 - i) * 3, 50.0 + i * 3 * 0.8) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "latency_ms", self.config["thresholds"]["latency_ms"], self.config)
        self.assertIn(res["status"], (PredictiveState.EARLY_WARNING, PredictiveState.HIGH_RISK))

    # 7. Increasing error rate is detected
    def test_07_increasing_error_rate_detected(self):
        # Error rate increases from 0.1% to 1.5%
        now = time.time()
        ts = [(now - (10 - i) * 3, 0.1 + i * 3 * 0.05) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "error_rate_pct", self.config["thresholds"]["error_rate_pct"], self.config)
        self.assertIn(res["status"], (PredictiveState.EARLY_WARNING, PredictiveState.HIGH_RISK))

    # 8. Insufficient historical samples return INSUFFICIENT_DATA
    def test_08_insufficient_samples_return_insufficient_data(self):
        now = time.time()
        ts = [(now - 3, 50.0), (now, 52.0)] # Only 2 samples
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.INSUFFICIENT_DATA)
        self.assertIn("Insufficient samples", res["reason"])

    # 9. Invalid or missing samples are handled safely
    def test_09_invalid_or_missing_samples_handled_safely(self):
        now = time.time()
        ts = [
            (now - 30, 50.0),
            (now - 25, None),
            (now - 20, "invalid_num"),
            (now - 15, float("nan")),
            (now - 10, 55.0),
            (now - 5, 60.0),
            (now, 65.0)
        ]
        # Only 4 valid samples remaining, below min_samples_early=5 -> returns INSUFFICIENT_DATA without crashing
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.INSUFFICIENT_DATA)

    # 10. Negative slope does not produce a positive saturation prediction
    def test_10_negative_slope_does_not_produce_saturation_prediction(self):
        now = time.time()
        # Decreasing from 80% down to 60%
        ts = [(now - (10 - i) * 3, 80.0 - i * 2.0) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.NORMAL)
        self.assertIsNone(res["time_to_warning_seconds"])
        self.assertIsNone(res["time_to_critical_seconds"])

    # 11. Zero slope does not generate a time-to-threshold estimate
    def test_11_zero_slope_does_not_generate_time_to_threshold(self):
        now = time.time()
        ts = [(now - (10 - i) * 3, 40.0) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.NORMAL)
        self.assertIsNone(res["time_to_warning_seconds"])

    # 12. Poor R² returns UNRELIABLE_PREDICTION
    def test_12_poor_r2_returns_unreliable_prediction(self):
        now = time.time()
        # Upward overall (+1.0/s), but wildly oscillating so R² < 0.65
        ts = [(now - (10 - i) * 3, 30.0 + i * 3 * 1.0 + (10.0 if i % 2 == 0 else -10.0)) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res["status"], PredictiveState.UNRELIABLE_PREDICTION)
        self.assertIn("R²", res["reason"])

    # 13. Correct time-unit conversion
    def test_13_correct_time_unit_conversion(self):
        # Over 54 seconds (10 points, dt=6s), metric increases by 0.2% per second.
        # At i=9, current value = 40.0 + 9*6*0.2 = 50.8%.
        # Warning threshold = 70.0%.
        # Expected time: (70.0 - 50.8) / 0.2 = 96.0 seconds.
        now = time.time()
        ts = [(now - (10 - i) * 6, 40.0 + i * 6 * 0.2) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertAlmostEqual(res["slope_per_second"], 0.2, places=3)
        self.assertAlmostEqual(res["time_to_warning_seconds"], 96.0, delta=2.0)

    # 14. Prediction confidence is calculated consistently
    def test_14_prediction_confidence_calculated_consistently(self):
        now = time.time()
        ts = [(now - (10 - i) * 3, 40.0 + i * 3 * 0.15) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertIn(res["confidence"], ("high", "medium", "low"))
        self.assertGreaterEqual(res["confidence_score"], 0.0)
        self.assertLessEqual(res["confidence_score"], 1.0)

    # 15. Prediction deduplication works
    def test_15_prediction_deduplication_works(self):
        history = [{"timestamp": time.time() - (10 - i) * 3, "cpu_percent": 40.0 + i * 1.5} for i in range(10)]
        # First evaluation generates active prediction
        self.engine.evaluate_server("app1", history, current_epoch=1000.0)
        count_1 = len(self.engine.active_predictions)
        self.assertGreater(count_1, 0)

        # Immediate second evaluation with same signature within 30s should NOT duplicate
        self.engine.evaluate_server("app1", history, current_epoch=1010.0)
        count_2 = len(self.engine.active_predictions)
        self.assertEqual(count_1, count_2)

    # 16. Historical prediction storage works
    def test_16_historical_prediction_storage_works(self):
        # Record a manual completed evaluation
        self.engine.record_manual_evaluation_sample(EvaluationOutcome.TRUE_POSITIVE, lead_time=45.2)
        history = self.engine.get_history()
        self.assertIsInstance(history, list)

    # 17. Evaluation remains PENDING before its evaluation window
    def test_17_evaluation_remains_pending_before_evaluation_window(self):
        now = 1000.0
        history = [{"timestamp": now - (10 - i) * 3, "cpu_percent": 40.0 + i * 1.5} for i in range(10)]
        self.engine.evaluate_server("app1", history, current_epoch=now)
        active = list(self.engine.active_predictions.values())
        self.assertGreater(len(active), 0)
        self.assertEqual(active[0]["evaluation_state"], EvaluationOutcome.PENDING)

    # 18. True-positive classification works
    def test_18_true_positive_classification_works(self):
        now = 1000.0
        history = [{"timestamp": now - (10 - i) * 3, "cpu_percent": 50.0 + i * 1.5} for i in range(10)]
        self.engine.evaluate_server("app1", history, current_epoch=now)
        pred_id = list(self.engine.active_predictions.keys())[0]

        # Subsequent observation breaches the threshold (>= 70.0%) within deadline
        self.engine._update_prediction_evaluations("app1", {"cpu_percent": 72.5}, now + 20.0)
        self.assertNotIn(pred_id, self.engine.active_predictions)
        completed = [h for h in self.engine.evaluation_history if h["prediction_id"] == pred_id]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["evaluation_state"], EvaluationOutcome.TRUE_POSITIVE)
        self.assertEqual(completed[0]["lead_time"], 20.0)

    # 19. False-positive classification works
    def test_19_false_positive_classification_works(self):
        now = 1000.0
        history = [{"timestamp": now - (10 - i) * 3, "cpu_percent": 50.0 + i * 1.5} for i in range(10)]
        self.engine.evaluate_server("app1", history, current_epoch=now)
        pred_id = list(self.engine.active_predictions.keys())[0]

        # Time passes beyond the evaluation deadline without breaching threshold
        deadline = self.engine.active_predictions[pred_id]["evaluation_deadline_epoch"]
        self.engine._update_prediction_evaluations("app1", {"cpu_percent": 55.0}, deadline + 5.0)
        completed = [h for h in self.engine.evaluation_history if h["prediction_id"] == pred_id]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["evaluation_state"], EvaluationOutcome.FALSE_POSITIVE)

    # 20. False-negative classification works
    def test_20_false_negative_classification_works(self):
        self.engine.record_manual_evaluation_sample(EvaluationOutcome.FALSE_NEGATIVE)
        acc = self.engine.get_accuracy_metrics()
        self.assertEqual(acc["false_negatives"], 1)

    # 21. True-negative classification works
    def test_21_true_negative_classification_works(self):
        self.engine.record_manual_evaluation_sample(EvaluationOutcome.TRUE_NEGATIVE)
        acc = self.engine.get_accuracy_metrics()
        self.assertEqual(acc["true_negatives"], 1)

    # 22. Precision and recall calculations are correct
    def test_22_precision_and_recall_calculations_correct(self):
        # 8 TP, 2 FP, 10 TN, 2 FN
        for _ in range(8):
            self.engine.record_manual_evaluation_sample(EvaluationOutcome.TRUE_POSITIVE, lead_time=30.0)
        for _ in range(2):
            self.engine.record_manual_evaluation_sample(EvaluationOutcome.FALSE_POSITIVE)
        for _ in range(10):
            self.engine.record_manual_evaluation_sample(EvaluationOutcome.TRUE_NEGATIVE)
        for _ in range(2):
            self.engine.record_manual_evaluation_sample(EvaluationOutcome.FALSE_NEGATIVE)

        acc = self.engine.get_accuracy_metrics()
        # Precision = 8 / (8 + 2) = 0.80
        self.assertEqual(acc["precision"], 0.8)
        # Recall = 8 / (8 + 2) = 0.80
        self.assertEqual(acc["recall"], 0.8)
        # F1 = 0.80
        self.assertEqual(acc["f1_score"], 0.8)
        # FPR = 2 / (2 + 10) = 0.167
        self.assertAlmostEqual(acc["false_positive_rate"], 0.167, places=2)
        # Average lead time = 30.0
        self.assertEqual(acc["average_lead_time_seconds"], 30.0)

    # 23. Redis failure fallback works
    def test_23_redis_failure_fallback_works(self):
        failing_redis = MagicMock()
        failing_redis.get.side_effect = Exception("Redis connection refused")
        failing_redis.set.side_effect = Exception("Redis connection refused")

        engine = PredictionEngine(
            target_services=["app1"],
            redis_client=failing_redis,
            config=self.config
        )
        # Should operate smoothly in-memory without throwing exceptions
        status = engine.get_cluster_status()
        self.assertEqual(status["total_servers"], 1)

    # 24. Predictive traffic management can be disabled
    def test_24_predictive_traffic_management_can_be_disabled(self):
        self.engine.set_traffic_control_enabled(False)
        self.assertFalse(self.engine.config["predictive_traffic_enabled"])
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "NONE")
        self.assertIn("globally disabled", res["reason"])

    # 25. EARLY_WARNING does not change traffic weight
    def test_25_early_warning_does_not_change_traffic_weight(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 100, "in_maint": False, "current_state": "NORMAL"}
        self.engine.traffic_manager = mock_tm

        self.engine.server_predictions["app1"]["predictive_state"] = PredictiveState.EARLY_WARNING
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "NONE")
        self.assertIn("EARLY_WARNING policy", res["reason"])
        mock_tm.haproxy_client.set_server_weight.assert_not_called()

    # 26. HIGH_RISK traffic reduction follows configured policy
    def test_26_high_risk_traffic_reduction_follows_policy(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 100, "in_maint": False, "current_state": "NORMAL"}
        mock_tm.count_healthy_eligible.return_value = 5 # 5 eligible
        mock_tm.haproxy_client.set_server_weight.return_value = {"success": True}
        self.engine.traffic_manager = mock_tm

        self.engine.server_predictions["app1"]["predictive_state"] = PredictiveState.HIGH_RISK
        self.engine.server_predictions["app1"]["sample_count"] = 20
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "PREDICTIVE_WEIGHT_REDUCED")
        self.assertEqual(res["weight"], 75)
        mock_tm.haproxy_client.set_server_weight.assert_called_with("app1", 75)

    # 27. Minimum eligible backend safeguard is respected
    def test_27_minimum_eligible_backend_safeguard_respected(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 100, "in_maint": False, "current_state": "NORMAL"}
        mock_tm.count_healthy_eligible.return_value = 1 # Only 1 eligible backend left!
        self.engine.traffic_manager = mock_tm

        self.engine.server_predictions["app1"]["predictive_state"] = PredictiveState.PREDICTED_SATURATION
        self.engine.server_predictions["app1"]["sample_count"] = 20
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "ACTION_REJECTED")
        self.assertIn("Minimum eligible backend safeguard", res["reason"])
        mock_tm.haproxy_client.set_server_weight.assert_not_called()

    # 28. Existing Phase 5.2 traffic actions are not overwritten
    def test_28_existing_phase52_actions_not_overwritten(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        # Server is in maintenance or excluded by Phase 5.2
        mock_tm.get_server_status.return_value = {"current_weight": 0, "in_maint": True, "current_state": "EXCLUDED"}
        self.engine.traffic_manager = mock_tm

        self.engine.server_predictions["app1"]["predictive_state"] = PredictiveState.HIGH_RISK
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "NONE")
        self.assertIn("currently excluded/in maintenance", res["reason"])

    # 29. Phase 5.3 recovery is not triggered solely by predictions
    def test_29_phase53_recovery_not_triggered_solely_by_predictions(self):
        # Verify PredictionEngine has no access or code triggering docker container restarts
        self.assertFalse(hasattr(self.engine, "docker_client"))
        self.assertFalse(hasattr(self.engine, "restart_container"))
        # Predictive saturation only reduces weight to 50, does not restart container
        self.assertEqual(self.config["predictive_reduced_weight_saturation"], 50)

    # 30. Concurrent prediction evaluation does not corrupt history
    def test_30_concurrent_prediction_evaluation_thread_safe(self):
        import concurrent.futures
        def worker(srv):
            history = [{"timestamp": time.time() - (10 - i) * 3, "cpu_percent": 40.0 + i} for i in range(10)]
            return self.engine.evaluate_server(srv, history)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(worker, s) for s in ["app1", "app2", "app3", "app4", "app5"]]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 5)
        status = self.engine.get_cluster_status()
        self.assertEqual(status["total_servers"], 5)

    # 31. Configuration validation works
    def test_31_configuration_validation_works(self):
        # Invalid min_samples
        res1 = self.engine.update_config({"min_samples": -5})
        self.assertFalse(res1["success"])

        # Invalid R^2
        res2 = self.engine.update_config({"min_r_squared": 1.5})
        self.assertFalse(res2["success"])

        # Valid update
        res3 = self.engine.update_config({"forecast_horizon_seconds": 450.0})
        self.assertTrue(res3["success"])
        self.assertEqual(self.engine.config["forecast_horizon_seconds"], 450.0)

    # 32. Existing phases continue functioning
    def test_32_existing_phases_continue_functioning(self):
        # Verify engine returns clean cluster summary without altering target services
        status = self.engine.get_cluster_status()
        self.assertIn("servers", status)
        self.assertIn("accuracy_summary", status)
    # 33. Metric-specific slope noise thresholds (below, at, and above)
    def test_33_metric_specific_slope_thresholds(self):
        now = time.time()
        # CPU: threshold 0.02 %/s, warning is 70%
        cpu_sub = [(now - (10 - i) * 2, 40.0 + i * 2 * 0.005) for i in range(10)] # 0.005 %/s <= 0.02
        res_cpu_sub = MetricTrendAnalyzer.analyze_metric(cpu_sub, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res_cpu_sub["status"], PredictiveState.NORMAL)

        # Starts at 60.0%, +0.05 %/s -> reaches 70% warning in 182s <= 300s horizon
        cpu_above = [(now - (10 - i) * 2, 60.0 + i * 2 * 0.05) for i in range(10)] # 0.05 %/s > 0.02
        res_cpu_above = MetricTrendAnalyzer.analyze_metric(cpu_above, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res_cpu_above["status"], PredictiveState.EARLY_WARNING)

        # Memory: threshold 0.05 MB/s, warning is 120MB
        mem_sub = [(now - (10 - i) * 2, 50.0 + i * 2 * 0.002) for i in range(10)] # 0.002 MB/s <= 0.05
        res_mem_sub = MetricTrendAnalyzer.analyze_metric(mem_sub, "memory_mb", self.config["thresholds"]["memory_mb"], self.config)
        self.assertEqual(res_mem_sub["status"], PredictiveState.NORMAL)

        # Starts at 100.0MB, +0.20 MB/s -> reaches 120MB in 82s <= 300s horizon
        mem_above = [(now - (10 - i) * 2, 100.0 + i * 2 * 0.20) for i in range(10)] # 0.20 MB/s > 0.05
        res_mem_above = MetricTrendAnalyzer.analyze_metric(mem_above, "memory_mb", self.config["thresholds"]["memory_mb"], self.config)
        self.assertEqual(res_mem_above["status"], PredictiveState.EARLY_WARNING)

        # Latency: threshold 0.20 ms/s, warning is 200ms
        lat_sub = [(now - (10 - i) * 2, 5.0 + i * 2 * 0.01) for i in range(10)] # 0.01 ms/s <= 0.20 (normal network jitter)
        res_lat_sub = MetricTrendAnalyzer.analyze_metric(lat_sub, "latency_ms", self.config["thresholds"]["latency_ms"], self.config)
        self.assertEqual(res_lat_sub["status"], PredictiveState.NORMAL)

        # Starts at 100.0ms, +0.50 ms/s -> reaches 200ms in 182s <= 300s horizon
        lat_above = [(now - (10 - i) * 2, 100.0 + i * 2 * 0.50) for i in range(10)] # 0.50 ms/s > 0.20
        res_lat_above = MetricTrendAnalyzer.analyze_metric(lat_above, "latency_ms", self.config["thresholds"]["latency_ms"], self.config)
        self.assertEqual(res_lat_above["status"], PredictiveState.EARLY_WARNING)

        # Error rate: threshold 0.02 %/s, warning is 2.0%
        err_sub = [(now - (10 - i) * 2, 0.0 + i * 2 * 0.005) for i in range(10)]
        res_err_sub = MetricTrendAnalyzer.analyze_metric(err_sub, "error_rate_pct", self.config["thresholds"]["error_rate_pct"], self.config)
        self.assertEqual(res_err_sub["status"], PredictiveState.NORMAL)

        # Starts at 1.0%, +0.05 %/s -> reaches 2.0% in 2s <= 60s -> HIGH_RISK or EARLY_WARNING
        err_above = [(now - (10 - i) * 2, 1.0 + i * 2 * 0.05) for i in range(10)]
        res_err_above = MetricTrendAnalyzer.analyze_metric(err_above, "error_rate_pct", self.config["thresholds"]["error_rate_pct"], self.config)
        self.assertIn(res_err_above["status"], (PredictiveState.EARLY_WARNING, PredictiveState.HIGH_RISK))

    # 34. Unconstrained negative R^2 handling
    def test_34_unconstrained_negative_r_squared_handling(self):
        now = time.time()
        # Create an oscillating non-linear series where SS_res > SS_tot
        y_oscillating = [10.0, 90.0, 5.0, 95.0, 8.0, 92.0, 4.0, 96.0, 12.0, 88.0]
        ts = [(now - (10 - i) * 2, y_oscillating[i]) for i in range(10)]
        res = MetricTrendAnalyzer.analyze_metric(ts, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        # R^2 should be negative or well below min_r2 without clamping to 0.0
        self.assertLess(res["r_squared"], 0.65)
        self.assertEqual(res["status"], PredictiveState.UNRELIABLE_PREDICTION)

    # 35. Tiered sample requirements
    def test_35_tiered_sample_requirements(self):
        now = time.time()
        # A. < 5 samples (< min_samples_preliminary) -> INSUFFICIENT_DATA
        ts_few = [(now - (4 - i) * 2, 50.0 + i * 5) for i in range(4)]
        res_few = MetricTrendAnalyzer.analyze_metric(ts_few, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res_few["status"], PredictiveState.INSUFFICIENT_DATA)

        # B. 8 samples (< min_samples_reliable = 10) with rapid growth -> capped at preliminary EARLY_WARNING
        ts_prelim = [(now - (8 - i) * 2, 70.0 + i * 2 * 0.8) for i in range(8)]
        res_prelim = MetricTrendAnalyzer.analyze_metric(ts_prelim, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res_prelim["status"], PredictiveState.EARLY_WARNING)
        self.assertIn("Preliminary early warning", res_prelim["reason"])

        # C. 10 samples (>= min_samples_reliable = 10) with rapid growth -> PREDICTED_SATURATION
        ts_mature = [(now - (10 - i) * 2, 70.0 + i * 2 * 0.8) for i in range(10)]
        res_mature = MetricTrendAnalyzer.analyze_metric(ts_mature, "cpu_percent", self.config["thresholds"]["cpu_percent"], self.config)
        self.assertEqual(res_mature["status"], PredictiveState.PREDICTED_SATURATION)

    # 36. Traffic management minimum sample safeguard
    def test_36_traffic_management_minimum_sample_safeguard(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 100, "in_maint": False, "current_state": "NORMAL"}
        mock_tm.count_healthy_eligible.return_value = 5
        self.engine.traffic_manager = mock_tm

        # Server is HIGH_RISK but has only 8 samples (< min_samples_traffic_action = 10)
        self.engine.server_predictions["app1"]["predictive_state"] = PredictiveState.HIGH_RISK
        self.engine.server_predictions["app1"]["sample_count"] = 8
        res = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(res["action"], "ACTION_REJECTED")
        self.assertIn("below min_samples_traffic_action", res["reason"])
        mock_tm.haproxy_client.set_server_weight.assert_not_called()

    # 37. Separate live and synthetic accuracy tracking
    def test_37_separate_live_and_synthetic_accuracy_tracking(self):
        # Initial accuracy should have 0 evaluated for both
        acc = self.engine.get_accuracy_metrics()
        self.assertIn("live", acc)
        self.assertIn("synthetic", acc)
        self.assertEqual(acc["live"]["total_evaluated"], 0)
        self.assertEqual(acc["synthetic"]["total_evaluated"], 0)

        # Register synthetic benchmark samples
        self.engine.record_manual_evaluation_sample(EvaluationOutcome.TRUE_POSITIVE, lead_time=30.0)
        self.engine.record_manual_evaluation_sample(EvaluationOutcome.FALSE_POSITIVE)

        acc2 = self.engine.get_accuracy_metrics()
        self.assertEqual(acc2["synthetic"]["total_evaluated"], 2)
        self.assertEqual(acc2["synthetic"]["true_positives"], 1)
        self.assertEqual(acc2["synthetic"]["false_positives"], 1)
        self.assertEqual(acc2["live"]["total_evaluated"], 0) # Live remains untouched

    # 38. Comprehensive Sample Count Boundary Regression: N=0, N=1, N=5, N=6, N=14, N=15, N=20
    def test_38_sample_count_boundary_conditions_0_1_5_6_14_15_20(self):
        engine = PredictionEngine(
            target_services=["app1", "app2", "app3", "app4", "app5"],
            config={
                "min_samples_preliminary": 6,
                "min_samples_reliable": 15,
                "min_samples_traffic_action": 20,
                "min_samples": 20,
                "forecast_horizon_seconds": 300.0,
                "high_risk_horizon_seconds": 120.0,
                "saturation_horizon_seconds": 60.0,
                "thresholds": DEFAULT_PREDICTION_CONFIG["thresholds"],
                "min_slope": DEFAULT_PREDICTION_CONFIG["min_slope"]
            }
        )
        engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 100, "in_maint": False, "current_state": "NORMAL"}
        mock_tm.count_healthy_eligible.return_value = 5
        mock_tm.haproxy_client.set_server_weight.return_value = {"success": True}
        engine.traffic_manager = mock_tm

        now = time.time()
        base_cpu = 70.0
        slope = 0.8  # Rapid increase toward 85% critical in ~18s, 95% saturation in ~31s

        # -------------------------------------------------------------
        # N = 0: Zero samples -> INSUFFICIENT_DATA, confidence "none", traffic rejected
        # -------------------------------------------------------------
        res_0 = engine.evaluate_server("app1", history_samples=[], current_epoch=now)
        self.assertEqual(res_0["predictive_state"], PredictiveState.INSUFFICIENT_DATA)
        self.assertEqual(res_0["confidence"], "none")
        self.assertEqual(res_0["confidence_score"], 0.0)
        self.assertEqual(res_0["consecutive_normal_cycles"], 0)
        self.assertEqual(res_0["sample_count"], 0)
        trf_0 = engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(trf_0["action"], "NONE")

        # -------------------------------------------------------------
        # N = 1: Single sample -> INSUFFICIENT_DATA, confidence "none"
        # -------------------------------------------------------------
        h_1 = [{"timestamp": now, "cpu_percent": 70.0, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}]
        res_1 = engine.evaluate_server("app1", history_samples=h_1, current_epoch=now)
        self.assertEqual(res_1["predictive_state"], PredictiveState.INSUFFICIENT_DATA)
        self.assertEqual(res_1["confidence"], "none")
        self.assertEqual(res_1["confidence_score"], 0.0)
        self.assertEqual(res_1["consecutive_normal_cycles"], 0)
        self.assertEqual(res_1["sample_count"], 1)

        # -------------------------------------------------------------
        # N = 5: Below preliminary threshold (5 < 6) -> INSUFFICIENT_DATA, confidence "none"
        # -------------------------------------------------------------
        h_5 = [
            {"timestamp": now - (5 - i) * 2, "cpu_percent": base_cpu + i * 2 * slope, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}
            for i in range(5)
        ]
        res_5 = engine.evaluate_server("app1", history_samples=h_5, current_epoch=now)
        self.assertEqual(res_5["predictive_state"], PredictiveState.INSUFFICIENT_DATA)
        self.assertEqual(res_5["confidence"], "none")
        self.assertEqual(res_5["confidence_score"], 0.0)
        self.assertEqual(res_5["consecutive_normal_cycles"], 0)
        self.assertEqual(res_5["sample_count"], 5)
        trf_5 = engine.apply_predictive_traffic_policy("app1")
        self.assertIn(trf_5["action"], ("NONE", "ACTION_REJECTED"))

        # -------------------------------------------------------------
        # N = 6: Preliminary boundary reached (6 >= 6). Capped at EARLY_WARNING, traffic prohibited (6 < 20)
        # -------------------------------------------------------------
        h_6 = [
            {"timestamp": now - (6 - i) * 2, "cpu_percent": base_cpu + i * 2 * slope, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}
            for i in range(6)
        ]
        res_6 = engine.evaluate_server("app1", history_samples=h_6, current_epoch=now)
        self.assertEqual(res_6["predictive_state"], PredictiveState.EARLY_WARNING)
        self.assertNotEqual(res_6["predictive_state"], PredictiveState.HIGH_RISK)
        self.assertNotEqual(res_6["predictive_state"], PredictiveState.PREDICTED_SATURATION)
        self.assertIn(res_6["confidence"], ("medium", "low"))
        self.assertEqual(res_6["sample_count"], 6)
        trf_6 = engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(trf_6["action"], "NONE")

        # -------------------------------------------------------------
        # N = 14: Upper bound of preliminary tier (14 < 15). Still capped at EARLY_WARNING, traffic prohibited (14 < 20)
        # -------------------------------------------------------------
        h_14 = [
            {"timestamp": now - (14 - i) * 2, "cpu_percent": base_cpu + i * 2 * slope, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}
            for i in range(14)
        ]
        res_14 = engine.evaluate_server("app1", history_samples=h_14, current_epoch=now)
        self.assertEqual(res_14["predictive_state"], PredictiveState.EARLY_WARNING)
        self.assertEqual(res_14["sample_count"], 14)
        trf_14 = engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(trf_14["action"], "NONE")

        # -------------------------------------------------------------
        # N = 15: Reliable forecasting boundary reached (15 >= 15). Escalates to PREDICTED_SATURATION / HIGH_RISK.
        # But traffic management is STILL rejected because N=15 < 20.
        # -------------------------------------------------------------
        h_15 = [
            {"timestamp": now - (15 - i) * 2, "cpu_percent": 60.0 + i * 2 * 0.8, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}
            for i in range(15)
        ]
        res_15 = engine.evaluate_server("app1", history_samples=h_15, current_epoch=now)
        self.assertIn(res_15["predictive_state"], (PredictiveState.HIGH_RISK, PredictiveState.PREDICTED_SATURATION))
        self.assertEqual(res_15["confidence"], "high")
        self.assertEqual(res_15["sample_count"], 15)
        trf_15 = engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(trf_15["action"], "ACTION_REJECTED")
        self.assertIn("below min_samples_traffic_action", trf_15["reason"])
        mock_tm.haproxy_client.set_server_weight.assert_not_called()

        # -------------------------------------------------------------
        # N = 20: Traffic management boundary reached (20 >= 20).
        # Predictive traffic intervention is now fully PERMITTED.
        # -------------------------------------------------------------
        h_20 = [
            {"timestamp": now - (20 - i) * 2, "cpu_percent": 60.0 + i * 2 * 0.8, "memory_mb": 50.0, "latency_ms": 30.0, "error_rate_pct": 0.0}
            for i in range(20)
        ]
        res_20 = engine.evaluate_server("app1", history_samples=h_20, current_epoch=now)
        self.assertEqual(res_20["predictive_state"], PredictiveState.PREDICTED_SATURATION)
        self.assertEqual(res_20["sample_count"], 20)
        trf_20 = engine.apply_predictive_traffic_policy("app1")
        self.assertIn(trf_20["action"], ("PREDICTIVE_SATURATION_WEIGHT_REDUCED", "PREDICTIVE_WEIGHT_REDUCED"))
        self.assertTrue(trf_20.get("success"))
        self.assertEqual(trf_20["weight"], 50)

    # 39. Insufficient data is never interpreted as proof of server health
    def test_39_insufficient_data_never_interprets_as_healthy(self):
        self.engine.set_traffic_control_enabled(True)
        mock_tm = MagicMock()
        mock_tm.get_server_status.return_value = {"current_weight": 50, "in_maint": False, "current_state": "NORMAL"}
        mock_tm.count_healthy_eligible.return_value = 5
        self.engine.traffic_manager = mock_tm

        # Server is evaluated with 3 samples (< 6)
        now = time.time()
        h = [{"timestamp": now - i * 2, "cpu_percent": 20.0, "memory_mb": 40.0, "latency_ms": 10.0, "error_rate_pct": 0.0} for i in range(3)]
        res = self.engine.evaluate_server("app1", history_samples=h, current_epoch=now)
        self.assertEqual(res["predictive_state"], PredictiveState.INSUFFICIENT_DATA)
        self.assertEqual(res["consecutive_normal_cycles"], 0)

        # Attempt restoration
        trf = self.engine.apply_predictive_traffic_policy("app1")
        self.assertEqual(trf["action"], "NONE") # Cannot restore weight
        mock_tm.haproxy_client.set_server_weight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
