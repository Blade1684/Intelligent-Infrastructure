import os
import socket
import redis
import threading
import time
import multiprocessing
import json
import uuid
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
memory_processes = []
memory_process_lock = threading.Lock()


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


# ============================================================
# PROCESS FAILURE INJECTION STATE
# ============================================================

# Shared mutable state for controlled worker process failure simulation.
_process_failure_injection = {
    "active": False,
    "expires_at": 0.0,
    "reason": ""
}

_process_failure_lock = threading.Lock()
DEFAULT_PROCESS_FAILURE_DURATION_S = 60


def is_worker_failure_active():
    """Check if controlled worker failure is currently active."""
    with _process_failure_lock:
        if _process_failure_injection["active"]:
            if _process_failure_injection["expires_at"] > time.monotonic():
                return True
            _process_failure_injection["active"] = False
        return False


# ============================================================
# DISK STRESS STATE & SAFETY CONSTRAINTS
# ============================================================

DISK_STRESS_DIR = "/tmp/test_disk_fill"
APPROVED_CLEANUP_DIRS = [DISK_STRESS_DIR]
DEFAULT_DISK_STRESS_SIZE_MB = 50
MIN_DISK_STRESS_SIZE_MB = 1
MAX_DISK_STRESS_SIZE_MB = 200
DEFAULT_DISK_STRESS_DURATION_S = 60

_disk_stress_lock = threading.Lock()


def is_safe_disk_cleanup_path(file_path, approved_dir=DISK_STRESS_DIR):
    """
    Ensure the path is strictly within the approved test directory, prevents path traversal,
    and only matches disk stress artifacts.
    """
    try:
        resolved_dir = os.path.realpath(approved_dir)
        resolved_file = os.path.realpath(file_path)
        if resolved_file == resolved_dir:
            return False
        common = os.path.commonpath([resolved_dir, resolved_file])
        if common != resolved_dir:
            return False
        basename = os.path.basename(resolved_file)
        if not (basename.startswith("disk_stress_") or basename == ".metadata.json"):
            return False
        return True
    except Exception:
        return False



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
    allocated_mb = 0
    while allocated_mb < total_mb:
        block_size = min(
            step_mb,
            total_mb - allocated_mb
        )
        block = bytearray(b'x' * (block_size * 1024 * 1024))
        with memory_lock:
            memory_blocks.append(block)
        allocated_mb += block_size
        time.sleep(interval)


def memory_stress_worker(total_mb, step_mb=20, interval=0.5):
    """
    Dedicated worker process for memory stress simulation.
    When terminated by clear_memory_stress, all allocated pages
    are immediately returned to the OS kernel.
    """
    blocks = []
    allocated_mb = 0
    while allocated_mb < total_mb:
        block_size = min(step_mb, total_mb - allocated_mb)
        block = bytearray(b'x' * (block_size * 1024 * 1024))
        blocks.append(block)
        allocated_mb += block_size
        time.sleep(interval)
    while True:
        time.sleep(1)


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

    # Check for controlled worker failure simulation
    if is_worker_failure_active():
        return jsonify({
            "status": "unhealthy",
            "server": SERVER_NAME,
            "error": "Application worker process crashed unexpectedly",
            "master_pid": os.getppid(),
            "worker_pid": os.getpid()
        }), 500

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
        cpu_processes.append(process)

    try:
        app.config["SESSION_REDIS"].sadd(f"stress:cpu_pids:{SERVER_NAME}", str(process.pid))
    except Exception:
        pass

    return jsonify({
        "message": "CPU stress started",
        "server": SERVER_NAME,
        "duration_seconds": duration,
        "process_id": process.pid
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
                process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.5)
                stopped += 1
        cpu_processes.clear()

    # Shared Redis registration across Gunicorn workers
    try:
        r_client = app.config["SESSION_REDIS"]
        pids = r_client.smembers(f"stress:cpu_pids:{SERVER_NAME}")
        for pid_bytes in pids:
            try:
                pid = int(pid_bytes)
                import signal
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                stopped += 1
            except OSError:
                pass
        r_client.delete(f"stress:cpu_pids:{SERVER_NAME}")
    except Exception:
        pass

    return jsonify({
        "message": "CPU stress stopped",
        "processes_stopped": max(stopped, 1) if stopped > 0 else 0,
        "server": SERVER_NAME
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
        step_mb = parse_bounded_integer(data, "step_mb", 10, 1, 100) if "step_mb" in data else 10
        interval = float(data.get("interval", 1.0))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    proc = multiprocessing.Process(
        target=memory_stress_worker,
        args=(total_mb, step_mb, interval),
        daemon=True
    )
    with memory_process_lock:
        memory_processes.append(proc)
    proc.start()

    try:
        app.config["SESSION_REDIS"].sadd(f"stress:mem_pids:{SERVER_NAME}", str(proc.pid))
    except Exception:
        pass


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

    global memory_blocks, memory_processes

    with memory_process_lock:
        for p in memory_processes:
            try:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
                    if p.is_alive():
                        p.kill()
            except Exception:
                pass
        memory_processes.clear()

    try:
        r_client = app.config["SESSION_REDIS"]
        pids = r_client.smembers(f"stress:mem_pids:{SERVER_NAME}")
        for pid_bytes in pids:
            try:
                pid = int(pid_bytes)
                import signal
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        r_client.delete(f"stress:mem_pids:{SERVER_NAME}")
    except Exception:
        pass

    # Also terminate any child processes of current process
    try:
        import os, signal
        my_pid = os.getpid()
        for p in os.listdir('/proc'):
            if p.isdigit() and int(p) not in (1, my_pid):
                try:
                    stat_content = open(f'/proc/{p}/stat').read().split()
                    ppid = int(stat_content[3])
                    if ppid == my_pid:
                        os.kill(int(p), signal.SIGKILL)
                except Exception:
                    pass
    except Exception:
        pass

    with memory_lock:
        memory_blocks.clear()

    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

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
# PROCESS FAILURE STRESS ENDPOINTS
# ============================================================

@app.route(
    "/stress/process/crash",
    methods=["POST"]
)
def inject_process_crash():
    """
    Simulate an application worker failure in a safe, controlled manner.
    Causes /health to return HTTP 500 with 'worker crashed unexpectedly'.
    Automatically expires after duration_s (default 60s) to prevent permanent failure.
    """
    data = request.get_json(silent=True) or {}
    raw_duration = data.get("duration_s", request.args.get("duration_s", DEFAULT_PROCESS_FAILURE_DURATION_S))
    try:
        duration_s = int(raw_duration)
    except (TypeError, ValueError):
        duration_s = DEFAULT_PROCESS_FAILURE_DURATION_S
    duration_s = max(1, min(duration_s, 120))

    with _process_failure_lock:
        _process_failure_injection["active"] = True
        _process_failure_injection["expires_at"] = time.monotonic() + duration_s
        _process_failure_injection["reason"] = "Application worker process crashed unexpectedly"

    return jsonify({
        "message": "Process failure injected (worker simulated crash)",
        "server": SERVER_NAME,
        "master_pid": os.getppid(),
        "worker_pid": os.getpid(),
        "duration_s": duration_s
    })


@app.route(
    "/stress/process/reset",
    methods=["POST"]
)
def reset_process_crash():
    """Immediately clear simulated worker failure."""
    with _process_failure_lock:
        _process_failure_injection["active"] = False
        _process_failure_injection["expires_at"] = 0.0
        _process_failure_injection["reason"] = ""

    return jsonify({
        "message": "Process failure cleared",
        "server": SERVER_NAME,
        "worker_pid": os.getpid()
    })


# ============================================================
# DISK STRESS ENDPOINTS
# ============================================================

@app.route(
    "/stress/disk/fill",
    methods=["POST"]
)
def fill_disk():
    """
    Generate controlled temporary test files inside the dedicated test directory.
    Operates strictly within DISK_STRESS_DIR and enforces a strict maximum allocation limit.
    """
    data = request.get_json(silent=True) or {}

    try:
        size_mb = parse_bounded_integer(
            data, "size_mb", DEFAULT_DISK_STRESS_SIZE_MB, MIN_DISK_STRESS_SIZE_MB, MAX_DISK_STRESS_SIZE_MB
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    raw_duration = data.get("duration_s", request.args.get("duration_s", DEFAULT_DISK_STRESS_DURATION_S))
    try:
        duration_s = max(1, min(int(raw_duration), 300))
    except (TypeError, ValueError):
        duration_s = DEFAULT_DISK_STRESS_DURATION_S

    simulated_percent = float(data.get("simulated_percent", 92.0))

    with _disk_stress_lock:
        os.makedirs(DISK_STRESS_DIR, exist_ok=True)
        file_id = f"disk_stress_{int(time.time())}_{uuid.uuid4().hex[:8]}.dat"
        file_path = os.path.join(DISK_STRESS_DIR, file_id)

        # Allocate test file safely with null bytes (1MB chunk size)
        chunk = b"\0" * (1024 * 1024)
        with open(file_path, "wb") as f:
            for _ in range(size_mb):
                f.write(chunk)

        # Write metadata file for monitoring and safety verification
        meta_path = os.path.join(DISK_STRESS_DIR, ".metadata.json")
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + duration_s,
            "duration_s": duration_s,
            "size_mb": size_mb,
            "simulated_percent": simulated_percent,
            "files": [file_id]
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f)

    # Auto-cleanup timer ensures test artifacts do not persist indefinitely
    def _auto_cleanup():
        time.sleep(duration_s)
        try:
            with _disk_stress_lock:
                if os.path.exists(file_path) and is_safe_disk_cleanup_path(file_path):
                    os.remove(file_path)
                if os.path.exists(meta_path):
                    remaining = [
                        fname for fname in os.listdir(DISK_STRESS_DIR)
                        if fname.startswith("disk_stress_")
                    ]
                    if not remaining:
                        os.remove(meta_path)
        except Exception:
            pass

    threading.Thread(target=_auto_cleanup, daemon=True).start()

    return jsonify({
        "message": "Disk stress files created",
        "server": SERVER_NAME,
        "directory": DISK_STRESS_DIR,
        "file": file_id,
        "size_mb": size_mb,
        "simulated_percent": simulated_percent,
        "duration_s": duration_s
    })


@app.route(
    "/stress/disk/reset",
    methods=["POST"]
)
def reset_disk():
    """
    Remove all temporary test files created by the disk stress mechanism.
    Restricted strictly to approved test directory and validated safe filenames.
    """
    reclaimed_bytes = 0
    removed_files = []

    with _disk_stress_lock:
        if os.path.exists(DISK_STRESS_DIR):
            for entry in os.listdir(DISK_STRESS_DIR):
                target_path = os.path.join(DISK_STRESS_DIR, entry)
                if is_safe_disk_cleanup_path(target_path, DISK_STRESS_DIR) and os.path.isfile(target_path):
                    try:
                        size = os.path.getsize(target_path)
                        os.remove(target_path)
                        reclaimed_bytes += size
                        removed_files.append(entry)
                    except Exception:
                        pass

    return jsonify({
        "message": "Disk stress files cleaned",
        "server": SERVER_NAME,
        "directory": DISK_STRESS_DIR,
        "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2),
        "files_removed": removed_files
    })


@app.route(
    "/stress/disk/status",
    methods=["GET"]
)
def status_disk():
    """Report current temporary disk stress allocation inside the dedicated test directory."""
    files_list = []
    total_bytes = 0

    if os.path.exists(DISK_STRESS_DIR):
        for entry in os.listdir(DISK_STRESS_DIR):
            target_path = os.path.join(DISK_STRESS_DIR, entry)
            if is_safe_disk_cleanup_path(target_path, DISK_STRESS_DIR) and os.path.isfile(target_path):
                size = os.path.getsize(target_path)
                total_bytes += size
                files_list.append({
                    "name": entry,
                    "size_mb": round(size / (1024 * 1024), 2)
                })

    return jsonify({
        "server": SERVER_NAME,
        "directory": DISK_STRESS_DIR,
        "active": total_bytes > 0,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "files": files_list
    })


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=5000

    )
