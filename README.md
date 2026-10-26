# Intelligent Infrastructure: AI-Enabled Predictive Monitoring and Self-Healing Failure Prevention

An autonomous, multi-tiered resilience and monitoring framework designed for containerized web applications. The system combines statistical anomaly detection, Machine Learning (Isolation Forest), Ordinary Least Squares ($OLS$) predictive trend forecasting, real-time dynamic traffic management via HAProxy, and automated fault remediation to prevent downtime in critical infrastructure.

---

## System Overview

Modern web applications running in microservice environments frequently experience gradual resource exhaustion (memory leaks, CPU saturation, disk depletion, latency degradation) and abrupt worker crashes. Traditional monitoring tools often detect failures only after downtime occurs or thresholds are breached.

This project implements an **end-to-end, multi-layer monitoring and self-healing architecture** that:
1. **Continuously monitors** 5 application servers, load balancers, and cache backends.
2. **Detects anomalies early** using multi-variable statistical analysis and machine learning.
3. **Forecasts saturation** using Ordinary Least Squares linear regression ($R^2$ goodness-of-fit) to provide early warnings minutes before failure.
4. **Dynamically manages traffic** through HAProxy runtime socket control (shedding weight or temporarily isolating degraded nodes).
5. **Remediates root causes** non-destructively (targeted PID termination, safe memory freeing, temporary disk purging, graceful container restarts).
6. **Safely re-admits backends** through multi-cycle health observation hysteresis and direct HTTP health verification.
7. **Enforces hard safety invariants** to prevent cluster blackouts, cascading restarts, and remediation loops.

---

## Key Architecture & Service Topology

The environment consists of **11 interconnected Docker containers** operating on an isolated bridge network:

```
                               [ Client Requests / Locust Load ]
                                              │
                                              ▼
                                   [ HAProxy Load Balancer ]
                                 (Port 8080 / Stats: 8404)
                                              │
                     ┌────────────┬───────────┼───────────┬────────────┐
                     ▼            ▼           ▼           ▼            ▼
                 [ App 1 ]    [ App 2 ]   [ App 3 ]   [ App 4 ]    [ App 5 ]
                 (:5002)      (:5003)     (:5004)     (:5005)      (:5006)
                     │            │           │           │            │
                     └────────────┴─────┬─────┴───────────┴────────────┘
                                        │ (Shared Session State)
                                        ▼
                                [ Redis State Store ]
                                     (:6379)
                                        ▲
                                        │ Telemetry & State
                                        ▼
                          [ Monitor & Decision Engine ]
                             (Port 5001 / Internal API)
                           ┌────────────┴────────────┐
                           ▼                         ▼
                  [ React Dashboard ]       [ Nginx Reverse Proxy ]
                     (Port 8500)                   (Port 8081)
```

### Component Summary
- **5 Flask Application Backends (`app1`–`app5`)**: Multi-worker Gunicorn/Flask applications serving session-aware traffic with built-in stress injection harnesses.
- **HAProxy 2.8 Load Balancer**: Layer 7 reverse proxy performing round-robin load distribution with sub-10ms runtime socket control.
- **Redis 7 State Store**: Central repository for application session replication, metric time-series, anomaly audit logs, and remediation locks.
- **Monitoring & Decision Engine**: Python daemon running continuous telemetry collection, ML anomaly detection, predictive forecasting, traffic routing control, and automated self-healing.
- **React Operations Dashboard**: Live single-page operational console hosted via Nginx displaying cluster health, telemetry graphs, and incident timelines.
- **Locust Load Generator**: Headless/web-based performance engine generating deterministic user concurrency profiles.
- **Static Nginx Proxy**: Reverse proxy gateway providing unified routing for static assets and API paths.

---

## Technical Capabilities by Phase

| Phase | Core Capability | Implementation Highlights |
| :--- | :--- | :--- |
| **Phase 1** | Containerized Microservices | 5 Gunicorn/Flask backends with shared Redis session persistence. |
| **Phase 2** | HAProxy Load Balancing | Dynamic health checks (`fall 2 rise 2`), admin stats, runtime UNIX socket IPC. |
| **Phase 3** | Anomaly Detection & Self-Healing | Isolation Forest ML, service crash detection, automated container lifecycle recovery. |
| **Phase 4** | Locust Load Testing | Headless & web-driven concurrency profiles (Normal, Moderate, Heavy) with live stats. |
| **Phase 5.1**| Intelligent Overload Detection | Multi-contributor classification (NORMAL, ELEVATED, OVERLOAD, CRITICAL) with debouncing. |
| **Phase 5.2**| Dynamic Traffic Management | Zero-reload HAProxy weight shedding (100 $\to$ 50), safe temporary maintenance exclusion, and 3-cycle re-admission hysteresis. |
| **Phase 5.3**| Targeted Fault Remediation | Granular remediation: CPU stress worker termination, memory freeing, approved disk cleanup, and restart loop suppression. |
| **Phase 5.4**| Predictive Failure Prevention | Transparent $OLS$ trend forecasting, multi-factor confidence, tiered sample policies, and early warning traffic damping. |
| **Phase 5.5**| Integration & Verification | 10 end-to-end integration scenarios, performance profiling, reliability auditing, and 244-test regression verification. |

---

## Quickstart Guide

### Prerequisites
- **OS**: Windows 11 (with WSL2 enabled), macOS, or Linux
- **Runtime**: Docker Desktop 4.20+ with Docker Compose v2
- **Network Ports**: `8080` (HAProxy), `8404` (HAProxy Stats), `8500` (Dashboard), `8089` (Locust), `5001` (Monitor), `5002`–`5006` (App backends)

### 1. Launch the Infrastructure
```bash
# Clone and navigate into the repository
cd Intelligent-Infrastructure

# Build and start all 11 containers in detached mode
docker-compose up -d --build
```

### 2. Verify Container Health
```bash
docker-compose ps
```
> **Note on Container Status**: All 11 containers will report `running`. The 6 containers configured with Docker `HEALTHCHECK` directives (`app1`–`app5` and `redis`) will report `healthy`. Containers without built-in Docker health checks (`monitor`, `haproxy`, `dashboard`, `locust`, `nginx`) operate nominally in the `running` state.

### 3. Access Services
- **Operations Dashboard**: [http://localhost:8500](http://localhost:8500)
- **Application Gateway (HAProxy)**: [http://localhost:8080](http://localhost:8080)
- **HAProxy Stats Console**: [http://localhost:8404](http://localhost:8404) *(Credentials: `admin` / `admin_infrastructure_secret`)*
- **Locust Load Generator**: [http://localhost:8089](http://localhost:8089)
- **Monitoring API**: [http://localhost:5001/metrics](http://localhost:5001/metrics)

### 4. Execute Regression Test Suite
Run the 244-test suite inside the running monitor container:
```bash
docker exec intelligent-infrastructure-monitor-1 pytest tests/ -v
```
*(Result: 244 passed in ~15 seconds, 0 failures, 0 regressions)*

---

## Documentation Directory

Complete architectural, operational, and test documentation is available in the [`docs/`](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs) directory:

1. [**Architecture & Implementation Guide**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/ARCHITECTURE.md): Deep-dive breakdown of the 11-container topology, monitoring algorithms, predictive regression models, and failover flows.
2. [**API Reference Manual**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/API_REFERENCE.md): Full reference of all REST routes across application backends, monitoring engine, and control endpoints.
3. [**Setup & Execution Guide**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/SETUP_GUIDE.md): Step-by-step installation, Docker Desktop configuration, troubleshooting, and tear-down procedures.
4. [**Testing & Validation Summary**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/TESTING_AND_VALIDATION.md): Complete empirical results from Phase 5.5.2 (E2E Scenarios A–J) and Phase 5.5.3 (Performance, Reliability, Safety, and Regressions).
5. [**Known Limitations & Future Work**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/LIMITATIONS_AND_FUTURE_WORK.md): Transparent technical evaluation of Windows/WSL2 Docker socket overhead, monitoring cycle latencies, and scalability recommendations.
6. [**Demonstration Guide & Script**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/DEMO_GUIDE.md): Step-by-step demonstration walkthrough, fallback procedures, and 12–15 minute presentation script with timings.
7. [**Demonstration Exact Commands**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/DEMO_COMMANDS.md): Terminal commands (PowerShell/Bash), API requests, and post-demo restoration scripts.
8. [**Evaluation Panel Technical Q&A**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/EVALUATION_QA.md): Detailed algorithmic defenses, mathematical justifications, and answers for evaluators.
9. [**Final Readiness Checklist**](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/FINAL_READINESS_CHECKLIST.md): Master operational checklist covering all 18 verification items.

---

## System Operational State & Caveats

- **Scope & Maturity**: This project is an advanced engineering prototype and validation testbed for self-healing infrastructure. It is **not** currently certified for mission-critical enterprise production without implementing the clustered state and high-availability improvements detailed in [LIMITATIONS_AND_FUTURE_WORK.md](file:///c:/Users/jerin/OneDrive/Documents/OneDrive/Documents/self%20healing%20infrastrructure%202/Intelligent-Infrastructure/docs/LIMITATIONS_AND_FUTURE_WORK.md).
- **Monitoring Loop Duration**: On Docker Desktop for Windows (WSL2), synchronous Docker socket stats queries across 5 containers introduce ~20 seconds of IPC latency per monitoring cycle. While metric timestamps preserve mathematical accuracy in regressions, detection response times reflect this polling duration.
- **Safety Invariants**: Hard rules prevent cluster blackouts (minimum 1 backend online), reject disk traversal outside approved temp paths, suppress repeated restart loops, and require 3 healthy verification cycles before traffic re-admission.

---

## License & Attribution
Developed as part of the *AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention* research initiative. All code is provided under the project's standard development terms.
