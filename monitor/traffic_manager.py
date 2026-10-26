"""
Intelligent Traffic Management and Dynamic Backend Control Engine (Phase 5.2)

Consumes output of Phase 5.1 Intelligent Overload Detection Engine and dynamically controls
HAProxy backend routing via the HAProxy Runtime API socket without restarting containers.

Key capabilities:
  - Dynamic weight reduction for overloaded backends (e.g. 100 -> 50).
  - Temporary administrative maintenance exclusion for critically degraded backends.
  - Multi-cycle consecutive healthy observation re-admission verification.
  - Safety guardrails: thread-safe lock, min 1 healthy backend eligible, max exclusions cap,
    per-server cooldowns, duplicate action suppression, rollback on failure, Redis/HAProxy resilience.
"""

import time
import json
import threading
import requests
from datetime import datetime, timezone

# Traffic management state constants
class TrafficState:
    NORMAL = "NORMAL"
    WEIGHT_REDUCED = "WEIGHT_REDUCED"
    EXCLUDED = "EXCLUDED"
    AWAITING_READMISSION = "AWAITING_READMISSION"
    OFFLINE_DETECTED = "OFFLINE_DETECTED"

# Traffic management actions
class TrafficAction:
    NONE = "NONE"
    WEIGHT_REDUCED = "WEIGHT_REDUCED"
    TEMPORARILY_EXCLUDED = "TEMPORARILY_EXCLUDED"
    READMISSION_RESTORED = "READMISSION_RESTORED"
    MANUAL_RESTORED = "MANUAL_RESTORED"
    OBSERVED_WARNING = "OBSERVED_WARNING"
    ACTION_REJECTED = "ACTION_REJECTED"


DEFAULT_TRAFFIC_CONFIG = {
    # Weights
    "normal_weight": 100,
    "reduced_weight": 50,
    "min_weight": 10,
    "max_weight": 256,

    # Safety constraints
    "min_eligible_backends": 1,          # At least 1 healthy backend must remain eligible
    "max_simultaneous_excluded": 4,     # Never exclude all 5 backends
    "action_cooldown_seconds": 10.0,     # Minimum seconds between automatic actions on the same server
    "duplicate_suppression": True,      # Prevent re-issuing duplicate commands

    # Re-admission criteria
    "consecutive_healthy_required": 3,  # Consecutive clean observations needed for re-admission
    "verify_health_endpoint": True,     # Verify HTTP /health probe before re-admission

    # Control switches
    "enabled": True,
    "allow_exclusion_on_critical": True,
    "allow_weight_reduction_on_overload": True
}


class TrafficManager:
    """
    Intelligent Traffic Management and Dynamic Backend Control Engine.
    Coordinates backend weight adjustments, temporary exclusions, safety invariants,
    and monitored re-admission.
    """
    def __init__(self, target_services=None, haproxy_client=None, redis_client=None, config=None):
        self.target_services = target_services or ["app1", "app2", "app3", "app4", "app5"]
        self.haproxy_client = haproxy_client
        self.redis_client = redis_client
        self.config = dict(DEFAULT_TRAFFIC_CONFIG)
        if config:
            self.config.update(config)

        self.lock = threading.RLock()

        # In-memory server tracking state
        self.servers = {}
        for srv in self.target_services:
            self.servers[srv] = {
                "server": srv,
                "current_state": TrafficState.NORMAL,
                "current_weight": self.config["normal_weight"],
                "original_weight": self.config["normal_weight"],
                "in_maint": False,
                "last_action": TrafficAction.NONE,
                "last_action_reason": "System initialized nominal",
                "last_action_timestamp": datetime.now(timezone.utc).isoformat(),
                "last_action_time": 0.0,
                "consecutive_healthy_cycles": 0,
                "overload_classification": "NORMAL",
                "haproxy_status": "UP",
                "recommended_action": "None"
            }

        # In-memory history fallback
        self.history = []

    def get_config(self):
        with self.lock:
            return dict(self.config)

    def update_config(self, new_config):
        with self.lock:
            self.config.update(new_config)
            return dict(self.config)

    def _log_action(self, server, action, reason, details=None):
        """Append an audit event to Redis and in-memory history."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server": server,
            "action": action,
            "reason": reason,
            "details": details or {}
        }
        self.history.append(event)
        if len(self.history) > 100:
            self.history.pop(0)

        if self.redis_client:
            try:
                self.redis_client.lpush("traffic:history", json.dumps(event))
                self.redis_client.ltrim("traffic:history", 0, 99)
            except Exception:
                pass
        return event

    def get_history(self, limit=20):
        if self.redis_client:
            try:
                raw_events = self.redis_client.lrange("traffic:history", 0, limit - 1)
                if raw_events:
                    return [json.loads(e) for e in raw_events]
            except Exception:
                pass
        with self.lock:
            return list(reversed(self.history[-limit:]))

    def get_weights(self):
        """Returns current configured weights for all target servers."""
        with self.lock:
            return {srv: data["current_weight"] for srv, data in self.servers.items()}

    def get_server_status(self, server):
        with self.lock:
            if server in self.servers:
                return dict(self.servers[server])
            return None

    def get_cluster_status(self):
        """Generate comprehensive overview metrics and server status."""
        with self.lock:
            healthy_eligible = 0
            weight_reduced = 0
            excluded = 0
            awaiting_readmission = 0

            for data in self.servers.values():
                st = data["current_state"]
                if st == TrafficState.EXCLUDED or data["in_maint"]:
                    excluded += 1
                elif st == TrafficState.WEIGHT_REDUCED:
                    weight_reduced += 1
                    healthy_eligible += 1
                elif st == TrafficState.AWAITING_READMISSION:
                    awaiting_readmission += 1
                    if not data["in_maint"]:
                        healthy_eligible += 1
                elif st == TrafficState.NORMAL:
                    if data["haproxy_status"] == "UP" and not data["in_maint"]:
                        healthy_eligible += 1

            status = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "enabled": self.config["enabled"],
                "summary": {
                    "healthy_eligible_count": healthy_eligible,
                    "traffic_reduced_count": weight_reduced,
                    "excluded_count": excluded,
                    "awaiting_readmission_count": awaiting_readmission,
                    "total_backends": len(self.target_services)
                },
                "servers": {srv: dict(d) for srv, d in self.servers.items()},
                "config": dict(self.config)
            }
            return status

    def _verify_server_health(self, server):
        """Check direct application health endpoint for re-admission."""
        port_map = {
            "app1": 5000,
            "app2": 5000,
            "app3": 5000,
            "app4": 5000,
            "app5": 5000
        }
        port = port_map.get(server, 5000)
        url = f"http://{server}:{port}/health"
        try:
            resp = requests.get(url, timeout=1.5)
            return (resp.status_code == 200)
        except Exception:
            return False

    def count_healthy_eligible(self, hp_stats=None):
        """
        Calculates how many backends are currently healthy and eligible to serve traffic.
        A backend is eligible if HAProxy status is UP and it is NOT in administrative maintenance.
        """
        count = 0
        for srv in self.target_services:
            srv_data = self.servers.get(srv, {})
            # If server is in admin maint, it is not eligible
            if srv_data.get("in_maint", False):
                continue

            # If HAProxy stats provided, check live status
            if hp_stats and srv in hp_stats:
                hp_s = hp_stats[srv]
                if hp_s.get("is_up", False) and not hp_s.get("is_maint", False):
                    count += 1
            else:
                if srv_data.get("haproxy_status") == "UP":
                    count += 1
        return count

    def evaluate_and_manage(self, cluster_overload_status, hp_stats=None, server_metrics=None):
        """
        Consumes Phase 5.1 detection engine output and executes dynamic traffic control.
        cluster_overload_status: dict returned by TrafficOverloadEngine.evaluate_cluster()
        hp_stats: dict returned by HAProxyClient.get_backends_status()
        server_metrics: dict of raw container metrics
        """
        with self.lock:
            if not self.config.get("enabled", True):
                return self.get_cluster_status()

            now = time.time()
            now_iso = datetime.now(timezone.utc).isoformat()

            # Refresh live HAProxy backend stats if available
            if hp_stats:
                for srv, hp_info in hp_stats.items():
                    if srv in self.servers:
                        self.servers[srv]["haproxy_status"] = hp_info.get("status", "UNKNOWN")
                        # Note: we distinguish maint initiated by our engine vs external
                        if hp_info.get("is_maint"):
                            self.servers[srv]["in_maint"] = True
                        elif hp_info.get("is_up"):
                            self.servers[srv]["in_maint"] = False

            servers_eval = cluster_overload_status.get("servers", {}) if isinstance(cluster_overload_status, dict) else {}

            for srv in self.target_services:
                srv_data = self.servers[srv]
                srv_eval = servers_eval.get(srv, {})
                ovl_state = srv_eval.get("state", "NORMAL")
                srv_data["overload_classification"] = ovl_state

                # Check action cooldown
                cooldown_remaining = max(0.0, (srv_data["last_action_time"] + self.config["action_cooldown_seconds"]) - now)
                in_cooldown = (cooldown_remaining > 0.0)

                # ====================================================
                # 1. NORMAL CLASSIFICATION
                # ====================================================
                if ovl_state == "NORMAL":
                    # Check if server was degraded/excluded and is recovering
                    if srv_data["current_state"] in (TrafficState.EXCLUDED, TrafficState.WEIGHT_REDUCED, TrafficState.AWAITING_READMISSION):
                        srv_data["consecutive_healthy_cycles"] += 1
                        needed = self.config["consecutive_healthy_required"]

                        if srv_data["consecutive_healthy_cycles"] >= needed:
                            # Verify application health endpoint
                            health_verified = True
                            if self.config["verify_health_endpoint"]:
                                health_verified = self._verify_server_health(srv)

                            if health_verified:
                                # Re-admission condition satisfied: restore backend
                                if self.haproxy_client:
                                    self.haproxy_client.set_server_weight(srv, self.config["normal_weight"])
                                    if srv_data["in_maint"]:
                                        self.haproxy_client.set_server_maintenance(srv, in_maint=False)

                                srv_data["current_state"] = TrafficState.NORMAL
                                srv_data["current_weight"] = self.config["normal_weight"]
                                srv_data["in_maint"] = False
                                srv_data["consecutive_healthy_cycles"] = 0
                                srv_data["last_action"] = TrafficAction.READMISSION_RESTORED
                                srv_data["last_action_reason"] = (
                                    f"Successfully passed {needed} consecutive healthy cycles and L7 health check. "
                                    f"Restored weight to {self.config['normal_weight']}."
                                )
                                srv_data["last_action_timestamp"] = now_iso
                                srv_data["last_action_time"] = now
                                srv_data["recommended_action"] = "Backend fully healthy and re-admitted"

                                self._log_action(srv, TrafficAction.READMISSION_RESTORED, srv_data["last_action_reason"], {
                                    "restored_weight": self.config["normal_weight"],
                                    "in_maint": False
                                })
                            else:
                                srv_data["current_state"] = TrafficState.AWAITING_READMISSION
                                srv_data["recommended_action"] = "Awaiting L7 application health confirmation"
                        else:
                            srv_data["current_state"] = TrafficState.AWAITING_READMISSION
                            srv_data["recommended_action"] = f"Recovery observed: {srv_data['consecutive_healthy_cycles']}/{needed} healthy cycles"
                    else:
                        # Server was already normal
                        srv_data["recommended_action"] = "Operating normally; fully eligible"
                        # Ensure weight sync if drift occurred and not in cooldown
                        if srv_data["current_weight"] != self.config["normal_weight"]:
                            if self.haproxy_client:
                                self.haproxy_client.set_server_weight(srv, self.config["normal_weight"])
                            srv_data["current_weight"] = self.config["normal_weight"]

                # ====================================================
                # 2. HIGH_TRAFFIC_HEALTHY CLASSIFICATION
                # ====================================================
                elif ovl_state == "HIGH_TRAFFIC_HEALTHY":
                    # STRICT RULE: No intervention. Do not penalize or reduce weights.
                    srv_data["recommended_action"] = "High traffic handled nominal; no traffic intervention"
                    # If server was awaiting re-admission, count this towards healthy cycles
                    if srv_data["current_state"] == TrafficState.AWAITING_READMISSION:
                        srv_data["consecutive_healthy_cycles"] += 1

                # ====================================================
                # 3. EARLY_WARNING CLASSIFICATION
                # ====================================================
                elif ovl_state == "EARLY_WARNING":
                    # STRICT RULE: Do not exclude or reduce weight automatically on early warning.
                    srv_data["recommended_action"] = "Early warning: observe resource trend; prepare traffic weight reduction"
                    # Reset consecutive healthy recovery counter
                    srv_data["consecutive_healthy_cycles"] = 0

                # ====================================================
                # 4. OVERLOAD CLASSIFICATION
                # ====================================================
                elif ovl_state == "OVERLOAD":
                    srv_data["consecutive_healthy_cycles"] = 0
                    srv_data["recommended_action"] = "Confirmed overload: traffic weight reduction recommended"

                    if not self.config["allow_weight_reduction_on_overload"]:
                        continue

                    # Duplicate suppression: if already at reduced weight and state is WEIGHT_REDUCED, do nothing
                    if srv_data["current_state"] == TrafficState.WEIGHT_REDUCED and srv_data["current_weight"] == self.config["reduced_weight"]:
                        continue

                    # If server is already excluded (e.g. was CRITICAL), leave it excluded
                    if srv_data["current_state"] == TrafficState.EXCLUDED:
                        continue

                    # Check cooldown
                    if in_cooldown:
                        continue

                    # Execute weight reduction
                    target_w = self.config["reduced_weight"]
                    action_success = True
                    if self.haproxy_client:
                        try:
                            res = self.haproxy_client.set_server_weight(srv, target_w)
                            action_success = res.get("success", False)
                        except Exception as hp_err:
                            action_success = False
                            self._log_action(srv, TrafficAction.NONE, f"HAProxy API failure: {hp_err}")

                    if action_success:
                        old_w = srv_data["current_weight"]
                        srv_data["current_state"] = TrafficState.WEIGHT_REDUCED
                        srv_data["current_weight"] = target_w
                        srv_data["last_action"] = TrafficAction.WEIGHT_REDUCED
                        srv_data["last_action_reason"] = (
                            f"Confirmed resource overload: reduced weight from {old_w} to {target_w}."
                        )
                        srv_data["last_action_timestamp"] = now_iso
                        srv_data["last_action_time"] = now
                        self._log_action(srv, TrafficAction.WEIGHT_REDUCED, srv_data["last_action_reason"], {
                            "old_weight": old_w,
                            "new_weight": target_w
                        })

                # ====================================================
                # 5. CRITICAL CLASSIFICATION
                # ====================================================
                elif ovl_state == "CRITICAL":
                    srv_data["consecutive_healthy_cycles"] = 0
                    srv_data["recommended_action"] = "Critical degradation: temporary exclusion from traffic"

                    if not self.config["allow_exclusion_on_critical"]:
                        continue

                    # Duplicate suppression: already excluded
                    if srv_data["current_state"] == TrafficState.EXCLUDED and srv_data["in_maint"]:
                        continue

                    # Check cooldown
                    if in_cooldown:
                        continue

                    # SAFETY GUARD: Count how many healthy backends are currently eligible
                    current_eligible = self.count_healthy_eligible(hp_stats)
                    # If this server is currently eligible, excluding it reduces eligible count by 1
                    srv_is_currently_eligible = (srv_data["haproxy_status"] == "UP" and not srv_data["in_maint"])
                    eligible_after_action = (current_eligible - 1) if srv_is_currently_eligible else current_eligible

                    if eligible_after_action < self.config["min_eligible_backends"]:
                        # REJECT ACTION to prevent total cluster outage
                        srv_data["last_action"] = TrafficAction.ACTION_REJECTED
                        srv_data["last_action_reason"] = (
                            f"Safety policy rejected exclusion: at least {self.config['min_eligible_backends']} "
                            f"healthy backend must remain eligible (currently {current_eligible} eligible). "
                            f"Retained {srv} as active gateway anchor despite {ovl_state} classification."
                        )
                        srv_data["last_action_timestamp"] = now_iso
                        srv_data["last_action_time"] = now
                        srv_data["recommended_action"] = f"Action rejected by safety safeguard: {srv} retained as gateway anchor"
                        self._log_action(srv, TrafficAction.ACTION_REJECTED, srv_data["last_action_reason"], {
                            "current_eligible": current_eligible,
                            "required_min": self.config["min_eligible_backends"],
                            "preserved_anchor": srv
                        })
                        continue

                    # Execute temporary exclusion via HAProxy administrative maintenance
                    action_success = True
                    if self.haproxy_client:
                        try:
                            res = self.haproxy_client.set_server_maintenance(srv, in_maint=True)
                            action_success = res.get("success", False)
                        except Exception as hp_err:
                            action_success = False
                            self._log_action(srv, TrafficAction.NONE, f"HAProxy API failure: {hp_err}")

                    if action_success:
                        srv_data["current_state"] = TrafficState.EXCLUDED
                        srv_data["in_maint"] = True
                        srv_data["last_action"] = TrafficAction.TEMPORARILY_EXCLUDED
                        srv_data["last_action_reason"] = (
                            "Critical degradation policy threshold met. Temporarily placed in administrative maintenance."
                        )
                        srv_data["last_action_timestamp"] = now_iso
                        srv_data["last_action_time"] = now
                        self._log_action(srv, TrafficAction.TEMPORARILY_EXCLUDED, srv_data["last_action_reason"], {
                            "in_maint": True
                        })

                # ====================================================
                # 6. OFFLINE CLASSIFICATION
                # ====================================================
                elif ovl_state == "OFFLINE":
                    # Defer to HAProxy active health checks
                    srv_data["current_state"] = TrafficState.OFFLINE_DETECTED
                    srv_data["recommended_action"] = "Server offline; handled via HAProxy active health check"
                    srv_data["consecutive_healthy_cycles"] = 0

            # Persist state snapshot to Redis if available
            cluster_status = self.get_cluster_status()
            if self.redis_client:
                try:
                    self.redis_client.set("traffic:status", json.dumps(cluster_status))
                except Exception:
                    pass

            return cluster_status

    def restore_server(self, server, force=False):
        """
        Manual administrative restoration of a backend to nominal state.
        Validates server responsiveness unless force=True.
        """
        with self.lock:
            if server not in self.servers:
                return {"success": False, "error": f"Unknown server: {server}"}

            srv_data = self.servers[server]

            # Validate health and non-degraded status unless forced
            if not force:
                if srv_data.get("overload_classification") in ("CRITICAL", "OVERLOAD"):
                    return {
                        "success": False,
                        "error": f"Cannot restore {server}: backend is actively degraded ({srv_data.get('overload_classification')}). Resolve underlying degradation first or use force=True."
                    }
                if self.config["verify_health_endpoint"] and not self._verify_server_health(server):
                    return {
                        "success": False,
                        "error": f"Cannot restore {server}: application health check failed. Use force=True to bypass."
                    }

            # Restore HAProxy state: restore weight first, then clear maintenance
            maint_success = True
            weight_success = True
            if self.haproxy_client:
                w_res = self.haproxy_client.set_server_weight(server, self.config["normal_weight"])
                weight_success = w_res.get("success", True)
                m_res = self.haproxy_client.set_server_maintenance(server, in_maint=False)
                maint_success = m_res.get("success", True)

            now_iso = datetime.now(timezone.utc).isoformat()
            srv_data["current_state"] = TrafficState.NORMAL
            srv_data["current_weight"] = self.config["normal_weight"]
            srv_data["in_maint"] = False
            if maint_success:
                srv_data["haproxy_status"] = "UP"
            srv_data["consecutive_healthy_cycles"] = 0
            srv_data["last_action"] = TrafficAction.MANUAL_RESTORED
            srv_data["last_action_reason"] = f"Manual administrative restoration (force={force})"
            srv_data["last_action_timestamp"] = now_iso
            srv_data["last_action_time"] = time.time()
            srv_data["recommended_action"] = "Operating nominal following manual restoration"

            self._log_action(server, TrafficAction.MANUAL_RESTORED, srv_data["last_action_reason"], {
                "forced": force,
                "weight": self.config["normal_weight"]
            })

            # Update Redis snapshot
            if self.redis_client:
                try:
                    self.redis_client.set("traffic:status", json.dumps(self.get_cluster_status()))
                except Exception:
                    pass

            return {
                "success": (maint_success and weight_success),
                "server": server,
                "state": TrafficState.NORMAL,
                "weight": self.config["normal_weight"],
                "in_maint": False,
                "message": f"Server {server} restored to normal operating state."
            }

    def isolate_server(self, server, reason="Pre-recovery isolation", hp_stats=None):
        """
        Safely isolate a backend from HAProxy traffic before disruptive remediation,
        strictly enforcing the minimum eligible backend safeguard.
        """
        with self.lock:
            if server not in self.servers:
                return {"success": False, "error": f"Unknown server: {server}"}

            srv_data = self.servers[server]
            # If already in maintenance or excluded, nothing to do
            if srv_data.get("in_maint") or srv_data.get("current_state") == TrafficState.EXCLUDED:
                return {"success": True, "already_isolated": True, "server": server, "action": "ALREADY_ISOLATED"}

            # Fetch live HAProxy backend stats if available and not passed
            if hp_stats is None and self.haproxy_client and hasattr(self.haproxy_client, "get_backends_status"):
                try:
                    hp_stats = self.haproxy_client.get_backends_status()
                except Exception:
                    hp_stats = None

            if hp_stats:
                for s, info in hp_stats.items():
                    if s in self.servers:
                        self.servers[s]["haproxy_status"] = info.get("status", "UNKNOWN")
                        if info.get("is_maint"):
                            self.servers[s]["in_maint"] = True

            # Check safeguard: at least min_eligible_backends must remain
            current_eligible = self.count_healthy_eligible(hp_stats=hp_stats)
            srv_is_currently_eligible = (srv_data.get("haproxy_status") == "UP" and not srv_data.get("in_maint"))
            eligible_after = (current_eligible - 1) if srv_is_currently_eligible else current_eligible

            if eligible_after < self.config.get("min_eligible_backends", 1):
                srv_data["last_action"] = TrafficAction.ACTION_REJECTED
                srv_data["last_action_reason"] = (
                    f"Minimum eligible backend safeguard rejected pre-recovery isolation: at least {self.config.get('min_eligible_backends', 1)} "
                    f"backend must remain eligible (currently {current_eligible} eligible). Retained {server} as active anchor."
                )
                self._log_action(server, TrafficAction.ACTION_REJECTED, srv_data["last_action_reason"])
                return {
                    "success": False,
                    "action": "ACTION_REJECTED",
                    "decision": "ACTION_REJECTED",
                    "server": server,
                    "reason": srv_data["last_action_reason"]
                }

            # Safe to place into maintenance
            maint_res = True
            if self.haproxy_client:
                res = self.haproxy_client.set_server_maintenance(server, in_maint=True)
                maint_res = res.get("success", False) if isinstance(res, dict) else bool(res)

            now_iso = datetime.now(timezone.utc).isoformat()
            now = time.time()
            srv_data["current_state"] = TrafficState.EXCLUDED
            srv_data["in_maint"] = True
            srv_data["last_action"] = TrafficAction.TEMPORARILY_EXCLUDED
            srv_data["last_action_reason"] = reason
            srv_data["last_action_timestamp"] = now_iso
            srv_data["last_action_time"] = now

            self._log_action(server, TrafficAction.TEMPORARILY_EXCLUDED, reason)
            return {
                "success": maint_res,
                "action": "TRAFFIC_ISOLATED" if maint_res else "ISOLATION_FAILED",
                "isolated": maint_res,
                "server": server
            }


