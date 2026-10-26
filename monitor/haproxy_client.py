import os
import socket
import csv
import io
import requests
from requests.auth import HTTPBasicAuth

DEFAULT_SOCKET_PATH = os.getenv("HAPROXY_SOCKET_PATH", "/tmp/haproxy/haproxy.sock")
DEFAULT_STATS_URL = os.getenv("HAPROXY_STATS_URL", "http://haproxy:8404/;csv")
DEFAULT_STATS_USER = os.getenv("HAPROXY_STATS_USER", "admin")
DEFAULT_STATS_PASS = os.getenv("HAPROXY_STATS_PASS", "admin_infrastructure_secret")


class HAProxyClient:
    """
    Communicates with HAProxy through the local UNIX administrative Runtime socket,
    with automatic HTTP stats fallback.
    
    Provides:
      - Real-time backend status inspection (UP / DOWN / MAINT)
      - L7 health check probe results and HTTP response codes
      - Optional administrative maintenance mode (maint / ready)
    """

    def __init__(self, socket_path=None, stats_url=None, stats_user=None, stats_pass=None):
        self.socket_path = socket_path or DEFAULT_SOCKET_PATH
        self.stats_url = stats_url or DEFAULT_STATS_URL
        self.stats_user = stats_user or DEFAULT_STATS_USER
        self.stats_pass = stats_pass or DEFAULT_STATS_PASS

    SAFE_COMMAND_PREFIXES = (
        "show stat",
        "show info",
        "get weight",
        "set server",
    )

    ALLOWED_BACKENDS = {"app_cluster"}
    ALLOWED_SERVERS = {"app1", "app2", "app3", "app4", "app5"}

    def validate_command(self, command):
        """
        Validates administrative commands against whitelist to avoid arbitrary execution.
        Disallows dangerous operations, newlines, semicolons, shell characters.
        Enforces that target backends and servers belong to permitted application clusters.
        """
        if not command or not isinstance(command, str):
            raise ValueError("Command must be a non-empty string")
        clean_cmd = command.strip()
        for sep in ("\n", "\r", ";", "|", "&", "$", "`", ">", "<"):
            if sep in clean_cmd:
                raise ValueError(f"Disallowed command separator in: {clean_cmd}")
        if not any(clean_cmd.startswith(prefix) for prefix in self.SAFE_COMMAND_PREFIXES):
            raise ValueError(f"Command not permitted by HAProxy safety whitelist: {clean_cmd}")

        # Validate target backend and server structure for set/get commands
        parts = clean_cmd.split()
        if parts[0:2] == ["set", "server"] and len(parts) >= 3:
            target = parts[2]
            if "/" not in target:
                raise ValueError(f"Invalid target format (expected backend/server): {target}")
            backend, srv = target.split("/", 1)
            if backend not in self.ALLOWED_BACKENDS:
                raise ValueError(f"Target backend '{backend}' not permitted in whitelist")
            if srv not in self.ALLOWED_SERVERS:
                raise ValueError(f"Target server '{srv}' not permitted in whitelist")
            if len(parts) >= 5:
                subcmd = parts[3]
                val = parts[4]
                if subcmd == "state" and val not in ("maint", "ready", "drain"):
                    raise ValueError(f"Invalid server state '{val}'; permitted: maint, ready, drain")
                elif subcmd == "weight":
                    try:
                        w_val = int(val)
                        if w_val < 0 or w_val > 256:
                            raise ValueError(f"Weight '{w_val}' out of valid range [0, 256]")
                    except ValueError:
                        raise ValueError(f"Invalid weight parameter: {val}")

        elif parts[0:2] == ["get", "weight"] and len(parts) >= 3:
            target = parts[2]
            if "/" not in target:
                raise ValueError(f"Invalid target format (expected backend/server): {target}")
            backend, srv = target.split("/", 1)
            if backend not in self.ALLOWED_BACKENDS or srv not in self.ALLOWED_SERVERS:
                raise ValueError(f"Target '{target}' not permitted in whitelist")

        return clean_cmd

    def send_socket_command(self, command, timeout=2.0):
        """Send a single administrative command over the UNIX socket and read output."""
        valid_cmd = self.validate_command(command)
        if not os.path.exists(self.socket_path):
            raise FileNotFoundError(f"HAProxy socket not found at: {self.socket_path}")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
            cmd_bytes = valid_cmd.encode("utf-8") + b"\n"
            sock.sendall(cmd_bytes)

            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except socket.timeout:
                    break
            return response.decode("utf-8", errors="replace")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def get_backends_status(self):
        """
        Query HAProxy backend health and operational states.
        Tries UNIX socket first, falls back to authenticated HTTP stats CSV.
        Returns a dict mapping server name -> status dictionary.
        """
        raw_csv = None
        # 1. Try UNIX socket
        try:
            raw_csv = self.send_socket_command("show stat")
        except Exception:
            pass

        # 2. Fall back to HTTP stats endpoint if socket returned nothing
        if not raw_csv or not raw_csv.startswith("# pxname"):
            try:
                resp = requests.get(
                    self.stats_url,
                    auth=HTTPBasicAuth(self.stats_user, self.stats_pass),
                    timeout=2.0
                )
                if resp.status_code == 200:
                    raw_csv = resp.text
            except Exception:
                pass

        if not raw_csv:
            return {}

        return self.parse_stats_csv(raw_csv)

    @staticmethod
    def parse_stats_csv(raw_csv):
        """Parse HAProxy stats CSV output into structured backend dictionary."""
        backends = {}
        lines = [l for l in raw_csv.splitlines() if l.strip() and not l.startswith("##")]
        if not lines:
            return backends

        reader = csv.reader(io.StringIO("\n".join(lines)))
        header = None
        for row in reader:
            if not row:
                continue
            if row[0].startswith("# pxname"):
                header = [col.strip("# ").strip() for col in row]
                continue
            if not header or len(row) < len(header):
                continue

            record = dict(zip(header, [col.strip() for col in row]))
            pxname = record.get("pxname", "")
            svname = record.get("svname", "")

            # Filter for our application cluster backends
            if pxname == "app_cluster" and svname not in {"FRONTEND", "BACKEND"}:
                status = record.get("status", "UNKNOWN")
                backends[svname] = {
                    "server": svname,
                    "pool": pxname,
                    "status": status,
                    "is_up": (status == "UP"),
                    "is_maint": ("MAINT" in status),
                    "is_down": ("DOWN" in status),
                    "check_status": record.get("check_status", ""),
                    "check_code": int(record["check_code"]) if record.get("check_code", "").isdigit() else None,
                    "check_duration_ms": int(record["check_duration"]) if record.get("check_duration", "").isdigit() else None,
                    "check_fail_count": int(record["chkfail"]) if record.get("chkfail", "").isdigit() else 0,
                    "check_down_count": int(record["chkdown"]) if record.get("chkdown", "").isdigit() else 0,
                    "active_sessions": int(record["scur"]) if record.get("scur", "").isdigit() else 0,
                    "total_sessions": int(record["stot"]) if record.get("stot", "").isdigit() else 0,
                    "hrsp_2xx": int(record["hrsp_2xx"]) if record.get("hrsp_2xx", "").isdigit() else 0,
                    "hrsp_5xx": int(record["hrsp_5xx"]) if record.get("hrsp_5xx", "").isdigit() else 0,
                    "weight": int(record["weight"]) if record.get("weight", "").isdigit() else 1
                }
        return backends

    def set_server_maintenance(self, service, in_maint=True, backend="app_cluster"):
        """
        Place a backend into administrative maintenance mode or restore it to ready.
        Only affects the specified backend; verifies the outcome over socket.
        Performs rollback to prior maintenance state if verification fails.
        """
        # Capture current state for potential rollback
        prev_maint = None
        try:
            statuses = self.get_backends_status()
            if service in statuses:
                prev_maint = statuses[service].get("is_maint", False)
        except Exception:
            pass

        state_str = "maint" if in_maint else "ready"
        cmd = f"set server {backend}/{service} state {state_str}"
        try:
            output = self.send_socket_command(cmd)
            # Verify outcome
            statuses = self.get_backends_status()
            srv_status = statuses.get(service, {})
            expected_maint = in_maint
            is_maint = srv_status.get("is_maint", False)
            success = (is_maint == expected_maint) if srv_status else False

            if not success and prev_maint is not None:
                try:
                    rb_str = "maint" if prev_maint else "ready"
                    self.send_socket_command(f"set server {backend}/{service} state {rb_str}")
                except Exception:
                    pass

            return {
                "success": success,
                "command": cmd,
                "output": output.strip(),
                "server": service,
                "is_maint": is_maint
            }
        except Exception as err:
            if prev_maint is not None:
                try:
                    rb_str = "maint" if prev_maint else "ready"
                    self.send_socket_command(f"set server {backend}/{service} state {rb_str}")
                except Exception:
                    pass
            return {
                "success": False,
                "command": cmd,
                "error": str(err),
                "server": service
            }

    def get_server_weight(self, service, backend="app_cluster"):
        """
        Query current effective weight of a backend server.
        Tries UNIX socket Runtime API first ('get weight <backend>/<srv>'),
        falls back to HTTP stats CSV.
        """
        cmd = f"get weight {backend}/{service}"
        try:
            raw = self.send_socket_command(cmd)
            # Response is typically '100 (initial 1)' or '50 (initial 1)'
            parts = raw.strip().split()
            if parts and parts[0].isdigit():
                return int(parts[0])
        except Exception:
            pass

        # Fallback to stats
        try:
            status = self.get_backends_status()
            if service in status and "weight" in status[service]:
                return int(status[service]["weight"])
        except Exception:
            pass

        return None

    def set_server_weight(self, service, weight, backend="app_cluster"):
        """
        Dynamically adjust the traffic weight of a backend server (0 <= weight <= 256).
        Verifies the resulting weight and performs rollback if verification fails.
        """
        try:
            target_weight = int(weight)
        except (ValueError, TypeError):
            raise ValueError(f"Weight must be an integer between 0 and 256, got: {weight}")

        if target_weight < 0 or target_weight > 256:
            raise ValueError(f"Weight out of valid range [0, 256]: {target_weight}")

        # Capture current weight for potential rollback
        previous_weight = self.get_server_weight(service, backend=backend)

        cmd = f"set server {backend}/{service} weight {target_weight}"
        try:
            output = self.send_socket_command(cmd)
            # Verify outcome
            new_weight = self.get_server_weight(service, backend=backend)
            success = (new_weight == target_weight)

            if not success and previous_weight is not None:
                # Rollback attempt
                try:
                    self.send_socket_command(f"set server {backend}/{service} weight {previous_weight}")
                except Exception:
                    pass

            return {
                "success": success,
                "command": cmd,
                "server": service,
                "weight": new_weight if new_weight is not None else target_weight,
                "previous_weight": previous_weight,
                "output": output.strip() if isinstance(output, str) else ""
            }
        except Exception as err:
            return {
                "success": False,
                "command": cmd,
                "server": service,
                "weight": previous_weight,
                "previous_weight": previous_weight,
                "error": str(err)
            }

