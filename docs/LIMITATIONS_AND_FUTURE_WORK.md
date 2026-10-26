# Known Technical Limitations and Future Engineering Roadmap

**Project:** AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention  
**Document:** Technical Limitations, Architectural Constraints, and Engineering Roadmap  
**Version:** Phase 5.5.4 Documentation  
**Status:** Engineering Review  

---

## 1. Environmental & Architectural Limitations

### 1.1 Docker Desktop on Windows (WSL2) Monitoring Cycle Overhead (~20 Seconds)
- **Constraint Description:** During steady-state execution across all 11 containers, each complete monitoring iteration in `monitor.py` requires an average of **20.01 seconds** (min: 19.86s, max: 20.43s).
- **Technical Root Cause:** On Windows 11 with Docker Desktop, the Docker Engine daemon runs inside a WSL2 virtual machine. When the Python Monitor container calls `container.stats(stream=False)`, the HTTP request traverses the named pipe / UNIX socket virtualization bridge. Each synchronous stats call takes between 1.8 and 2.2 seconds per container. Across 5 application containers, this sequential IPC overhead accounts for 10–12 seconds of raw I/O wait time per cycle.
- **Impact on Mathematical Accuracy:** All metric samples stored in Redis record their exact Unix epoch timestamp. Ordinary Least Squares ($OLS$) trend regressions calculate the dimensional slope as:
  $$\text{slope} = \frac{\Delta y}{\Delta t}$$
  Because $\Delta t$ uses the exact elapsed wall-clock seconds between sample timestamps rather than assumed tick counts, **the mathematical slope, $R^2$ goodness-of-fit, and time-to-saturation calculations remain fully sound and accurate**.
- **Impact on Detection & Recovery Latency:** While the math is unaffected, the physical sampling frequency is bounded to approximately 3 samples per minute. As a result:
  - An abrupt crash occurring immediately after a polling pass may take up to ~20 seconds to be ingested by the monitoring loop.
  - Debounced state transitions requiring 2 consecutive anomalous cycles take ~40 seconds to confirm.
  - Re-admission hysteresis requiring 3 consecutive healthy cycles takes ~60 seconds to complete.
- **Independent Fast-Path Failover:** HAProxy's internal TCP/HTTP health checker (`inter 2s fall 2`) operates completely independently of the Python monitoring daemon. It detects hard network/worker outages within ~4 seconds, shielding end users from HTTP 502/503 errors while the monitoring daemon diagnoses the underlying root cause.

### 1.2 HAProxy Socket Latency vs. End-to-End Mitigation Latency
A clear operational distinction must be maintained between:
- **HAProxy UNIX Socket Command Latency (~7.63 ms):** The time required for the HAProxy process to parse, validate, and execute a dynamic weight adjustment or server maintenance command over `/tmp/haproxy.sock`. This is near-instantaneous.
- **End-to-End Mitigation Latency (~20–40 seconds):** The elapsed wall-clock time from when an application starts experiencing CPU or memory degradation until traffic is dynamically shed. This includes the ~20s monitoring iteration delay plus confirmation debouncing before the 7.63ms command is dispatched.

### 1.3 Single-Host Bounded Load Testing
- **Constraint Description:** The heavy workload profile in Locust was evaluated at **50 concurrent users** (~93–102 RPS average, ~150 RPS peak).
- **Boundary Clarification:** This is a bounded evaluation on a single local development machine running Docker Desktop. Because all 11 containers and the Locust traffic generator share the host's virtualized CPU cores, pushing concurrency beyond 50 users induces CPU throttling in the hypervisor, starving the application backends. This test validates cluster routing stability under local stress and is **not** a high-throughput production capacity benchmark.

### 1.4 Single-Instance State Store (Redis)
- **Constraint Description:** Redis runs as a single container (`intelligent-infrastructure-redis-1`).
- **Operational Risk:** If Redis crashes or its volume corrupts, active user sessions and ephemeral metric sliding windows are lost. While the Monitor includes graceful degradation fallbacks (catching Redis exceptions and logging warnings without crashing), prolonged Redis downtime prevents session persistence and cross-worker lock synchronization.

### 1.5 Docker Socket Mounting Security Exposure
- **Constraint Description:** The monitor container mounts the host Docker socket (`/var/run/docker.sock:/var/run/docker.sock`).
- **Security Implication:** In containerized environments, granting a container access to the Docker socket effectively provides root-equivalent access to the Docker host. While necessary in this prototype for container restarts and stats inspection, this pattern violates least-privilege principles for production enterprise multi-tenant clouds.

### 1.6 Network Boundary Differences
- **Internal vs. External DNS:** Inside the Docker bridge network (`infrastructure-network`), containers resolve service names directly (e.g. `http://app1:5000/health`). Host machines and external testing scripts running in PowerShell must connect via mapped localhost ports (e.g. `http://localhost:5002/health`).

---

## 2. Engineering Roadmap and Future Work

To evolve this architecture from an advanced prototype to an enterprise-grade production platform, the following improvements are planned:

### 2.1 Parallelized Telemetry Ingestion (Target: < 3s Monitoring Cycle)
- **Proposed Enhancement:** Refactor `get_latest_metric()` in `monitor.py` to use Python `asyncio` (`aiohttp` / `aiodocker`) or `concurrent.futures.ThreadPoolExecutor`.
- **Expected Outcome:** Polling all 5 application backends concurrently rather than sequentially will reduce raw Docker API wait time from ~11 seconds to ~2.2 seconds, bringing the total cycle time under 4 seconds on Windows WSL2, and under 500ms on native Linux.

### 2.2 Clustered High-Availability Redis (Redis Sentinel / Cluster)
- **Proposed Enhancement:** Replace the single-node Redis deployment with a 3-node Redis Sentinel or Redis Cluster topology.
- **Expected Outcome:** Automatic master failover in < 2 seconds, persistent append-only file (AOF) durability, and eliminated single point of failure for session data and remediation locks.

### 2.3 Distributed Multi-Worker Locust Benchmarking
- **Proposed Enhancement:** Deploy Locust in master-worker mode across multiple dedicated physical machines or VMs rather than colocating it with the application containers.
- **Expected Outcome:** Enables stress testing beyond 10,000+ RPS without virtual CPU contention on the application host.

### 2.4 Least-Privilege Daemon Architecture (Sidecar Pattern)
- **Proposed Enhancement:** Remove `/var/run/docker.sock` from the monitoring container. Replace direct container lifecycle control with an external authenticated agent or Kubernetes Custom Resource Definition (CRD) controller.
- **Expected Outcome:** Eliminates root-equivalent container escape risks and provides Kubernetes-native Pod eviction and horizontal pod autoscaling (HPA) integration.

### 2.5 Advanced Telemetry via eBPF
- **Proposed Enhancement:** Complement user-space `/health` polling with kernel-level Extended Berkeley Packet Filter (eBPF) probes for TCP socket latency, connection queue drops, and thread starvation.
- **Expected Outcome:** Sub-millisecond anomaly detection independent of application HTTP responsiveness.

### 2.6 Mutual TLS (mTLS) and Role-Based Access Control
- **Proposed Enhancement:** Enforce mTLS across internal container communication and add API token authorization for all control endpoints (`/stress/*`, `/recovery/*`, `/traffic/*`).
- **Expected Outcome:** Zero-trust security posture preventing unauthorized fault injection or manual remediation overrides.
