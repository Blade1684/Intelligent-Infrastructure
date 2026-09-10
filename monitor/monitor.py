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
    """Probe Flask through the internal Compose network."""
    try:
        response = requests.get(
            f"http://{service}:5000/health",
            timeout=HEALTH_CHECK_TIMEOUT
        )
        if response.status_code == 200 and response.json().get("status") == "healthy":
            return True, None
        return False, f"Health endpoint returned HTTP {response.status_code}"
    except (requests.RequestException, ValueError) as error:
        return False, str(error)


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

                "container":
                    service,

                "status":
                    status,

                "cpu_percent":
                    0.0,

                "memory_mb":
                    0.0,

                "service_available":
                    False,

                "timestamp":
                    timestamp

            }


        # ----------------------------------------------------
        # RUNNING CONTAINER
        # ----------------------------------------------------

        service_available, health_error = check_application_health(service)

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


        return {

            "container":
                service,

            "status":
                status,

            "cpu_percent":

                calculate_cpu_percent(
                    stats
                ),

            "memory_mb":

                round(

                    memory_usage
                    /
                    (
                        1024
                        *
                        1024
                    ),

                    2

                ),

            "service_available":
                service_available,

            "health_error":
                health_error,

            "timestamp":
                timestamp

        }


    except docker.errors.NotFound:

        return {

            "container":
                service,

            "status":
                "not_found",

            "cpu_percent":
                0.0,

            "memory_mb":
                0.0,

            "timestamp":
                timestamp

        }


    except Exception as error:

        return {

            "container":
                service,

            "status":
                "error",

            "error":
                str(error),

            "timestamp":
                timestamp

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


    cpu_result = detect_cpu_anomaly(
        history
    )


    memory_result = detect_memory_anomaly(
        history
    )


    return {

        "server":
            service,

        "service_analysis":
            service_result,

        "cpu_analysis":
            cpu_result,

        "memory_analysis":
            memory_result

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


    incident = {

        "server":
            service,

        "incident_id":

            f"{service}-"
            f"{int(time.time())}",

        "status":
            "active",

        "anomaly_type":

            anomaly.get(
                "type"
            ),

        "detected_at":

            datetime.now(
                timezone.utc
            ).isoformat(),

        "details":
            anomaly

    }


    # Store active incident.

    redis_client.set(

        f"incident:{service}",

        json.dumps(
            incident
        )

    )


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

        redis_client.delete(

            f"incident:{service}"

        )


        print(

            f"Incident cleared "
            f"for {service}"

        )


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


    cpu_result = result.get(

        "cpu_analysis",

        {}

    )


    memory_result = result.get(

        "memory_analysis",

        {}

    )


    anomaly = None

    service_result = result.get(

        "service_analysis",

        {}

    )


    # Availability failures take priority because a service that cannot
    # answer traffic needs recovery even if resource usage is normal.
    if service_result.get("anomaly"):

        anomaly = service_result


    # CPU anomaly has priority.

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


    recovery_thread.start()


    return anomaly


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

                "container":
                    service,

                "status":
                    "waiting_for_data",

                "cpu_percent":
                    0.0,

                "memory_mb":
                    0.0

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
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    monitoring_thread = threading.Thread(

        target=monitoring_loop,

        daemon=True

    )


    monitoring_thread.start()


    app.run(

        host="0.0.0.0",

        port=5001

    )
