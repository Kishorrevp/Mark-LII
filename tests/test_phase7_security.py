"""Phase 7 — "lock it down": the dashboard's front door.

Verification level: GENUINE EXECUTION of dashboard/guard.py everywhere. The
end-to-end middleware test runs the real FastAPI app with Starlette's
TestClient where fastapi + httpx are installed (the user's PC); in the
sandbox, where they are not, it is skipped and says so.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import guard as G                                       # noqa: E402

HOSTS = G.allowed_hosts("192.168.0.101", (8000, 8001))


class GuardTests(unittest.TestCase):
    def test_host_header(self):
        self.assertTrue(G.check("GET", "/", {"Host": "localhost:8000"}, HOSTS)[0])
        self.assertTrue(G.check("GET", "/", {"Host": "192.168.0.101:8001"}, HOSTS)[0])
        ok, status, why = G.check("GET", "/", {"Host": "evil.example:8000"}, HOSTS)
        self.assertEqual((ok, status), (False, 421), "DNS rebinding: a foreign Host is refused")

    def test_origin_on_state_changing_requests(self):
        h = {"Host": "localhost:8000", "Origin": "https://evil.example"}
        self.assertEqual(G.check("POST", "/api/command", h, HOSTS)[:2], (False, 403))
        self.assertTrue(G.check("GET", "/api/files", h, HOSTS)[0], "a GET can't change anything")
        self.assertTrue(G.check("POST", "/api/command", {"Host": "localhost:8000",
                                                         "Origin": "http://localhost:8000"}, HOSTS)[0])
        self.assertEqual(G.check("POST", "/login", {"Host": "localhost:8000", "Origin": "null"}, HOSTS)[:2],
                         (False, 403))

    def test_body_caps(self):
        h = {"Host": "localhost:8000"}
        self.assertEqual(G.check("POST", "/api/command", h, HOSTS, body_len=70_000)[:2], (False, 413))
        self.assertTrue(G.check("POST", "/api/upload", h, HOSTS, body_len=50_000_000)[0])

    def test_login_lockout(self):
        t = [0.0]
        lim = G.LoginLimiter(max_fails=5, window_s=600, lock_s=600, clock=lambda: t[0])
        for _ in range(4):
            lim.fail("10.0.0.5")
        self.assertFalse(lim.locked("10.0.0.5"))
        lim.fail("10.0.0.5")
        self.assertTrue(lim.locked("10.0.0.5"))
        self.assertFalse(lim.locked("10.0.0.6"))
        t[0] = 601
        self.assertFalse(lim.locked("10.0.0.5"))


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception as e:
            self.skipTest(f"fastapi/httpx not installed here ({type(e).__name__}) — runs on the user's PC")

    def test_front_door_end_to_end(self):
        from fastapi.testclient import TestClient
        from dashboard.server import DashboardServer
        srv = DashboardServer()
        c = TestClient(srv.app)
        r = c.get("/login", headers={"Host": "localhost:8000"})
        self.assertEqual(r.status_code, 200)
        for k, v in G.SECURITY_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, k)
        self.assertEqual(c.get("/login", headers={"Host": "evil.example"}).status_code, 421)
        self.assertEqual(c.post("/api/command", json={"text": "x"},
                                headers={"Host": "localhost:8000", "Origin": "https://evil.example"}).status_code,
                         403)
        for _ in range(5):
            c.post("/login", json={"pin": "WRONG1"}, headers={"Host": "localhost:8000"})
        self.assertEqual(c.post("/login", json={"pin": "WRONG1"}, headers={"Host": "localhost:8000"}).status_code,
                         429, "five wrong PINs lock the address out")
        self.assertEqual(c.get("/config/api_keys.json", headers={"Host": "localhost:8000"}).status_code, 404)
        self.assertEqual(c.get("/.env", headers={"Host": "localhost:8000"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
