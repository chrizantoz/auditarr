"""End-to-end: the real audit.py daemon against a fake Sonarr/Radarr/Bazarr (mock_stack.py) and synthetic media under /data.
Proves the whole chain: listing -> probing -> findings -> API actions move real files -> Undo brings them back -> arr commands go out,
then hands the live page to the real-browser bot (uibot/browser_bot.js) which clicks the real buttons.
Needs: a writable /data (CI: sudo mkdir -p /data && sudo chown $USER /data), ffmpeg, node + playwright in uibot/.  Exit 1 on failure."""
import json, os, shutil, subprocess, sys, time, tempfile, urllib.request, urllib.error, socket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synthlib import make  # noqa: E402
import mock_stack  # noqa: E402

DATA = os.environ.get("E2E_DATA", "/data")
if not (os.path.isdir(DATA) and os.access(DATA, os.W_OK)): print(f"need a writable {DATA} (sudo mkdir -p {DATA} && sudo chown $USER {DATA})"); sys.exit(2)
TV, MOVIES, TRASH = os.path.join(DATA, "tv"), os.path.join(DATA, "movies"), os.path.join(DATA, ".e2e-trash")
for d in (TV, MOVIES, TRASH): shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
CFG = tempfile.mkdtemp(prefix="auditarr-e2e-"); os.makedirs(os.path.join(CFG, "reports"))
results = []
def check(name, cond, info=""): results.append((name, bool(cond), str(info)[:200])); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# ---- world: media files that match the mock's records
W = mock_stack.default_world(TV, MOVIES)
make(W["files"][0]["path"], alang="eng")                      # S01E01 clean
make(W["files"][1]["path"], alang="fre")                      # S01E02 French on an English show -> WRONG_LANGUAGE high
make(W["files"][2]["path"], alang="jpn")                      # S01E03 Japanese, no subs, Bazarr wanted -> NO_SUBS medium
make(W["mfiles"][0]["path"], audio=False)                     # movie with no audio -> NO_AUDIO critical
season = os.path.dirname(W["files"][0]["path"]); dup = os.path.join(season, "Alpha.Show.S01E01.other-group.mkv"); shutil.copy(W["files"][0]["path"], dup)   # DUPLICATE_VIDEO
orphan = os.path.join(TV, "Random.Home.Video.mkv"); shutil.copy(W["files"][0]["path"], orphan)                                                           # ORPHAN_FILE
for f in W["files"] + W["mfiles"]: f["size"] = os.path.getsize(f["path"])
mock = mock_stack.start(W); MP = mock.server_address[1]; MURL = f"http://127.0.0.1:{MP}"

# ---- upgrade test (Q13): start on the newest sample database from a previous release; it must migrate and keep its history
import glob
samples = sorted(glob.glob(os.path.join(ROOT, "tests", "db-samples", "*.db")))
if samples: shutil.copy(samples[-1], os.path.join(CFG, "state.db")); print("seeded with", os.path.basename(samples[-1]))
# ---- the real daemon
PORT = free_port()
env = dict(os.environ, CONFIG_DIR=CFG, REPORT_DIR=os.path.join(CFG, "reports"), ARR_CONFIG_DIR=os.path.join(CFG, "no-arr-dir"), HTTP_PORT=str(PORT), HOST_IP="127.0.0.1", TZ="UTC",
           RUN_AT="23:59", RUN_ON_START="1", SONARR_URL=f"{MURL}/sonarr", SONARR_APIKEY="test", RADARR_URL=f"{MURL}/radarr", RADARR_APIKEY="test", WHISPARR_APIKEY="",
           BAZARR_URL=f"{MURL}/bazarr", BAZARR_APIKEY="test", PLEX_CHECK="off", PLEX_REFRESH="off", PLEX_TOKEN="", PAPERCLIP="off", NTFY_URL="", TRASH_DIR=TRASH,
           DEDUPE="off", REMEDIATE="off", SUBTAG="off", AUTO_FIXHEADER="off", AUTO_VERIFY="off", AUTO_WHISPER="off", AUTO_ACCEPTLANG="off", TRACK_PREFS="off", ARR_RESCAN="on",
           MISSING_MIN_DAYS="3", SUBGEN_URLS="", DOCKER_API="", PROBE_WORKERS="2", TAUTULLI_INI=os.path.join(CFG, "none.ini"), DISCOVER_EXTRA=f"{MURL}/sonarr,{MURL}/radarr,{MURL}/bazarr")
log = open(os.path.join(CFG, "daemon.log"), "w")
daemon = subprocess.Popen([sys.executable, os.path.join(ROOT, "audit.py"), "--daemon"], env=env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as x: raw = x.read(); return x.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}
def tail(n=25):
    log.flush()
    try: return "".join(open(os.path.join(CFG, "daemon.log")).read().splitlines(True)[-n:])
    except Exception: return ""
def finish(code):
    daemon.terminate()
    try: daemon.wait(10)
    except Exception: daemon.kill()
    mock.shutdown(); failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed   (config {CFG}, daemon log {CFG}/daemon.log)")
    if failed: print("--- daemon log tail ---\n" + tail(40))
    for d in (TV, MOVIES, TRASH): shutil.rmtree(d, ignore_errors=True)
    sys.exit(code if code else (1 if failed else 0))

# wait for the first run
t0 = time.time(); up = False
while time.time() - t0 < 60:
    try: api("/health"); up = True; break
    except Exception: time.sleep(1)
check("daemon answers /health", up, tail(5))
if not up: finish(1)
phase = None
while time.time() - t0 < 300:
    s, st = api("/api/state"); phase = (st.get("progress") or {}).get("phase")
    if phase == "done": break
    if phase == "failed": break
    time.sleep(2)
check("first audit run finished", phase == "done", f"phase={phase}\n{tail(15)}")
if samples:
    s, hj = api("/api/history"); check("upgrade: a database from the previous release migrates and keeps its run history", s == 200 and len(hj.get("runs", [])) >= 3, len(hj.get("runs", [])))
s, st = api("/api/state"); F = st.get("findings", []); H = st.get("health", {})
byt = {}
for f in F: byt.setdefault(f["check"], []).append(f)
print("findings:", {k: len(v) for k, v in byt.items()})
check("both arrs and Bazarr were reachable (no degraded services)", not H.get("degraded"), H.get("degraded"))
check("French episode -> WRONG_LANGUAGE high", any("S01E02" in f["title"] and f["severity"] == "high" for f in byt.get("WRONG_LANGUAGE", [])), [(f["title"], f["severity"]) for f in byt.get("WRONG_LANGUAGE", [])])
check("Japanese episode Bazarr is hunting -> NO_SUBS medium", any("S01E03" in f["title"] and f["severity"] == "medium" for f in byt.get("NO_SUBS", [])), [(f["title"], f["severity"]) for f in byt.get("NO_SUBS", [])])
check("movie without audio -> NO_AUDIO critical", any(f["severity"] == "critical" for f in byt.get("NO_AUDIO", [])), byt.get("NO_AUDIO"))
check("missing aired episode -> MISSING_EPISODE", any("S01E04" in f["title"] for f in byt.get("MISSING_EPISODE", [])), [f["title"] for f in byt.get("MISSING_EPISODE", [])])
check("second file for S01E01 -> DUPLICATE_VIDEO", any(os.path.basename(dup) in (f["path"] or "") for f in byt.get("DUPLICATE_VIDEO", [])), byt.get("DUPLICATE_VIDEO"))
check("stray video in the root -> ORPHAN_FILE", any(os.path.basename(orphan) in (f["path"] or "") for f in byt.get("ORPHAN_FILE", [])), byt.get("ORPHAN_FILE"))
check("every finding has a plain sentence and a name", all(f.get("plain") and f.get("name") for f in F))
check("clean episode has no findings", not any("S01E01" in f["title"] and f["app"] == "sonarr" for f in F), [f["check"] for f in F if "S01E01" in f["title"] and f["app"] == "sonarr"])

# ---- actions move real files, Undo brings them back
dupf = next((f for f in byt.get("DUPLICATE_VIDEO", []) if os.path.basename(dup) in (f["path"] or "")), None)
if dupf:
    s, j = api("/api/act", {"id": dupf["id"], "action": "quarantine"})
    check("Move to trash: file leaves the folder and lands in the trash", s == 200 and not os.path.exists(dup) and j.get("dest") and os.path.exists(j["dest"]), (s, j))
    s2, tr = api("/api/trash"); check("Trash tab lists it with the reason", any(os.path.basename(dup) == t["name"] for t in tr.get("items", [])), [t["name"] for t in tr.get("items", [])])
    s3, rj = api("/api/restore", {"dest": j.get("dest", "")}); check("Undo / Put back: file is back where it was", s3 == 200 and os.path.exists(dup), (s3, rj))
fr = next((f for f in byt.get("WRONG_LANGUAGE", []) if "S01E02" in f["title"]), None)
if fr:
    mock.calls.clear(); s, j = api("/api/act", {"id": fr["id"], "action": "regrab"})
    cmds = [c["body"].get("name") for c in mock.calls if c["path"] == "/api/v3/command"]
    check("Download a better copy: file to trash + RescanSeries + EpisodeSearch sent to Sonarr", s == 200 and not os.path.exists(W["files"][1]["path"]) and "RescanSeries" in cmds and "EpisodeSearch" in cmds, (s, cmds, j.get("error")))
    s, st = api("/api/state"); check("re-grab is tracked as pending", any(r["status"] == "pending" and "S01E02" in r["label"] for r in st.get("regrabs", [])), [(r["label"], r["status"]) for r in st.get("regrabs", [])])
    s, rj = api("/api/restore", {"dest": j.get("dest", "")}); check("restoring the re-grabbed file works", s == 200 and os.path.exists(W["files"][1]["path"]), (s, rj))
# ---- import hook + F14 (moved ahead of the AI-subtitle button so the one-try-a-week guard does not hide the follow-up)
mock.calls.clear(); s, j = api("/api/hook_setup", {"app": "sonarr"})
hooks_posted = [c for c in mock.calls if c["path"] == "/api/v3/notification" and c["method"] == "POST"]
check("import hook: Auditarr registers its webhook in Sonarr", s == 200 and hooks_posted and "/webhook/sonarr" in json.dumps(hooks_posted[0]["body"]), (s, j))
s, j = api("/api/hooks"); check("hook status lists Sonarr as connected with the right address", s == 200 and any(h["app"] == "sonarr" and h.get("connected") and h.get("url_ok") for h in j.get("hooks", [])), j)
tok = json.dumps(hooks_posted[0]["body"]).split("token=")[1].split('"')[0] if hooks_posted else ""
s, j = api("/webhook/sonarr?token=nope", {"eventType": "Test"}); check("webhook rejects a wrong token", s == 403, s)
s, j = api(f"/webhook/sonarr?token={tok}", {"eventType": "Test"}); check("webhook answers Sonarr's Test", s == 200 and j.get("ok"), j)
make(W["files"][0]["path"], alang="fre")   # S01E01 just got 'upgraded' to a French release
s, j = api(f"/webhook/sonarr?token={tok}", {"eventType": "Download", "series": {"id": 1}, "episodes": [{"id": 101}], "episodeFile": {"id": 11, "path": W["files"][0]["path"]}})
check("webhook accepts an import event", s == 202, (s, j))
# ---- F14: a new download without subtitles gets AI subtitles the same day once Bazarr has had its head start (1 minute here)
api("/api/settings", {"set": {"AUTO_WHISPER": "on", "AUTO_WHISPER_IMPORT_MINUTES": "1"}})
s, j = api(f"/webhook/sonarr?token={tok}", {"eventType": "Download", "series": {"id": 1}, "episodes": [{"id": 103}], "episodeFile": {"id": 13, "path": W["files"][2]["path"]}})
deadline = time.time() + 150; queued = None
while time.time() < deadline and not queued:
    time.sleep(5); s, st_ = api("/api/state"); queued = next((a for a in st_.get("actions", []) if a.get("detail") == "AI subtitles queued after import"), None)
check("import follow-up: AI subtitles are queued when Bazarr still has nothing after the head start", bool(queued), tail(8))
api("/api/settings", {"set": {"AUTO_WHISPER": "off", "AUTO_WHISPER_IMPORT_MINUTES": "0"}})
t3 = time.time(); hit = None
while time.time() - t3 < 60:
    s, st = api("/api/state"); hit = next((f for f in st.get("findings", []) if f["check"] == "WRONG_LANGUAGE" and "S01E01" in f["title"]), None)
    if hit: break
    time.sleep(3)
check("the imported file is checked within a minute and the problem shows up", hit is not None and hit["severity"] == "high", hit)
ns = next((f for f in byt.get("NO_SUBS", []) if "S01E03" in f["title"]), None)
if ns:
    s, j = api("/api/act", {"id": ns["id"], "action": "whisper"}); check("Make AI subtitles queues a job", s == 200 and j.get("queued"), (s, j))
    s, st = api("/api/state"); wf = next((f for f in st.get("findings", []) if f["id"] == ns["id"]), None); t_click = time.time()
    done_fast = any(a.get("kind") == "whisper" and "S01E03" in a.get("title", "") and a.get("ts", "") >= time.strftime("%Y-%m-%d %H:%M", time.localtime(t_click - 60)) for a in st.get("actions", []))
    check("the row is flagged 'working' right away and leaves Needs you", (wf and wf["detail"].startswith("[working:") and not any(n.get("id") == ns["id"] for n in (st.get("today") or {}).get("needs", []))) or done_fast, (wf or {}).get("detail"))
    t4 = time.time(); wf2 = wf
    while time.time() - t4 < 90 and wf2 and wf2["detail"].startswith("[working:"):
        time.sleep(5); s, st = api("/api/state"); wf2 = next((f for f in st.get("findings", []) if f["id"] == ns["id"]), None)
    check("when the job gives up (fake Bazarr offers no Whisper) the flag is removed and the row is back", wf2 and not wf2["detail"].startswith("[working:"), (wf2 or {}).get("detail"))
    mock.calls.clear(); s, j = api("/api/act", {"id": ns["id"], "action": "subsearch"}); time.sleep(3)
    check("Find subtitles reaches Bazarr (PATCH subtitles)", s == 200 and any(c["app"] == "bazarr" and c["method"] == "PATCH" for c in mock.calls), [(c["app"], c["method"], c["path"]) for c in mock.calls][:5])
mock.calls.clear(); s, j = api("/api/search_show", {"show": "Alpha Show"})
check("Search all missing sends one EpisodeSearch with the missing ids", s == 200 and any(c["body"].get("name") == "EpisodeSearch" and 104 in (c["body"].get("episodeIds") or []) for c in mock.calls if c["path"] == "/api/v3/command"), (s, j, [c["body"] for c in mock.calls][:3]))
s, j = api("/api/plan"); check("tonight's plan answers", s == 200 and "regrab" in j, j)
s, j = api("/api/discover"); found = {f["kind"]: f for f in (j.get("found") or [])}
check("discovery finds the fake Sonarr, Radarr and Bazarr by their public pages", s == 200 and {"sonarr", "radarr", "bazarr"} <= set(found), (s, sorted(found)))
s, j = api("/api/test_connection", {"kind": "sonarr", "url": f"{MURL}/sonarr", "key": "wrongkey"}); check("a wrong key gets a plain explanation and a next step", s == 200 and not j.get("ok") and "rejected" in j.get("error", "") and "Settings" in j.get("fix", ""), j)
s, j = api("/api/test_connection", {"kind": "radarr", "url": "http://127.0.0.1:9", "key": "x"}); check("a dead address says nothing is listening and what to check", s == 200 and "listening" in j.get("error", "") and "port" in j.get("fix", "").lower(), j)
s, j = api("/api/about"); check("About reports version, uptime, database size and tools", s == 200 and all(k in j for k in ("version", "uptime_s", "db_mb", "tools", "latest")), j)
s, j = api("/api/update", {}); check("Update without a host hook explains how to update instead", s == 400 and "update" in j.get("error", "").lower(), (s, j))
s, st = api("/api/state"); sc = st.get("scores") or {}
check("library scores computed per library after a run", any(k in sc for k in ("TV", "Movies", "Other")) and all(0 <= v["score"] <= 100 for v in sc.values()), sc)
s, st = api("/api/state"); check("the import check is recorded in Actions", any(a["kind"] == "import-check" for a in st.get("actions", [])), [a["kind"] for a in st.get("actions", [])][:6])
make(W["files"][0]["path"], alang="eng")   # back to clean for the rest of the run: a fixed file re-imported must clear its old findings on the import check
s, j = api(f"/webhook/sonarr?token={tok}", {"eventType": "Download", "series": {"id": 1}, "episodes": [{"id": 101}], "episodeFile": {"id": 11, "path": W["files"][0]["path"]}})
t5 = time.time(); still = True
while time.time() - t5 < 60 and still:
    time.sleep(3); s, st = api("/api/state"); still = any(f["check"] in ("WRONG_LANGUAGE", "NO_SUBS") and "S01E01" in f["title"] for f in st.get("findings", []))
check("a fixed file re-imported clears its old findings on the import check", not still, [f["check"] for f in st.get("findings", []) if "S01E01" in f["title"]])
s, j = api("/api/test_connection", {"kind": "sonarr", "url": f"{MURL}/sonarr", "key": "test"}); check("setup: connection test to the (fake) Sonarr succeeds and lists its root folder as visible", s == 200 and j.get("ok") and any(r["visible"] for r in j.get("roots", [])), j)
s, j = api("/api/test_connection", {"kind": "sonarr", "url": f"{MURL}/sonarr", "key": "wrong"}); check("setup: a wrong key is reported as such", s == 200 and not j.get("ok") and "key" in (j.get("error") or ""), j)
s, j = api("/api/setup"); check("setup page: both arrs connected, media visible, first run done", s == 200 and j.get("complete") and j.get("first_run_done") and j.get("media_visible"), {k: j.get(k) for k in ("complete", "first_run_done", "media_visible")})
s, j = api("/api/settings", {"set": {"RADARR_APIKEY": "supersecretkey1234"}}); s2, st2 = api("/api/state")
leak = "supersecretkey1234" in json.dumps(j) or "supersecretkey1234" in json.dumps(st2.get("actions", [])) or "supersecretkey1234" in json.dumps(api("/api/settings")[1])
check("a saved API key never appears in a response or the Actions log", s == 200 and not leak, (s, [a for a in st2.get("actions", []) if a["kind"] == "settings"][:1]))
api("/api/settings", {"set": {"RADARR_APIKEY": "test"}})
s, j = api("/api/settings", {"set": {"DEDUPE": "dryrun"}}); s2, g = api("/api/settings")
check("a setting saved in the UI is reported as saved", s == 200 and any(i["name"] == "DEDUPE" and i["value"] == "dryrun" and i["source"] == "saved in UI" for i in g.get("items", [])), (s, j))
orp = next((f for f in byt.get("ORPHAN_FILE", []) if os.path.basename(orphan) in (f["path"] or "")), None)
if orp:
    s, j = api("/api/ignore", {"id": orp["id"]}); s2, st = api("/api/state")
    check("Ignore hides the finding", s == 200 and not any(f["id"] == orp["id"] for f in st.get("findings", [])), (s, j))
s0, st = api("/api/state"); prev_ts = ((st.get("tests") or {}).get("selftest") or {}).get("ts")
s, j = api("/api/selftest"); t1 = time.time(); st_ = {}
while time.time() - t1 < 150:   # the self-test runs in a thread; wait for the result of THIS trigger, not an earlier automatic one
    s2, st = api("/api/state"); st_ = (st.get("tests") or {}).get("selftest") or {}
    if st_.get("total") and st_.get("ts") and st_.get("ts") != prev_ts: break
    time.sleep(3)
print("in-container self-test:", st_.get("passed"), "/", st_.get("total"), st_.get("failed"))
check("the built-in self-test passes inside the fake stack", st_.get("total") and not st_.get("failed"), st_)

# ---- built-in sign-in: the test talks from 127.0.0.1 (always allowed), so prove the gate with a request that pretends to come from elsewhere
import http.client
def raw(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60); c.request(method, path, body=body, headers=headers or {}); r = c.getresponse(); data = r.read(); c.close(); return r.status, dict(r.getheaders()), data
s, j = api("/api/setpassword", {"user": "demo", "password": "secret123"}); check("sign-in: password can be set from the setup page", s == 200 and j.get("ok"), (s, j))
s, j = api("/api/state"); check("sign-in: the container itself (self-test, hooks) is still allowed", s == 200 and (j.get("auth") or {}).get("enabled"), (s, (j.get("auth") if isinstance(j, dict) else None)))
st_, hd, body_ = raw("POST", "/login", "user=demo&password=wrong", {"Content-Type": "application/x-www-form-urlencoded"}); check("sign-in: wrong password is refused", st_ == 401 and b"Wrong user or password" in body_, st_)
st_, hd, body_ = raw("POST", "/login", "user=demo&password=secret123", {"Content-Type": "application/x-www-form-urlencoded"}); cookie = (hd.get("Set-Cookie") or "").split(";")[0]
check("sign-in: right password sets a session cookie and redirects to the page", st_ == 303 and cookie.startswith("auditarr_s="), (st_, hd.get("Set-Cookie")))
st_, hd, body_ = raw("GET", "/api/state", headers={"Cookie": cookie}); check("sign-in: the cookie opens the API", st_ == 200)
st_, hd, body_ = raw("GET", "/login"); check("sign-in page is served without a cookie", st_ == 200 and b"Sign in" in body_)
st_, hd, body_ = raw("POST", "/api/setpassword", json.dumps({"role": "viewer", "user": "guest", "password": "guestguest"}), {"Content-Type": "application/json", "Cookie": cookie}); check("viewer: account can be added by the signed-in admin", st_ == 200, (st_, body_[:120]))
st_, hd, body_ = raw("POST", "/login", "user=guest&password=guestguest", {"Content-Type": "application/x-www-form-urlencoded"}); vcookie = (hd.get("Set-Cookie") or "").split(";")[0]
check("viewer: can sign in", st_ == 303 and vcookie.startswith("auditarr_s="), st_)
st_, hd, body_ = raw("GET", "/api/state", headers={"Cookie": vcookie}); check("viewer: sees the page state with role viewer", st_ == 200 and json.loads(body_).get("auth", {}).get("role") == "viewer", (st_, body_[:80]))
st_, hd, body_ = raw("POST", "/api/run", "{}", {"Content-Type": "application/json", "Cookie": vcookie}); check("viewer: any change is refused with 403", st_ == 403, (st_, body_[:120]))
st_, hd, body_ = raw("POST", "/api/setpassword", json.dumps({"role": "admin", "user": "evil", "password": "evilevil1"}), {"Content-Type": "application/json", "Cookie": vcookie}); check("viewer: cannot take over the main password", st_ == 403, st_)
st_, hd, body_ = raw("POST", "/api/logout", "{}", {"Content-Type": "application/json", "Cookie": vcookie}); check("viewer: can sign out", st_ == 200, st_)
st_, hd, body_ = raw("GET", "/health"); check("health stays open for monitoring", st_ == 200)
st_, hd, body_ = raw("POST", "/api/logout", headers={"Cookie": cookie}); check("sign-out clears the cookie", st_ == 200 and "Max-Age=0" in (hd.get("Set-Cookie") or ""))

# ---- a fresh run so the restored duplicate is a finding again, then the real browser clicks the real page
api("/run"); t2 = time.time(); ph = None
while time.time() - t2 < 240:
    s, st = api("/api/state"); ph = (st.get("progress") or {}).get("phase")
    if ph in ("done", "failed") and (st.get("run") or {}).get("finished", "") > time.strftime("%Y-%m-%dT%H:%M", time.localtime(t2 - 60)): break
    time.sleep(2)
s, st = api("/api/state"); check("second run re-detects the restored duplicate", any(os.path.basename(dup) in (f["path"] or "") for f in st.get("findings", []) if f["check"] == "DUPLICATE_VIDEO"), ph)
bot = os.path.join(ROOT, "uibot", "browser_bot.js")
if os.path.exists(os.path.join(ROOT, "uibot", "node_modules", "playwright")):
    p = subprocess.run(["node", bot, os.path.join(ROOT, "ui.html"), BASE], capture_output=True, text=True, timeout=600)
    print(p.stdout[-3000:]); bad = [l for l in p.stdout.splitlines() if l.startswith("FAIL")]
    if "Executable doesn't exist" in (p.stderr + p.stdout): print("SKIP browser bot (Chromium not downloaded here; CI runs it)")
    else: check("real-browser bot passes against the live app", p.returncode == 0, "; ".join(bad)[:200] or p.stderr[-300:])
else: print("SKIP browser bot (uibot/node_modules/playwright missing)")
# Q25: nothing unexpected may have happened during all of the above
errlog = os.path.join(CFG, "reports", "errors.log"); errtxt = open(errlog, encoding="utf-8", errors="replace").read() if os.path.exists(errlog) else ""
check("no unexpected error was recorded during the whole stack test (errors.log empty)", not errtxt.strip(), errtxt[-600:])
if errtxt.strip(): print("---- errors.log ----\n" + errtxt[-4000:])
finish(0)
