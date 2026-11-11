"""Performance budget (Q16): a big library must not make the page slow or the daemon fat.
World: 10,000 probed files, 5,000 open findings over 60 shows, 2,000 actions. Budgets are generous enough for a CI runner and tight
enough to catch an accidental O(n^2) or a report that balloons. Run: python tests/perf_budget.py   Exit 1 on failure."""
import json, os, random, resource, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-perf-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "r"); os.makedirs(os.environ["REPORT_DIR"])
os.environ["TRASH_DIR"] = os.path.join(TMP, "trash"); os.makedirs(os.environ["TRASH_DIR"]); os.environ["HOST_IP"] = ""
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
def timed(name, fn, budget_s):
    t0 = time.time(); out = fn(); dt = time.time() - t0; check(f"{name} in {dt:.2f}s (budget {budget_s}s)", dt <= budget_s, f"{dt:.2f}s"); return out

N_FILES, N_FIND, N_ACT = 10000, 5000, 2000
rnd = random.Random(16); now = time.time(); conn = audit.db()
codes = [("NO_SUBS", "medium"), ("WRONG_LANGUAGE", "high"), ("TITLE_MISMATCH", "low"), ("RUNTIME_LONG", "low"), ("HI10P", "low"), ("DUPLICATE_VIDEO", "medium"), ("NO_AUDIO", "critical"), ("MISSING_EPISODE", "medium"), ("AV1", "low"), ("UND_AUDIO_NO_SUBS", "low")]
shows = [f"Show {i:02d}" for i in range(60)]
t0 = time.time()
conn.executemany("INSERT OR REPLACE INTO probe(path,size,mtime,probed_at,ok,data) VALUES(?,?,?,?,1,?)",
                 [(f"/data/tv/{shows[i % 60]}/S{i // 600 + 1:02d}/{shows[i % 60]} S{i // 600 + 1:02d}E{i % 600 + 1:03d}.mkv", 1_500_000_000 + i, now - i, now, json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "tags": {"language": "eng"}}], "format": {"duration": "1400"}})) for i in range(N_FILES)])
rows = []
for i in range(N_FIND):
    code, sev = codes[i % len(codes)]; app = "sonarr" if i % 7 else "radarr"; show = shows[i % 60]
    label = f"{show} S{i // 600 + 1:02d}E{i % 600 + 1:03d}" if app == "sonarr" else f"Movie {i} (2020)"
    rows.append((f"p{i:05d}", app, i, code, sev, label, f"/data/tv/{show}/S{i // 600 + 1:02d}/{label}.mkv", f"detail {i} audio=['fre'] original=eng wanted=['eng']", now - rnd.randint(1, 90) * 86400, now))
conn.executemany("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,3,0)", rows)
conn.executemany("INSERT INTO action VALUES(?,?,?,?,?,?,?,?)", [(now - i * 30, "on", rnd.choice(["fixtag", "whisper", "regrab", "subsearch", "dedupe", "plex-refresh"]), "sonarr", str(i), f"{shows[i % 60]} S01E{i % 99 + 1:02d}", "perf", "ok") for i in range(N_ACT)])
conn.execute("INSERT INTO run(started,finished,files,probed,active,new,resolved,extra) VALUES(?,?,?,?,?,?,?,?)", (now - 7200, now - 3600, N_FILES, 300, N_FIND, 40, 12, "{}"))
conn.execute("INSERT OR REPLACE INTO kv VALUES('files_by_lib', ?)", (json.dumps({"TV": 9000, "Movies": 1000}),))
conn.execute("INSERT OR REPLACE INTO kv VALUES('last_run_info', ?)", (json.dumps(dict(files=N_FILES, probed=300, new=40, resolved=12, active=N_FIND, finished=time.strftime("%Y-%m-%dT%H:%M:%S"), trash={"files": 0, "gb": 0})),)); conn.commit()
print(f"seeded {N_FILES} files / {N_FIND} findings / {N_ACT} actions in {time.time() - t0:.1f}s")

timed("refresh_reports (latest.json + latest.html)", lambda: audit.refresh_reports(conn), 12)
st = timed("ui_state (the page's main request)", lambda: audit.ui_state(conn), 6)
size = len(json.dumps(st)); check(f"state payload {size/1e6:.1f} MB (budget 12 MB)", size <= 12e6)
check("state lists every open finding or caps with a note", len(st.get("findings", [])) == N_FIND or st.get("truncated") or st.get("cap"), len(st.get("findings", [])))
timed("today_summary", lambda: audit.today_summary(conn), 4)
timed("library_scores", lambda: audit.library_scores(conn), 3)
timed("plan_tonight (no arr connected)", lambda: audit.plan_tonight(conn), 6)
timed("ui_history", lambda: audit.ui_history(conn), 4)
timed("second ui_state (warm)", lambda: audit.ui_state(conn), 4)
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024; check(f"peak memory {rss:.0f} MB (budget 900 MB)", rss <= 900)
html = os.path.join(audit.REPORT_DIR, "latest.html"); check("latest.html written and under 20 MB", os.path.exists(html) and os.path.getsize(html) < 20e6, os.path.getsize(html) if os.path.exists(html) else None)
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
