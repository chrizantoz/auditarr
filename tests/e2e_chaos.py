"""Chaos in the fake stack (Q15): random 500s, slow answers and one arr vanishing mid-run.
The audit must finish (degraded, not crashed), keep the vanished app's findings open instead of resolving them, make no writes,
and come back clean on the next run. Needs a writable /data and ffmpeg, like e2e_stack.py.  Exit 1 on failure."""
import json, os, shutil, subprocess, sys, time, tempfile, urllib.request, urllib.error, socket
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synthlib import make  # noqa: E402
import mock_stack  # noqa: E402
DATA = os.environ.get("E2E_DATA", "/data")
if not (os.path.isdir(DATA) and os.access(DATA, os.W_OK)): print(f"need a writable {DATA}"); sys.exit(2)
TV, MOVIES, TRASH = os.path.join(DATA, "chaos-tv"), os.path.join(DATA, "chaos-movies"), os.path.join(DATA, ".chaos-trash")
for d in (TV, MOVIES, TRASH): shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
CFG = tempfile.mkdtemp(prefix="auditarr-chaos-"); os.makedirs(os.path.join(CFG, "reports"))
results = []
def check(name, cond, info=""): results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:300]}]"))
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
W = mock_stack.default_world(TV, MOVIES)
make(W["files"][0]["path"], alang="eng"); make(W["files"][1]["path"], alang="fre"); make(W["files"][2]["path"], alang="jpn"); make(W["mfiles"][0]["path"], audio=False)
for f in W["files"] + W["mfiles"]: f["size"] = os.path.getsize(f["path"])
mock = mock_stack.start(W); MURL = f"http://127.0.0.1:{mock.server_address[1]}"
PORT = free_port()
env = dict(os.environ, CONFIG_DIR=CFG, REPORT_DIR=os.path.join(CFG, "reports"), ARR_CONFIG_DIR=os.path.join(CFG, "no-arr-dir"), HTTP_PORT=str(PORT), HOST_IP="127.0.0.1", TZ="UTC",
           RUN_AT="23:59", RUN_ON_START="1", SONARR_URL=f"{MURL}/sonarr", SONARR_APIKEY="test", RADARR_URL=f"{MURL}/radarr", RADARR_APIKEY="test", WHISPARR_APIKEY="",
           BAZARR_URL=f"{MURL}/bazarr", BAZARR_APIKEY="test", PLEX_CHECK="off", PLEX_REFRESH="off", PLEX_TOKEN="", PAPERCLIP="off", NTFY_URL="", TRASH_DIR=TRASH,
           DEDUPE="off", REMEDIATE="off", SUBTAG="off", AUTO_FIXHEADER="off", AUTO_VERIFY="off", AUTO_WHISPER="off", AUTO_ACCEPTLANG="off", TRACK_PREFS="off", ARR_RESCAN="off",
           MISSING_MIN_DAYS="3", SUBGEN_URLS="", DOCKER_API="", PROBE_WORKERS="2", TAUTULLI_INI=os.path.join(CFG, "none.ini"))
log = open(os.path.join(CFG, "daemon.log"), "w")
daemon = subprocess.Popen([sys.executable, os.path.join(ROOT, "audit.py"), "--daemon"], env=env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data is not None else "GET", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as x: raw = x.read(); return x.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}
def tail(n=25):
    log.flush()
    try: return "".join(open(os.path.join(CFG, "daemon.log")).read().splitlines(True)[-n:])
    except Exception: return ""
def wait_run(limit=420):
    t0 = time.time(); phase = None; last = None
    while time.time() - t0 < limit:
        try: s, st = api("/api/state"); phase = (st.get("progress") or {}).get("phase"); last = st
        except Exception: phase = "unreachable"
        if phase in ("done", "failed"): break
        time.sleep(3)
    return phase, last or {}
def finish(code=0):
    daemon.terminate()
    try: daemon.wait(10)
    except Exception: daemon.kill()
    mock.shutdown(); failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed   (config {CFG}, daemon log {CFG}/daemon.log)")
    if failed: print("--- daemon log tail ---\n" + tail(40))
    for d in (TV, MOVIES, TRASH): shutil.rmtree(d, ignore_errors=True)
    sys.exit(code if code else (1 if failed else 0))

# 1. a clean baseline run
t0 = time.time()
while time.time() - t0 < 60:
    try: api("/health"); break
    except Exception: time.sleep(1)
phase, st = wait_run(); check("baseline run finishes", phase == "done", f"phase={phase}\n{tail(15)}")
base_ids = {f["id"]: (f["app"], f["check"]) for f in st.get("findings", [])}
radarr_ids = {i for i, (a, c) in base_ids.items() if a == "radarr"}
check("baseline has findings for both arrs", any(a == "sonarr" for a, _ in base_ids.values()) and radarr_ids, base_ids)

# 2. chaos on: 500s, slow answers, Radarr drops the connection for a window of requests
mock.chaos = mock_stack.chaos(); mock.calls.clear()
s, j = api("/api/run", {}); check("run starts under chaos", s in (200, 202), (s, j))
time.sleep(3); phase, st = wait_run()
check("chaotic run ends (done or failed), the daemon does not hang", phase in ("done", "failed"), f"phase={phase}\n{tail(15)}")
check("daemon process still alive after chaos", daemon.poll() is None, daemon.poll())
H = st.get("health") or {}
check("run reports degraded services instead of pretending all is well", H.get("degraded"), H)
ids_after = {f["id"] for f in st.get("findings", [])}
check("findings of the vanished app are kept open, not resolved", radarr_ids and radarr_ids <= ids_after, (radarr_ids, [i for i in radarr_ids if i not in ids_after]))
writes = [c for c in mock.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]
check("no half-done writes: chaos run sent no write to any arr or Bazarr", not writes, writes[:5])
text = open(os.path.join(CFG, "daemon.log")).read()
check("no Python traceback in the daemon log", "Traceback (most recent call last)" not in text, [l for l in text.splitlines() if "Error" in l][:5])
s, h = api("/health"); check("/health still answers after chaos", s in (200, 503), s)

# 3. chaos off: the next run is clean again and matches the baseline
mock.chaos = None; s, j = api("/api/run", {}); time.sleep(3); phase, st = wait_run()
check("recovery run finishes clean", phase == "done" and not (st.get("health") or {}).get("degraded"), (phase, (st.get("health") or {}).get("degraded"), tail(10)))
ids_rec = {f["id"] for f in st.get("findings", [])}
check("recovery run reproduces the baseline findings", ids_rec == set(base_ids), (sorted(set(base_ids) - ids_rec), sorted(ids_rec - set(base_ids))))
finish()
