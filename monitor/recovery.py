import json
import time
from datetime import datetime, timezone

import requests


SERVER_STATES = {}
RECOVERY_ATTEMPTS = 2
RECOVERY_STABILIZATION_SECONDS = 8
RECOVERY_RETRY_DELAY_SECONDS = 2
HEALTH_CHECK_TIMEOUT = 2


def set_server_state(service, state, reason=None, redis_client=None):
    """Store operational state in memory and Redis for monitor-restart resilience."""
    server_state = {
        "server": service,
        "state": state,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    SERVER_STATES[service] = server_state
    if redis_client:
        redis_client.set(f"server_state:{service}", json.dumps(server_state))

    print(f"\nSERVER STATE CHANGED\nServer: {service}\nState: {state}")
    if reason:
        print(f"Reason: {reason}")
    return server_state


def get_server_state(service, redis_client=None):
    if redis_client:
        persisted_state = redis_client.get(f"server_state:{service}")
        if persisted_state:
            return json.loads(persisted_state)
    return SERVER_STATES.get(service, {
        "server": service,
        "state": "healthy",
        "reason": None
    })


def get_container(service, docker_client):
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.service={service}"}
    )
    if not containers:
        raise RuntimeError(f"Container not found: {service}")
    return containers[0]


def isolate_server(service, anomaly_type, docker_client, redis_client=None):
    set_server_state(
        service, "isolated", f"Detected {anomaly_type}", redis_client
    )
    try:
        container = get_container(service, docker_client)
        container.reload()
        print(f"\nISOLATING SERVER: {service}")
        if container.status == "running":
            print(f"Stopping container: {container.name}")
            container.stop()
        print(f"SERVER {service} ISOLATED")
    except Exception as error:
        set_server_state(service, "failed", f"Isolation failed: {error}", redis_client)
        print(f"Isolation error: {error}")


def application_is_healthy(service):
    """Verify the actual Flask endpoint, not merely the Docker process."""
    response = requests.get(
        f"http://{service}:5000/health", timeout=HEALTH_CHECK_TIMEOUT
    )
    return response.status_code == 200 and response.json().get("status") == "healthy"


def recover_server(service, docker_client, redis_client=None):
    """Restart and verify a service, retrying before declaring escalation-worthy failure."""
    last_error = "Application health check failed"

    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        try:
            set_server_state(
                service,
                "recovering",
                f"Automatic recovery attempt {attempt} of {RECOVERY_ATTEMPTS}",
                redis_client
            )
            container = get_container(service, docker_client)
            container.reload()
            if container.status != "running":
                print(f"\nStarting container: {container.name}")
                container.start()

            print(f"Waiting for {service} to stabilize...")
            time.sleep(RECOVERY_STABILIZATION_SECONDS)
            container.reload()
            if container.status == "running" and application_is_healthy(service):
                set_server_state(
                    service,
                    "healthy",
                    "Recovery verified by application health endpoint",
                    redis_client
                )
                print(f"\nSERVER {service} RECOVERED SUCCESSFULLY")
                return True

            last_error = f"Container status={container.status}; health check failed"
        except (Exception, requests.RequestException, ValueError) as error:
            last_error = str(error)
            print(f"Recovery attempt {attempt} error: {error}")

        if attempt < RECOVERY_ATTEMPTS:
            time.sleep(RECOVERY_RETRY_DELAY_SECONDS)

    set_server_state(
        service,
        "failed",
        f"Recovery failed after {RECOVERY_ATTEMPTS} attempts: {last_error}",
        redis_client
    )
    return False
