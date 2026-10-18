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
    jsonify
)


from detector import (
    detect_cpu_anomaly,
    detect_disk_anomaly,
    detect_latency_anomaly,
    detect_memory_anomaly,
    detect_service_failure
)


from recovery import (

    get_server_state,

    isolate_server,

    recover_server

)


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

    "app2"

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

    incident = {
        "server": service,
        "incident_id": f"{service}-{int(time.time())}",
        "status": "active",
        "anomaly_type": anomaly.get("type"),
        "started_at": started_at,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "details": anomaly
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

        f"\n🚨 INCIDENT CREATED"

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

def automatic_recovery(service):

    print(

        f"\nRecovery scheduled "
        f"for {service}"

    )


    # Wait so traffic can migrate
    # to the healthy server.

    time.sleep(10)

    record_event(service, "recovery_started")

    recovery_success = recover_server(

        service,

        docker_client,

        redis_client

    )


    # --------------------------------------------------------
    # RECOVERY SUCCESSFUL
    # --------------------------------------------------------

    if recovery_success:
        record_event(service, "recovery_succeeded")
        
        # Calculate MTTR and close incident
        incident = get_active_incident(service)
        if incident:
            incident["status"] = "resolved"
            incident["recovered_at"] = datetime.now(timezone.utc).isoformat()
            
            # Parse times to calculate MTTR
            try:
                start = datetime.fromisoformat(incident["started_at"])
                end = datetime.fromisoformat(incident["recovered_at"])
                incident["mttr_seconds"] = (end - start).total_seconds()
            except Exception:
                pass
                
            # Move to closed incidents list
            redis_client.lpush(f"closed_incidents:{service}", json.dumps(incident))
            redis_client.ltrim(f"closed_incidents:{service}", 0, 99) # Keep last 100

        redis_client.delete(f"incident:{service}")

        print(f"Incident cleared for {service}")


    # --------------------------------------------------------
    # RECOVERY FAILED
    # --------------------------------------------------------

    else:

        incident = get_active_incident(service)
        if incident:
            incident["status"] = "escalated"
            incident["escalated_at"] = datetime.now(timezone.utc).isoformat()
            redis_client.set(f"incident:{service}", json.dumps(incident))

        record_event(service, "recovery_escalated", {
            "reason": "Automatic recovery attempts exhausted"
        })

        print(

            f"Recovery failed "
            f"for {service}"

        )


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

    if current_state.get("state") != "healthy":

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

    isolate_server(

        service,

        anomaly.get(
            "type"
        ),

        docker_client,

        redis_client

    )


    # --------------------------------------------------------
    # START AUTOMATIC RECOVERY
    # --------------------------------------------------------

    recovery_thread = threading.Thread(

        target=automatic_recovery,

        args=(service,),

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

            if current_state.get("state") != "isolated":
                continue


            # ------------------------------------------------
            # 2. Check for an unresolved active incident
            # ------------------------------------------------

            incident = get_active_incident(service)

            if not incident:
                continue

            if incident.get("status") != "active":
                # Escalated or already resolved – do not interfere.
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

                        f"\n⚠️ ALERT"

                    )


                    print(

                        f"Server: "
                        f"{service}"

                    )


                    print(

                        f"Anomaly: "
                        f"{anomaly.get('type')}"

                    )


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


    for service in TARGET_SERVICES:

        server_states.append(

            get_server_state(service, redis_client)

        )


    return jsonify({

        "servers":
            server_states

    })


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
        return jsonify({"error": "Unknown server"}), 404

    try:
        # Pre-record the incident start time in Redis before fault affects the system
        incident_stub = {
            "server": server,
            "incident_id": f"{server}-{int(time.time())}",
            "status": "pending_detection",
            "anomaly_type": fault_type,
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        redis_client.set(f"pending_incident:{server}", json.dumps(incident_stub), ex=300)
        
        # Call the application container to induce stress
        if fault_type == "cpu_spike":
            res = requests.post(f"http://{server}:5000/stress/cpu", json={"duration": 60}, timeout=5)
        elif fault_type == "memory_leak":
            res = requests.post(f"http://{server}:5000/stress/memory", json={"total_mb": 200}, timeout=5)
        else:
            return jsonify({"error": "Unsupported fault type"}), 400
            
        record_event(server, "fault_injected", {"fault_type": fault_type, "response": res.status_code})
        return jsonify({"message": f"Injected {fault_type} into {server}", "status": res.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# ALL INCIDENTS
# ============================================================

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
                
    # To keep it simple, we just return the active ones for now
    # A full incident log would require a separate Redis list for closed_incidents
    closed = []
    for server in TARGET_SERVICES:
        closed_records = redis_client.lrange(f"closed_incidents:{server}", 0, -1)
        for r in closed_records:
            closed.append(json.loads(r))
            
    return jsonify({
        "active": incidents,
        "closed": closed
    })


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
