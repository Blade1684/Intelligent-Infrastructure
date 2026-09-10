import os
import socket
import redis
import threading
import time
import multiprocessing

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
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=5000

    )
