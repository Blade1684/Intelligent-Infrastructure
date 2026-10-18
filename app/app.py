import os
import socket
import redis
import threading
import time
import multiprocessing
from datetime import datetime, timezone

from flask import Flask, jsonify, request, session
from flask_session import Session


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)


# ============================================================
# SERVER CONFIGURATION
# ============================================================

SERVER_NAME = os.getenv(
    "SERVER_NAME",
    socket.gethostname()
)


# ============================================================
# MEMORY STRESS STORAGE
# ============================================================

# Holds allocated memory for our controlled
# memory leak demonstration.
memory_blocks = []

memory_lock = threading.Lock()


# ============================================================
# CPU STRESS STORAGE
# ============================================================

# Holds CPU stress processes.
cpu_processes = []

cpu_process_lock = threading.Lock()


# ============================================================
# LATENCY INJECTION STATE
# ============================================================

# Shared mutable state for controlled latency injection.
# expires_at is a float epoch-second timestamp; 0 means inactive.
_latency_injection = {
    "delay_ms": 0.0,
    "expires_at": 0.0
}

_latency_lock = threading.Lock()

MAX_LATENCY_MS = 5000
DEFAULT_LATENCY_DURATION_S = 30


def get_active_latency_ms():
    """
    Return the currently injected delay in milliseconds if the
    injection window has not yet expired, otherwise return 0.

    Thread-safe; safe to call on every health-probe request.
    """
    with _latency_lock:
        if _latency_injection["expires_at"] > time.monotonic():
            return _latency_injection["delay_ms"]
        return 0.0


def parse_bounded_integer(data, field, default, minimum, maximum):
    """Validate a numeric demonstration-control value."""
    try:
        value = int(data.get(field, default))
    except (TypeError, ValueError):
        raise ValueError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )

    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")

    return value


def remove_finished_cpu_processes():
    """Prevent completed demo processes accumulating in the parent process."""
    global cpu_processes
    cpu_processes = [process for process in cpu_processes if process.is_alive()]


# ============================================================
# FLASK SESSION CONFIGURATION
# ============================================================

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY",
    "development-secret-change-this"
)


# Store sessions in Redis so both
# application servers share sessions.

app.config["SESSION_TYPE"] = "redis"

app.config["SESSION_REDIS"] = redis.Redis(
    host=os.getenv(
        "REDIS_HOST",
        "redis"
    ),

    port=int(
        os.getenv(
            "REDIS_PORT",
            "6379"
        )
    )
)


app.config["SESSION_PERMANENT"] = False

app.config["SESSION_USE_SIGNER"] = True


Session(app)


# ============================================================
# CPU STRESS WORKER
# ============================================================

def cpu_stress_worker(duration):
    """
    Run a CPU-intensive calculation for the
    specified duration.

    This function runs inside a separate process
    so that CPU usage is visible at the Docker
    container level.
    """

    end_time = time.time() + duration

    value = 0


    while time.time() < end_time:

        # Continuous mathematical operations.
        value = (
            value * 3
            + 7
        ) % 1000003


# ============================================================
# MEMORY STRESS WORKER
# ============================================================

def memory_stress(
    total_mb,
    step_mb=10,
    interval=1
):
    """
    Gradually allocate memory to simulate
    a memory leak.

    Memory is allocated in steps so the
    monitoring system can observe a gradual
    upward trend.
    """

    allocated_mb = 0


    while allocated_mb < total_mb:

        block_size = min(
            step_mb,
            total_mb - allocated_mb
        )


        # Allocate approximately block_size MB.
        block = bytearray(
            block_size
            * 1024
            * 1024
        )


        with memory_lock:

            memory_blocks.append(
                block
            )


        allocated_mb += block_size


        time.sleep(
            interval
        )


# ============================================================
# HOME ENDPOINT
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "message":
            "Intelligent Infrastructure Demo",

        "server":
            SERVER_NAME,

        "hostname":
            socket.gethostname(),

        "status":
            "healthy",

        "logged_in_user":
            session.get("username")

    })


# ============================================================
# LOGIN ENDPOINT
# ============================================================

@app.route(
    "/login",
    methods=["POST"]
)
def login():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )


    username = data.get(
        "username"
    )


    if not username:

        return jsonify({

            "error":
                "Username is required"

        }), 400


    session["username"] = username

    session["work_counter"] = 0


    return jsonify({

        "message":
            f"Logged in as {username}",

        "server":
            SERVER_NAME,

        "username":
            username,

        "work_counter":
            session["work_counter"]

    })


# ============================================================
# WORK ENDPOINT
# ============================================================

@app.route(
    "/work",
    methods=["POST"]
)
def do_work():

    if "username" not in session:

        return jsonify({

            "error":
                "Not logged in"

        }), 401


    session["work_counter"] = (

        session.get(
            "work_counter",
            0
        )

        + 1

    )


    return jsonify({

        "message":
            "Work completed",

        "username":
            session["username"],

        "work_counter":
            session["work_counter"],

        "server":
            SERVER_NAME

    })


# ============================================================
# SESSION ENDPOINT
# ============================================================

@app.route("/session")
def session_info():

    if "username" not in session:

        return jsonify({

            "logged_in":
                False,

            "server":
                SERVER_NAME

        })


    return jsonify({

        "logged_in":
            True,

        "username":
            session["username"],

        "work_counter":

            session.get(
                "work_counter",
                0
            ),

        "server":
            SERVER_NAME

    })


# ============================================================
# HEALTH ENDPOINT
# ============================================================

@app.route("/health")
def health():

    # Apply injected latency before responding so the monitor's
    # HTTP probe measures a realistic elevated response time.
    delay = get_active_latency_ms()
    if delay > 0:
        time.sleep(delay / 1000.0)

    try:
        app.config["SESSION_REDIS"].ping()
    except redis.RedisError as error:
        return jsonify({
            "status": "unhealthy",
            "server": SERVER_NAME,
            "dependency": "redis",
            "error": str(error)
        }), 503

    return jsonify({"status": "healthy", "server": SERVER_NAME})


# ============================================================
# CPU STRESS ENDPOINT
# ============================================================

@app.route(
    "/stress/cpu",
    methods=["POST"]
)
def stress_cpu():

    data = (

        request.get_json(
            silent=True
        )

        or {}

    )


    try:
        duration = parse_bounded_integer(data, "duration", 30, 1, 120)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


    process = multiprocessing.Process(

        target=cpu_stress_worker,

        args=(duration,)

    )


    process.start()


    with cpu_process_lock:

        remove_finished_cpu_processes()
        cpu_processes.append(
            process
        )


    return jsonify({

        "message":
            "CPU stress started",

        "server":
            SERVER_NAME,

        "duration_seconds":
            duration,

        "process_id":
            process.pid

    })


# ============================================================
# STOP CPU STRESS
# ============================================================

@app.route(
    "/stress/cpu/stop",
    methods=["POST"]
)
def stop_cpu_stress():

    stopped = 0


    with cpu_process_lock:

        for process in cpu_processes:

            if process.is_alive():

                process.terminate()

                stopped += 1


        cpu_processes.clear()


    return jsonify({

        "message":
            "CPU stress stopped",

        "processes_stopped":
            stopped,

        "server":
            SERVER_NAME

    })


# ============================================================
# MEMORY STRESS ENDPOINT
# ============================================================

@app.route(
    "/stress/memory",
    methods=["POST"]
)
def stress_memory():

    data = (

        request.get_json(
            silent=True
        )

        or {}

    )


    try:
        total_mb = parse_bounded_integer(data, "total_mb", 100, 1, 300)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


    thread = threading.Thread(

        target=memory_stress,

        args=(total_mb,),

        daemon=True

    )


    thread.start()


    return jsonify({

        "message":
            "Memory stress started",

        "server":
            SERVER_NAME,

        "target_memory_mb":
            total_mb

    })


# ============================================================
# CLEAR MEMORY STRESS
# ============================================================

@app.route(
    "/stress/memory/clear",
    methods=["POST"]
)
def clear_memory_stress():

    global memory_blocks


    with memory_lock:

        memory_blocks.clear()


    return jsonify({

        "message":
            "Allocated demonstration memory cleared",

        "server":
            SERVER_NAME

    })


# ============================================================
# LATENCY STRESS ENDPOINT
# ============================================================

@app.route(
    "/stress/latency",
    methods=["POST"]
)
def stress_latency():
    """
    Activate a controlled response delay on the /health endpoint.

    Accepts JSON body or query-string parameters:
      delay_ms   float  Delay in milliseconds (0 – 5000).  Required.
      duration_s int    How long the delay stays active (1 – 120 s).
                        Defaults to 30 s so it cannot be left on indefinitely.
    """
    data = request.get_json(silent=True) or {}

    # Accept delay_ms from JSON body or query string
    raw_delay = data.get("delay_ms", request.args.get("delay_ms", None))

    if raw_delay is None:
        return jsonify({"error": "delay_ms is required"}), 400

    try:
        delay_ms = float(raw_delay)
    except (TypeError, ValueError):
        return jsonify({"error": "delay_ms must be a number"}), 400

    if delay_ms < 0:
        return jsonify(
            {"error": "delay_ms must be >= 0"}
        ), 400

    if delay_ms > MAX_LATENCY_MS:
        return jsonify(
            {"error": f"delay_ms must be <= {MAX_LATENCY_MS}"}
        ), 400

    raw_duration = data.get("duration_s", request.args.get("duration_s", DEFAULT_LATENCY_DURATION_S))
    try:
        duration_s = int(raw_duration)
    except (TypeError, ValueError):
        duration_s = DEFAULT_LATENCY_DURATION_S
    duration_s = max(1, min(duration_s, 120))

    expires_at = time.monotonic() + duration_s

    with _latency_lock:
        _latency_injection["delay_ms"] = delay_ms
        _latency_injection["expires_at"] = expires_at

    # Human-readable wall-clock expiry for the response
    wall_expires = datetime.now(timezone.utc).isoformat()

    return jsonify({
        "message": "Latency injection active",
        "server": SERVER_NAME,
        "delay_ms": delay_ms,
        "duration_s": duration_s,
        "expires_in_s": duration_s
    })


# ============================================================
# RESET LATENCY INJECTION
# ============================================================

@app.route(
    "/stress/latency/reset",
    methods=["POST"]
)
def reset_latency():
    """Immediately disable any active latency injection."""
    with _latency_lock:
        _latency_injection["delay_ms"] = 0.0
        _latency_injection["expires_at"] = 0.0

    return jsonify({
        "message": "Latency injection cleared",
        "server": SERVER_NAME
    })


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=5000

    )
