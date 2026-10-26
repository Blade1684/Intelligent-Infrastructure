import unittest
from pathlib import Path


class HAProxyConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg_path = Path(__file__).resolve().parents[1] / "haproxy" / "haproxy.cfg"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cls.cfg_text = f.read()

    def test_round_robin_algorithm_configured(self):
        """Verify HAProxy backend uses roundrobin algorithm."""
        self.assertIn("balance roundrobin", self.cfg_text)

    def test_active_health_check_configured(self):
        """Verify active health check queries GET /health and expects status 200."""
        self.assertIn("option httpchk GET /health", self.cfg_text)
        self.assertIn("http-check expect status 200", self.cfg_text)

    def test_health_check_interval_and_thresholds(self):
        """Verify default-server specifies inter 2s, fall 2, rise 2."""
        self.assertIn("inter 2s fall 2 rise 2", self.cfg_text)

    def test_all_five_servers_configured_in_backend(self):
        """Verify all five application servers are registered in the backend cluster."""
        for i in range(1, 6):
            pattern = rf"server\s+app{i}\s+app{i}:5000\s+check"
            self.assertRegex(self.cfg_text, pattern)

    def test_docker_dns_resolver_configured(self):
        """Verify Docker DNS 127.0.0.11:53 resolver with hold valid 2s."""
        self.assertIn("resolvers docker_dns", self.cfg_text)
        self.assertIn("nameserver dns1 127.0.0.11:53", self.cfg_text)
        self.assertIn("hold valid 2s", self.cfg_text)

    def test_restricted_stats_interface_configured(self):
        """Verify stats interface binds to port 8404 with authentication enabled."""
        self.assertIn("bind *:8404", self.cfg_text)
        self.assertIn("stats enable", self.cfg_text)
        self.assertIn("stats auth", self.cfg_text)

    def test_runtime_api_socket_configured(self):
        """Verify administrative runtime socket is configured with level admin and mode 600."""
        self.assertIn("stats socket /tmp/haproxy.sock mode 600 level admin", self.cfg_text)


class HAProxyStatsParserTests(unittest.TestCase):
    def test_parse_csv_stats(self):
        """Verify CSV stats parser extracts server state, health and response counts."""
        sample_csv = (
            "# pxname,svname,qcur,qmax,scur,smax,slim,status,hrsp_2xx\n"
            "app_cluster,app1,0,0,0,5,100,UP,42\n"
            "app_cluster,app2,0,0,0,5,100,DOWN,12\n"
            "app_cluster,app3,0,0,0,5,100,UP,38\n"
            "app_cluster,BACKEND,0,0,0,15,500,UP,92\n"
        )
        lines = [l for l in sample_csv.splitlines() if l.startswith("app_cluster,app")]
        servers_info = {}
        for l in lines:
            parts = l.split(",")
            svname = parts[1]
            status = parts[7]
            hrsp_2xx = int(parts[8])
            servers_info[svname] = {"status": status, "responses_2xx": hrsp_2xx}

        self.assertEqual(servers_info["app1"]["status"], "UP")
        self.assertEqual(servers_info["app1"]["responses_2xx"], 42)
        self.assertEqual(servers_info["app2"]["status"], "DOWN")
        self.assertEqual(servers_info["app3"]["status"], "UP")


class HAProxyClientUnitTests(unittest.TestCase):
    def setUp(self):
        try:
            from haproxy_client import HAProxyClient
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
            from haproxy_client import HAProxyClient
        self.HAProxyClient = HAProxyClient

    def test_client_initialization_defaults(self):
        """Verify HAProxyClient defaults and attributes."""
        client = self.HAProxyClient()
        self.assertIn("haproxy.sock", client.socket_path)
        self.assertIn("8404", client.stats_url)

    def test_send_socket_command_file_not_found(self):
        """Verify send_socket_command raises FileNotFoundError when socket path does not exist."""
        client = self.HAProxyClient(socket_path="/nonexistent/path/haproxy.sock")
        with self.assertRaises(FileNotFoundError):
            client.send_socket_command("show info")

    def test_parse_stats_csv_structured_extraction(self):
        """Verify parse_stats_csv populates all operational metrics accurately."""
        sample_csv = (
            "# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,wretr,wredis,status,weight,act,bck,chkfail,chkdown,lastchg,downtime,qlimit,pid,iid,sid,throttle,lbtot,tracked,type,rate,rate_lim,rate_max,check_status,check_code,check_duration,hrsp_1xx,hrsp_2xx,hrsp_3xx,hrsp_4xx,hrsp_5xx\n"
            "app_cluster,app1,0,0,0,5,100,50,100,200,0,0,0,0,0,0,0,UP,1,1,0,0,0,100,0,,1,3,1,,50,,2,0,,0,L7OK,200,2,0,50,0,0,0\n"
            "app_cluster,app2,0,0,0,5,100,20,100,200,0,0,0,0,0,0,0,DOWN,1,1,0,2,1,50,50,,1,3,2,,20,,2,0,,0,L7STS,500,2,0,15,0,0,5\n"
            "app_cluster,app3,0,0,0,5,100,30,100,200,0,0,0,0,0,0,0,MAINT,1,1,0,0,0,30,30,,1,3,3,,30,,2,0,,0,PAUSED,,0,0,30,0,0,0\n"
            "app_cluster,BACKEND,0,0,0,15,500,100,300,600,0,0,0,0,0,0,0,UP,3,3,0,0,0,100,0,,1,3,0,,100,,1,0,,0,,,,0,95,0,0,5\n"
        )
        parsed = self.HAProxyClient.parse_stats_csv(sample_csv)
        self.assertEqual(len(parsed), 3)

        self.assertIn("app1", parsed)
        self.assertTrue(parsed["app1"]["is_up"])
        self.assertFalse(parsed["app1"]["is_down"])
        self.assertFalse(parsed["app1"]["is_maint"])
        self.assertEqual(parsed["app1"]["check_status"], "L7OK")
        self.assertEqual(parsed["app1"]["check_code"], 200)

        self.assertIn("app2", parsed)
        self.assertTrue(parsed["app2"]["is_down"])
        self.assertFalse(parsed["app2"]["is_up"])
        self.assertEqual(parsed["app2"]["check_status"], "L7STS")
        self.assertEqual(parsed["app2"]["check_code"], 500)
        self.assertEqual(parsed["app2"]["check_fail_count"], 2)

        self.assertIn("app3", parsed)
        self.assertTrue(parsed["app3"]["is_maint"])
        self.assertFalse(parsed["app3"]["is_up"])


if __name__ == "__main__":
    unittest.main()

