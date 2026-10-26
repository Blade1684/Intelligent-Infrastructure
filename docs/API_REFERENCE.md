# Intelligent Infrastructure API Reference Manual

**Project:** AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention  
**Document:** Complete API Reference Manual  
**Version:** Phase 5.5.4 Documentation  
**Status:** Verified Against Live Implementation  

---

## 1. Overview and Base URLs

The cluster exposes endpoints through two primary surfaces:
1. **Application Backends (`app1`–`app5`)**: Mapped to host ports `5002` through `5006` (internally port `5000`), or via HAProxy gateway at `http://localhost:8080/`.
2. **Monitoring & Decision Engine (`monitor`)**: Mapped to host port `5001` (internally port `5001`), or via Dashboard reverse proxy at `http://localhost:8500/api/monitor/`.

---

## 2. Application Backend API Reference

### 2.1 Standard Web Workload & Session Endpoints

#### `GET /`
- **Purpose:** Home landing endpoint returning server identification and session state.
- **Parameters:** None.
- **Example Request:**
  ```http
  GET / HTTP/1.1
  Host: localhost:8080
  ```
- **Example Response (`200 OK`):**
  ```json
  {
    "hostname": "9b12a84c2e11",
    "logged_in_user": null,
    "message": "Intelligent Infrastructure Demo",
    "server": "Server-1",
    "status": "healthy"
  }
  ```

#### `POST /login`
- **Purpose:** Authenticates a user session backed by Redis.
- **Request Body (JSON):**
  - `username` (string, required): Username for the session.
- **Example Request:**
  ```json
  {
    "username": "operator1"
  }
  ```
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Logged in as operator1",
    "server": "Server-1",
    "username": "operator1",
    "work_counter": 0
  }
  ```
- **Error Response (`400 Bad Request`):**
  ```json
  {
    "error": "Username is required"
  }
  ```

#### `POST /work`
- **Purpose:** Increments the user's persistent transaction work counter in Redis.
- **Headers:** Session cookie from `/login`.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Work completed",
    "server": "Server-2",
    "username": "operator1",
    "work_counter": 1
  }
  ```
- **Error Response (`401 Unauthorized`):**
  ```json
  {
    "error": "Not logged in"
  }
  ```

#### `GET /session`
- **Purpose:** Queries current session status and active transaction counter.
- **Example Response (`200 OK` - Logged in):**
  ```json
  {
    "logged_in": true,
    "server": "Server-3",
    "username": "operator1",
    "work_counter": 1
  }
  ```

#### `GET /health`
- **Purpose:** Health probe used by HAProxy, Docker, and the Monitor. Verifies local worker state and Redis connectivity.
- **Example Response (`200 OK`):**
  ```json
  {
    "server": "Server-1",
    "status": "healthy"
  }
  ```
- **Error Response (`500 Internal Server Error` - Process Crash Injected):**
  ```json
  {
    "error": "Application worker process crashed unexpectedly",
    "master_pid": 1,
    "server": "Server-1",
    "status": "unhealthy",
    "worker_pid": 18
  }
  ```
- **Error Response (`503 Service Unavailable` - Redis Down):**
  ```json
  {
    "dependency": "redis",
    "error": "Error connecting to redis:6379",
    "server": "Server-1",
    "status": "unhealthy"
  }
  ```

---

### 2.2 Fault-Injection & Stress Simulation Endpoints

#### `POST /stress/cpu`
- **Purpose:** Starts a background multiprocessing calculation loop generating high CPU usage visible at the container level.
- **Request Body (JSON):**
  - `duration` (integer, optional, default: 30, range: 1–120): Duration in seconds.
- **Example Response (`200 OK`):**
  ```json
  {
    "duration_seconds": 30,
    "message": "CPU stress started",
    "process_id": 42,
    "server": "Server-1"
  }
  ```

#### `POST /stress/cpu/stop`
- **Purpose:** Immediately terminates CPU stress worker processes by PID without restarting the container or interrupting Gunicorn.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "CPU stress stopped",
    "processes_stopped": 1,
    "server": "Server-1"
  }
  ```

#### `POST /stress/memory`
- **Purpose:** Spawns a dedicated subprocess that progressively allocates bytearrays to simulate a memory leak.
- **Request Body (JSON):**
  - `total_mb` (integer, optional, default: 100, range: 1–300): Target memory allocation in MB.
  - `step_mb` (integer, optional, default: 10, range: 1–100): Allocation step size.
  - `interval` (float, optional, default: 1.0): Seconds between allocation steps.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Memory stress started",
    "server": "Server-1",
    "target_memory_mb": 150
  }
  ```

#### `POST /stress/memory/clear`
- **Purpose:** Kills memory stress worker subprocesses, clears bytearray references, triggers garbage collection, and calls `malloc_trim(0)`.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Allocated demonstration memory cleared",
    "server": "Server-1"
  }
  ```

#### `POST /stress/latency`
- **Purpose:** Injects a controlled response delay into `/health` probes.
- **Request Body (JSON or Query Params):**
  - `delay_ms` (float, required, range: 0–5000): Response delay in milliseconds.
  - `duration_s` (integer, optional, default: 30, range: 1–120): Expiration time in seconds.
- **Example Response (`200 OK`):**
  ```json
  {
    "delay_ms": 600.0,
    "duration_s": 30,
    "expires_in_s": 30,
    "message": "Latency injection active",
    "server": "Server-1"
  }
  ```

#### `POST /stress/latency/reset`
- **Purpose:** Immediately removes any active latency injection on `/health`.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Latency injection cleared",
    "server": "Server-1"
  }
  ```

#### `POST /stress/process/crash`
- **Purpose:** Simulates an application worker failure. Causes `/health` to immediately return HTTP 500. Automatically expires after `duration_s`.
- **Request Body (JSON):**
  - `duration_s` (integer, optional, default: 60, range: 1–120): Expiration time in seconds.
- **Example Response (`200 OK`):**
  ```json
  {
    "duration_s": 60,
    "master_pid": 1,
    "message": "Process failure injected (worker simulated crash)",
    "server": "Server-1",
    "worker_pid": 14
  }
  ```

#### `POST /stress/process/reset`
- **Purpose:** Clears simulated worker failure state immediately.
- **Example Response (`200 OK`):**
  ```json
  {
    "message": "Process failure cleared",
    "server": "Server-1",
    "worker_pid": 14
  }
  ```

#### `POST /stress/disk/fill`
- **Purpose:** Creates temporary test files strictly within `/tmp/test_disk_fill/` to simulate disk consumption.
- **Request Body (JSON):**
  - `size_mb` (integer, optional, default: 50, range: 1–200): Total MB to allocate.
  - `duration_s` (integer, optional, default: 60, range: 1–300): Auto-cleanup duration in seconds.
  - `simulated_percent` (float, optional, default: 92.0): Reported percentage for testing.
- **Example Response (`200 OK`):**
  ```json
  {
    "directory": "/tmp/test_disk_fill",
    "duration_s": 60,
    "file": "disk_stress_1729758000_a1b2c3d4.dat",
    "message": "Disk stress files created",
    "server": "Server-1",
    "size_mb": 50,
    "simulated_percent": 92.0
  }
  ```

#### `POST /stress/disk/reset`
- **Purpose:** Removes all temporary test files inside `/tmp/test_disk_fill/` matching the approved naming pattern.
- **Example Response (`200 OK`):**
  ```json
  {
    "directory": "/tmp/test_disk_fill",
    "files_removed": ["disk_stress_1729758000_a1b2c3d4.dat"],
    "message": "Disk stress files cleaned",
    "reclaimed_mb": 50.0,
    "server": "Server-1"
  }
  ```

#### `GET /stress/disk/status`
- **Purpose:** Queries active temporary disk allocations in `/tmp/test_disk_fill/`.
- **Example Response (`200 OK`):**
  ```json
  {
    "active": true,
    "directory": "/tmp/test_disk_fill",
    "files": [
      {
        "name": "disk_stress_1729758000_a1b2c3d4.dat",
        "size_mb": 50.0
      }
    ],
    "server": "Server-1",
    "total_mb": 50.0
  }
  ```

---

## 3. Monitoring & Decision Engine API Reference

All routes below run on `http://localhost:5001` (or `http://localhost:8500/api/monitor/`).

### 3.1 Telemetry, Anomalies & HAProxy Status

#### `GET /metrics`
- **Purpose:** Returns the latest instantaneous telemetry for all 5 monitored servers.
- **Example Response (`200 OK`):**
  ```json
  {
    "servers": [
      {
        "container": "app1",
        "cpu_percent": 2.15,
        "disk_percent": 14.2,
        "disk_total_mb": 61440.0,
        "disk_used_mb": 8724.5,
        "latency_ms": 6.8,
        "memory_mb": 42.1,
        "service_available": true,
        "status": "running",
        "timestamp": "2026-10-24T09:30:00.123456+00:00"
      }
    ],
    "timestamp": "2026-10-24T09:30:00.200000+00:00"
  }
  ```

#### `GET /history/<server>`
- **Purpose:** Retrieves the sliding-window time series history (up to 100 samples) for a specific server (`app1`–`app5`).
- **Example Response (`200 OK`):**
  ```json
  {
    "sample_count": 25,
    "samples": [
      {"cpu_percent": 1.8, "memory_mb": 41.2, "latency_ms": 5.4, "timestamp": 1729758000.0}
    ],
    "server": "app1"
  }
  ```

#### `GET /anomalies/<server>`
- **Purpose:** Runs instantaneous anomaly detection (process checks, disk usage, latency threshold, Isolation Forest) for a specific server.
- **Example Response (`200 OK` - Normal):**
  ```json
  {
    "anomaly": false,
    "reason": "Normal metrics",
    "server": "app1"
  }
  ```
- **Example Response (`200 OK` - Anomaly Detected):**
  ```json
  {
    "anomaly": true,
    "current_latency_ms": 620.0,
    "reason": "Sustained response latency exceeded threshold",
    "server": "app1",
    "sustained_count": 3,
    "threshold_ms": 500.0,
    "type": "high_latency"
  }
  ```

#### `GET /events/<server>`
- **Purpose:** Returns the recent operational audit trail and active incident status for a server.
- **Example Response (`200 OK`):**
  ```json
  {
    "active_incident": null,
    "event_count": 4,
    "events": [
      {
        "details": {"action": "weight_reduced", "new_weight": 50},
        "event_type": "traffic_mitigation_applied",
        "server": "app1",
        "timestamp": "2026-10-24T09:32:00.000000+00:00"
      }
    ],
    "server": "app1"
  }
  ```

#### `GET /servers`
- **Purpose:** Combined operational state overview covering Redis state, HAProxy routing status, and health codes.
- **Example Response (`200 OK`):**
  ```json
  {
    "haproxy_available": true,
    "servers": [
      {
        "haproxy_check_code": "200",
        "haproxy_check_status": "L7OK",
        "haproxy_eligible": true,
        "haproxy_status": "UP",
        "reason": null,
        "server": "app1",
        "state": "healthy",
        "updated_at": "2026-10-24T09:30:00.000000+00:00"
      }
    ]
  }
  ```

#### `GET /haproxy/status`
- **Purpose:** Queries live backend routing metrics directly from HAProxy over `/tmp/haproxy.sock`.
- **Example Response (`200 OK`):**
  ```json
  {
    "backends": {
      "app1": {
        "check_code": "200",
        "check_status": "L7OK",
        "is_up": true,
        "status": "UP",
        "weight": 100
      }
    },
    "status": "healthy",
    "timestamp": "2026-10-24T09:30:00.000000+00:00"
  }
  ```

#### `POST /haproxy/maintenance/<server>/<action>`
- **Purpose:** Operator override to place a backend in maintenance (`action=enable`) or ready status (`action=disable`).
- **Example Request:** `POST /haproxy/maintenance/app1/enable`
- **Example Response (`200 OK`):**
  ```json
  {
    "action": "maint",
    "output": "",
    "server": "app1",
    "success": true
  }
  ```

---

### 3.2 Intelligent Traffic Overload Detection API (Phase 5.1)

#### `GET /overload/status`
- **Purpose:** Returns cluster-wide overload classification, active traffic summary, and per-server states.
- **Example Response (`200 OK`):**
  ```json
  {
    "cluster_classification": "NORMAL",
    "servers": {
      "app1": {
        "classification": "NORMAL",
        "consecutive_anomalous_cycles": 0,
        "consecutive_normal_cycles": 12,
        "contributing_metrics": [],
        "score": 0.0
      }
    },
    "timestamp": "2026-10-24T09:30:00.000000+00:00",
    "traffic_summary": {
      "avg_rps": 12.4,
      "error_rate_pct": 0.0,
      "workload_profile": "normal"
    }
  }
  ```

#### `GET /overload/servers/<server>`
- **Purpose:** Retrieves granular scoring, thresholds, and debouncing counters for a specific server.
- **Example Response (`200 OK`):**
  ```json
  {
    "classification": "OVERLOAD",
    "consecutive_anomalous_cycles": 2,
    "contributing_metrics": ["cpu_percent"],
    "metrics": {"cpu_percent": 88.5, "latency_ms": 12.0, "memory_mb": 45.0},
    "score": 88.5,
    "server": "app1"
  }
  ```

#### `GET /overload/config` and `POST /overload/config`
- **Purpose:** Gets or dynamically updates threshold scoring boundaries and debouncing parameters.
- **Example Response (`200 OK`):**
  ```json
  {
    "anomalous_cycles_required": 2,
    "cpu_threshold_overload": 80.0,
    "latency_threshold_ms": 300.0,
    "normal_cycles_required": 3
  }
  ```

---

### 3.3 Dynamic Traffic Management API (Phase 5.2)

#### `GET /traffic/status`
- **Purpose:** Returns HAProxy weight allocations, maintenance states, and eligible backend counts.
- **Example Response (`200 OK`):**
  ```json
  {
    "eligible_count": 5,
    "excluded_count": 0,
    "servers": {
      "app1": {
        "current_state": "NORMAL",
        "current_weight": 100,
        "in_maint": false,
        "last_action": "NONE",
        "server": "app1"
      }
    }
  }
  ```

#### `GET /traffic/weights`
- **Purpose:** Returns the current HAProxy traffic distribution weights across all 5 backends.
- **Example Response (`200 OK`):**
  ```json
  {
    "app1": 100,
    "app2": 100,
    "app3": 100,
    "app4": 100,
    "app5": 100
  }
  ```

#### `POST /traffic/restore/<server>`
- **Purpose:** Manually restores an excluded or weight-reduced backend back to normal weight 100 and ready state.
- **Query / Body Parameter:** `force=true` (bypasses health requirement).
- **Example Response (`200 OK`):**
  ```json
  {
    "new_state": "NORMAL",
    "new_weight": 100,
    "server": "app1",
    "success": true
  }
  ```

---

### 3.4 Automated Fault Recovery API (Phase 5.3)

#### `GET /recovery/status`
- **Purpose:** Returns cluster recovery status, active remediations, and cooldown timers.
- **Example Response (`200 OK`):**
  ```json
  {
    "active_recoveries": [],
    "circuit_breakers": {
      "app1": {"consecutive_failures": 0, "tripped": false}
    },
    "enabled": true,
    "recent_incidents": 3
  }
  ```

#### `POST /recovery/enable` and `POST /recovery/disable`
- **Purpose:** Globally enables or pauses automated self-healing actions.
- **Example Response (`200 OK`):**
  ```json
  {
    "config": {
      "cooldown_seconds": 15.0,
      "enabled": true,
      "max_attempts": 2
    },
    "message": "Automatic recovery enabled"
  }
  ```

#### `POST /recovery/execute/<server>`
- **Purpose:** Dispatches an operator-triggered remediation action for a specific server.
- **Request Body (JSON):**
  - `strategy` (string, required): One of `cpu_worker_termination`, `memory_cleanup`, `process_reload`, `disk_cleanup`, `container_restart`, `latency_reset`.
- **Example Response (`200 OK`):**
  ```json
  {
    "details": {"processes_stopped": 1},
    "server": "app1",
    "strategy": "cpu_worker_termination",
    "success": true
  }
  ```

---

### 3.5 Predictive Failure Prevention API (Phase 5.4)

#### `GET /prediction/status`
- **Purpose:** Retrieves cluster predictive states, early warnings, and estimated times-to-saturation.
- **Example Response (`200 OK`):**
  ```json
  {
    "cluster_state": "NORMAL",
    "servers": {
      "app1": {
        "confidence": 0.85,
        "lead_metric": "cpu_percent",
        "predictive_state": "NORMAL",
        "sample_count": 22,
        "time_to_saturation_seconds": null
      }
    },
    "timestamp": "2026-10-24T09:30:00.000000+00:00"
  }
  ```

#### `GET /prediction/accuracy`
- **Purpose:** Returns empirical model evaluation metrics and confusion matrix values.
- **Example Response (`200 OK`):**
  ```json
  {
    "confusion_matrix": {
      "false_negatives": 0,
      "false_positives": 0,
      "true_negatives": 12,
      "true_positives": 8
    },
    "metrics": {
      "f1_score": 1.0,
      "precision": 1.0,
      "recall": 1.0,
      "total_evaluated": 20
    }
  }
  ```

#### `POST /prediction/traffic/enable` and `POST /prediction/traffic/disable`
- **Purpose:** Enables or disables conservative preventive traffic management (weight shedding based on high-confidence saturation warnings).
- **Example Response (`200 OK`):**
  ```json
  {
    "predictive_traffic_enabled": true,
    "success": true
  }
  ```

---

### 3.6 Locust Load Testing API (Phase 4)

#### `GET /load-test/profiles`
- **Purpose:** Returns pre-configured traffic profiles (`normal`, `moderate`, `heavy`).
- **Example Response (`200 OK`):**
  ```json
  {
    "heavy": {"duration_seconds": 60, "spawn_rate": 10, "user_count": 50},
    "moderate": {"duration_seconds": 60, "spawn_rate": 5, "user_count": 20},
    "normal": {"duration_seconds": 60, "spawn_rate": 2, "user_count": 5}
  }
  ```

#### `POST /load-test/start`
- **Purpose:** Starts a headless Locust load testing session against HAProxy.
- **Request Body (JSON):**
  - `profile` (string, optional): Profile name (`normal`, `moderate`, `heavy`).
  - `user_count` (integer, optional): Override concurrent users.
  - `duration_seconds` (integer, optional): Duration in seconds.
- **Example Response (`200 OK`):**
  ```json
  {
    "config": {"profile": "normal", "user_count": 5},
    "session_id": "locust-sess-1729758000",
    "status": "running"
  }
  ```

#### `POST /load-test/stop`
- **Purpose:** Stops the currently running Locust session.
- **Example Response (`200 OK`):**
  ```json
  {
    "session_id": "locust-sess-1729758000",
    "status": "stopped"
  }
  ```

#### `GET /load-test/metrics`
- **Purpose:** Fetches live real-time RPS, latency, and error rate telemetry from Locust.
- **Example Response (`200 OK`):**
  ```json
  {
    "avg_latency_ms": 4.8,
    "current_rps": 12.4,
    "error_rate_pct": 0.0,
    "total_requests": 340
  }
  ```
