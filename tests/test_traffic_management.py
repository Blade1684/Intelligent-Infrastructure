"""
Unit and Integration Tests for Phase 5.2: Intelligent Traffic Management and Dynamic Backend Control
Covers all 20 requirements specified in Phase 5.2 prompt:
 1. NORMAL causes no traffic-management action.
 2. HIGH_TRAFFIC_HEALTHY causes no action.
 3. EARLY_WARNING causes no exclusion.
 4. Confirmed OVERLOAD triggers the configured policy.
 5. CRITICAL triggers appropriate exclusion when justified.
 6. OFFLINE is handled through HAProxy health checks.
 7. Weight reduction works.
 8. Original weight is restored after recovery.
 9. Temporary exclusion works.
10. Re-admission requires consecutive healthy observations.
11. Duplicate actions are suppressed.
12. Cooldowns prevent repeated intervention.
13. At least one healthy backend always remains eligible.
14. HAProxy API failure is handled safely.
15. Redis failure does not crash the monitor.
16. Invalid HAProxy commands are rejected.
17. Failed actions trigger rollback or a safe state.
18. Multiple simultaneous overloaded backends are handled safely.
19. Existing Phase 1–4 functionality remains operational.
20. Phase 5.1 classification remains accurate and unchanged.
"""

import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add paths to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from traffic_manager import (
        TrafficManager,
        TrafficState,
        TrafficAction,
        DEFAULT_TRAFFIC_CONFIG
    )
    from haproxy_client import HAProxyClient
    from overload_detector import OverloadState, ServerOverloadDetector, TrafficOverloadEngine
except ImportError:
    from monitor.traffic_manager import (
        TrafficManager,
        TrafficState,
        TrafficAction,
        DEFAULT_TRAFFIC_CONFIG
    )
    from monitor.haproxy_client import HAProxyClient
    from monitor.overload_detector import OverloadState, ServerOverloadDetector, TrafficOverloadEngine


class TestPhase52TrafficManagement:

    @pytest.fixture
    def mock_haproxy(self):
        client = MagicMock(spec=HAProxyClient)
        # Default mock behaviour: successful weight and maint calls
        client.set_server_weight.return_value = {"success": True, "weight": 50}
        client.set_server_maintenance.return_value = {"success": True, "in_maint": True}
        client.get_server_weight.return_value = 100
        client.get_backends_status.return_value = {
            f"app{i}": {
                "server": f"app{i}",
                "is_up": True,
                "is_maint": False,
                "is_down": False,
                "status": "UP",
                "weight": 100
            } for i in range(1, 6)
        }
        return client

    @pytest.fixture
    def mock_redis(self):
        r = MagicMock()
        r.lpush.return_value = 1
        r.ltrim.return_value = True
        r.lrange.return_value = []
        r.set.return_value = True
        r.get.return_value = None
        return r

    @pytest.fixture
    def manager(self, mock_haproxy, mock_redis):
        # Disable cooldown for unit test deterministic stepping
        config = {
            "action_cooldown_seconds": 0.0,
            "consecutive_healthy_required": 3,
            "verify_health_endpoint": False  # mock direct HTTP
        }
        return TrafficManager(
            target_services=["app1", "app2", "app3", "app4", "app5"],
            haproxy_client=mock_haproxy,
            redis_client=mock_redis,
            config=config
        )

    # 1. NORMAL causes no traffic-management action
    def test_01_normal_causes_no_traffic_management_action(self, manager, mock_haproxy):
        status = {
            "cluster_state": "NORMAL",
            "servers": {
                f"app{i}": {"state": "NORMAL", "reason": "Nominal operations"}
                for i in range(1, 6)
            }
        }
        res = manager.evaluate_and_manage(status)
        for srv in manager.target_services:
            srv_status = res["servers"][srv]
            assert srv_status["current_state"] == TrafficState.NORMAL
            assert srv_status["current_weight"] == 100
            assert srv_status["in_maint"] is False
            assert srv_status["last_action"] == TrafficAction.NONE
        # HAProxy set methods should not have been called to alter state
        mock_haproxy.set_server_maintenance.assert_not_called()

    # 2. HIGH_TRAFFIC_HEALTHY causes no action
    def test_02_high_traffic_healthy_causes_no_action(self, manager, mock_haproxy):
        status = {
            "cluster_state": "HIGH_TRAFFIC_HEALTHY",
            "servers": {
                f"app{i}": {"state": "HIGH_TRAFFIC_HEALTHY", "reason": "Handling 25 RPS nominal"}
                for i in range(1, 6)
            }
        }
        res = manager.evaluate_and_manage(status)
        for srv in manager.target_services:
            srv_status = res["servers"][srv]
            assert srv_status["current_weight"] == 100
            assert srv_status["in_maint"] is False
        mock_haproxy.set_server_maintenance.assert_not_called()

    # 3. EARLY_WARNING causes no exclusion
    def test_03_early_warning_causes_no_exclusion(self, manager, mock_haproxy):
        status = {
            "cluster_state": "EARLY_WARNING",
            "servers": {
                "app1": {"state": "EARLY_WARNING", "reason": "Approaching CPU threshold"},
                "app2": {"state": "NORMAL"},
                "app3": {"state": "NORMAL"},
                "app4": {"state": "NORMAL"},
                "app5": {"state": "NORMAL"}
            }
        }
        res = manager.evaluate_and_manage(status)
        app1_status = res["servers"]["app1"]
        assert app1_status["in_maint"] is False
        assert app1_status["current_weight"] == 100
        assert "prepare traffic weight reduction" in app1_status["recommended_action"]
        mock_haproxy.set_server_maintenance.assert_not_called()

    # 4. Confirmed OVERLOAD triggers the configured policy (weight reduction)
    def test_04_confirmed_overload_triggers_configured_policy(self, manager, mock_haproxy):
        status = {
            "cluster_state": "OVERLOAD",
            "servers": {
                "app1": {"state": "OVERLOAD", "reason": "Confirmed CPU and latency overload"},
                "app2": {"state": "NORMAL"},
                "app3": {"state": "NORMAL"},
                "app4": {"state": "NORMAL"},
                "app5": {"state": "NORMAL"}
            }
        }
        res = manager.evaluate_and_manage(status)
        mock_haproxy.set_server_weight.assert_called_with("app1", 50)
        app1_status = res["servers"]["app1"]
        assert app1_status["current_state"] == TrafficState.WEIGHT_REDUCED
        assert app1_status["current_weight"] == 50
        assert app1_status["last_action"] == TrafficAction.WEIGHT_REDUCED

    # 5. CRITICAL triggers appropriate exclusion when justified
    def test_05_critical_triggers_appropriate_exclusion_when_justified(self, manager, mock_haproxy):
        status = {
            "cluster_state": "CRITICAL",
            "servers": {
                "app1": {"state": "CRITICAL", "reason": "Severe resource saturation"},
                "app2": {"state": "NORMAL"},
                "app3": {"state": "NORMAL"},
                "app4": {"state": "NORMAL"},
                "app5": {"state": "NORMAL"}
            }
        }
        res = manager.evaluate_and_manage(status)
        mock_haproxy.set_server_maintenance.assert_called_with("app1", in_maint=True)
        app1_status = res["servers"]["app1"]
        assert app1_status["current_state"] == TrafficState.EXCLUDED
        assert app1_status["in_maint"] is True
        assert app1_status["last_action"] == TrafficAction.TEMPORARILY_EXCLUDED

    # 6. OFFLINE is handled through HAProxy health checks
    def test_06_offline_is_handled_through_haproxy_health_checks(self, manager, mock_haproxy):
        status = {
            "cluster_state": "NORMAL",
            "servers": {
                "app1": {"state": "OFFLINE", "reason": "Container stopped"},
                "app2": {"state": "NORMAL"},
                "app3": {"state": "NORMAL"},
                "app4": {"state": "NORMAL"},
                "app5": {"state": "NORMAL"}
            }
        }
        res = manager.evaluate_and_manage(status)
        # Should NOT explicitly set maint mode; HAProxy active check handles it
        mock_haproxy.set_server_maintenance.assert_not_called()
        assert res["servers"]["app1"]["current_state"] == TrafficState.OFFLINE_DETECTED

    # 7. Weight reduction works
    def test_07_weight_reduction_works(self, manager, mock_haproxy):
        mock_haproxy.set_server_weight.return_value = {"success": True, "weight": 50}
        status = {
            "cluster_state": "OVERLOAD",
            "servers": {
                "app2": {"state": "OVERLOAD", "reason": "High latency spike"}
            }
        }
        res = manager.evaluate_and_manage(status)
        assert res["servers"]["app2"]["current_weight"] == 50
        assert res["summary"]["traffic_reduced_count"] == 1

    # 8. Original weight is restored after recovery
    def test_08_original_weight_is_restored_after_recovery(self, manager, mock_haproxy):
        # 1. Place in overload
        manager.evaluate_and_manage({
            "servers": {"app1": {"state": "OVERLOAD", "reason": "Overloaded"}}
        })
        assert manager.servers["app1"]["current_weight"] == 50

        # 2. Feed 3 consecutive NORMAL observations
        for i in range(3):
            manager.evaluate_and_manage({
                "servers": {"app1": {"state": "NORMAL", "reason": "Healthy"}}
            })

        # Original weight 100 must be restored
        assert manager.servers["app1"]["current_weight"] == 100
        assert manager.servers["app1"]["current_state"] == TrafficState.NORMAL
        assert manager.servers["app1"]["last_action"] == TrafficAction.READMISSION_RESTORED

    # 9. Temporary exclusion works
    def test_09_temporary_exclusion_works(self, manager, mock_haproxy):
        manager.evaluate_and_manage({
            "servers": {"app3": {"state": "CRITICAL", "reason": "High error rate"}}
        })
        assert manager.servers["app3"]["in_maint"] is True
        assert manager.servers["app3"]["current_state"] == TrafficState.EXCLUDED
        assert manager.get_cluster_status()["summary"]["excluded_count"] == 1

    # 10. Re-admission requires consecutive healthy observations
    def test_10_readmission_requires_consecutive_healthy_observations(self, manager, mock_haproxy):
        # Degrade app1 to EXCLUDED
        manager.evaluate_and_manage({
            "servers": {"app1": {"state": "CRITICAL", "reason": "Severe fault"}}
        })
        assert manager.servers["app1"]["in_maint"] is True

        # First clean cycle: should NOT restore yet
        manager.evaluate_and_manage({
            "servers": {"app1": {"state": "NORMAL", "reason": "Recovering 1"}}
        })
        assert manager.servers["app1"]["current_state"] == TrafficState.AWAITING_READMISSION
        assert manager.servers["app1"]["consecutive_healthy_cycles"] == 1
        assert manager.servers["app1"]["in_maint"] is True

        # Second clean cycle: still awaiting
        manager.evaluate_and_manage({
            "servers": {"app1": {"state": "NORMAL", "reason": "Recovering 2"}}
        })
        assert manager.servers["app1"]["consecutive_healthy_cycles"] == 2
        assert manager.servers["app1"]["in_maint"] is True

        # Interruption by an EARLY_WARNING resets recovery counter!
        manager.evaluate_and_manage({
            "servers": {"app1": {"state": "EARLY_WARNING", "reason": "Flap warning"}}
        })
        assert manager.servers["app1"]["consecutive_healthy_cycles"] == 0

        # Now require 3 clean cycles again
        for _ in range(2):
            manager.evaluate_and_manage({"servers": {"app1": {"state": "NORMAL"}}})
        assert manager.servers["app1"]["in_maint"] is True

        manager.evaluate_and_manage({"servers": {"app1": {"state": "NORMAL"}}})
        # 3rd clean cycle restores it!
        assert manager.servers["app1"]["in_maint"] is False
        assert manager.servers["app1"]["current_state"] == TrafficState.NORMAL

    # 11. Duplicate actions are suppressed
    def test_11_duplicate_actions_are_suppressed(self, manager, mock_haproxy):
        # First overload call triggers set_server_weight
        manager.evaluate_and_manage({"servers": {"app1": {"state": "OVERLOAD"}}})
        assert mock_haproxy.set_server_weight.call_count == 1

        # Second identical evaluation in OVERLOAD should NOT call HAProxy again
        manager.evaluate_and_manage({"servers": {"app1": {"state": "OVERLOAD"}}})
        assert mock_haproxy.set_server_weight.call_count == 1

    # 12. Cooldowns prevent repeated intervention
    def test_12_cooldowns_prevent_repeated_intervention(self, mock_haproxy, mock_redis):
        # Initialize manager with 15s cooldown
        mgr = TrafficManager(
            target_services=["app1", "app2", "app3", "app4", "app5"],
            haproxy_client=mock_haproxy,
            redis_client=mock_redis,
            config={"action_cooldown_seconds": 15.0, "verify_health_endpoint": False}
        )
        # Action 1 on app1
        mgr.evaluate_and_manage({"servers": {"app1": {"state": "OVERLOAD"}}})
        assert mock_haproxy.set_server_weight.call_count == 1

        # Immediate switch to CRITICAL while cooldown is active: must be ignored due to cooldown
        mgr.evaluate_and_manage({"servers": {"app1": {"state": "CRITICAL"}}})
        assert mock_haproxy.set_server_maintenance.call_count == 0

    # 13. At least one healthy backend always remains eligible
    def test_13_at_least_one_healthy_backend_always_remains_eligible(self, manager, mock_haproxy):
        # Simulate app1, app2, app3, app4 already excluded or down
        for srv in ["app1", "app2", "app3", "app4"]:
            manager.servers[srv]["in_maint"] = True
            manager.servers[srv]["current_state"] = TrafficState.EXCLUDED

        # Only app5 remains eligible
        mock_hp_stats = {
            "app1": {"is_up": False, "is_maint": True},
            "app2": {"is_up": False, "is_maint": True},
            "app3": {"is_up": False, "is_maint": True},
            "app4": {"is_up": False, "is_maint": True},
            "app5": {"is_up": True, "is_maint": False, "status": "UP"}
        }

        # Now app5 encounters CRITICAL degradation
        manager.evaluate_and_manage(
            {"servers": {"app5": {"state": "CRITICAL", "reason": "App5 degraded"}}},
            hp_stats=mock_hp_stats
        )

        # Safeguard must REJECT excluding app5!
        assert manager.servers["app5"]["in_maint"] is False
        assert manager.servers["app5"]["last_action"] == TrafficAction.ACTION_REJECTED
        assert "at least 1 healthy backend must remain eligible" in manager.servers["app5"]["last_action_reason"].lower()
        mock_haproxy.set_server_maintenance.assert_not_called()

    # 14. HAProxy API failure is handled safely
    def test_14_haproxy_api_failure_is_handled_safely(self, manager, mock_haproxy):
        mock_haproxy.set_server_weight.side_effect = RuntimeError("HAProxy socket timeout")
        mock_haproxy.set_server_maintenance.side_effect = ConnectionError("HAProxy down")

        # Must not raise an unhandled exception
        try:
            manager.evaluate_and_manage({"servers": {"app1": {"state": "OVERLOAD"}}})
            manager.evaluate_and_manage({"servers": {"app2": {"state": "CRITICAL"}}})
        except Exception as e:
            pytest.fail(f"TrafficManager crashed on HAProxy error: {e}")

    # 15. Redis failure does not crash the monitor
    def test_15_redis_failure_does_not_crash_monitor(self, manager, mock_redis):
        mock_redis.lpush.side_effect = Exception("Redis ConnectionRefused")
        mock_redis.set.side_effect = Exception("Redis OOM")

        try:
            manager.evaluate_and_manage({"servers": {"app1": {"state": "OVERLOAD"}}})
            hist = manager.get_history(limit=5)
            assert len(hist) > 0  # In-memory history maintained
        except Exception as e:
            pytest.fail(f"TrafficManager crashed on Redis failure: {e}")

    # 16. Invalid HAProxy commands are rejected
    def test_16_invalid_haproxy_commands_are_rejected(self):
        client = HAProxyClient(socket_path="/tmp/fake.sock")

        # Disallowed command prefixes
        with pytest.raises(ValueError, match="whitelist"):
            client.validate_command("shutdown frontend http_front")

        # Semicolons / injection attempts
        with pytest.raises(ValueError, match="separator"):
            client.validate_command("show info; rm -rf /")

        with pytest.raises(ValueError, match="separator"):
            client.validate_command("show info\nshutdown haproxy")

        # Weight validation in set_server_weight
        with pytest.raises(ValueError, match="range"):
            client.set_server_weight("app1", -5)

        with pytest.raises(ValueError, match="range"):
            client.set_server_weight("app1", 300)

    # 17. Failed actions trigger rollback or a safe state
    def test_17_failed_actions_trigger_rollback_or_safe_state(self):
        client = HAProxyClient(socket_path="/tmp/fake.sock")
        with patch.object(client, "get_server_weight", side_effect=[100, 100]): # fails to change to 50
            with patch.object(client, "send_socket_command") as mock_cmd:
                mock_cmd.return_value = ""
                res = client.set_server_weight("app1", 50)
                # Verification failed (new_weight != 50) -> rollback command attempted
                assert res["success"] is False
                assert mock_cmd.call_count >= 2
                assert "weight 100" in mock_cmd.call_args[0][0]

    # 18. Multiple simultaneous overloaded backends are handled safely
    def test_18_multiple_simultaneous_overloaded_backends_handled_safely(self, manager, mock_haproxy):
        status = {
            "servers": {
                "app1": {"state": "OVERLOAD"},
                "app2": {"state": "OVERLOAD"},
                "app3": {"state": "NORMAL"},
                "app4": {"state": "NORMAL"},
                "app5": {"state": "NORMAL"}
            }
        }
        res = manager.evaluate_and_manage(status)
        assert res["servers"]["app1"]["current_weight"] == 50
        assert res["servers"]["app2"]["current_weight"] == 50
        assert res["summary"]["traffic_reduced_count"] == 2
        assert res["summary"]["healthy_eligible_count"] == 5  # Reduced weight backends are still eligible!

    # 19. Existing Phase 1–4 functionality remains operational
    def test_19_existing_phase1_to_4_functionality_operational(self, manager):
        # Target services count must remain 5
        assert len(manager.target_services) == 5
        # Manual restore returns backend to original state
        manager.servers["app1"]["current_weight"] = 50
        res = manager.restore_server("app1", force=True)
        assert res["success"] is True
        assert manager.servers["app1"]["current_weight"] == 100
        assert manager.servers["app1"]["current_state"] == TrafficState.NORMAL

    # 20. Phase 5.1 classification remains accurate and unchanged
    def test_20_phase51_classification_remains_accurate_and_unchanged(self):
        detector = ServerOverloadDetector("app1")
        # Ensure default Phase 5.1 thresholds remain intact
        assert detector.config["cpu_overload_threshold"] == 65.0
        assert detector.config["cpu_critical_threshold"] == 85.0

        # Normal metric produces NORMAL
        metric_normal = {
            "status": "running",
            "service_available": True,
            "cpu_percent": 15.0,
            "memory_mb": 50.0,
            "latency_ms": 25.0
        }
        eval_res = detector.evaluate(metric_normal)
        assert eval_res["state"] == OverloadState.NORMAL

    # 21. Minimum healthy backend safeguard when all 5 backends are degraded
    def test_21_all_five_backends_simultaneously_critical_safeguard(self, manager, mock_haproxy):
        """
        Verify that when all 5 backends simultaneously experience CRITICAL degradation:
        - The system never excludes all 5 backends.
        - Exactly at least 1 backend remains eligible for traffic (not in maintenance).
        - The decision is deterministic and logged.
        - The safeguard does NOT silently reclassify the preserved backend as healthy.
        """
        all_critical_eval = {
            "servers": {
                f"app{i}": {"state": "CRITICAL", "reason": f"app{i} severe resource saturation"}
                for i in range(1, 6)
            }
        }

        # Mock HAProxy status: all 5 are currently UP and not in maintenance
        hp_stats = {
            f"app{i}": {"is_up": True, "is_maint": False, "status": "UP"}
            for i in range(1, 6)
        }

        # Side-effect on mock_haproxy: update hp_stats when set_server_maintenance is called
        def set_maint_side_effect(srv, in_maint=True, backend="app_cluster"):
            if srv in hp_stats:
                hp_stats[srv]["is_maint"] = in_maint
                hp_stats[srv]["is_up"] = not in_maint
            return {"success": True, "server": srv, "is_maint": in_maint}

        mock_haproxy.set_server_maintenance.side_effect = set_maint_side_effect

        manager.evaluate_and_manage(all_critical_eval, hp_stats=hp_stats)

        # 1. Total excluded must be strictly less than 5
        excluded = [srv for srv in manager.target_services if manager.servers[srv]["in_maint"]]
        eligible = [srv for srv in manager.target_services if not manager.servers[srv]["in_maint"]]

        assert len(excluded) == 4, f"Exactly 4 should be excluded, got {len(excluded)}"
        assert len(eligible) == 1, f"Exactly 1 must remain eligible, got {len(eligible)}"
        assert eligible[0] == "app5", f"Deterministic anchor preserved should be app5, got {eligible[0]}"

        # 2. Preserved backend must NOT be silently marked healthy
        preserved_data = manager.servers[eligible[0]]
        assert preserved_data["overload_classification"] == "CRITICAL", "Safeguard must not falsify detector classification"
        assert preserved_data["last_action"] == TrafficAction.ACTION_REJECTED
        assert "retained" in preserved_data["last_action_reason"].lower()
        assert "gateway anchor" in preserved_data["last_action_reason"].lower()

        # 3. Exactly 4 set_server_maintenance calls made
        assert mock_haproxy.set_server_maintenance.call_count == 4
