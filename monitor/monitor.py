import time
import json
import threading

from datetime import (
    datetime,
    timezone
)

import docker
import redis
import requests

from flask import (
    Flask,
    jsonify,
    request
)


from detector import (
    detect_cpu_anomaly,
    detect_disk_anomaly,
    detect_latency_anomaly,
    detect_memory_anomaly,
    detect_service_failure
)


from recovery import (
    RecoveryDecisionEngine,
    RecoveryStrategy,
    get_server_state,
    isolate_server,
    select_recovery_strategy
)

try:
    from haproxy_client import HAProxyClient
except ImportError:
    from monitor.haproxy_client import HAProxyClient

try:
    from load_test_client import LoadTestManager
except ImportError:
    from monitor.load_test_client import LoadTestManager

try:
    from overload_detector import TrafficOverloadEngine, DEFAULT_CONFIG
except ImportError:
    from monitor.overload_detector import TrafficOverloadEngine, DEFAULT_CONFIG

try:
    from traffic_manager import TrafficManager
except ImportError:
    from monitor.traffic_manager import TrafficManager

try:
    from prediction_engine import PredictionEngine
except ImportError:
    from monitor.prediction_engine import PredictionEngine

haproxy_client = HAProxyClient()


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

COLLECTION_INTERVAL = 2

HEALTH_CHECK_TIMEOUT = 2


TARGET_SERVICES = [
    "app1",
    "app2",
    "app3",
    "app4",
    "app5"
]


# ============================================================
# DOCKER CONNECTION
# ============================================================

docker_client = docker.from_env()


# ============================================================
# REDIS CONNECTION
# ============================================================

redis_client = redis.Redis(

    host="redis",

    port=6379,

    decode_responses=True

)

load_test_manager = LoadTestManager(redis_client=redis_client)

overload_engine = TrafficOverloadEngine(
    target_services=TARGET_SERVICES,
    redis_client=redis_client,
    load_test_manager=load_test_manager,
    haproxy_client=haproxy_client
)

traffic_manager = TrafficManager(
    target_services=TARGET_SERVICES,
    haproxy_client=haproxy_client,
    redis_client=redis_client
)

recovery_engine = RecoveryDecisionEngine(
    docker_client=docker_client,
    redis_client=redis_client,
    traffic_manager=traffic_manager,
    haproxy_client=haproxy_client,
    config={"cooldown_seconds": 15.0}
)

prediction_engine = PredictionEngine(
    target_services=TARGET_SERVICES,
    redis_client=redis_client,
    traffic_manager=traffic_manager
)


# ============================================================
# RECOVERY THREAD REGISTRY
# ============================================================

# Maps service name -> threading.Thread so we can check whether a
# recovery thread is already alive before spawning a second one.
recovery_threads = {}


# ============================================================
# CPU CALCULATION
# ============================================================

def calculate_cpu_percent(stats):

    cpu_stats = stats.get(

        "cpu_stats",

        {}

    )


    precpu_stats = stats.get(

        "precpu_stats",

        {}

    )


    cpu_usage = cpu_stats.get(

        "cpu_usage",

        {}

    )


    precpu_usage = precpu_stats.get(

        "cpu_usage",

        {}

    )


    cpu_delta = (

        cpu_usage.get(
            "total_usage",
            0
        )

        -

        precpu_usage.get(
            "total_usage",
            0
        )

    )


    system_delta = (

        cpu_stats.get(
            "system_cpu_usage",
            0
        )

        -

        precpu_stats.get(
            "system_cpu_usage",
            0
        )

    )


    online_cpus = cpu_stats.get(

        "online_cpus",

        1

    )


    if (

        system_delta > 0

        and

        cpu_delta > 0

    ):

        return round(

            (
                cpu_delta
                /
                system_delta
            )

            *

            online_cpus

            *

            100,

            2

        )


    return 0.0


# ============================================================
# DOCKER CONTAINER LOOKUP
# ============================================================

def get_container_for_service(service_name):

    containers = docker_client.containers.list(

        all=True,

        filters={

            "label":

                f"com.docker.compose.service="
                f"{service_name}"

        }

    )


    if not containers:

        raise docker.errors.NotFound(

            f"No container found "
            f"for service: {service_name}"

        )


    return containers[0]


def check_application_health(service):
    """Probe Flask through the internal Compose network and measure response time."""
    start_time = time.perf_counter()
    if redis_client:
        try:
            if redis_client.get(f"simulate_network_failure:{service}"):
                time.sleep(0.2)
                return False, "Simulated network failure: connection timed out", 1500.0, None
        except Exception:
            pass
    try:
        response = requests.get(
            f"http://{service}:5000/health",
            timeout=HEALTH_CHECK_TIMEOUT
        )
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        status_code = response.status_code
        if status_code == 200 and response.json().get("status") == "healthy":
            return True, None, latency_ms, status_code
        return False, f"Health endpoint returned HTTP {status_code}", latency_ms, status_code
    except (requests.RequestException, ValueError) as error:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return False, str(error), latency_ms, None


def get_container_disk_usage(container):
    """Query real container root filesystem disk usage via df -m /."""
    try:
        exec_res = container.exec_run("df -m /")
        if exec_res.exit_code == 0:
            output = exec_res.output.decode("utf-8").strip().splitlines()
            if len(output) >= 2:
                parts = output[1].split()
                if len(parts) >= 5:
                    total_mb = float(parts[1])
                    used_mb = float(parts[2])
                    avail_mb = float(parts[3])
                    percent_str = parts[4].rstrip("%")
                    disk_percent = float(percent_str)

                    # Check for active controlled disk fault injection metadata
                    try:
                        meta_res = container.exec_run("cat /tmp/test_disk_fill/.metadata.json")
                        if meta_res.exit_code == 0:
                            meta = json.loads(meta_res.output.decode("utf-8"))
                            if meta.get("expires_at", 0) > time.time():
                                sim_pct = float(meta.get("simulated_percent", 92.0))
                                disk_percent = max(disk_percent, sim_pct)
                    except Exception:
                        pass

                    return {
                        "disk_total_mb": total_mb,
                        "disk_used_mb": used_mb,
                        "disk_available_mb": avail_mb,
                        "disk_percent": disk_percent
                    }
    except Exception:
        pass
    return {
        "disk_total_mb": 0.0,
        "disk_used_mb": 0.0,
        "disk_available_mb": 0.0,
        "disk_percent": 0.0
    }


# ============================================================
# METRICS COLLECTION
# ============================================================

def get_container_metrics(service):

    timestamp = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    try:
        container = get_container_for_service(
            service
        )

        container.reload()

        status = container.status

        # ----------------------------------------------------
        # STOPPED CONTAINER
        # ----------------------------------------------------
        if status != "running":
            return {
                "container": service,
                "status": status,
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
                "disk_percent": 0.0,
                "disk_used_mb": 0.0,
                "disk_total_mb": 0.0,
                "pids_current": 0,
                "latency_ms": None,
                "status_code": None,
                "service_available": False,
                "health_error": f"Container status is {status}",
                "timestamp": timestamp
            }

        # ----------------------------------------------------
        # RUNNING CONTAINER
        # ----------------------------------------------------
        service_available, health_error, latency_ms, status_code = check_application_health(service)

        stats = container.stats(
            stream=False
        )

        memory_usage = (
            stats.get(
                "memory_stats",
                {}
            ).get(
                "usage",
                0
            )
        )

        pids_current = stats.get("pids_stats", {}).get("current", 1)
        disk_info = get_container_disk_usage(container)

        return {
            "container": service,
            "status": status,
            "cpu_percent": calculate_cpu_percent(stats),
            "memory_mb": round(
                memory_usage / (1024 * 1024),
                2
            ),
            "disk_percent": disk_info["disk_percent"],
            "disk_used_mb": disk_info["disk_used_mb"],
            "disk_total_mb": disk_info["disk_total_mb"],
            "pids_current": pids_current,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "service_available": service_available,
            "health_error": health_error,
            "timestamp": timestamp
        }

    except docker.errors.NotFound:
        return {
            "container": service,
            "status": "not_found",
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "disk_percent": 0.0,
            "disk_used_mb": 0.0,
            "disk_total_mb": 0.0,
            "pids_current": 0,
            "latency_ms": None,
            "status_code": None,
            "service_available": False,
            "health_error": "Container not found",
            "timestamp": timestamp
        }

    except Exception as error:
        return {
            "container": service,
            "status": "error",
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "disk_percent": 0.0,
            "disk_used_mb": 0.0,
            "disk_total_mb": 0.0,
            "pids_current": 0,
            "latency_ms": None,
            "status_code": None,
            "service_available": False,
            "error": str(error),
            "health_error": str(error),
            "timestamp": timestamp
        }


# ============================================================
# METRICS STORAGE
# ============================================================

def save_metrics(metric):

    key = (

        f"metrics:"
        f"{metric['container']}"

    )


    redis_client.rpush(

        key,

        json.dumps(
            metric
        )

    )


    # Keep the latest 100 measurements.

    redis_client.ltrim(

        key,

        -100,

        -1

    )


# ============================================================
# GET LATEST METRIC
# ============================================================

def get_latest_metric(service):

    key = f"metrics:{service}"


    record = redis_client.lindex(

        key,

        -1

    )


    if record:

        return json.loads(
            record
        )


    return None


# ============================================================
# GET METRIC HISTORY
# ============================================================

def get_metric_history(service):

    key = f"metrics:{service}"


    records = redis_client.lrange(

        key,

        0,

        -1

    )


    return [

        json.loads(record)

        for record in records

    ]


# ============================================================
# ANOMALY DETECTION
# ============================================================

def detect_anomalies(service):

    history = get_metric_history(
        service
    )

    latest_metric = history[-1] if history else {"status": "not_found"}

    service_result = detect_service_failure(latest_metric)
    disk_result = detect_disk_anomaly(latest_metric)
    latency_result = detect_latency_anomaly(history, service)
    cpu_result = detect_cpu_anomaly(history, service)
    memory_result = detect_memory_anomaly(history, service)

    return {
        "server": service,
        "service_analysis": service_result,
        "disk_analysis": disk_result,
        "latency_analysis": latency_result,
        "cpu_analysis": cpu_result,
        "memory_analysis": memory_result
    }


# ============================================================
# ACTIVE INCIDENT
# ============================================================

def get_active_incident(service):

    incident = redis_client.get(

        f"incident:{service}"

    )


    if incident:

        return json.loads(
            incident
        )


    return None


# ============================================================
# CREATE INCIDENT
# ============================================================

def create_incident(
    service,
    anomaly
):

    existing_incident = get_active_incident(
        service
    )


    # Prevent duplicate incidents.

    if existing_incident:

        return existing_incident


    # Check if this incident was pre-registered by fault injection
    pending = redis_client.get(f"pending_incident:{service}")
    started_at = datetime.now(timezone.utc).isoformat()
    if pending:
        pending_data = json.loads(pending)
        if pending_data.get("anomaly_type") == anomaly.get("type"):
            started_at = pending_data.get("started_at", started_at)
        redis_client.delete(f"pending_incident:{service}")

    anomaly_type = anomaly.get("type", "unknown")
    if anomaly_type in {"container_crash", "container_missing", "process_failure", "network_failure"}:
        severity = "critical"
    else:
        severity = "high"

    current_val = (
        anomaly.get("current_cpu") or
        anomaly.get("current_memory") or
        anomaly.get("current_disk_percent") or
        anomaly.get("current_latency_ms") or
        anomaly.get("status_code") or
        anomaly.get("pids_current")
    )
    threshold_val = (
        anomaly.get("threshold_percent") or
        anomaly.get("threshold_ms") or
        (40.0 if anomaly_type == "cpu_spike" else None)
    )

    incident = {
        "server": service,
        "incident_id": f"{service}-{int(time.time())}",
        "status": "active",
        "recovery_state": "detected",
        "anomaly_type": anomaly_type,
        "severity": severity,
        "current_metric_value": current_val,
        "threshold_used": threshold_val,
        "started_at": started_at,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "details": anomaly,
        "metric_details": {
            "current_value": current_val,
            "threshold": threshold_val,
            "raw_details": anomaly
        }
    }

    # Store active incident.
    redis_client.set(f"incident:{service}", json.dumps(incident))


    # Store incident history.

    redis_client.rpush(

        f"events:{service}",

        json.dumps(
            incident
        )

    )


    # Keep latest 50 incidents.

    redis_client.ltrim(

        f"events:{service}",

        -50,

        -1

    )


    print(

        f"\n???? INCIDENT CREATED"

    )


    print(

        f"Incident ID: "
        f"{incident['incident_id']}"

    )


    print(

        f"Server: "
        f"{service}"

    )


    print(

        f"Anomaly: "
        f"{incident['anomaly_type']}"

    )


    return incident


def record_event(service, event_type, details=None):
    """Keep a bounded operational audit trail alongside incident records."""
    event = {
        "server": service,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details or {}
    }
    redis_client.rpush(f"events:{service}", json.dumps(event))
    redis_client.ltrim(f"events:{service}", -50, -1)
    return event


# ============================================================
# AUTOMATIC RECOVERY
# ============================================================

def automatic_recovery(service, anomaly_type=None):

    print(
        f"\n[RecoveryOrchestrator] Recovery scheduled "
        f"for {service}"
    )

    incident = get_active_incident(service)
    if not anomaly_type and incident:
        anomaly_type = incident.get("anomaly_type")
    anomaly_details = incident.get("details", {}) if incident else {}
    incident_id = incident.get("incident_id") if incident else f"{service}-{int(time.time())}"

    outcome = recovery_engine.execute_recovery(
        service,
        anomaly_type=anomaly_type,
        details=anomaly_details,
        incident_id=incident_id
    )

    if outcome.get("success"):
        print(f"[RecoveryOrchestrator] Automatic recovery succeeded for {service} via {outcome.get('action')}")
    else:
        print(f"[RecoveryOrchestrator] Automatic recovery finished with status: {outcome.get('reason')}")
    return outcome


# ============================================================
# SERVICE ANALYSIS
# ============================================================

def analyze_service(service):

    # --------------------------------------------------------
    # CHECK SERVER STATE
    # --------------------------------------------------------

    current_state = get_server_state(service, redis_client)


    # Do not process anomalies while
    # already isolated or recovering.
    if current_state.get("state") in ("isolated", "recovering"):
        return None


    # --------------------------------------------------------
    # CHECK ACTIVE INCIDENT
    # --------------------------------------------------------

    active_incident = get_active_incident(
        service
    )


    if active_incident:

        return None


    # --------------------------------------------------------
    # DETECT ANOMALIES
    # --------------------------------------------------------

    result = detect_anomalies(
        service
    )

    service_result = result.get(
        "service_analysis",
        {}
    )

    disk_result = result.get(
        "disk_analysis",
        {}
    )

    latency_result = result.get(
        "latency_analysis",
        {}
    )

    cpu_result = result.get(
        "cpu_analysis",
        {}
    )

    memory_result = result.get(
        "memory_analysis",
        {}
    )

    anomaly = None

    # Availability failures (crashes, process failures, network unreachable) take top priority
    if service_result.get("anomaly"):
        anomaly = service_result
    elif disk_result.get("anomaly"):
        anomaly = disk_result
    elif latency_result.get("anomaly"):
        anomaly = latency_result
    elif cpu_result.get("anomaly"):
        anomaly = cpu_result
    elif memory_result.get("anomaly"):
        anomaly = memory_result


    # --------------------------------------------------------
    # NO ANOMALY
    # --------------------------------------------------------

    if not anomaly:

        return None


    # --------------------------------------------------------
    # CREATE INCIDENT
    # --------------------------------------------------------

    create_incident(

        service,

        anomaly

    )


    # --------------------------------------------------------
    # ISOLATE SERVER
    # --------------------------------------------------------

    strategy = select_recovery_strategy(anomaly.get("type"), service, anomaly)
    needs_stop = (strategy == RecoveryStrategy.CONTAINER_RESTART)

    isolate_server(
        service,
        anomaly.get("type"),
        docker_client,
        redis_client,
        stop_container=needs_stop
    )

    # --------------------------------------------------------
    # START AUTOMATIC RECOVERY
    # --------------------------------------------------------

    recovery_thread = threading.Thread(
        target=automatic_recovery,
        args=(service, anomaly.get("type")),
        daemon=True
    )

    recovery_threads[service] = recovery_thread

    recovery_thread.start()

    return anomaly


# ============================================================
# STARTUP RECONCILIATION
# ============================================================

def startup_reconciliation():
    """
    Called once at startup before the monitoring loop begins.

    Scans each monitored service for the following condition:
      - Server state is "isolated"  (set by isolate_server)
      - An active incident exists with status "active"
      - No live recovery thread is registered for this service

    When all three conditions are met the monitor must have restarted
    while a recovery was in progress.  We resume the recovery so the
    service is not left stuck in isolation indefinitely.

    Intentionally isolated services whose incident has already been
    escalated (status != "active") are skipped to avoid overriding an
    operator decision.
    """

    print("\n[Reconciliation] Checking for stale isolated services...")

    for service in TARGET_SERVICES:

        try:

            # ------------------------------------------------
            # 1. Check server state
            # ------------------------------------------------

            current_state = get_server_state(service, redis_client)

            if current_state.get("state") in ("failed", "isolated", "recovering"):
                incident = get_active_incident(service)
                if not incident or incident.get("status") != "active":
                    # If server application is healthy and responsive with zero active incidents, reconcile to healthy
                    if application_is_healthy(service):
                        print(f"[Reconciliation] {service}: healthy with no active incident. Reconciling state to healthy.")
                        set_server_state(service, "healthy", "Reconciled to healthy: verified responsive with zero active incidents", redis_client)
                        continue

            if current_state.get("state") != "isolated":
                continue


            # ------------------------------------------------
            # 2. Check for an unresolved active incident
            # ------------------------------------------------

            incident = get_active_incident(service)

            if not incident:
                continue

            if incident.get("status") != "active":
                # Escalated or already resolved ??? do not interfere.
                continue


            # ------------------------------------------------
            # 3. Check whether a recovery thread is already live
            # ------------------------------------------------

            existing = recovery_threads.get(service)

            if existing is not None and existing.is_alive():
                print(
                    f"[Reconciliation] {service}: recovery already running, skipping."
                )
                continue


            # ------------------------------------------------
            # 4. Resume recovery
            # ------------------------------------------------

            print(
                f"[Reconciliation] {service}: stale isolation detected. "
                f"Resuming automatic recovery."
            )

            record_event(service, "recovery_resumed_after_restart", {
                "incident_id": incident.get("id"),
                "incident_type": incident.get("type")
            })

            recovery_thread = threading.Thread(
                target=automatic_recovery,
                args=(service,),
                daemon=True
            )

            recovery_threads[service] = recovery_thread

            recovery_thread.start()

        except Exception as reconciliation_error:

            print(
                f"[Reconciliation] Error checking {service}: "
                f"{reconciliation_error}"
            )

    print("[Reconciliation] Complete.\n")


# ============================================================
# BACKGROUND MONITORING
# ============================================================

def monitoring_loop():

    print(

        "\n===================================="

    )


    print(

        "MONITORING SYSTEM STARTED"

    )


    print(

        "Automatic anomaly detection enabled"

    )


    print(

        "Automatic recovery enabled"

    )


    print(

        "====================================\n"

    )


    while True:

        try:

            for service in TARGET_SERVICES:


                # --------------------------------------------
                # COLLECT METRICS
                # --------------------------------------------

                metric = get_container_metrics(
                    service
                )


                # --------------------------------------------
                # SAVE METRICS
                # --------------------------------------------

                save_metrics(
                    metric
                )


                # --------------------------------------------
                # ANALYZE SERVICE
                # --------------------------------------------

                anomaly = analyze_service(
                    service
                )


                # --------------------------------------------
                # PRINT ALERT
                # --------------------------------------------

                if anomaly:

                    print(

                        f"\n?????? ALERT"

                    )


                    print(

                        f"Server: "
                        f"{service}"

                    )


                    print(

                        f"Anomaly: "
                        f"{anomaly.get('type')}"

                    )

            # --------------------------------------------
            # EVALUATE OVERLOAD DETECTION ENGINE (PHASE 5.1)
            # --------------------------------------------
            try:
                server_metrics_dict = {srv: get_latest_metric(srv) for srv in TARGET_SERVICES}
                locust_live = None
                try:
                    locust_live = load_test_manager.get_live_metrics_unlocked()
                except Exception:
                    pass
                hp_stats = None
                try:
                    hp_stats = haproxy_client.get_backends_status()
                except Exception:
                    pass
                cluster_overload = overload_engine.evaluate_cluster(server_metrics_dict, locust_metrics=locust_live, haproxy_stats=hp_stats)
            except Exception as ovl_err:
                print(f"[OverloadEngine] Evaluation error: {ovl_err}")
                cluster_overload = {}

            # --------------------------------------------
            # EVALUATE TRAFFIC MANAGEMENT ENGINE (PHASE 5.2)
            # --------------------------------------------
            try:
                traffic_manager.evaluate_and_manage(
                    cluster_overload,
                    hp_stats=hp_stats,
                    server_metrics=server_metrics_dict
                )
            except Exception as trf_err:
                print(f"[TrafficManager] Evaluation error: {trf_err}")

            # --------------------------------------------
            # EVALUATE PREDICTIVE FAILURE PREVENTION (PHASE 5.4)
            # --------------------------------------------
            try:
                history_dict = {}
                for srv in TARGET_SERVICES:
                    try:
                        raw_list = redis_client.lrange(f"metrics:{srv}", 0, -1)
                        history_dict[srv] = [json.loads(x) for x in raw_list] if raw_list else []
                    except Exception:
                        history_dict[srv] = []

                prediction_engine.evaluate_cluster(
                    server_metrics_dict=server_metrics_dict,
                    history_dict=history_dict,
                    current_traffic=cluster_overload.get("traffic_summary"),
                    hp_stats=hp_stats
                )
            except Exception as pred_err:
                print(f"[PredictionEngine] Evaluation error: {pred_err}")


        except Exception as error:

            print(

                f"\nMonitoring error: "
                f"{error}"

            )


        time.sleep(

            COLLECTION_INTERVAL

        )


# ============================================================
# API ROUTES
# ============================================================


# ============================================================
# METRICS
# ============================================================

@app.route("/metrics")
def metrics():

    collected_metrics = []


    for service in TARGET_SERVICES:

        metric = get_latest_metric(
            service
        )


        if metric:

            collected_metrics.append(
                metric
            )


        else:

            collected_metrics.append({

                "container": service,
                "status": "waiting_for_data",
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
                "disk_percent": 0.0,
                "disk_used_mb": 0.0,
                "disk_total_mb": 0.0,
                "latency_ms": None,
                "service_available": False
            })


    return jsonify({

        "timestamp":

            datetime.now(
                timezone.utc
            ).isoformat(),

        "servers":
            collected_metrics

    })


# ============================================================
# HISTORY
# ============================================================

@app.route(
    "/history/<server>"
)
def history(server):

    if server not in TARGET_SERVICES:

        return jsonify({

            "error":
                "Unknown server"

        }), 404


    samples = get_metric_history(
        server
    )


    return jsonify({

        "server":
            server,

        "sample_count":
            len(samples),

        "samples":
            samples

    })


# ============================================================
# ANOMALIES
# ============================================================

@app.route(
    "/anomalies/<server>"
)
def anomalies(server):

    if server not in TARGET_SERVICES:

        return jsonify({

            "error":
                "Unknown server"

        }), 404


    return jsonify(

        detect_anomalies(
            server
        )

    )


# ============================================================
# EVENTS
# ============================================================

@app.route(
    "/events/<server>"
)
def events(server):

    if server not in TARGET_SERVICES:

        return jsonify({

            "error":
                "Unknown server"

        }), 404


    records = redis_client.lrange(

        f"events:{server}",

        0,

        -1

    )


    events_list = [

        json.loads(record)

        for record in records

    ]


    active_incident = get_active_incident(
        server
    )


    return jsonify({

        "server":
            server,

        "event_count":
            len(events_list),

        "active_incident":
            active_incident,

        "events":
            events_list

    })


# ============================================================
# SERVER STATES
# ============================================================

@app.route("/servers")
def servers():
    server_states = []
    haproxy_states = {}
    try:
        haproxy_states = haproxy_client.get_backends_status()
    except Exception as e:
        print(f"Error fetching HAProxy status in /servers: {e}")

    for service in TARGET_SERVICES:
        st = get_server_state(service, redis_client)
        hp = haproxy_states.get(service, {})
        st["haproxy_status"] = hp.get("status", "UNKNOWN")
        st["haproxy_eligible"] = hp.get("is_up", False)
        st["haproxy_check_code"] = hp.get("check_code")
        st["haproxy_check_status"] = hp.get("check_status")
        server_states.append(st)

    return jsonify({
        "servers": server_states,
        "haproxy_available": bool(haproxy_states)
    })


# ============================================================
# HAPROXY STATUS & RUNTIME API
# ============================================================

@app.route("/haproxy/status")
def haproxy_status():
    try:
        backends = haproxy_client.get_backends_status()
        return jsonify({
            "status": "healthy" if backends else "degraded",
            "backends": backends,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as err:
        return jsonify({"status": "error", "error": str(err)}), 500


@app.route("/haproxy/maintenance/<server>/<action>", methods=["POST"])
def haproxy_maintenance(server, action):
    if server not in TARGET_SERVICES:
        return jsonify({"error": "Unknown server"}), 404
    act = str(action).lower()
    if act in {"enable", "drain", "maint"}:
        in_maint = True
    elif act in {"disable", "ready", "restore"}:
        in_maint = False
    else:
        return jsonify({"error": "Action must be 'enable'/'drain' or 'disable'/'ready'"}), 400

    res = haproxy_client.set_server_maintenance(server, in_maint=in_maint)
    record_event(server, "haproxy_maintenance_changed", res)
    return jsonify(res)


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status":
            "healthy",

        "service":
            "monitor"

    })

# ============================================================
# FAULT INJECTION
# ============================================================

from flask import request

@app.route("/api/inject/<server>/<fault_type>", methods=["POST"])
def inject_fault(server, fault_type):
    if server not in TARGET_SERVICES:
        return jsonify({"error": f"Unknown server: {server}"}), 404

    try:
        data = request.get_json(silent=True) or {}
        # Pre-record the incident start time in Redis before fault affects the system
        incident_stub = {
            "server": server,
            "incident_id": f"{server}-{int(time.time())}",
            "status": "pending_detection",
            "anomaly_type": fault_type,
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        redis_client.set(f"pending_incident:{server}", json.dumps(incident_stub), ex=300)
        
        status_code = 200
        # Call the application container to induce stress
        if fault_type == "cpu_spike":
            dur = int(data.get("duration", 45))
            load = int(data.get("load", 90))
            res = requests.post(f"http://{server}:5000/stress/cpu", json={"duration": dur, "load": load}, timeout=5)
            status_code = res.status_code
        elif fault_type == "memory_leak":
            total_mb = int(data.get("total_mb", data.get("mb", 150)))
            res = requests.post(f"http://{server}:5000/stress/memory", json={"total_mb": total_mb}, timeout=5)
            status_code = res.status_code
        elif fault_type == "high_latency":
            delay_ms = float(data.get("delay_ms", 650))
            dur_s = int(data.get("duration_s", data.get("duration", 60)))
            res = requests.post(f"http://{server}:5000/stress/latency", json={"delay_ms": delay_ms, "duration_s": dur_s}, timeout=5)
            status_code = res.status_code
        elif fault_type == "process_failure":
            dur_s = int(data.get("duration_s", data.get("duration", 60)))
            res = requests.post(f"http://{server}:5000/stress/process/crash", json={"duration_s": dur_s}, timeout=5)
            status_code = res.status_code
        elif fault_type in {"disk_exhaustion", "disk_stress"}:
            size_mb = int(data.get("size_mb", 50))
            dur_s = int(data.get("duration_s", data.get("duration", 60)))
            res = requests.post(f"http://{server}:5000/stress/disk/fill", json={"size_mb": size_mb, "duration_s": dur_s}, timeout=5)
            status_code = res.status_code
        elif fault_type == "container_crash":
            # Gracefully stop the container in background to simulate unexpected termination
            try:
                container = get_container_for_service(server)
                threading.Thread(target=lambda c=container: c.stop(timeout=1), daemon=True).start()
                status_code = 200
            except Exception as d_err:
                return jsonify({"error": f"Failed to stop container {server}: {d_err}"}), 500
        elif fault_type == "network_failure":
            ttl = int(data.get("duration", 45))
            scope = data.get("network_scope", "container_network")
            if scope == "external_partition":
                redis_client.set(f"simulate_external_partition:{server}", "1", ex=ttl)
            redis_client.set(f"simulate_network_failure:{server}", "1", ex=ttl)
            status_code = 200
        else:
            return jsonify({"error": f"Unsupported fault type: {fault_type}"}), 400
            
        record_event(server, "fault_injected", {"fault_type": fault_type, "status": status_code})
        return jsonify({"message": f"Injected {fault_type} into {server}", "status": status_code, "server": server})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inject/<server>/<fault_type>/stop", methods=["POST"])
@app.route("/api/inject/<server>/stop", methods=["POST"])
def stop_fault(server, fault_type=None):
    if server not in TARGET_SERVICES:
        return jsonify({"error": f"Unknown server: {server}"}), 404
    try:
        # 1. Clear simulated network failure and pending incident keys immediately
        try:
            redis_client.delete(f"simulate_network_failure:{server}")
            redis_client.delete(f"simulate_external_partition:{server}")
            redis_client.delete(f"pending_incident:{server}")
        except Exception: pass

        # 2. Reset app stress endpoints in background thread
        def _reset_app():
            for ep in ["stress/cpu/stop", "stress/memory/clear", "stress/latency/reset", "stress/process/reset", "stress/disk/clean"]:
                try: requests.post(f"http://{server}:5000/{ep}", timeout=1.0)
                except Exception: pass
        threading.Thread(target=_reset_app, daemon=True).start()

        # 3. Ensure container is started if stopped
        try:
            container = get_container_for_service(server)
            def _ensure_running(c):
                try:
                    c.reload()
                    if c.status != "running":
                        c.start()
                except Exception: pass
            threading.Thread(target=_ensure_running, args=(container,), daemon=True).start()
        except Exception: pass

        return jsonify({"message": f"All stress faults stopped and cleared for {server}", "server": server})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# ALL INCIDENTS
# ============================================================

def _parse_incident_timestamp(inc):
    """Extract authoritative epoch timestamp for sorting incidents newest first."""
    if not isinstance(inc, dict):
        return 0.0
    ts_val = (
        inc.get("started_at") or 
        inc.get("detected_at") or 
        inc.get("timestamp") or 
        inc.get("created_at")
    )
    if ts_val:
        if isinstance(ts_val, (int, float)):
            return float(ts_val) if ts_val > 1e11 else float(ts_val)
        try:
            clean_ts = str(ts_val).replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
            return dt.timestamp()
        except Exception:
            pass
    # Fallback to numeric suffix in incident_id (e.g. app1-1729891234)
    inc_id = str(inc.get("incident_id", ""))
    parts = inc_id.split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        try:
            return float(parts[-1])
        except Exception:
            pass
    return 0.0


@app.route("/incidents")
def all_incidents():
    incidents = []
    for server in TARGET_SERVICES:
        # Get active
        active = get_active_incident(server)
        if active:
            incidents.append(active)
            
        # Get historical from events (filter for created/recovered to build list)
        records = redis_client.lrange(f"events:{server}", 0, -1)
        for record in records:
            event = json.loads(record)
            if event.get("event_type") == "recovery_succeeded":
                # We could reconstruct closed incidents here or store them properly
                pass
                
    closed = []
    for server in TARGET_SERVICES:
        closed_records = redis_client.lrange(f"closed_incidents:{server}", 0, -1)
        for r in closed_records:
            try:
                closed.append(json.loads(r))
            except Exception:
                pass
            
    # Sort chronologically, newest incident first
    incidents.sort(key=_parse_incident_timestamp, reverse=True)
    closed.sort(key=_parse_incident_timestamp, reverse=True)

    return jsonify({
        "active": incidents,
        "closed": closed
    })


# ============================================================
# TRAFFIC LOAD TESTING ENDPOINTS (PHASE 4)
# ============================================================

@app.route("/load-test/profiles")
def get_load_test_profiles():
    return jsonify(load_test_manager.get_profiles())


@app.route("/load-test/start", methods=["POST"])
def start_load_test():
    data = request.get_json(silent=True) or {}
    try:
        session_info = load_test_manager.start_test(data)
        record_event("cluster", "load_test_started", {
            "session_id": session_info.get("session_id"),
            "profile": session_info.get("config", {}).get("profile"),
            "users": session_info.get("config", {}).get("user_count"),
            "duration": session_info.get("config", {}).get("duration_seconds")
        })
        return jsonify(session_info), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except RuntimeError as re:
        return jsonify({"error": str(re)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/load-test/stop", methods=["POST"])
def stop_load_test():
    try:
        session_info = load_test_manager.stop_test(final_status="stopped")
        record_event("cluster", "load_test_stopped", {
            "session_id": session_info.get("session_id"),
            "status": session_info.get("status")
        })
        return jsonify(session_info), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/load-test/status")
def get_load_test_status():
    try:
        return jsonify(load_test_manager.get_status()), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/load-test/metrics")
def get_load_test_metrics():
    try:
        metrics = load_test_manager.get_live_metrics_unlocked()
        return jsonify(metrics), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/load-test/history")
def get_load_test_history():
    try:
        limit = int(request.args.get("limit", 20))
        history = load_test_manager.get_history(limit=limit)
        return jsonify({"history": history}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# INTELLIGENT TRAFFIC OVERLOAD DETECTION API (PHASE 5.1)
# ============================================================

@app.route("/overload/status")
def get_overload_status():
    """
    Returns overall infrastructure classification, state counts,
    traffic summary, and per-server overload states.
    """
    try:
        status = overload_engine.get_live_status()
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/overload/servers/<server>")
def get_server_overload(server):
    """
    Returns detailed overload classification, contributing metrics,
    baseline status, and trend predictions for a specific server.
    """
    if server not in TARGET_SERVICES:
        return jsonify({"error": "Unknown server"}), 404
    try:
        status = overload_engine.get_live_status()
        srv_eval = status.get("servers", {}).get(server)
        if not srv_eval:
            detector = overload_engine.get_detector(server)
            metric = get_latest_metric(server) or {}
            srv_eval = detector.evaluate(metric)
        return jsonify(srv_eval), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/overload/predictions")
def get_overload_predictions():
    """
    Returns early-warning trend predictions and estimated time-to-threshold
    for all monitored application servers.
    """
    try:
        status = overload_engine.get_live_status()
        predictions = {}
        for srv in TARGET_SERVICES:
            srv_eval = status.get("servers", {}).get(srv, {})
            pred = srv_eval.get("details", {}).get("prediction", {})
            predictions[srv] = pred
        return jsonify({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "predictions": predictions
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/overload/history")
def get_overload_history():
    """
    Returns detection history and audit events of overload state transitions.
    """
    try:
        limit = int(request.args.get("limit", 20))
        history = overload_engine.get_history(limit=limit)
        return jsonify({"history": history}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/overload/config", methods=["GET", "POST"])
def get_overload_config():
    """
    Returns or updates configured overload thresholds and debouncing parameters.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        overload_engine.update_config(data)
        det = next(iter(overload_engine.detectors.values()), None)
        cfg = det.config if det else DEFAULT_CONFIG
        return jsonify({"message": "Config updated", "config": cfg}), 200
    det = next(iter(overload_engine.detectors.values()), None)
    cfg = det.config if det else DEFAULT_CONFIG
    return jsonify(cfg), 200


# ============================================================
# INTELLIGENT TRAFFIC MANAGEMENT API (PHASE 5.2)
# ============================================================

@app.route("/traffic/status")
def get_traffic_status():
    """
    Returns current cluster traffic management status, backend eligibility counts,
    per-server traffic weights, and administrative maintenance states.
    """
    try:
        status = traffic_manager.get_cluster_status()
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/traffic/servers/<server>")
def get_server_traffic_status(server):
    """
    Returns detailed traffic management status for a specific server.
    """
    if server not in TARGET_SERVICES:
        return jsonify({"error": "Unknown server"}), 404
    try:
        srv_status = traffic_manager.get_server_status(server)
        if not srv_status:
            return jsonify({"error": "Server not tracked"}), 404
        return jsonify(srv_status), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/traffic/weights")
def get_traffic_weights():
    """
    Returns current configured traffic weights for all backends.
    """
    try:
        weights = traffic_manager.get_weights()
        return jsonify(weights), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/traffic/history")
def get_traffic_history():
    """
    Returns traffic-management activity and audit timeline.
    """
    try:
        limit = int(request.args.get("limit", 20))
        history = traffic_manager.get_history(limit=limit)
        return jsonify({"history": history}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/traffic/config")
def get_traffic_config():
    """
    Returns traffic-management configuration parameters and safeguards.
    """
    return jsonify(traffic_manager.get_config()), 200


@app.route("/traffic/restore/<server>", methods=["POST"])
def restore_server_traffic(server):
    """
    Manually restores a backend to its normal configured weight and ready state.
    Validates health before restoring unless force=true in query or body.
    """
    if server not in TARGET_SERVICES:
        return jsonify({"error": "Unknown server"}), 404
    try:
        force = False
        if request.is_json:
            force = bool(request.json.get("force", False))
        elif request.args.get("force", "").lower() in ("true", "1", "yes"):
            force = True

        try:
            if hasattr(overload_engine, "detectors") and server in overload_engine.detectors:
                overload_engine.detectors[server].current_state = "NORMAL"
                overload_engine.detectors[server].consecutive_anomalous_cycles = 0
                overload_engine.detectors[server].consecutive_normal_cycles = 0
        except Exception:
            pass

        result = traffic_manager.restore_server(server, force=force)
        code = 200 if result.get("success") else 400
        if result.get("success"):
            set_server_state(server, "healthy", f"Restored to nominal operational state (force={force})", redis_client)
        return jsonify(result), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500





# ============================================================
# RECOVERY MONITORING AND CONTROL ENDPOINTS (PHASE 5.3)
# ============================================================

@app.route("/recovery/status")
def get_recovery_status():
    """Returns cluster recovery status, active recoveries, counts, and per-server details."""
    try:
        return jsonify(recovery_engine.get_status()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/servers/<server>")
def get_recovery_server_status(server):
    """Returns detailed recovery status and recent history for a specific server."""
    if server not in TARGET_SERVICES:
        return jsonify({"error": f"Unknown server: {server}"}), 404
    try:
        data = recovery_engine.get_server_status(server)
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/history")
def get_recovery_history():
    """Returns recovery incident history and MTTR timeline."""
    try:
        limit = int(request.args.get("limit", 50))
        history = recovery_engine.get_history(limit=limit)
        return jsonify({"history": history}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/config")
def get_recovery_config():
    """Returns current automated recovery configuration and safety parameters."""
    try:
        return jsonify(recovery_engine.get_config()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/enable", methods=["POST"])
def enable_recovery():
    """Globally enables automatic fault remediation."""
    try:
        recovery_engine.enable()
        return jsonify({"message": "Automatic recovery enabled", "config": recovery_engine.get_config()}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/disable", methods=["POST"])
def disable_recovery():
    """Globally disables automatic fault remediation."""
    try:
        recovery_engine.disable()
        return jsonify({"message": "Automatic recovery disabled", "config": recovery_engine.get_config()}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recovery/execute/<server>", methods=["POST"])
def execute_manual_recovery(server):
    """
    Operator-triggered manual recovery for a backend.
    Validates server name, optional strategy, and current state.
    """
    if server not in TARGET_SERVICES:
        return jsonify({"error": f"Unknown server: {server}"}), 404
    try:
        data = request.get_json(silent=True) or {}
        anomaly_type = data.get("anomaly_type")
        strategy = data.get("strategy")
        details = data.get("details", {})
        force = bool(data.get("force", True))

        if strategy == "process_recovery":
            strategy = RecoveryStrategy.PROCESS_RELOAD

        allowed_strategies = {
            RecoveryStrategy.CPU_WORKER_TERMINATION,
            RecoveryStrategy.MEMORY_CLEANUP,
            RecoveryStrategy.PROCESS_RELOAD,
            RecoveryStrategy.DISK_CLEANUP,
            RecoveryStrategy.CONTAINER_RESTART,
            RecoveryStrategy.LATENCY_RESET,
            RecoveryStrategy.UNSUPPORTED
        }
        if strategy and strategy not in allowed_strategies:
            return jsonify({
                "error": f"Invalid strategy: {strategy}. Must be one of {list(allowed_strategies)}"
            }), 400

        strategy_to_anomaly = {
            RecoveryStrategy.CPU_WORKER_TERMINATION: "cpu_spike",
            RecoveryStrategy.MEMORY_CLEANUP: "memory_leak",
            RecoveryStrategy.PROCESS_RELOAD: "process_failure",
            RecoveryStrategy.DISK_CLEANUP: "disk_exhaustion",
            RecoveryStrategy.LATENCY_RESET: "high_latency",
            RecoveryStrategy.CONTAINER_RESTART: "container_crash",
            RecoveryStrategy.UNSUPPORTED: "unsupported"
        }
        if not anomaly_type and strategy in strategy_to_anomaly:
            anomaly_type = strategy_to_anomaly[strategy]

        if strategy:
            details["target_strategy"] = strategy

        result = recovery_engine.execute_recovery(
            server,
            anomaly_type=anomaly_type or "manual_intervention",
            details=details,
            force=force
        )
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# PREDICTIVE FAILURE PREVENTION ENDPOINTS (PHASE 5.4)
# ============================================================

@app.route("/prediction/status")
def get_prediction_status():
    """Cluster predictive failure prevention status and active warnings."""
    try:
        status = prediction_engine.get_cluster_status()
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prediction/servers/<server>")
def get_prediction_server(server):
    """Predictive failure prevention telemetry and forecasts for a specific server."""
    if server not in TARGET_SERVICES:
        return jsonify({"error": f"Unknown server: {server}"}), 404
    try:
        data = prediction_engine.get_server_prediction(server)
        if not data:
            return jsonify({"error": f"No predictive data available for {server}"}), 404
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prediction/history")
def get_prediction_history():
    """Historical forecasts and evaluated outcomes."""
    try:
        limit = int(request.args.get("limit", 50))
        history = prediction_engine.get_history(limit=limit)
        return jsonify({"history": history, "total": len(history)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prediction/accuracy")
def get_prediction_accuracy():
    """Comprehensive prediction evaluation metrics, confusion matrix, and lead times."""
    try:
        acc = prediction_engine.get_accuracy_metrics()
        # Add confusion matrix representation
        tp = acc.get("true_positives", 0)
        fp = acc.get("false_positives", 0)
        tn = acc.get("true_negatives", 0)
        fn = acc.get("false_negatives", 0)
        return jsonify({
            "metrics": acc,
            "confusion_matrix": {
                "true_positives": tp,
                "false_positives": fp,
                "true_negatives": tn,
                "false_negatives": fn
            },
            "sample_counts": {
                "total_evaluated": acc.get("total_evaluated", 0),
                "pending_evaluations": acc.get("pending_evaluations", 0)
            }
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prediction/config", methods=["GET", "POST"])
def prediction_config():
    """Retrieve or validate and update prediction configuration parameters."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Request body must be a valid JSON object"}), 400
        res = prediction_engine.update_config(data)
        code = 200 if res.get("success") else 400
        return jsonify(res), code
    return jsonify({"config": prediction_engine.get_config()}), 200


@app.route("/prediction/traffic/enable", methods=["POST"])
def enable_prediction_traffic():
    """Globally enables conservative preventive traffic management."""
    try:
        res = prediction_engine.set_traffic_control_enabled(True)
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prediction/traffic/disable", methods=["POST"])
def disable_prediction_traffic():
    """Globally disables preventive traffic management while preserving prediction and monitoring."""
    try:
        res = prediction_engine.set_traffic_control_enabled(False)
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    monitoring_thread = threading.Thread(

        target=monitoring_loop,

        daemon=True

    )


    monitoring_thread.start()


    # Run reconciliation once after the monitoring thread is up
    # so that any services stranded by a previous monitor crash
    # are recovered without waiting for the next polling cycle.
    startup_reconciliation()


    app.run(

        host="0.0.0.0",

        port=5001

    )

