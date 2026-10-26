"""
Phase 5.4: Predictive Failure Prevention and Early Warning System

Analyzes historical and current infrastructure metrics (CPU, Memory, Latency, Error Rate)
to estimate whether an application server is approaching resource-related failure or overload,
generating early warnings before Phase 5.1 classifies the server as OVERLOAD or CRITICAL.

Key capabilities:
  - Transparent, explainable OLS trend forecasting with R^2 goodness of fit.
  - Multi-factor confidence calculation (sample sufficiency, fit quality, trend consistency).
  - Independent predictive risk states: NORMAL, EARLY_WARNING, HIGH_RISK, PREDICTED_SATURATION.
  - Conservative, configurable preventive traffic management (weight reduction to 75 / 50).
  - Complete prediction evaluation tracking (TP, FP, TN, FN, Precision, Recall, F1, Lead Time).
  - Thread-safe Redis persistence with automatic in-memory fallback.
"""

import time
import json
import uuid
import threading
import numpy as np
from datetime import datetime, timezone

# Predictive Risk States (Independent of Phase 5.1 operational state)
class PredictiveState:
    NORMAL = "NORMAL"
    EARLY_WARNING = "EARLY_WARNING"
    HIGH_RISK = "HIGH_RISK"
    PREDICTED_SATURATION = "PREDICTED_SATURATION"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNRELIABLE_PREDICTION = "UNRELIABLE_PREDICTION"


# Prediction Evaluation Outcomes
class EvaluationOutcome:
    PENDING = "PENDING"
    TRUE_POSITIVE = "TRUE_POSITIVE"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    TRUE_NEGATIVE = "TRUE_NEGATIVE"
    FALSE_NEGATIVE = "FALSE_NEGATIVE"


DEFAULT_PREDICTION_CONFIG = {
    # Tiered sample requirements
    "min_samples_preliminary": 6,       # Minimum samples to compute initial regression (INSUFFICIENT_DATA if < 6)
    "min_samples_reliable": 15,         # Minimum samples to produce reliable HIGH_RISK or PREDICTED_SATURATION
    "min_samples_traffic_action": 20,   # Minimum samples before preventive traffic reduction is permitted
    "min_samples": 20,                   # General historical baseline window

    # Horizons
    "forecast_horizon_seconds": 300.0,   # 5-minute forecast horizon
    "high_risk_horizon_seconds": 120.0,  # 2-minute horizon for high-risk classification
    "saturation_horizon_seconds": 60.0,  # 1-minute horizon for saturation classification
    "min_r_squared": 0.65,               # Minimum goodness-of-fit (R^2)
    "min_trend_consistency": 0.60,       # Minimum positive slope consistency fraction

    # Metric-Specific Minimum Trend / Noise Slope Thresholds (Dimensional Units)
    "min_slope": {
        "cpu_percent": 0.02,     # 0.02 %/s (1.2% per minute)
        "memory_mb": 0.05,       # 0.05 MB/s (3.0 MB per minute)
        "latency_ms": 0.20,      # 0.20 ms/s (12.0 ms per minute)
        "error_rate_pct": 0.02   # 0.02 %/s (1.2% per minute)
    },

    # Predictive Traffic Management (Safe & Conservative)
    "predictive_traffic_enabled": False, # Disabled by default; must be explicitly enabled
    "predictive_reduced_weight_high_risk": 75,
    "predictive_reduced_weight_saturation": 50,
    "action_cooldown_seconds": 15.0,
    "consecutive_healthy_required": 3,

    # Metric Thresholds for Prediction
    "thresholds": {
        "cpu_percent": {
            "warning": 70.0,
            "critical": 85.0,
            "saturation": 95.0
        },
        "memory_mb": {
            "warning": 120.0,
            "critical": 180.0,
            "saturation": 220.0
        },
        "latency_ms": {
            "warning": 200.0,
            "critical": 500.0,
            "saturation": 1000.0
        },
        "error_rate_pct": {
            "warning": 2.0,
            "critical": 5.0,
            "saturation": 15.0
        }
    }
}


class MetricTrendAnalyzer:
    """
    Transparent OLS linear regression analyzer for a single metric time series.
    Calculates slope per second, R^2, trend consistency, and time-to-threshold.
    """

    @staticmethod
    def analyze_metric(time_series, metric_name, threshold_cfg, config):
        """
        time_series: list of (timestamp_epoch, metric_value) sorted ascending.
        metric_name: 'cpu_percent', 'memory_mb', 'latency_ms', or 'error_rate_pct'
        threshold_cfg: dict with 'warning', 'critical', 'saturation'
        config: active prediction configuration
        """
        min_samples = config.get("min_samples", 20)
        min_samples_preliminary = config.get("min_samples_preliminary", config.get("min_samples_early_warning", 6))
        min_samples_reliable = config.get("min_samples_reliable", min_samples)
        forecast_horizon = config.get("forecast_horizon_seconds", 300.0)
        min_r2 = config.get("min_r_squared", 0.65)
        min_consistency = config.get("min_trend_consistency", 0.60)

        # 1. Validate sample sufficiency against preliminary threshold
        if not time_series or len(time_series) < min_samples_preliminary:
            return {
                "metric": metric_name,
                "status": PredictiveState.INSUFFICIENT_DATA,
                "trend": "unknown",
                "slope_per_second": 0.0,
                "r_squared": None,
                "confidence": "none",
                "confidence_score": 0.0,
                "sample_count": len(time_series) if time_series else 0,
                "current_value": round(float(time_series[-1][1]), 2) if time_series else 0.0,
                "time_to_warning_seconds": None,
                "time_to_critical_seconds": None,
                "time_to_saturation_seconds": None,
                "reason": f"Insufficient samples: have {len(time_series) if time_series else 0}, need at least {min_samples_preliminary} for preliminary trend analysis"
            }

        # Filter invalid or missing values
        clean_pts = []
        for pt in time_series:
            try:
                t = float(pt[0])
                y = float(pt[1])
                if np.isfinite(t) and np.isfinite(y):
                    clean_pts.append((t, y))
            except (ValueError, TypeError):
                continue

        if len(clean_pts) < min_samples_preliminary:
            return {
                "metric": metric_name,
                "status": PredictiveState.INSUFFICIENT_DATA,
                "trend": "unknown",
                "slope_per_second": 0.0,
                "r_squared": None,
                "confidence": "none",
                "confidence_score": 0.0,
                "sample_count": len(clean_pts),
                "current_value": round(float(clean_pts[-1][1]), 2) if clean_pts else 0.0,
                "time_to_warning_seconds": None,
                "time_to_critical_seconds": None,
                "time_to_saturation_seconds": None,
                "reason": f"Valid sample count {len(clean_pts)} below minimum preliminary threshold {min_samples_preliminary}"
            }

        t_vals = np.array([pt[0] for pt in clean_pts], dtype=float)
        y_vals = np.array([pt[1] for pt in clean_pts], dtype=float)

        t_rel = t_vals - t_vals[0]
        duration = float(t_rel[-1])
        if duration < 1.0:
            return {
                "metric": metric_name,
                "status": PredictiveState.INSUFFICIENT_DATA,
                "trend": "unknown",
                "slope_per_second": 0.0,
                "r_squared": None,
                "confidence": "none",
                "confidence_score": 0.0,
                "sample_count": len(clean_pts),
                "current_value": round(float(y_vals[-1]), 2),
                "time_to_warning_seconds": None,
                "time_to_critical_seconds": None,
                "time_to_saturation_seconds": None,
                "reason": f"Observation duration {duration:.2f}s too short to establish rate of change"
            }

        # 2. OLS Linear Regression: y = m*t + c
        try:
            slope, intercept = np.polyfit(t_rel, y_vals, 1)
        except Exception as e:
            return {
                "metric": metric_name,
                "status": PredictiveState.UNRELIABLE_PREDICTION,
                "trend": "unknown",
                "slope_per_second": 0.0,
                "r_squared": 0.0,
                "confidence": "low",
                "confidence_score": 0.0,
                "sample_count": len(clean_pts),
                "current_value": round(float(y_vals[-1]), 2),
                "time_to_warning_seconds": None,
                "time_to_critical_seconds": None,
                "time_to_saturation_seconds": None,
                "reason": f"Polynomial fit error: {str(e)}"
            }

        current_val = float(y_vals[-1])
        slope_per_sec = round(float(slope), 5)

        # 3. Goodness of Fit (R^2) - Correct unconstrained calculation
        y_pred = slope * t_rel + intercept
        ss_tot = float(np.sum((y_vals - np.mean(y_vals)) ** 2))
        ss_res = float(np.sum((y_vals - y_pred) ** 2))
        if ss_tot > 1e-6:
            # True R^2 can be negative when fit is worse than horizontal mean line
            r2 = round(float(1.0 - (ss_res / ss_tot)), 4)
        else:
            # Zero variance / constant values: no trend exists
            r2 = 0.0

        # 4. Trend consistency: fraction of positive delta steps
        deltas = np.diff(y_vals)
        positive_deltas = np.sum(deltas > 0)
        consistency = round(float(positive_deltas / len(deltas)), 3) if len(deltas) > 0 else 0.0

        # 5. Metric-Specific Minimum Trend / Noise Slope Threshold
        min_slope_cfg = config.get("min_slope", {}).get(metric_name, 0.01)
        if slope_per_sec <= min_slope_cfg:
            trend_str = "decreasing" if slope_per_sec < -min_slope_cfg else "stable"
            return {
                "metric": metric_name,
                "status": PredictiveState.NORMAL,
                "trend": trend_str,
                "slope_per_second": slope_per_sec,
                "r_squared": r2,
                "consistency": consistency,
                "confidence": "high" if len(clean_pts) >= min_samples_reliable else "medium",
                "confidence_score": 0.90 if len(clean_pts) >= min_samples_reliable else 0.60,
                "sample_count": len(clean_pts),
                "current_value": round(current_val, 2),
                "window_duration_seconds": round(duration, 1),
                "time_to_warning_seconds": None,
                "time_to_critical_seconds": None,
                "time_to_saturation_seconds": None,
                "reason": f"Metric trend is {trend_str} (slope {slope_per_sec:.5f}/s <= {min_slope_cfg} threshold for {metric_name})"
            }

        trend_str = "increasing"

        # 6. Time-to-threshold estimation (Strictly non-negative)
        def calc_time_to_t(thresh):
            if current_val >= thresh:
                return 0.0
            if slope_per_sec <= 0.0001:
                return None
            t_s = (thresh - current_val) / slope_per_sec
            return round(float(t_s), 1) if t_s >= 0 else None

        t_warning = calc_time_to_t(threshold_cfg.get("warning", 1e9))
        t_critical = calc_time_to_t(threshold_cfg.get("critical", 1e9))
        t_saturation = calc_time_to_t(threshold_cfg.get("saturation", 1e9))

        # 7. Multi-factor Confidence Calculation (bounded empirical index)
        # Components: sample sufficiency (0.25), R^2 fit (0.35), consistency (0.25), proximity factor (0.15)
        sample_factor = min(1.0, len(clean_pts) / float(min_samples_reliable))
        r2_factor = max(0.0, min(1.0, r2)) # Negative R^2 contributes 0.0
        consistency_factor = max(0.0, min(1.0, consistency))
        prox_target = threshold_cfg.get("warning", 100.0)
        proximity_factor = min(1.0, max(0.0, current_val / prox_target)) if prox_target > 0 else 0.5

        raw_conf = (
            0.25 * sample_factor +
            0.35 * r2_factor +
            0.25 * consistency_factor +
            0.15 * proximity_factor
        )
        conf_score = round(float(raw_conf), 3)

        if conf_score >= 0.75 and r2 >= min_r2 and consistency >= min_consistency and len(clean_pts) >= min_samples_reliable:
            confidence = "high"
        elif conf_score >= 0.50 and r2 >= (min_r2 * 0.75) and consistency >= 0.50 and len(clean_pts) >= min_samples_preliminary:
            confidence = "medium"
        else:
            confidence = "low"

        # 8. Unreliable trend check: poor R^2 or low consistency
        if (r2 < min_r2 or consistency < min_consistency) and confidence == "low":
            return {
                "metric": metric_name,
                "status": PredictiveState.UNRELIABLE_PREDICTION,
                "trend": trend_str,
                "slope_per_second": slope_per_sec,
                "r_squared": r2,
                "consistency": consistency,
                "confidence": confidence,
                "confidence_score": conf_score,
                "sample_count": len(clean_pts),
                "current_value": round(current_val, 2),
                "window_duration_seconds": round(duration, 1),
                "time_to_warning_seconds": t_warning,
                "time_to_critical_seconds": t_critical,
                "time_to_saturation_seconds": t_saturation,
                "reason": f"Trend is unreliable (R² {r2} < {min_r2} or consistency {consistency} < {min_consistency})"
            }

        # 9. Determine predictive state based on forecast horizons and sample maturity
        high_risk_horizon = config.get("high_risk_horizon_seconds", 120.0)
        sat_horizon = config.get("saturation_horizon_seconds", 60.0)
        is_preliminary = len(clean_pts) < min_samples_reliable

        if is_preliminary:
            # Preliminary sample window: cannot escalate to HIGH_RISK or PREDICTED_SATURATION
            if t_warning is not None and t_warning <= forecast_horizon and confidence in ("high", "medium"):
                state = PredictiveState.EARLY_WARNING
                reason = f"Preliminary early warning: warning threshold predicted in ~{t_warning}s (N={len(clean_pts)} < {min_samples_reliable}; awaiting mature baseline)"
            else:
                state = PredictiveState.NORMAL
                reason = f"Metric stable or threshold not predicted within {forecast_horizon}s horizon (preliminary window N={len(clean_pts)})"
        else:
            # Mature sample window: evaluate full failure prevention states
            # Check Saturation: critical/saturation threshold reached in <= saturation_horizon
            if (
                (t_saturation is not None and t_saturation <= sat_horizon) or
                (t_critical is not None and t_critical <= sat_horizon)
            ) and confidence in ("high", "medium"):
                state = PredictiveState.PREDICTED_SATURATION
                reason = f"Predicted critical saturation in ~{t_critical if t_critical is not None else t_saturation}s (slope: +{slope_per_sec}/s, R²: {r2})"
            # Check High Risk: critical reached in <= high_risk_horizon or warning in <= 60s
            elif (
                (t_critical is not None and t_critical <= high_risk_horizon) or
                (t_warning is not None and t_warning <= 60.0)
            ) and confidence in ("high", "medium"):
                state = PredictiveState.HIGH_RISK
                reason = f"Approaching threshold within {t_warning if t_warning is not None else t_critical}s (slope: +{slope_per_sec}/s, R²: {r2})"
            # Check Early Warning: warning reached within forecast_horizon
            elif t_warning is not None and t_warning <= forecast_horizon and confidence in ("high", "medium"):
                state = PredictiveState.EARLY_WARNING
                reason = f"Steady upward trend: warning threshold predicted in ~{t_warning}s (slope: +{slope_per_sec}/s, R²: {r2})"
            else:
                state = PredictiveState.NORMAL
                reason = f"Metric stable or threshold not predicted within {forecast_horizon}s horizon"

        return {
            "metric": metric_name,
            "status": state,
            "trend": trend_str,
            "slope_per_second": slope_per_sec,
            "r_squared": r2,
            "consistency": consistency,
            "confidence": confidence,
            "confidence_score": conf_score,
            "sample_count": len(clean_pts),
            "current_value": round(current_val, 2),
            "window_duration_seconds": round(duration, 1),
            "time_to_warning_seconds": t_warning,
            "time_to_critical_seconds": t_critical,
            "time_to_saturation_seconds": t_saturation,
            "reason": reason
        }


class PredictionEngine:
    """
    Central Predictive Failure Prevention Engine (Phase 5.4).
    Coordinates trend estimation across CPU, Memory, Latency, and Error Rate.
    Evaluates predictions against subsequent observations to compute Precision, Recall, and F1.
    Integrates safe preventive traffic control with Phase 5.2 TrafficManager.
    """

    def __init__(self, target_services=None, redis_client=None, traffic_manager=None, config=None):
        self.target_services = target_services or ["app1", "app2", "app3", "app4", "app5"]
        self.redis_client = redis_client
        self.traffic_manager = traffic_manager
        self.config = dict(DEFAULT_PREDICTION_CONFIG)
        if config:
            self.config.update(config)

        self.lock = threading.RLock()

        # In-memory server predictive states
        self.server_predictions = {}
        for srv in self.target_services:
            self.server_predictions[srv] = {
                "server": srv,
                "predictive_state": PredictiveState.INSUFFICIENT_DATA,
                "previous_predictive_state": PredictiveState.INSUFFICIENT_DATA,
                "active_metric_warning": None,
                "metrics": {},
                "sample_count": 0,
                "confidence": "none",
                "confidence_score": 0.0,
                "consecutive_normal_cycles": 0,
                "consecutive_warning_cycles": 0,
                "last_action_time": 0.0,
                "last_action": "NONE",
                "last_action_reason": "Insufficient observation samples for predictive evaluation",
                "preventive_weight": 100,
                "last_evaluated_timestamp": None,
                "explanation": "Collecting initial historical telemetry baselines (0 samples). Insufficient observation samples for trend regression (need at least 6 samples)."
            }

        # Active predictions undergoing evaluation
        # Map: prediction_id -> prediction_record
        self.active_predictions = {}

        # Historical evaluated predictions (bounded)
        self.evaluation_history = []

        # Deduplication tracker: (server, metric, state) -> last_generated_epoch
        self.recent_prediction_signatures = {}

        # Accuracy summary metrics
        self.accuracy_metrics = {
            "true_positives": 0,
            "false_positives": 0,
            "true_negatives": 0,
            "false_negatives": 0,
            "total_evaluated": 0,
            "pending_evaluations": 0,
            "precision": 0.0,
            "recall": 0.0,
            "f1_score": 0.0,
            "false_positive_rate": 0.0,
            "accuracy": 0.0,
            "average_lead_time_seconds": 0.0,
            "lead_times": []
        }
        self.live_accuracy = dict(self.accuracy_metrics)
        self.synthetic_accuracy = dict(self.accuracy_metrics)

        # Load persisted config/history from Redis if present
        self._load_from_redis()

    def _load_from_redis(self):
        if not self.redis_client:
            return
        try:
            cfg_raw = self.redis_client.get("prediction:config")
            if cfg_raw:
                loaded = json.loads(cfg_raw)
                self.config.update(loaded)

            acc_raw = self.redis_client.get("prediction:accuracy")
            if acc_raw:
                self.accuracy_metrics.update(json.loads(acc_raw))
        except Exception:
            pass

    def _persist_to_redis(self):
        if not self.redis_client:
            return
        try:
            cluster_status = self.get_cluster_status()
            self.redis_client.set("prediction:status", json.dumps(cluster_status))
            self.redis_client.set("prediction:accuracy", json.dumps(self.get_accuracy_metrics()))
            self.redis_client.set("prediction:config", json.dumps(self.config))
        except Exception:
            pass

    def get_config(self):
        with self.lock:
            return dict(self.config)

    def update_config(self, new_config):
        with self.lock:
            # Validate input parameters
            if not isinstance(new_config, dict):
                return {"success": False, "error": "Configuration must be a JSON object"}

            for key in ["min_samples", "forecast_horizon_seconds", "min_r_squared", "min_trend_consistency"]:
                if key in new_config:
                    val = new_config[key]
                    if not isinstance(val, (int, float)) or val <= 0:
                        return {"success": False, "error": f"Invalid value for {key}: must be positive number"}

            if "min_r_squared" in new_config and (new_config["min_r_squared"] < 0 or new_config["min_r_squared"] > 1.0):
                return {"success": False, "error": "min_r_squared must be between 0.0 and 1.0"}

            if "predictive_traffic_enabled" in new_config:
                self.config["predictive_traffic_enabled"] = bool(new_config["predictive_traffic_enabled"])

            self.config.update(new_config)
            self._persist_to_redis()
            return {"success": True, "config": dict(self.config)}

    def set_traffic_control_enabled(self, enabled=True):
        with self.lock:
            self.config["predictive_traffic_enabled"] = bool(enabled)
            self._persist_to_redis()
            return {
                "success": True,
                "predictive_traffic_enabled": self.config["predictive_traffic_enabled"],
                "message": f"Predictive traffic management {'enabled' if enabled else 'disabled'}."
            }

    # =========================================================================
    # CORE PREDICTION EVALUATION
    # =========================================================================

    def evaluate_server(self, server, history_samples, current_metric=None, current_traffic=None, current_epoch=None):
        """
        Evaluates trend forecasts across CPU, Memory, Latency, and Error Rate for one server.
        Updates active predictions and generates explainability text.
        """
        now = current_epoch if current_epoch is not None else time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        if not history_samples:
            history_samples = []

        # Extract time series for the 4 metrics
        cpu_ts = []
        mem_ts = []
        lat_ts = []
        err_ts = []

        for pt in history_samples:
            if not isinstance(pt, dict):
                continue
            ts_str = pt.get("timestamp")
            if not ts_str:
                continue
            try:
                # Parse timestamp to epoch seconds
                if isinstance(ts_str, (int, float)):
                    ep = float(ts_str)
                else:
                    ep = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue

            cpu_val = pt.get("cpu_percent")
            if cpu_val is not None:
                cpu_ts.append((ep, float(cpu_val)))

            mem_val = pt.get("memory_mb")
            if mem_val is not None:
                mem_ts.append((ep, float(mem_val)))

            lat_val = pt.get("latency_ms")
            if lat_val is not None:
                lat_ts.append((ep, float(lat_val)))

            err_val = pt.get("error_rate_pct")
            if err_val is None and pt.get("status_code") is not None:
                # 0 if 200, 100 if error
                err_val = 100.0 if int(pt["status_code"]) != 200 else 0.0
            if err_val is not None:
                err_ts.append((ep, float(err_val)))

        # Append current metric if passed
        if current_metric and isinstance(current_metric, dict):
            if current_metric.get("cpu_percent") is not None:
                cpu_ts.append((now, float(current_metric["cpu_percent"])))
            if current_metric.get("memory_mb") is not None:
                mem_ts.append((now, float(current_metric["memory_mb"])))
            if current_metric.get("latency_ms") is not None:
                lat_ts.append((now, float(current_metric["latency_ms"])))
            err_now = current_traffic.get("error_rate_pct") if current_traffic else None
            if err_now is not None:
                err_ts.append((now, float(err_now)))

        thresh_cfg = self.config.get("thresholds", DEFAULT_PREDICTION_CONFIG["thresholds"])

        # Run trend analysis on each metric
        cpu_res = MetricTrendAnalyzer.analyze_metric(cpu_ts, "cpu_percent", thresh_cfg.get("cpu_percent", {}), self.config)
        mem_res = MetricTrendAnalyzer.analyze_metric(mem_ts, "memory_mb", thresh_cfg.get("memory_mb", {}), self.config)
        lat_res = MetricTrendAnalyzer.analyze_metric(lat_ts, "latency_ms", thresh_cfg.get("latency_ms", {}), self.config)
        err_res = MetricTrendAnalyzer.analyze_metric(err_ts, "error_rate_pct", thresh_cfg.get("error_rate_pct", {}), self.config)

        metric_results = {
            "cpu_percent": cpu_res,
            "memory_mb": mem_res,
            "latency_ms": lat_res,
            "error_rate_pct": err_res
        }

        min_preliminary = self.config.get("min_samples_preliminary", 6)
        min_reliable = self.config.get("min_samples_reliable", 15)

        # Track maximum valid sample count observed across metrics for this server
        max_samples = 0
        for m_name, res in metric_results.items():
            sc = res.get("sample_count", 0)
            if sc > max_samples:
                max_samples = sc

        if max_samples < min_preliminary:
            chosen_state = PredictiveState.INSUFFICIENT_DATA
            chosen_metric = None
        else:
            # Determine aggregate server predictive risk state (worst-case priority)
            # Priority: PREDICTED_SATURATION > HIGH_RISK > EARLY_WARNING > UNRELIABLE_PREDICTION > INSUFFICIENT_DATA > NORMAL
            state_priority = {
                PredictiveState.PREDICTED_SATURATION: 5,
                PredictiveState.HIGH_RISK: 4,
                PredictiveState.EARLY_WARNING: 3,
                PredictiveState.UNRELIABLE_PREDICTION: 2,
                PredictiveState.INSUFFICIENT_DATA: 1,
                PredictiveState.NORMAL: 0
            }

            chosen_state = PredictiveState.NORMAL
            chosen_metric = None
            max_p = -1

            for m_name, res in metric_results.items():
                st = res.get("status", PredictiveState.NORMAL)
                p = state_priority.get(st, 0)
                if p > max_p:
                    max_p = p
                    chosen_state = st
                    chosen_metric = m_name

        srv_rec = self.server_predictions[server]
        srv_rec["previous_predictive_state"] = srv_rec["predictive_state"]
        srv_rec["predictive_state"] = chosen_state
        srv_rec["sample_count"] = max_samples
        srv_rec["active_metric_warning"] = chosen_metric if chosen_state not in (PredictiveState.NORMAL, PredictiveState.INSUFFICIENT_DATA) else None
        srv_rec["metrics"] = metric_results
        srv_rec["last_evaluated_timestamp"] = now_iso

        # Build Explainability Text
        if chosen_state in (PredictiveState.EARLY_WARNING, PredictiveState.HIGH_RISK, PredictiveState.PREDICTED_SATURATION):
            srv_rec["consecutive_warning_cycles"] += 1
            srv_rec["consecutive_normal_cycles"] = 0
            m_info = metric_results[chosen_metric]
            m_label = chosen_metric.replace("_", " ").upper()
            cur_v = m_info.get("current_value")
            t_warn = m_info.get("time_to_warning_seconds")
            t_crit = m_info.get("time_to_critical_seconds")
            conf = m_info.get("confidence")
            conf_score = m_info.get("confidence_score")
            slope = m_info.get("slope_per_second")
            r2 = m_info.get("r_squared")

            srv_rec["confidence"] = conf
            srv_rec["confidence_score"] = conf_score

            if chosen_state == PredictiveState.PREDICTED_SATURATION:
                t_val = t_crit if t_crit is not None else t_warn
                srv_rec["explanation"] = (
                    f"CRITICAL WARNING: {m_label} is increasing rapidly (+{slope}/s, R²={r2}) and currently at {cur_v}. "
                    f"Predicted to reach saturation threshold in ~{t_val} seconds. Confidence: {conf}. "
                    f"Immediate preventive traffic reduction advised."
                )
            elif chosen_state == PredictiveState.HIGH_RISK:
                t_val = t_crit if t_crit is not None else t_warn
                srv_rec["explanation"] = (
                    f"HIGH RISK: {m_label} exhibits sustained upward trajectory (+{slope}/s, R²={r2}) currently at {cur_v}. "
                    f"Approaching threshold within ~{t_val} seconds. Confidence: {conf}. "
                    f"Controlled preventive weight reduction permitted."
                )
            else:
                srv_rec["explanation"] = (
                    f"EARLY WARNING: {m_label} has sustained positive growth (+{slope}/s, R²={r2}) currently at {cur_v}. "
                    f"Warning threshold may be reached in approximately {t_warn} seconds. Confidence: {conf}. "
                    f"Continuous observation maintained; no immediate traffic alteration."
                )

            # Record Prediction for Evaluation (Deduplicated within 30s)
            sig = (server, chosen_metric, chosen_state)
            last_sig_time = self.recent_prediction_signatures.get(sig, 0.0)
            if (now - last_sig_time) > 30.0:
                self.recent_prediction_signatures[sig] = now
                self._record_prediction_for_evaluation(
                    server=server,
                    metric=chosen_metric,
                    predictive_state=chosen_state,
                    metric_info=m_info,
                    now=now,
                    now_iso=now_iso
                )

        elif chosen_state == PredictiveState.INSUFFICIENT_DATA:
            srv_rec["consecutive_warning_cycles"] = 0
            srv_rec["consecutive_normal_cycles"] = 0
            srv_rec["confidence"] = "none"
            srv_rec["confidence_score"] = 0.0
            srv_rec["explanation"] = f"Collecting initial historical telemetry baselines ({max_samples} samples). Insufficient observation samples for trend regression (need at least {min_preliminary} samples)."
        elif chosen_state == PredictiveState.UNRELIABLE_PREDICTION:
            srv_rec["consecutive_warning_cycles"] = 0
            srv_rec["consecutive_normal_cycles"] = 0
            srv_rec["confidence"] = "low"
            srv_rec["confidence_score"] = 0.20
            srv_rec["explanation"] = "Fluctuations or transient anomalies detected, but linear goodness of fit (R²) is insufficient to warrant a predictive alert."
        else:
            srv_rec["consecutive_warning_cycles"] = 0
            srv_rec["consecutive_normal_cycles"] += 1
            srv_rec["confidence"] = "medium" if max_samples < min_reliable else "high"
            srv_rec["confidence_score"] = 0.60 if max_samples < min_reliable else 0.90
            srv_rec["explanation"] = "Operating within nominal predictive baselines. All resource consumption metrics stable or below warning thresholds."

        # Update evaluation outcomes using latest actual metric observations
        if current_metric:
            self._update_prediction_evaluations(server, current_metric, now)

        return dict(srv_rec)

    def _record_prediction_for_evaluation(self, server, metric, predictive_state, metric_info, now, now_iso):
        """Records a structured forecast to be evaluated against subsequent observations."""
        pred_id = f"pred-{uuid.uuid4().hex[:8]}"
        thresh_cfg = self.config.get("thresholds", {}).get(metric, {})
        target_thresh = thresh_cfg.get("warning", 100.0)
        time_to_t = metric_info.get("time_to_warning_seconds") or metric_info.get("time_to_critical_seconds") or 300.0
        horizon = self.config.get("forecast_horizon_seconds", 300.0)

        record = {
            "prediction_id": pred_id,
            "server": server,
            "metric": metric,
            "predictive_state": predictive_state,
            "current_value": metric_info.get("current_value"),
            "predicted_threshold": target_thresh,
            "predicted_time_to_threshold": time_to_t,
            "forecast_horizon": horizon,
            "confidence": metric_info.get("confidence"),
            "confidence_score": metric_info.get("confidence_score"),
            "slope_per_second": metric_info.get("slope_per_second"),
            "r_squared": metric_info.get("r_squared"),
            "prediction_timestamp": now_iso,
            "prediction_epoch": now,
            "evaluation_deadline_epoch": now + min(horizon, max(30.0, time_to_t * 1.5)),
            "actual_subsequent_values": [],
            "threshold_reached": False,
            "is_correct": None,
            "lead_time": None,
            "evaluation_state": EvaluationOutcome.PENDING
        }

        self.active_predictions[pred_id] = record
        self.accuracy_metrics["pending_evaluations"] = len(self.active_predictions)

    def _update_prediction_evaluations(self, server, current_metric, now):
        """
        Compares ongoing real metric observations against active predictions.
        Transitions PENDING forecasts to TRUE_POSITIVE or FALSE_POSITIVE when verified or expired.
        """
        completed_ids = []

        for pred_id, pred in list(self.active_predictions.items()):
            if pred["server"] != server:
                continue

            metric_name = pred["metric"]
            val = current_metric.get(metric_name)
            if val is None:
                continue

            val = float(val)
            pred["actual_subsequent_values"].append(round(val, 2))
            if len(pred["actual_subsequent_values"]) > 30:
                pred["actual_subsequent_values"].pop(0)

            target = pred["predicted_threshold"]

            # Did the metric breach the predicted threshold?
            if val >= target:
                lead_time = round(now - pred["prediction_epoch"], 1)
                pred["threshold_reached"] = True
                pred["is_correct"] = True
                pred["lead_time"] = lead_time
                pred["evaluation_state"] = EvaluationOutcome.TRUE_POSITIVE
                completed_ids.append(pred_id)
                self.live_accuracy["true_positives"] += 1
                self.live_accuracy["lead_times"].append(lead_time)

            # Did the evaluation window expire without reaching the threshold?
            elif now >= pred["evaluation_deadline_epoch"]:
                pred["threshold_reached"] = False
                pred["is_correct"] = False
                pred["lead_time"] = None
                pred["evaluation_state"] = EvaluationOutcome.FALSE_POSITIVE
                completed_ids.append(pred_id)
                self.live_accuracy["false_positives"] += 1

        for pid in completed_ids:
            record = self.active_predictions.pop(pid)
            self.evaluation_history.append(record)
            if len(self.evaluation_history) > 100:
                self.evaluation_history.pop(0)

        self._recalculate_accuracy_metrics()

    def record_manual_evaluation_sample(self, outcome_type, lead_time=None):
        """Helper for synthetic test harness to register evaluated outcomes."""
        with self.lock:
            if outcome_type == EvaluationOutcome.TRUE_POSITIVE:
                self.synthetic_accuracy["true_positives"] += 1
                if lead_time is not None:
                    self.synthetic_accuracy["lead_times"].append(float(lead_time))
            elif outcome_type == EvaluationOutcome.FALSE_POSITIVE:
                self.synthetic_accuracy["false_positives"] += 1
            elif outcome_type == EvaluationOutcome.TRUE_NEGATIVE:
                self.synthetic_accuracy["true_negatives"] += 1
            elif outcome_type == EvaluationOutcome.FALSE_NEGATIVE:
                self.synthetic_accuracy["false_negatives"] += 1

            self._recalculate_accuracy_metrics()

    def _compute_metrics_dict(self, target_dict, pending_count=0):
        tp = target_dict["true_positives"]
        fp = target_dict["false_positives"]
        tn = target_dict["true_negatives"]
        fn = target_dict["false_negatives"]
        total = tp + fp + tn + fn

        target_dict["total_evaluated"] = total
        target_dict["pending_evaluations"] = pending_count

        if total == 0:
            target_dict["precision"] = None
            target_dict["recall"] = None
            target_dict["f1_score"] = None
            target_dict["false_positive_rate"] = None
            target_dict["accuracy"] = None
            target_dict["average_lead_time_seconds"] = 0.0
            return

        # Precision = TP / (TP + FP)
        prec = (tp / (tp + fp)) if (tp + fp) > 0 else (0.0 if fn > 0 or tn > 0 else None)
        # Recall = TP / (TP + FN)
        rec = (tp / (tp + fn)) if (tp + fn) > 0 else (1.0 if total > 0 and fn == 0 and tp > 0 else 0.0)
        # F1 = 2 * (Prec * Rec) / (Prec + Rec)
        if prec is not None and rec is not None and (prec + rec) > 0:
            f1 = (2.0 * prec * rec / (prec + rec))
        else:
            f1 = 0.0
        # FPR = FP / (FP + TN)
        fpr = (fp / (fp + tn)) if (fp + tn) > 0 else 0.0
        # Accuracy = (TP + TN) / Total
        acc = ((tp + tn) / total) if total > 0 else 0.0

        leads = target_dict.get("lead_times", [])
        avg_lead = round(float(np.mean(leads)), 2) if leads else 0.0

        target_dict["precision"] = round(float(prec), 3) if prec is not None else None
        target_dict["recall"] = round(float(rec), 3) if rec is not None else None
        target_dict["f1_score"] = round(float(f1), 3)
        target_dict["false_positive_rate"] = round(float(fpr), 3)
        target_dict["accuracy"] = round(float(acc), 3)
        target_dict["average_lead_time_seconds"] = avg_lead

    def _recalculate_accuracy_metrics(self):
        self._compute_metrics_dict(self.live_accuracy, len(self.active_predictions))
        self._compute_metrics_dict(self.synthetic_accuracy, 0)
        active = self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy
        for k in self.accuracy_metrics:
            if k == "pending_evaluations":
                self.accuracy_metrics[k] = len(self.active_predictions)
            elif k in active:
                self.accuracy_metrics[k] = active[k] if active[k] is not None else 0.0

    # =========================================================================
    # PREDICTIVE TRAFFIC CONTROL (PHASE 5.2 INTEGRATION)
    # =========================================================================

    def apply_predictive_traffic_policy(self, server, hp_stats=None, server_metrics=None):
        """
        Conservative, safe traffic weight adjustment based on predictive risk.
        Enforces all Phase 5.2 safeguards: minimum eligible backends, cooldowns,
        and never interferes with Phase 5.3 recovery or Phase 5.2 active exclusions.
        """
        with self.lock:
            srv_pred = self.server_predictions.get(server)
            if not srv_pred:
                return {"action": "NONE", "reason": f"Unknown server {server}"}

            pred_state = srv_pred["predictive_state"]
            now = time.time()
            cooldown = self.config.get("action_cooldown_seconds", 15.0)

            # 1. Master enable/disable switch
            if not self.config.get("predictive_traffic_enabled", False):
                return {
                    "action": "NONE",
                    "reason": "Predictive traffic management is globally disabled",
                    "predictive_state": pred_state
                }

            # 2. Check if TrafficManager is available
            if not self.traffic_manager:
                return {"action": "NONE", "reason": "TrafficManager not configured"}

            # 3. Check server operational status in TrafficManager
            tm_srv = self.traffic_manager.get_server_status(server)
            if not tm_srv:
                return {"action": "NONE", "reason": "Server not registered in TrafficManager"}

            # If server is already excluded, in maintenance, or undergoing recovery -> DO NOT TOUCH
            if tm_srv.get("in_maint", False) or tm_srv.get("current_state") == "EXCLUDED":
                return {
                    "action": "NONE",
                    "reason": f"Server {server} currently excluded/in maintenance by operational controller; predictive action suppressed"
                }

            # Check cooldown
            if (now - srv_pred.get("last_action_time", 0.0)) < cooldown:
                return {
                    "action": "NONE",
                    "reason": f"Server {server} in predictive action cooldown ({round(cooldown - (now - srv_pred['last_action_time']), 1)}s remaining)"
                }

            # Safeguard: Insufficient data cannot trigger or alter traffic policies
            if pred_state == PredictiveState.INSUFFICIENT_DATA:
                return {
                    "action": "NONE",
                    "reason": f"INSUFFICIENT_DATA: server {server} has insufficient telemetry ({srv_pred.get('sample_count', 0)} samples); cannot alter traffic weights",
                    "predictive_state": pred_state
                }

            cur_weight = tm_srv.get("current_weight", 100)

            # 4. NORMAL: Safe restoration back to 100 after 3 consecutive normal cycles
            if pred_state == PredictiveState.NORMAL:
                if cur_weight < 100 and srv_pred.get("consecutive_normal_cycles", 0) >= self.config.get("consecutive_healthy_required", 3):
                    min_samples_traffic = self.config.get("min_samples_traffic_action", 20)
                    if srv_pred.get("sample_count", 0) < min_samples_traffic:
                        return {"action": "NONE", "reason": f"Insufficient sample baseline for weight restoration"}
                    # Verify health endpoint before restoring
                    if self.traffic_manager.haproxy_client:
                        res = self.traffic_manager.haproxy_client.set_server_weight(server, 100)
                        if res.get("success"):
                            tm_srv["current_weight"] = 100
                            srv_pred["preventive_weight"] = 100
                            srv_pred["last_action_time"] = now
                            srv_pred["last_action"] = "PREDICTIVE_RESTORED"
                            srv_pred["last_action_reason"] = "Restored nominal weight 100 after sustained normal predictive observations"
                            self.traffic_manager._log_action(server, "PREDICTIVE_RESTORED", srv_pred["last_action_reason"], {"weight": 100})
                            return {"action": "PREDICTIVE_RESTORED", "weight": 100, "success": True}
                return {"action": "NONE", "reason": "Operating normal; no predictive action required"}

            # 5. EARLY_WARNING: Observation only; no traffic weight reduction
            if pred_state == PredictiveState.EARLY_WARNING:
                return {
                    "action": "NONE",
                    "reason": "EARLY_WARNING policy: active observation maintained without modifying traffic weights",
                    "predictive_state": pred_state
                }

            # 6. Check minimum samples required before preventive traffic actions are permitted
            min_samples_traffic = self.config.get("min_samples_traffic_action", 20)
            sample_cnt = srv_pred.get("sample_count", 0)
            if sample_cnt < min_samples_traffic:
                return {
                    "action": "ACTION_REJECTED",
                    "reason": f"Sample count {sample_cnt} below min_samples_traffic_action ({min_samples_traffic}); preventive traffic alteration prohibited"
                }

            # 6. HIGH_RISK: Controlled reduction to 75
            target_weight = None
            action_name = None
            if pred_state == PredictiveState.HIGH_RISK:
                target_weight = self.config.get("predictive_reduced_weight_high_risk", 75)
                action_name = "PREDICTIVE_WEIGHT_REDUCED"
                action_reason = f"Predictive HIGH_RISK: preventive weight reduction to {target_weight} to avert resource exhaustion"

            # 7. PREDICTED_SATURATION: Preventive reduction to 50 (NEVER place into maintenance solely on forecast)
            elif pred_state == PredictiveState.PREDICTED_SATURATION:
                target_weight = self.config.get("predictive_reduced_weight_saturation", 50)
                action_name = "PREDICTIVE_SATURATION_WEIGHT_REDUCED"
                action_reason = f"Predictive SATURATION: preventive weight reduction to {target_weight} to protect backend stability"

            if target_weight is not None and cur_weight > target_weight:
                # Minimum Eligible Backend Safeguard
                current_eligible = self.traffic_manager.count_healthy_eligible(hp_stats)
                if current_eligible < 2:
                    return {
                        "action": "ACTION_REJECTED",
                        "reason": f"Minimum eligible backend safeguard: cannot reduce weight on {server} (only {current_eligible} eligible backends remain)"
                    }

                if self.traffic_manager.haproxy_client:
                    res = self.traffic_manager.haproxy_client.set_server_weight(server, target_weight)
                    if res.get("success"):
                        tm_srv["current_weight"] = target_weight
                        srv_pred["preventive_weight"] = target_weight
                        srv_pred["last_action_time"] = now
                        srv_pred["last_action"] = action_name
                        srv_pred["last_action_reason"] = action_reason
                        self.traffic_manager._log_action(server, action_name, action_reason, {
                            "old_weight": cur_weight,
                            "new_weight": target_weight,
                            "predictive_state": pred_state
                        })
                        return {"action": action_name, "weight": target_weight, "success": True}

            return {"action": "NONE", "reason": f"No change needed (current weight {cur_weight} <= target {target_weight})"}

    # =========================================================================
    # CLUSTER EVALUATION & SUMMARY API
    # =========================================================================

    def evaluate_cluster(self, server_metrics_dict, history_dict=None, current_traffic=None, hp_stats=None):
        """
        Evaluates all target application servers across historical telemetry.
        Executes safe predictive traffic management when enabled.
        Returns full cluster predictive status.
        """
        now = time.time()
        results = {}

        for srv in self.target_services:
            srv_metric = server_metrics_dict.get(srv, {})
            srv_history = (history_dict.get(srv) if history_dict else None) or []
            res = self.evaluate_server(srv, srv_history, current_metric=srv_metric, current_traffic=current_traffic, current_epoch=now)
            results[srv] = res

            # Apply predictive traffic policy if enabled
            if self.config.get("predictive_traffic_enabled", False):
                try:
                    self.apply_predictive_traffic_policy(srv, hp_stats=hp_stats, server_metrics=server_metrics_dict)
                except Exception as trf_err:
                    print(f"[PredictionEngine] Traffic policy error for {srv}: {trf_err}")

        self._persist_to_redis()
        return self.get_cluster_status()

    def get_cluster_status(self):
        with self.lock:
            counts = {
                PredictiveState.NORMAL: 0,
                PredictiveState.EARLY_WARNING: 0,
                PredictiveState.HIGH_RISK: 0,
                PredictiveState.PREDICTED_SATURATION: 0,
                PredictiveState.INSUFFICIENT_DATA: 0,
                PredictiveState.UNRELIABLE_PREDICTION: 0
            }

            active_warnings = []
            for srv, data in self.server_predictions.items():
                st = data.get("predictive_state", PredictiveState.NORMAL)
                counts[st] = counts.get(st, 0) + 1
                if st in (PredictiveState.EARLY_WARNING, PredictiveState.HIGH_RISK, PredictiveState.PREDICTED_SATURATION):
                    active_warnings.append({
                        "server": srv,
                        "predictive_state": st,
                        "metric": data.get("active_metric_warning"),
                        "explanation": data.get("explanation"),
                        "preventive_weight": data.get("preventive_weight", 100)
                    })

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "predictive_traffic_enabled": self.config.get("predictive_traffic_enabled", False),
                "total_servers": len(self.target_services),
                "counts": counts,
                "active_warnings": active_warnings,
                "servers": {srv: dict(d) for srv, d in self.server_predictions.items()},
                "accuracy_summary": {
                    "precision": (self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy)["precision"] or 0.0,
                    "recall": (self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy)["recall"] or 0.0,
                    "f1_score": (self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy)["f1_score"] or 0.0,
                    "total_evaluated": (self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy)["total_evaluated"],
                    "pending_evaluations": len(self.active_predictions),
                    "average_lead_time_seconds": (self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy)["average_lead_time_seconds"]
                }
            }

    def get_server_prediction(self, server):
        with self.lock:
            if server in self.server_predictions:
                return dict(self.server_predictions[server])
            return None

    def get_history(self, limit=50):
        with self.lock:
            combined = list(self.active_predictions.values()) + list(self.evaluation_history)
            combined.sort(key=lambda x: x.get("prediction_epoch", 0), reverse=True)
            return combined[:limit]

    def get_accuracy_metrics(self):
        with self.lock:
            active = self.live_accuracy if self.live_accuracy["total_evaluated"] > 0 else self.synthetic_accuracy
            return {
                "live": dict(self.live_accuracy),
                "synthetic": dict(self.synthetic_accuracy),
                "combined": {
                    "total_evaluated": self.live_accuracy["total_evaluated"] + self.synthetic_accuracy["total_evaluated"],
                    "true_positives": self.live_accuracy["true_positives"] + self.synthetic_accuracy["true_positives"],
                    "false_positives": self.live_accuracy["false_positives"] + self.synthetic_accuracy["false_positives"],
                    "true_negatives": self.live_accuracy["true_negatives"] + self.synthetic_accuracy["true_negatives"],
                    "false_negatives": self.live_accuracy["false_negatives"] + self.synthetic_accuracy["false_negatives"],
                    "pending_evaluations": len(self.active_predictions)
                },
                "true_positives": active["true_positives"],
                "false_positives": active["false_positives"],
                "true_negatives": active["true_negatives"],
                "false_negatives": active["false_negatives"],
                "total_evaluated": active["total_evaluated"],
                "pending_evaluations": len(self.active_predictions),
                "precision": active["precision"] if active["precision"] is not None else 0.0,
                "recall": active["recall"] if active["recall"] is not None else 0.0,
                "f1_score": active["f1_score"] if active["f1_score"] is not None else 0.0,
                "false_positive_rate": active["false_positive_rate"] if active["false_positive_rate"] is not None else 0.0,
                "accuracy": active["accuracy"] if active["accuracy"] is not None else 0.0,
                "average_lead_time_seconds": active["average_lead_time_seconds"]
            }
