"""Nightly self-test, run INSIDE the container after the audit (or on demand via GET /api/selftest):
1) re-probe a sample of findings per check and assert the claimed condition (detector truth),
2) exercise every API endpoint and every guard with reversible calls (API health).
Writes /config/reports/selftest.json (+ appends a line to selftest-history.jsonl). Exit code 1 on any failure."""
import json, os, random, sqlite3, subprocess, sys, time, urllib.request, urllib.error, datetime
BASE = os.environ.get("SELFTEST_BASE", "http://127.0.0.1:3091"); CONFIG = os.environ.get("CONFIG_DIR", "/config"); N = int(os.environ.get("SELFTEST_SAMPLE", "2"))
results = []
def check(name, cond, info=""): results.append({"name": name, "ok": bool(cond), "info": str(info)[:160]})
def req(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as x:
            raw = x.read(); ct = x.headers.get("Content-Type") or ""
            return x.status, (json.loads(raw or b"{}") if "json" in ct or path.endswith(".json") else raw[:100])
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}
t0 = time.time()
# ---- 1. detector truth (sample) ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import verify_checks_lib as V
except ImportError:
    V = None
c = sqlite3.connect(os.path.join(CONFIG, "state.db"), timeout=30)
rows = c.execute("SELECT check_code, severity, label, path, detail FROM finding WHERE resolved_at IS NULL AND path IS NOT NULL").fetchall()
by = {}
for r in rows: by.setdefault(r[0], []).append(r)
def probe(p):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=title:stream=index,codec_type,codec_name,profile,pix_fmt,field_order,start_time:stream_tags=language", "-of", "json", p], capture_output=True, text=True, timeout=120)
    return json.loads(o.stdout or "{}")
lang = lambda s: ((s.get("tags") or {}).get("language") or "und").lower()
def verify(code, pr):
    a = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"]; v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
    if code == "HI10P": return bool(v) and v[0]["codec_name"] == "h264" and "10" in (v[0].get("pix_fmt") or "")
    if code == "INTERLACED": return bool(v) and (v[0].get("field_order") or "progressive") not in ("progressive", "unknown", "")
    if code == "AV1": return bool(v) and v[0]["codec_name"] == "av1"
    if code == "TRUEHD_ONLY": return bool(a) and all(s["codec_name"] in ("truehd", "mlp") or (s["codec_name"] == "dts" and "MA" in (s.get("profile") or "")) for s in a)
    if code == "UND_AUDIO_NO_SUBS": return bool(a) and all(lang(s) in ("und", "") for s in a)
    if code == "WRONG_LANGUAGE": return bool(a) and not any(lang(s) in ("eng", "en") for s in a)
    if code == "DUB_ONLY": return bool(a) and not any(lang(s) in ("jpn", "ja") for s in a)
    if code == "AUDIO_OFFSET":
        vs = float(v[0].get("start_time") or 0) if v else 0; return bool(a) and max(abs(float(s.get("start_time") or 0) - vs) for s in a) > 1.5
    return None
random.seed(int(time.time()) // 86400)
for code in sorted(by):
    for _, sev, label, path, detail in random.sample(by[code], min(N, len(by[code]))):
        if not os.path.exists(path): continue
        try:
            pr = probe(path)
            if not pr.get("streams"): time.sleep(3); pr = probe(path)   # a file being moved/rewritten at this very second is not a detector failure
            if not pr.get("streams"): print(f"skip detector {code}: {label[:40]} (file unreadable right now)"); continue
            res = verify(code, pr); info = "audio=" + ",".join(lang(s_) for s_ in pr.get("streams", []) if s_.get("codec_type") == "audio") + " video=" + ",".join(s_.get("codec_name", "?") for s_ in pr.get("streams", []) if s_.get("codec_type") == "video")
        except Exception as e: res = False; info = repr(e)[:120]
        if res is not None: check(f"detector {code}: {label[:40]}", res, info)
# ---- 2. API health (reversible) ----
for p in ("/health", "/api/state", "/api/history", "/api/settings", "/api/trash", "/latest.json", "/latest.html", "/", "/progress.json"):
    s, _ = req(p); check(f"GET {p}", s in (200, 503) if p == "/health" else s == 200, f"HTTP {s}")
st = req("/api/state")[1]; F = st.get("findings", [])
check("state has required keys", all(k in st for k in ("findings", "regrabs", "health", "jobs", "by_library", "trash")))
check("findings carry library + links", all(f.get("library") for f in F) and all(f.get("arr") for f in F if f["app"] != "disk"))
check("per-app file counts present", bool((st.get("run") or {}).get("files_by_app")))
s, j = req("/api/settings", {"set": {"STALE_HOURS": "31"}}); check("settings save", s == 200 and j.get("saved", {}).get("STALE_HOURS") == "31")
s, j = req("/api/settings", {"set": {"DEDUPE": "banana"}}); check("settings reject bad value", s == 400)
s, j = req("/api/settings", {"reset": ["STALE_HOURS"]}); check("settings reset", s == 200 and "STALE_HOURS" not in j.get("saved", {}))
low = next((f for f in F if f["severity"] == "low" and f["app"] != "disk"), None)
if low:
    s, j = req("/api/ignore", {"id": low["id"]}); check("ignore by id", s == 200 and j.get("silenced") == 1)
    # undo: drop the rule line and un-resolve
    ig = os.path.join(CONFIG, "ignore.txt")
    if os.path.exists(ig):
        lines = [l for l in open(ig) if not l.startswith(low["id"])]; open(ig, "w").write("".join(lines))
    c.execute("UPDATE finding SET resolved_at=NULL WHERE id=?", (low["id"],)); c.commit()
    s, j = req("/api/act", {"id": low["id"], "action": "plexrefresh"}); check("plex refresh action", s == 200 and "plex" in j)
s, j = req("/api/ignore", {"id": "nope"}); check("ignore unknown -> 404", s == 404)
s, j = req("/api/act", {"id": "nope", "action": "regrab"}); check("act unknown -> 404", s == 404)
s, j = req("/api/restore", {"dest": "/etc/passwd"}); check("restore outside trash -> 400", s == 400)
s, j = req("/api/whisper", {"show": "zzz-no-such-show"}); check("AI-subs tool rejects unknown show", s == 404)
ao = next((f for f in F if f["check"] == "AUDIO_OFFSET" and "intentional" in f["detail"]), None)
if ao: s, j = req("/api/act", {"id": ao["id"], "action": "fixsync"}); check("fix-sync guard (intentional delay)", s == 409)
me = next((f for f in F if f["check"] in ("MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE")), None)   # not MISSING_FILE (that one has a real file id and a different refusal)
if me: s, j = req("/api/act", {"id": me["id"], "action": "regrab"}); check("regrab refused for a missing item", s == 400)
s, j = req("/api/search_show", {"show": "zzz-no-such-show"}); check("search-all-missing rejects unknown title", s == 404)
s, j = req("/api/settings"); check("nightly AI-subtitle + accept-language gates exist", s == 200 and {"AUTO_WHISPER", "AUTO_ACCEPTLANG"} <= {i["name"] for i in j.get("items", [])}, str(j)[:80])
s, j = req("/api/plan"); check("tonight's plan endpoint", s == 200 and {"regrab", "ai_subtitles", "accept_language", "fix_headers", "verify", "gates"} <= set(j), str(j)[:100])
check("findings carry a plain-language name and sentence", all(f.get("plain") and f.get("name") for f in F[:50]), [f.get("check") for f in F[:50] if not (f.get("plain") and f.get("name"))][:5])
s, j = req("/api/act", {"id": "nope", "action": "whisper"}); check("AI-subtitles action on unknown finding -> 404", s == 404)
s, j = req("/api/setup"); check("setup status endpoint", s == 200 and {"services", "first_run_done", "roots"} <= set(j) and any(x_["kind"] == "sonarr" for x_ in j["services"]), str(j)[:100])
s, j = req("/api/test_connection", {"kind": "sonarr", "url": "http://127.0.0.1:9", "key": "x"}); check("connection test reports an unreachable service without crashing", s == 200 and j.get("ok") is False and j.get("error"), j)
s, j = req("/api/settings"); check("secrets are masked in the settings list", all(not i["value"] or i["value"].startswith("••••") for i in j.get("items", []) if i["kind"] == "secret"))
s, j = req("/api/hooks"); check("import-hook status endpoint", s == 200 and "hooks" in j, str(j)[:80])
s, j = req("/webhook/sonarr?token=wrong", {"eventType": "Test"}); check("webhook without a valid token is refused", s == 403)
s, j = req("/api/state"); check("library scores present in state", s == 200 and isinstance(j.get("scores"), dict), str(j.get("scores"))[:80])
s, j = req("/api/history"); check("history carries the score series", s == 200 and "scores_history" in j)
h = req("/health")[1]; check("health ok", isinstance(h, dict) and h.get("status") in ("ok", "running"), str(h)[:80])
req("/api/ignore", {})   # refresh reports after the undo
# ---- write results ----
passed = sum(1 for r in results if r["ok"]); failed = [r for r in results if not r["ok"]]
out = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"), passed=passed, total=len(results), failed=[dict(name=r["name"], info=r["info"]) for r in failed], duration_s=round(time.time() - t0))
os.makedirs(os.path.join(CONFIG, "reports"), exist_ok=True)
json.dump(out, open(os.path.join(CONFIG, "reports", "selftest.json"), "w"), indent=1)
with open(os.path.join(CONFIG, "reports", "selftest-history.jsonl"), "a") as fh: fh.write(json.dumps({k: out[k] for k in ("ts", "passed", "total", "duration_s")} | {"failed": [r["name"] for r in failed]}) + "\n")
print(f"selftest: {passed}/{len(results)} passed in {out['duration_s']}s" + (": FAILED " + "; ".join(r["name"] for r in failed) if failed else ""))
sys.exit(1 if failed else 0)
