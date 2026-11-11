"""Golden digest (Q18): the morning push is pinned word for word, and delivered to a mock ntfy with the right headers.
Builds a small, fixed world (findings, actions, a 'working' row that must stay out of Needs-you), renders digest_text(today_summary())
and compares it with tests/golden/digest.txt. A changed wording is a deliberate change: run with --update to re-pin.
Run: python tests/golden_digest.py [--update]   Exit 1 on failure."""
import os, sys, tempfile, threading, time
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
conn.execute("INSERT INTO run(started,finished,files,probed,active,new,resolved) VALUES(?,?,?,?,?,?,?)", (now - 7200, now - 5400, 1234, 40, 6, 2, 3)); conn.commit()

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
check("push carries a title with the counts", got.get("headers", {}).get("Title") == "Auditarr daily: 3 need you · 5 fixes overnight", got.get("headers", {}).get("Title"))
check("push priority is high when something critical is open", got.get("headers", {}).get("Priority") == "high")
check("push links straight into the page (PUBLIC_URL + #findings)", got.get("headers", {}).get("Click") == "https://auditarr.example.test/#findings", got.get("headers", {}).get("Click"))
check("trash link variant", audit.page_url("trash") == "https://auditarr.example.test/#trash", audit.page_url("trash"))
srv.shutdown(); conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
