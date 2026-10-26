import uuid
import threading
from collections import defaultdict
from locust import HttpUser, task, between, events
from flask import jsonify

# Thread-safe tracker for backend server distribution
distribution_lock = threading.Lock()
backend_distribution = defaultdict(int)


def record_backend_hit(server_name):
    if server_name:
        with distribution_lock:
            backend_distribution[server_name] += 1


@events.reset_stats.add_listener
def on_reset_stats(**kwargs):
    with distribution_lock:
        backend_distribution.clear()


@events.init.add_listener
def on_locust_init(web_ui, **kwargs):
    """Expose custom backend distribution endpoint on Locust web interface."""
    if web_ui:
        @web_ui.app.route("/stats/distribution")
        def get_distribution():
            with distribution_lock:
                dist = dict(backend_distribution)
            total = sum(dist.values())
            percentages = {k: round((v / total) * 100, 2) if total > 0 else 0.0 for k, v in dist.items()}
            return jsonify({
                "distribution": dist,
                "percentages": percentages,
                "total_recorded": total
            })


class InfrastructureUser(HttpUser):
    """
    Simulates realistic user traffic against the HAProxy application gateway.
    Interacts with home, health, authentication, session state, and work endpoints.
    """
    wait_time = between(0.1, 0.8)

    def on_start(self):
        self.username = f"locust_{uuid.uuid4().hex[:8]}"
        self.logged_in = False

    @task(5)
    def view_home(self):
        """Standard browse query to the landing page."""
        with self.client.get("/", name="GET /", catch_response=True) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    record_backend_hit(data.get("server"))
                    resp.success()
                except Exception:
                    resp.failure("Failed to parse JSON response")
            elif resp.status_code == 503:
                resp.failure("Gateway 503: No healthy backends")
            else:
                resp.failure(f"HTTP {resp.status_code}")

    @task(3)
    def query_health(self):
        """Active health status verification."""
        with self.client.get("/health", name="GET /health", catch_response=True) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    record_backend_hit(data.get("server"))
                    resp.success()
                except Exception:
                    resp.failure("Failed to parse JSON response")
            elif resp.status_code == 503:
                resp.failure("Gateway 503: Service Unavailable")
            else:
                resp.failure(f"HTTP {resp.status_code}")

    @task(2)
    def user_session_workflow(self):
        """
        Executes a multi-step session transaction exercising shared Redis storage:
        Login -> Query Session -> Do Work -> Logout.
        """
        # 1. Login
        login_resp = self.client.post(
            "/login",
            json={"username": self.username},
            name="POST /login"
        )
        if login_resp.status_code == 200:
            self.logged_in = True
            try:
                record_backend_hit(login_resp.json().get("server"))
            except Exception:
                pass

            # 2. Check Session
            sess_resp = self.client.get("/session", name="GET /session")
            if sess_resp.status_code == 200:
                try:
                    record_backend_hit(sess_resp.json().get("server"))
                except Exception:
                    pass

            # 3. Perform Work
            work_resp = self.client.post("/work", name="POST /work")
            if work_resp.status_code == 200:
                try:
                    record_backend_hit(work_resp.json().get("server"))
                except Exception:
                    pass
