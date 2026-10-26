# Final Demonstration Exact Commands and Runbook

**Project:** AI-Enabled Predictive Infrastructure Monitoring and Failure Prevention  
**Document:** Exact Terminal Commands, API Requests, and Dashboard Runbook  
**Execution Environment:** Windows 11 (PowerShell) / WSL2 / Bash  
**Version:** Phase 5.5.4 Documentation  
**Status:** Verified on Live System  

---

## 1. Environment Startup & Baseline Inspection

### 1.1 Start Infrastructure Stack
Run from the root of `Intelligent-Infrastructure`:
```powershell
# PowerShell
docker-compose up -d --build
```
```bash
# Bash
docker-compose up -d --build
```

### 1.2 Confirm Container Status
```powershell
docker-compose ps
```
**Expected Observation:**
11 containers running. Exactly 6 containers (`app1`–`app5`, `redis`) report `healthy`. The other 5 containers (`monitor`, `haproxy`, `dashboard`, `locust`, `nginx`) report `Up` without health check indicators.

---

## 2. Demonstration A: Normal Infrastructure Baseline

### 2.1 Verify HAProxy Gateway Round-Robin Routing
Send 10 sequential HTTP requests to the HAProxy entrypoint:
```powershell
# PowerShell
1..10 | ForEach-Object { (Invoke-RestMethod -Uri "http://localhost:8080/").server }
```
```bash
# Bash
for i in {1..10}; do curl -s http://localhost:8080/ | jq -r .server; done
```
**Expected Observation:**
Servers alternate evenly: `Server-1`, `Server-2`, `Server-3`, `Server-4`, `Server-5`, `Server-1`, ... (exact 20% distribution).

### 2.2 Verify Monitor API Telemetry
Query current server metrics:
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/metrics" | ConvertTo-Json -Depth 3
```
```bash
curl -s http://localhost:5001/metrics | jq .
```
**Expected Observation:**
Returns 5 server metric objects with CPU < 5%, memory ~35–45 MB, disk ~14%, and `service_available: true`.

### 2.3 Dashboard Navigation
1. Open browser to **`http://localhost:8500`**.
2. Observe 5 green cards for `Server-1` through `Server-5`.
3. Check the **Cluster Traffic Management** card: all 5 backends show `Weight: 100`, `State: NORMAL`, `HAProxy: UP`.

---

## 3. Demonstration B: Bounded Load Generation

### 3.1 Start Moderate Load Test via API
```powershell
# PowerShell
$body = @{ profile = "moderate"; user_count = 20; duration_seconds = 30 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5001/load-test/start" -Method Post -Body $body -ContentType "application/json"
```
```bash
# Bash
curl -s -X POST http://localhost:5001/load-test/start \
  -H "Content-Type: application/json" \
  -d '{"profile":"moderate","user_count":20,"duration_seconds":30}' | jq .
```
**Expected Observation:** Returns `{"status": "running", "session_id": "locust-sess-..."}`.

### 3.2 Poll Live Traffic Telemetry
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/load-test/metrics"
```
```bash
curl -s http://localhost:5001/load-test/metrics | jq .
```
**Expected Observation:**
Throughput reaches ~35–60 RPS, latency is ~4–6 ms, error rate is `0.0%`.

### 3.3 Verify Cluster Overload State Remains NORMAL
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/overload/status" | ConvertTo-Json -Depth 2
```
```bash
curl -s http://localhost:5001/overload/status | jq .
```
**Expected Observation:**
`cluster_classification: "NORMAL"`. Demonstrates that high normal traffic does *not* trigger false overload alarms.

### 3.4 Stop Load Test (Cleanup)
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/load-test/stop" -Method Post
```
```bash
curl -s -X POST http://localhost:5001/load-test/stop | jq .
```

---

## 4. Demonstration C: Anomaly & Overload Detection

### 4.1 Inject Controlled CPU Stress into `Server-1`
```powershell
# PowerShell
$body = @{ duration = 60 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5002/stress/cpu" -Method Post -Body $body -ContentType "application/json"
```
```bash
# Bash
curl -s -X POST http://localhost:5002/stress/cpu \
  -H "Content-Type: application/json" \
  -d '{"duration":60}' | jq .
```
**Expected Observation:** Returns `{"message": "CPU stress started", "process_id": <PID>, "server": "Server-1"}`.

### 4.2 Observe Anomaly Classification
Wait ~20 seconds for the next monitoring cycle, then query:
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/overload/servers/app1" | ConvertTo-Json -Depth 3
```
```bash
curl -s http://localhost:5001/overload/servers/app1 | jq .
```
**Expected Observation:**
`classification: "OVERLOAD"`, `contributing_metrics: ["cpu_percent"]`, `consecutive_anomalous_cycles: 2`.

---

## 5. Demonstration D: Predictive Failure Warning

### 5.1 Query Predictive Telemetry for `Server-1`
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/prediction/servers/app1" | ConvertTo-Json -Depth 4
```
```bash
curl -s http://localhost:5001/prediction/servers/app1 | jq .
```
**Expected Observation:**
Returns $OLS$ regression statistics:
- `lead_metric: "cpu_percent"`
- `slope`: Positive slope in %/sec
- `r_squared`: Goodness of fit ($R^2 \ge 0.65$)
- `confidence`: Confidence score based on sample sufficiency and trend stability.
- `predictive_state`: `EARLY_WARNING` or `PREDICTED_SATURATION`.

### 5.2 Verify INSUFFICIENT_DATA Safeguard
Query predictive status when historical samples are below minimum ($N < 6$):
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/prediction/status" | ConvertTo-Json -Depth 3
```
```bash
curl -s http://localhost:5001/prediction/status | jq .
```
**Expected Observation:**
If sample count is below 6, server reports `INSUFFICIENT_DATA` with confidence null, proving the system refuses to guess without sufficient evidence.

---

## 6. Demonstration E: Intelligent Traffic Management

### 6.1 Inspect Dynamic Weight Reduction
When `app1` is classified as `OVERLOAD`, check traffic management status:
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/traffic/status" | ConvertTo-Json -Depth 3
```
```bash
curl -s http://localhost:5001/traffic/status | jq .
```
**Expected Observation:**
`app1` reports:
- `current_state: "WEIGHT_REDUCED"`
- `current_weight: 50`
- `last_action: "WEIGHT_REDUCED"`
- `healthy_eligible_count: 5` (all 5 remain eligible, but `app1` receives 50% less traffic).

### 6.2 Query Direct HAProxy Runtime Socket
Verify that HAProxy internal weights reflect the change:
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/traffic/weights"
```
```bash
curl -s http://localhost:5001/traffic/weights | jq .
```
**Expected Observation:**
`{"app1": 50, "app2": 100, "app3": 100, "app4": 100, "app5": 100}`.

### 6.3 Test Traffic Damping via Gateway
Send 15 requests through the gateway:
```powershell
1..15 | ForEach-Object { (Invoke-RestMethod -Uri "http://localhost:8080/").server } | Group-Object | Select-Object Name, Count
```
**Expected Observation:** `Server-1` receives approximately half as many hits as each other server.

---

## 7. Demonstration F: Automated Root-Cause Remediation

### 7.1 Observe Automated Remediation Dispatch
The Monitoring Engine identifies `cpu_spike` on `app1` and automatically executes `cpu_worker_termination`. To observe or manually invoke if automatic recovery is paused:
```powershell
# Status check
Invoke-RestMethod -Uri "http://localhost:5001/recovery/status" | ConvertTo-Json -Depth 3
```
```bash
curl -s http://localhost:5001/recovery/status | jq .
```

### 7.2 Manual Trigger (Optional Verification)
```powershell
$body = @{ strategy = "cpu_worker_termination"; force = $true } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5001/recovery/execute/app1" -Method Post -Body $body -ContentType "application/json"
```
```bash
curl -s -X POST http://localhost:5001/recovery/execute/app1 \
  -H "Content-Type: application/json" \
  -d '{"strategy":"cpu_worker_termination","force":true}' | jq .
```
**Expected Observation:** Returns `{"details": {"processes_stopped": 1}, "strategy": "cpu_worker_termination", "success": true}`.

### 7.3 Observe 3-Cycle Re-Admission Hysteresis
Wait 3 monitoring cycles (~60s):
```powershell
Invoke-RestMethod -Uri "http://localhost:5001/traffic/servers/app1"
```
```bash
curl -s http://localhost:5001/traffic/servers/app1 | jq .
```
**Expected Observation:**
Cycle 1: `AWAITING_READMISSION` (consecutive healthy: 1)  
Cycle 2: `AWAITING_READMISSION` (consecutive healthy: 2)  
Cycle 3: `NORMAL`, `current_weight: 100`, `last_action: "READMISSION_RESTORED"`.

---

## 8. Demonstration G: Failure & Safety Handling

### 8.1 Simulate Hard Node Crash
Abruptly stop container `app2`:
```powershell
docker stop intelligent-infrastructure-app2-1
```
```bash
docker stop intelligent-infrastructure-app2-1
```

### 8.2 Verify Zero-Downtime Gateway Routing
Immediately send 10 requests through HAProxy:
```powershell
1..10 | ForEach-Object { (Invoke-RestMethod -Uri "http://localhost:8080/").server }
```
```bash
for i in {1..10}; do curl -s http://localhost:8080/ | jq -r .server; done
```
**Expected Observation:**
All 10 requests succeed (0% error rate). Requests are routed exclusively across `Server-1`, `Server-3`, `Server-4`, and `Server-5`.

### 8.3 Restart Container & Verify Readmission (Cleanup)
```powershell
docker start intelligent-infrastructure-app2-1
```
```bash
docker start intelligent-infrastructure-app2-1
```
**Expected Observation:**
Container boots, passes internal health checks, HAProxy marks it `UP` (`rise 2`), and traffic seamlessly resumes.

---

## 9. Comprehensive Cluster Reset (Post-Demo Restoration)

To return the entire cluster to a pristine baseline state:
```powershell
# 1. Stop any residual stress workers
Invoke-RestMethod -Uri "http://localhost:5002/stress/cpu/stop" -Method Post
Invoke-RestMethod -Uri "http://localhost:5003/stress/cpu/stop" -Method Post
Invoke-RestMethod -Uri "http://localhost:5005/stress/memory/clear" -Method Post
Invoke-RestMethod -Uri "http://localhost:5002/stress/latency/reset" -Method Post
Invoke-RestMethod -Uri "http://localhost:5003/stress/process/reset" -Method Post
Invoke-RestMethod -Uri "http://localhost:5002/stress/disk/reset" -Method Post

# 2. Restore all traffic weights to 100
1..5 | ForEach-Object {
    Invoke-RestMethod -Uri "http://localhost:5001/traffic/restore/app$_?force=true" -Method Post
}

# 3. Confirm 5 backends healthy
Invoke-RestMethod -Uri "http://localhost:5001/traffic/status" | ConvertTo-Json -Depth 2
```
