# Windows 11 & Docker Desktop Setup and Execution Guide

**Project:** AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention  
**Document:** Setup, Deployment, and Operational Runbook  
**Target Platform:** Windows 11 (WSL2 Backend) & Docker Desktop  
**Version:** Phase 5.5.4 Documentation  

---

## 1. Prerequisites and System Requirements

Before deploying the cluster, verify that your host system satisfies these requirements:

### 1.1 Operating System & Virtualization
- **Operating System:** Windows 11 Pro, Enterprise, or Home (Build 22000 or higher).
- **Virtualization:** Hyper-V and Virtual Machine Platform enabled in Windows Features.
- **WSL2:** Windows Subsystem for Linux (WSL2) installed and set as default:
  ```powershell
  wsl --status
  wsl --set-default-version 2
  ```

### 1.2 Docker Desktop Configuration
- **Version:** Docker Desktop 4.20 or newer.
- **WSL2 Engine:** In Docker Desktop Settings $\to$ **General**, ensure **Use the WSL 2 based engine** is checked.
- **Resource Allocations:** In Docker Desktop Settings $\to$ **Resources**, recommend:
  - **Memory:** Minimum 6.0 GB (8.0 GB recommended).
  - **CPUs:** Minimum 4 virtual cores.
  - **Swap:** 2.0 GB.

### 1.3 Local Development Tools (Optional for Host Development)
- Python 3.10+ (Python 3.11 recommended).
- PowerShell 7 or Windows PowerShell 5.1.
- `curl` or Postman for API testing.

---

## 2. Port Allocations & Network Topology

The project maps multiple host ports to avoid conflicts:

| Component | Host Port | Container Port | Purpose & Description |
| :--- | :--- | :--- | :--- |
| **HAProxy Gateway** | `8080` | `80` | Client entrypoint; round-robin load balancer. |
| **HAProxy Stats** | `8404` | `8404` | HAProxy administration console and live traffic gauges. |
| **React Dashboard** | `8500` | `80` | Operations UI with live telemetry, metrics, and overrides. |
| **Locust Load Test** | `8089` | `8089` | Locust web interface and traffic generator control. |
| **Monitor API** | `5001` | `5001` | Anomaly detection, prediction, and self-healing API. |
| **Nginx Reverse Proxy**| `8081` | `80` | Static asset and reverse proxy gateway. |
| **Backend App 1** | `5002` | `5000` | Application Server 1 direct debugging port. |
| **Backend App 2** | `5003` | `5000` | Application Server 2 direct debugging port. |
| **Backend App 3** | `5004` | `5000` | Application Server 3 direct debugging port. |
| **Backend App 4** | `5005` | `5000` | Application Server 4 direct debugging port. |
| **Backend App 5** | `5006` | `5000` | Application Server 5 direct debugging port. |
| **Redis Store** | Internal | `6379` | Unexposed on host; internal bridge access only. |

---

## 3. Deployment & Startup Procedure

### Step 1: Open Terminal in Project Directory
Launch PowerShell or Windows Terminal and navigate to the root directory containing `docker-compose.yml`:
```powershell
cd "c:\Users\jerin\OneDrive\Documents\OneDrive\Documents\self healing infrastrructure 2\Intelligent-Infrastructure"
```

### Step 2: Build and Launch Containers
Execute Docker Compose to build images and spawn the 11 containers in detached mode:
```powershell
docker-compose up -d --build
```

### Step 3: Verify Container States
Check the state of the 11 containers:
```powershell
docker-compose ps
```

#### Expected Output Analysis:
You will see 11 active containers:
- **6 containers reporting `healthy`:** `app1`, `app2`, `app3`, `app4`, `app5`, and `redis`. These containers define native Docker `HEALTHCHECK` directives in `docker-compose.yml`.
- **5 containers reporting `running` (Up):** `monitor`, `haproxy`, `dashboard`, `locust`, and `nginx`. These containers do not define an internal Docker health check instruction, so Docker reports them as `running` without an active health evaluation status. This is normal and expected.

---

## 4. Operational Verification & Access Links

### 4.1 Operations Dashboard
Open your browser and navigate to:
**[http://localhost:8500](http://localhost:8500)**
- Confirm that all 5 server cards display green health badges.
- Verify that live CPU, memory, and disk telemetry cards update every 2 seconds.
- Verify that the traffic distribution chart reflects equal 20% splits.

### 4.2 HAProxy Traffic Gateway & Stats
- **Gateway Traffic Test:**
  ```powershell
  curl http://localhost:8080/
  ```
  Returns `{"server": "Server-X", "status": "healthy"}`.
- **HAProxy Stats Dashboard:**
  **[http://localhost:8404](http://localhost:8404)**
  - Username: `admin`
  - Password: `admin_infrastructure_secret`

### 4.3 Locust Load Generator
Navigate to:
**[http://localhost:8089](http://localhost:8089)**
- Host is automatically set to `http://haproxy:80`.
- Choose users (e.g., 10) and spawn rate (e.g., 2) to start generating simulated user traffic.

### 4.4 Monitoring Engine Health & Status
Verify the Monitor REST API directly:
```powershell
# Ingest live telemetry
curl http://localhost:5001/metrics

# Query active cluster traffic weights
curl http://localhost:5001/traffic/weights

# Query predictive engine status
curl http://localhost:5001/prediction/status
```

---

## 5. Running the Automated Regression Test Suite

All 244 unit, integration, and safety tests are packaged within the monitor container. To execute the entire suite without installing Python packages locally on Windows:

```powershell
docker exec intelligent-infrastructure-monitor-1 pytest tests/ -v
```

### Expected Test Result:
```
============================= 244 passed in 14.73s =============================
```
- **Total Test Count:** 244
- **Passed:** 244
- **Failures:** 0
- **Errors:** 0
- **Skipped:** 0

---

## 6. Fault-Injection & Demonstration Scenarios

To observe self-healing and dynamic traffic management in action:

### Scenario 1: CPU Saturation & Worker Termination
1. Inject CPU stress on `app1`:
   ```powershell
   curl -X POST http://localhost:5002/stress/cpu -H "Content-Type: application/json" -d '{"duration": 60}'
   ```
2. Open Dashboard (`http://localhost:8500`) to observe `app1` CPU spike to ~100%.
3. Observe `OverloadEngine` transition `app1` from `NORMAL` $\to$ `OVERLOAD`.
4. Observe `TrafficManager` shed weight from `100` $\to$ `50`.
5. Observe `RecoveryEngine` dispatch `cpu_worker_termination`, terminating the stress PID while keeping the main container alive.
6. Observe 3-cycle health re-admission returning `app1` weight to `100`.

### Scenario 2: Simulated Worker Crash & Container Isolation
1. Crash the worker process on `app2`:
   ```powershell
   curl -X POST http://localhost:5003/stress/process/crash
   ```
2. HAProxy health checks fail `app2` within 4 seconds (`fall 2`), immediately diverting incoming requests to `app1`, `app3`, `app4`, and `app5`.
3. The Monitoring Engine diagnoses `process_failure` and restores health.

---

## 7. Controlled Shutdown and Cleanup

To stop and remove containers cleanly:

### Standard Shutdown (Preserves Volumes & State):
```powershell
docker-compose down
```

### Full Teardown (Cleans Volumes & Shared Socket):
```powershell
docker-compose down -v
```

### Hard Reset (In Case of Stuck Containers):
```powershell
# Force-remove all project containers
docker rm -f $(docker ps -aq --filter "name=intelligent-infrastructure")

# Prune unused networks
docker network prune -f
```
