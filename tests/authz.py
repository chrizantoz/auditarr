"""Authorization rules (Q19), tested directly on the request handler's decision functions with fake requests.
Open mode (no password): writes only from private networks or through a proxy that sets Remote-User.
Sign-in mode: a valid session cookie, the container itself, or (only with TRUST_PROXY_AUTH=1) the proxy header.
Run: python tests/authz.py   Exit 1 on failure."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-authz-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]; os.environ["TRUST_PROXY_AUTH"] = "0"
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:160]}]"))

class Fake:
    """Just enough of a BaseHTTPRequestHandler for _actor() and _auth(): a client address and headers."""
    def __init__(self, ip, headers=None): self.client_address = (ip, 12345); self.headers = {k: v for k, v in (headers or {}).items()}
actor = audit.Handler._actor; auth = audit.Handler._auth

# ---- open mode (no password set)
audit.AUTH["loaded"] = True; audit.AUTH["conf"] = None
check("open mode: public address without proxy header cannot write", actor(Fake("8.8.8.8")) is None)
check("open mode: public address with a proxy Remote-User header can write", actor(Fake("8.8.8.8", {"Remote-User": "alice"})) == "alice@8.8.8.8")
check("open mode: LAN address can write", (actor(Fake("192.168.1.20")) or "").startswith("lan:"))
check("open mode: tailnet address can write", (actor(Fake("100.64.0.9")) or "").startswith("lan:"))
check("open mode: docker bridge address (not the gateway) cannot write", actor(Fake("172.25.0.7")) is None)
check("open mode: page is readable by anyone (no auth gate)", auth(Fake("8.8.8.8")) == (True, None, "admin"))

# ---- sign-in mode
conn = audit.db(); conf = audit.set_password(conn, "owner", "correct horse battery", persist=True); cookie = "auditarr_s=" + audit.session_cookie(conf)
check("sign-in: public address without cookie is refused", auth(Fake("8.8.8.8")) == (False, None, None))
check("sign-in: LAN address without cookie is refused too", auth(Fake("192.168.1.20")) == (False, None, None))
check("sign-in: valid cookie is accepted and names the user", auth(Fake("8.8.8.8", {"Cookie": cookie})) == (True, "owner", "admin"))
bad = cookie[:-4] + ("0000" if not cookie.endswith("0000") else "1111"); check("sign-in: tampered cookie is refused", auth(Fake("8.8.8.8", {"Cookie": bad})) == (False, None, None))
check("sign-in: container itself (self-test, hooks) is allowed", auth(Fake("127.0.0.1")) == (True, "local", "admin"))
check("sign-in: proxy header is ignored unless TRUST_PROXY_AUTH=1", auth(Fake("10.0.0.5", {"Remote-User": "alice"})) == (False, None, None))
audit.TRUST_PROXY_AUTH = True
check("sign-in: with TRUST_PROXY_AUTH=1 the proxy header counts", auth(Fake("10.0.0.5", {"Remote-User": "alice"})) == (True, "alice", "admin"))
audit.TRUST_PROXY_AUTH = False
check("passwords are not stored in clear", "correct horse battery" not in open(os.path.join(os.environ["CONFIG_DIR"], "state.db"), "rb").read().decode("latin1"))
check("wrong password is rejected, right one accepted", not audit.check_password(conf, "owner", "nope") and audit.check_password(conf, "owner", "correct horse battery"))
try: audit.set_password(conn, "x", "short", persist=False); check("short passwords are refused", False)
except ValueError: check("short passwords are refused", True)
expired = audit.session_cookie(conf, days=-1); check("expired cookie is refused", auth(Fake("8.8.8.8", {"Cookie": "auditarr_s=" + expired})) == (False, None, None))
# ---- viewer role (F9): a second, read-only login
conf = audit.set_password(conn, "kid", "lookdonttouch", role="viewer"); vcookie = "auditarr_s=" + audit.session_cookie(conf, role="viewer")
check("viewer: valid viewer cookie is accepted with role viewer", auth(Fake("8.8.8.8", {"Cookie": vcookie})) == (True, "kid", "viewer"))
check("viewer: password check tells the roles apart", audit.check_password(conf, "kid", "lookdonttouch") == "viewer" and audit.check_password(conf, "owner", "correct horse battery") == "admin" and not audit.check_password(conf, "kid", "correct horse battery"))
forged = vcookie.replace("|viewer|", "|admin|"); check("viewer: cookie with the role flipped to admin is refused", auth(Fake("8.8.8.8", {"Cookie": forged})) == (False, None, None))
check("viewer: a viewer cookie is honoured even from the container's own address", auth(Fake("127.0.0.1", {"Cookie": vcookie})) == (True, "kid", "viewer"))
check("viewer: main password still valid after adding a viewer", audit.check_password(conf, "owner", "correct horse battery") == "admin")
conf2 = audit.set_password(conn, "owner", "new horse battery 2"); check("changing the main password keeps the viewer account", conf2.get("viewer", {}).get("user") == "kid")
conf3 = audit.set_password(conn, "kid", "", role="viewer"); check("viewer: empty password removes the account", "viewer" not in conf3 and auth(Fake("8.8.8.8", {"Cookie": vcookie})) == (False, None, None))
try: audit.set_password(conn, "kid", "short", role="viewer"); check("viewer: short passwords are refused", False)
except ValueError: check("viewer: short passwords are refused", True)
conn.close(); bad_n = results.count(False); print(f"\n{len(results) - bad_n} passed, {bad_n} failed"); sys.exit(1 if bad_n else 0)
