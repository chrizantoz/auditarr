"""U10 playground mode (SANDBOX=1): the locks that make a public copy safe to expose - connections, Find my apps, hooks, update, passwords refused;
Test only for the fixed fake hosts; a per-address write budget; the page told it is a playground. Run: python tests/sandbox_mode.py   Exit 1 on failure."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-sandbox-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]; os.environ["SANDBOX"] = "1"; os.environ["SONARR_URL"] = "http://stack:8765/sonarr"; os.environ["SONARR_APIKEY"] = "test"
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
check("SANDBOX=1 is recognised", audit.SANDBOX)
check("writes are budgeted per address (120 a minute)", all(audit.sandbox_rate_ok("1.2.3.4") for _ in range(120)) and not audit.sandbox_rate_ok("1.2.3.4") and audit.sandbox_rate_ok("5.6.7.8"))
conn = audit.db(); audit.refresh_reports(conn)
import json
st = audit.ui_state(conn)
check("the page state says it is a playground", st.get("sandbox") is True, {k: st.get(k) for k in ("sandbox",)})
# the route guards: drive the handler's POST dispatch with a fake request
class Req:
    def __init__(self, path, body): self.path = path; self._b = json.dumps(body).encode(); self.headers = {"Content-Length": str(len(self._b)), "Content-Type": "application/json"}; self.client_address = ("192.168.1.9", 1); self.out = None
    def _json(self, code, obj): self.out = (code, obj); return None
import io
def post(path, body):
    r = Req(path, body); r.rfile = io.BytesIO(r._b)
    r._auth = lambda: (True, "local", "admin"); r._actor = lambda: "lan:192.168.1.9"; r.send_response = lambda *a: None; r.send_header = lambda *a: None; r.end_headers = lambda: None; r.wfile = io.BytesIO()
    audit.Handler.do_POST(r); return r.out
class _R:
    def __init__(self, ip, xff=None): self.client_address = (ip, 1); self.headers = {"X-Forwarded-For": xff} if xff else {}
check("a visitor from the public internet may click (the locks guard, not the address)", audit.Handler._actor(_R("172.25.0.9", "203.0.113.7")) == "visitor:203.0.113.7" and audit.Handler._actor(_R("8.8.8.8")) == "visitor:8.8.8.8")
check("connections cannot be changed", post("/api/settings", {"set": {"SONARR_URL": "http://192.168.1.10:8989"}})[0] == 403)
check("keys cannot be changed", post("/api/settings", {"set": {"SONARR_APIKEY": "abc"}})[0] == 403)
check("ordinary settings still save (dry-run gates, retention)", post("/api/settings", {"set": {"TRASH_DAYS": "3"}})[0] == 200)
check("webhook setup is refused", post("/api/hook_setup", {"app": "sonarr"})[0] == 403)
check("the update hook is refused", post("/api/update", {})[0] == 403)
check("setting a password is refused", post("/api/setpassword", {"user": "x", "password": "longenough1"})[0] == 403)
check("Test refuses a host that is not one of the fakes", post("/api/test_connection", {"kind": "sonarr", "url": "http://192.168.1.10:8989", "key": "x"})[0] == 403)
c, o = post("/api/test_connection", {"kind": "sonarr", "url": "http://stack:8765/sonarr", "key": "test"}); check("Test still runs against the fake host (and may fail to connect here)", c == 200, (c, o))
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
