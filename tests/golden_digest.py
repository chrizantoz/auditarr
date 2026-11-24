"""Golden digest (Q18): the morning push is pinned word for word, and delivered to a mock ntfy with the right headers.
Builds a small, fixed world (findings, actions, a 'working' row that must stay out of Needs-you), renders digest_text(today_summary())
and compares it with tests/golden/digest.txt. A changed wording is a deliberate change: run with --update to re-pin.
Run: python tests/golden_digest.py [--update]   Exit 1 on failure."""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-golden-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "r"); os.makedirs(os.environ["REPORT_DIR"])
os.environ["TRASH_DIR"] = os.path.join(TMP, "trash"); os.makedirs(os.environ["TRASH_DIR"]); os.environ["PUBLIC_URL"] = "https://auditarr.example.test/"; os.environ["HOST_IP"] = ""
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:300]}]"))

now = time.time(); since = now - 26 * 3600; conn = audit.db()
def finding(i, app, code, sev, label, detail, age_h):
    conn.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,1,0)",
                 (f"g{i:02d}", app, i, code, sev, label, f"/data/x/{i}.mkv", detail, now - age_h * 3600, now))
finding(1, "radarr", "NO_AUDIO", "critical", "Beta Movie (2021)", "no audio stream", 3)
finding(2, "sonarr", "WRONG_LANGUAGE", "high", "Alpha Show S01E02", "audio=['fre'] original=eng wanted=['eng']", 5)
finding(3, "sonarr", "SAMPLE_FILE", "high", "Alpha Show S01E05", "40 MB, 0.3 min where 25 min expected - sample/stub?", 8)
finding(4, "sonarr", "NO_SUBS", "medium", "Anime S01E03", "audio=['jpn'] embedded_subs=[] external=[] | Bazarr still searching", 30)
finding(5, "sonarr", "NO_SUBS", "high", "Anime S01E04", "[working: AI subtitles queued 02:10] || audio=['jpn'] embedded_subs=[] external=[]", 2)   # must NOT appear in Needs you
finding(6, "disk", "DUPLICATE_VIDEO", "medium", "x.mkv", "900 MB in sonarr root /data/tv - not managed by any arr (same episode as a managed file)", 40)
acts = [("on", "fixtag", "sonarr", "Alpha Show S01E01", "eng written"), ("on", "fixtag", "sonarr", "Alpha Show S01E03", "eng written"), ("on", "whisper", "sonarr", "Anime S01E01", "whisper subtitle landed after 1.8 min"),
        ("on", "whisper", "sonarr", "Kakegurui S01E02", "ERROR TypeError('boom')"), ("on", "regrab", "sonarr", "Alpha Show S01E02", "moved -> trash"), ("on", "settings", "ui", "x", "changed"), ("dryrun", "dedupe", "disk", "y.mkv", "would move"),
        ("on", "plex-refresh", "sonarr", "Alpha Show", "refreshed"), ("on", "subsearch", "sonarr", "Anime S01E02", "no subtitle found yet")]
for k, (mode, kind, app, label, result) in enumerate(acts): conn.execute("INSERT INTO action VALUES(?,?,?,?,?,?,?,?)", (now - 3600 - k, mode, kind, app, str(k), label, "golden", result))
conn.execute("INSERT INTO run(started,finished,files,probed,active,new,resolved,extra) VALUES(?,?,?,?,?,?,?,?)", (now - 7200, now - 5400, 1234, 40, 6, 2, 3, "{}"))
conn.execute("INSERT OR REPLACE INTO kv VALUES('last_run_info', ?)", (json.dumps(dict(files=1234, probed=40, new=2, resolved=3, active=6, finished="2026-11-11T02:30:00", trash={"files": 1, "gb": 0.9})),)); conn.commit()

audit.refresh_reports(conn); t = audit.today_summary(conn, since); text = audit.digest_text(t)
gold = os.path.join(ROOT, "tests", "golden", "digest.txt")
if "--update" in sys.argv: open(gold, "w").write(text + "\n"); print("pinned:\n" + text); sys.exit(0)
want = open(gold).read().rstrip("\n") if os.path.exists(gold) else None
check("digest matches the pinned text (tests/golden/digest.txt)", want is not None and text == want, "\n--- got ---\n" + text + "\n--- want ---\n" + str(want))
check("a row being worked on stays out of Needs you", not any("S01E04" in n["title"] for n in t["needs"]), [n["title"] for n in t["needs"]])
check("critical before high in Needs you", [n["sev"] for n in t["needs"][:2]] == ["critical", "high"], [n["sev"] for n in t["needs"]])
check("error actions are listed but not counted as done", t["did"].get("AI subtitles made") == 1 and any("Kakegurui" in e for e in t["errors"]), (t["did"], t["errors"]))
check("dry-run and settings actions are not 'done'", "duplicates quarantined" not in t["did"] and all("setting" not in k for k in t["did"]), t["did"])
check("no private address or secret in the digest", "192.168." not in text and "apikey" not in text.lower(), text)

# ---- delivery: a mock ntfy receives exactly this text with a title, a priority and a link into the page
got = {}
class H(BaseHTTPRequestHandler):
    def do_POST(self): got.update(headers={k: v for k, v in self.headers.items()}, body=self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
audit.NTFY_URL = f"http://127.0.0.1:{srv.server_address[1]}/auditarr"; audit.NTFY_TOKEN_FILE = ""
audit.ntfy(f"Auditarr daily: {len(t['needs'])} need you · {sum(t['did'].values())} fixes overnight", text, "high", click=audit.page_url("findings")); time.sleep(0.3)
check("mock ntfy received the digest body unchanged", got.get("body") == text, got.get("body"))
check("push carries a title with the counts", got.get("headers", {}).get("Title") == f"Auditarr daily: {len(t['needs'])} need you · 5 fixes overnight", got.get("headers", {}).get("Title"))
check("push priority is high when something critical is open", got.get("headers", {}).get("Priority") == "high")
check("push links straight into the page (PUBLIC_URL + #findings)", got.get("headers", {}).get("Click") == "https://auditarr.example.test/#findings", got.get("headers", {}).get("Click"))
check("trash link variant", audit.page_url("trash") == "https://auditarr.example.test/#trash", audit.page_url("trash"))

# ---- one message for both apps: with DEDUPEARR_URL set, Dedupearr's own summary (its /api/digest) is added under its name
DD = {"title": "Dedupearr daily: 6 need you · 10.5 MB to get back", "lines": ["Needs you: 6 files, 10.5 MB to get back", "  - Stored twice: 1 file, 4.2 MB", "Since yesterday: nothing was moved", "No errors since yesterday"],
      "priority": "default", "needs": 6, "reclaim_bytes": 10500000, "reclaim_h": "10.5 MB"}
answer = {"body": json.dumps(DD).encode(), "code": 200}
seen_keys = []
class D(BaseHTTPRequestHandler):
    def do_GET(self): seen_keys.append(self.headers.get("X-Digest-Key")); self.send_response(answer["code"]); self.end_headers(); self.wfile.write(answer["body"])
    def log_message(self, *a): pass
dsrv = ThreadingHTTPServer(("127.0.0.1", 0), D); threading.Thread(target=dsrv.serve_forever, daemon=True).start()
check("without a Dedupearr address the message is Auditarr's alone", audit.dedupearr_digest() is None and audit.digest_join(text, None) == text)
audit.SETTINGS["DEDUPEARR_URL"] = f"http://127.0.0.1:{dsrv.server_address[1]}/"; dd = audit.dedupearr_digest(); both = audit.digest_join(text, dd)
check("with an address, Dedupearr's lines follow Auditarr's under its own name", both == text + "\n\nDedupearr:\n" + "\n".join(DD["lines"]), both)
check("the space to get back is offered for the title", dd["reclaim_h"] == "10.5 MB" and dd["high"] is False, dd)
# dedupearr #39 (2026-11-24): with Dedupearr's sign-in on this call got 401 and the message lost its lines. The key goes in a header, and only when one is set
check("without a Dedupearr key no key header is sent", seen_keys[-1] is None, seen_keys)
audit.SETTINGS["DEDUPEARR_KEY"] = "k" * 32; audit.dedupearr_digest(); check("with a Dedupearr key it is sent as X-Digest-Key", seen_keys[-1] == "k" * 32, seen_keys[-1]); audit.SETTINGS.pop("DEDUPEARR_KEY", None)
answer["code"] = 401; dd401 = audit.dedupearr_digest(); answer["code"] = 200
check("a Dedupearr that asks for a sign-in becomes one line that names the key to set", len(dd401["lines"]) == 1 and "Dedupearr key" in dd401["lines"][0] and "DIGEST_KEY" in dd401["lines"][0] and len(dd401["lines"][0]) <= 160, dd401)
answer["body"] = json.dumps(dict(DD, priority="high", lines=["x" * 500, 7, "a\nb"] + ["l"] * 40)).encode(); dd2 = audit.dedupearr_digest()
check("what Dedupearr sends is cut to size and kept as plain lines", len(dd2["lines"]) == 14 and len(dd2["lines"][0]) == 160 and dd2["lines"][1] == "a b" and dd2["high"] is True, dd2["lines"][:3])
for bad_answer in ((500, b"oops"), (200, b"<html>sign in</html>"), (200, b'{"lines": []}')):
    answer["code"], answer["body"] = bad_answer; dd3 = audit.dedupearr_digest()
    check(f"a Dedupearr that answers {bad_answer[0]} {bad_answer[1][:12]!r} becomes one plain line, and the message still goes out", len(dd3["lines"]) == 1 and "no answer this morning" in dd3["lines"][0] and audit.digest_join(text, dd3).startswith(text), dd3)
audit.SETTINGS.pop("DEDUPEARR_URL", None); dsrv.shutdown()
srv.shutdown(); conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
