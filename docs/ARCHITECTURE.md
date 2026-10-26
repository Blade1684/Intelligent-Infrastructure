# System Architecture and Implementation Specification

**Project:** AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention  
**Document:** Architecture and Implementation Specification  
**Version:** Phase 5.5.4 Documentation  
**Status:** Validated on Live Infrastructure  

---

## 1. High-Level Architectural Topology

The system comprises **11 containerized services** networked through a private Docker bridge (`infrastructure-network`). The design decouples client request processing from monitoring, telemetry analysis, and remediation pipelines to ensure that monitoring operations cannot degrade live application traffic.

```
                                      [ Clients / Traffic Generator ]
                                                     │
                                                     ▼
                                        ┌─────────────────────────┐
                                        │  HAProxy Load Balancer  │
                                        │ (Port 8080 / Stats:8404)│
                                        └────────────┬────────────┘
                                                     │ Layer 7 Routing
                        ┌──────────────┬─────────────┼──────────────┬──────────────┐
                        ▼              ▼             ▼              ▼              ▼
                  ┌───────────┐  ┌───────────┐ ┌───────────┐  ┌───────────┐  ┌───────────┐
                  │   App 1   │  │   App 2   │ │   App 3   │  │   App 4   │  │   App 5   │
                  │ (:5002)   │  │ (:5003)   │ │ (:5004)   │  │ (:5005)   │  │ (:5006)   │
                  └─────┬─────┘  └─────┬─────┘ └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
                        │              │             │              │              │
                        └──────────────┴──────┬──────┴──────────────┴──────────────┘
                                              │ Session Replication / Shared Locks
                                              ▼
                                    ┌───────────────────┐
                                    │   Redis 7 Store   │
                                    │      (:6379)      │
                                    └─────────▲─────────┘
                                              │ State, Events, Time Series
                                              ▼
                                    ┌───────────────────┐
                                    │ Monitor & Decision│
                                    │   Engine (:5001)  │
                                    └─────────┬─────────┘
                                              │
                        ┌─────────────────────┴─────────────────────┐
                        ▼                                           ▼
             ┌─────────────────────┐                     ┌─────────────────────┐
             │   React Dashboard   │                     │  Locust Load Engine │
             │     (Port 8500)     │                     │     (Port 8089)     │
             └─────────────────────┘                     └─────────────────────┘
```

---

## 2. Container Inventory & Runtime State

The cluster consists of 11 distinct containers defined in [`docker-compose.yml`](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docker-compose.yml):

| Service | Container Name | Base Image / Build | Port Mappings | Health Status Mode |
| :--- | :--- | :--- | :--- | :--- |
| **`app1`** | `intelligent-infrastructure-app1-1` | `./app` (Python 3.11/Gunicorn) | `5002:5000` | Docker `HEALTHCHECK` (`healthy`) |
| **`app2`** | `intelligent-infrastructure-app2-1` | `./app` (Python 3.11/Gunicorn) | `5003:5000` | Docker `HEALTHCHECK` (`healthy`) |
| **`app3`** | `intelligent-infrastructure-app3-1` | `./app` (Python 3.11/Gunicorn) | `5004:5000` | Docker `HEALTHCHECK` (`healthy`) |
| **`app4`** | `intelligent-infrastructure-app4-1` | `./app` (Python 3.11/Gunicorn) | `5005:5000` | Docker `HEALTHCHECK` (`healthy`) |
| **`app5`** | `intelligent-infrastructure-app5-1` | `./app` (Python 3.11/Gunicorn) | `5006:5000` | Docker `HEALTHCHECK` (`healthy`) |
| **`redis`** | `intelligent-infrastructure-redis-1` | `redis:7-alpine` | `6379:6379` (internal) | Docker `HEALTHCHECK` (`healthy`) |
| **`haproxy`** | `intelligent-infrastructure-haproxy-1`| `haproxy:2.8-alpine` | `8080:80`, `8404:8404`| Docker standard (`running`) |
| **`monitor`** | `intelligent-infrastructure-monitor-1`| `./monitor` (Python 3.11) | `5001:5001` | Docker standard (`running`) |
| **`dashboard`**| `intelligent-infrastructure-dashboard-1`| `./dashboard` (Nginx/HTML5) | `8500:80` | Docker standard (`running`) |
| **`locust`** | `intelligent-infrastructure-locust-1` | `locustio/locust:2.47.0` | `8089:8089` | Docker standard (`running`) |
| **`nginx`** | `intelligent-infrastructure-nginx-1` | `nginx:alpine` | `8081:80` | Docker standard (`running`) |

> **Critical Health Check Distinction:**
> While all 11 containers are active and operational in Docker (`state == "running"`), **only six containers** (`app1`–`app5` and `redis`) define native Dockerfile/Compose `HEALTHCHECK` instructions. Consequently, Docker CLI reports those six as `healthy`, while the remaining five (`haproxy`, `monitor`, `dashboard`, `locust`, `nginx`) report standard `running` status without an internal Docker health evaluator.

---

## 3. Detailed Component Breakdown

### 3.1 Five Application Backends (`app1` through `app5`)
Each backend runs a Flask application served by Gunicorn workers. 
- **Session Continuity:** User sessions are backed by Redis via `flask-session`. Sessions are serialized to Redis key prefixes, allowing requests from any user session to be routed to any healthy backend without losing authentication state or transaction counters.
- **Health Probe:** Exposes `GET /health`, verifying local process integrity and validating Redis connectivity via `SESSION_REDIS.ping()`.
- **Fault Simulation Instrumentation:** Built-in endpoints allow safe, controlled fault induction:
  - `/stress/cpu`: Dispatches dedicated multiprocessing workers executing modulo arithmetic loops.
  - `/stress/cpu/stop`: Terminates stress worker PIDs without impacting Gunicorn master or worker processes.
  - `/stress/memory`: Allocates memory chunks in a dedicated subprocess.
  - `/stress/memory/clear`: Kills the memory process, clears bytearrays, and calls `malloc_trim(0)`.
  - `/stress/latency`: Intercepts `/health` requests with a bounded `time.sleep()`.
  - `/stress/disk/fill`: Creates test files strictly within `/tmp/test_disk_fill/`.
  - `/stress/process/crash`: Simulates application worker crash returning HTTP 500.

### 3.2 HAProxy Load Balancer (Layer 7 Reverse Proxy)
- **Routing Strategy:** Round-robin balance across `app1` through `app5` with equal default weight (`weight 100`).
- **Health Checking:** Issues `GET /health` probes every 2 seconds (`inter 2s fall 2 rise 2`). If a backend fails two consecutive checks, HAProxy immediately routes client traffic away from it.
- **Runtime API Socket (`/tmp/haproxy.sock`):** HAProxy exposes an admin UNIX socket mounted into both HAProxy and the Monitor containers. Through this socket, the Monitor can:
  - Dynamically alter backend weights (`set weight app_cluster/app1 50`).
  - Place backends in administrative maintenance (`set server app_cluster/app1 state maint`).
  - Readmit backends to active routing (`set server app_cluster/app1 state ready`).
- **Socket Responsiveness:** Socket commands execute with an observed latency of **~7.63 ms**, allowing instant traffic redirection without restarting the proxy process or dropping open TCP connections.

### 3.3 Redis 7 State Store
- **Session Layer:** Stores user authentication and workload counters for web clients.
- **Telemetry Buffering:** Maintains bounded sliding windows (`LPUSH` / `LTRIM`) for time-series metrics (`metrics:<server>`), historical anomaly logs (`events:<server>`), and recovery incidents (`incident:<server>`).
- **Distributed Locks & Hysteresis:** Manages readmission cycle counters (`readmission_cycles:<server>`) and temporary exclusion records to prevent race conditions across multi-threaded monitor subroutines.

### 3.4 Monitoring & Decision Engine
Runs as a continuous daemon in `intelligent-infrastructure-monitor-1` executing five modular subsystems:
1. **Collector (`collector.py` / `monitor.py`):** Queries Docker API (`container.stats(stream=False)`) and HTTP health probes (`http://<server>:5000/health`) for all 5 servers sequentially.
2. **Anomaly Detector (`detector.py`):**
   - Evaluates process health, container lifecycle state, and HTTP error codes.
   - Evaluates disk thresholds (`DEFAULT_DISK_THRESHOLD_PERCENT = 85.0%`).
   - Evaluates sustained response latency (`DEFAULT_LATENCY_THRESHOLD_MS = 500.0ms` across 3 checks).
   - Trains an unsupervised **Isolation Forest** model (`sklearn.ensemble.IsolationForest`) with clean-sample baseline filtering to detect multivariate CPU/Memory outliers.
3. **Overload Classification Engine (`overload_detector.py`):** Classifies servers into four operational states (`NORMAL`, `ELEVATED`, `OVERLOAD`, `CRITICAL`) using multi-contributor scoring and state debouncing.
4. **Intelligent Traffic Manager (`traffic_manager.py`):**
   - Applies dynamic weight shedding (`weight 100 -> 50`) when a server reaches `OVERLOAD`.
   - Places a server in administrative maintenance when it reaches `CRITICAL`.
   - Enforces the **Minimum Eligible Backend Guardrail** (always keeps at least 1 backend online; caps simultaneous exclusions at 4).
   - Manages **3-Cycle Re-admission Hysteresis**: requires 3 consecutive healthy cycles plus direct HTTP health check verification before restoring a server to rotation.
5. **Automated Recovery Engine (`recovery.py`):**
   - Diagnoses root causes and maps them to granular, non-destructive strategies.
   - Enforces a **15-second recovery cooldown** to prevent flapping.
   - Enforces **Restart Loop Circuit-Breaking** (`MAX_RESTARTS_PER_WINDOW = 3` within 300 seconds).
   - Rejects container restarts for external network partitions (`action: "awaiting_operator"`).
6. **Predictive Failure Prevention Engine (`prediction_engine.py`):**
   - Implements Ordinary Least Squares ($OLS$) linear regression across CPU, memory, latency, and error rate time series.
   - Enforces a three-tier sample policy: Preliminary ($N \ge 6$), Reliable ($N \ge 15$), Traffic Action ($N \ge 20$).
   - Calculates $R^2$ goodness-of-fit and trend consistency before issuing warnings (`EARLY_WARNING`, `HIGH_RISK`, `PREDICTED_SATURATION`).

### 3.5 React Operations Dashboard
Single-page application served via Nginx on port `8500`. The frontend queries the Monitor API (`/api/monitor/...`) and HAProxy stats (`/api/haproxy/...`) to visualize:
- Server status cards with live CPU, memory, disk, and latency gauges.
- Cluster traffic distribution and dynamic backend weights.
- Early warning trend projections and estimated time-to-saturation.
- Live incident logs, MTTR metrics, and manual control overrides.

### 3.6 Locust Load Testing Engine
Runs in container `intelligent-infrastructure-locust-1` configured with pre-defined load profiles:
- **Normal Profile:** 5 concurrent users, spawn rate 2.
- **Moderate Profile:** 20 concurrent users, spawn rate 5.
- **Heavy Profile (Bounded):** 50 concurrent users, spawn rate 10.
Simulates realistic browser behavior: viewing home, polling health, logging in, executing session work, and logging out.

---

## 4. Lifecycle Flow of an Infrastructure Problem

The diagram below traces how a developing infrastructure failure progresses through detection, traffic mitigation, remediation, and re-admission:

```
[ Fault Triggered on App 2 (e.g. CPU Stress / Memory Leak) ]
                             │
                             ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. Telemetry Polling (Iteration ~20s)                       │
 │    - Docker container.stats() records elevated usage.       │
 │    - HTTP /health probe measures response latency.          │
 └───────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 2. Analysis & Forecasting                                   │
 │    - Detector flags statistical outlier / threshold breach.  │
 │    - PredictionEngine calculates OLS slope & time-to-sat.   │
 │    - OverloadEngine debounces transition to OVERLOAD/CRITICAL│
 └───────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 3. Dynamic Traffic Management (Phase 5.2)                   │
 │    - TrafficManager checks guardrails (min 1 eligible node).│
 │    - Issues HAProxy socket command in < 10ms:               │
 │      * OVERLOAD: 'set weight app_cluster/app2 50'           │
 │      * CRITICAL: 'set server app_cluster/app2 state maint'  │
 │    - HAProxy instantly diverts incoming traffic away.       │
 └───────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 4. Automated Root-Cause Remediation (Phase 5.3)             │
 │    - Checks cooldown (15s) and restart circuit breaker.     │
 │    - Selects least disruptive strategy:                     │
 │      * CPU Spike -> Kill stress worker PID only.            │
 │      * Memory Leak -> Terminate memory subprocess.          │
 │      * Disk Full -> Purge whitelist files in /tmp/.         │
 │      * Crash -> Graceful container restart.                 │
 │    - Verifies post-remediation health via HTTP probe.       │
 └───────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 5. Safe Readmission Hysteresis (Phase 5.2)                  │
 │    - Backend enters AWAITING_READMISSION.                   │
 │    - Requires 3 consecutive clean monitoring cycles.        │
 │    - Performs direct HTTP /health probe verification.       │
 │    - Issues HAProxy socket command:                         │
 │      'set server app_cluster/app2 state ready'              │
 │      'set weight app_cluster/app2 100'                      │
 │    - Traffic distribution seamlessly restored to 20%.       │
 └─────────────────────────────────────────────────────────────┘
```

---

## 5. Performance and Windows/WSL2 Environmental Realities

### 5.1 Monitoring Cycle Execution Duration (~20 Seconds)
In a native Linux deployment, Docker socket IPC commands execute in sub-millisecond time. On **Docker Desktop for Windows**, API calls traverse a virtualized named pipe translation bridge into the WSL2 VM.
- Each synchronous `container.stats(stream=False)` call takes approximately 1.8 to 2.2 seconds per container.
- Querying 5 application backends sequentially accumulates **~10–12 seconds** of raw Docker socket latency.
- Factoring in HTTP health checks, Redis reads, regression slope calculations, and HAProxy socket telemetry, **the monitoring cycle averages 20.01 seconds** (min: 19.86s, max: 20.43s).

### 5.2 Mathematical Correctness vs. Detection Delay
- **Mathematical Exactness Preserved:** The linear regression analyzer records actual Unix epoch timestamps for each sample. Slopes are calculated as $\Delta y / \Delta t$ where $\Delta t$ is measured in exact elapsed seconds. Goodness-of-fit ($R^2$) and time-to-saturation calculations remain mathematically precise.
- **Detection & Response Delay:** While the math is unaffected, the physical sampling rate is constrained to once every ~20 seconds. An abrupt failure occurring immediately after a collection pass will take up to ~20 seconds to be ingested by the monitor. HAProxy's internal health check (`inter 2s fall 2`) operates independently and detects hard network outages much faster (~4s), mitigating client impact while the monitor prepares remediation.

### 5.3 HAProxy Socket Latency vs. End-to-End Mitigation Time
- **Socket Latency (~7.63 ms):** The time required for HAProxy to parse and execute a weight reduction or maintenance exclusion command over `/tmp/haproxy.sock` is sub-10ms.
- **End-to-End Mitigation Time (~20–40 s):** The elapsed time from when an anomaly begins to when traffic management takes action includes:
  1. Monitoring cycle collection interval (~20s).
  2. Debouncing confirmation window (1–2 cycles, ~20–40s).
  3. Dispatch of the 7.63ms socket command.
This clear distinction must be maintained when evaluating operational SLAs.
