"""Regression corpus (Q11): one replayable case per real incident. Each function reproduces the exact input that bit us and
asserts the behaviour we fixed. Add a case whenever a bug is fixed; never delete one.
Run: python tests/regressions.py   Exit 1 on failure."""
import datetime, json, os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-reg-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "reports"); os.makedirs(os.environ["REPORT_DIR"])
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))

# 2026-11-10: Bazarr's history 'timestamp' is prose ("7 minutes ago"); comparing it to a float raised TypeError and Whisper jobs never completed
def bazarr_ts(row):
    for v, fmt in ((row.get("parsed_timestamp"), "%m/%d/%y %H:%M:%S"), (row.get("timestamp"), None)):
        if v is None: continue
        try: return datetime.datetime.strptime(str(v), fmt).timestamp() if fmt else float(v)
        except (TypeError, ValueError): continue
    return 0
try:
    t = bazarr_ts({"timestamp": "7 minutes ago", "parsed_timestamp": "11/10/26 18:27:11"}); check("bazarr history: prose timestamp falls back to parsed_timestamp", abs(t - datetime.datetime(2026, 11, 10, 18, 27, 11).timestamp()) < 1, t)
    check("bazarr history: garbage in both fields -> 0, no exception", bazarr_ts({"timestamp": "yesterday", "parsed_timestamp": "n/a"}) == 0)
    src = open(os.path.join(ROOT, "audit.py")).read(); check("whisper worker reads parsed_timestamp, not the prose field", "parsed_timestamp" in src and 'r.get("provider") == "whisperai" and _ts(r)' in src)
except Exception as e: check("bazarr timestamp case runs", False, repr(e))

# 2026-11-10: self-test picked any check starting with MISSING_ (MISSING_FILE has a real file id) and judged re-grab refusal wrongly
st = open(os.path.join(ROOT, "tests", "selftest.py")).read()
check("self-test picks a missing-item finding, never MISSING_FILE", '("MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE")' in st and 'startswith("MISSING_")' not in st)

# 2026-11-10: the verified-content sentence fell through when the verdict carried extra words ("CONTENT MATCHES S01E02 (3/3 samples)")
s1 = audit.plain_sentence("TITLE_MISMATCH", "low", "Show S01E02", "[verified 14:24: CONTENT MATCHES S01E02 (3/3 samples)] || embedded title 'x'", "sonarr")
check("verified verdict with extra words still maps to the plain sentence", s1.startswith("Checked: the dialogue matches"), s1)
s2 = audit.plain_sentence("RUNTIME_LONG", "medium", "Show S01E02", "[verified 15:00: inconclusive (leaning S01E07, arr says S01E09)] || file 48 min", "sonarr")
check("inconclusive verdict gets an honest sentence, not raw text", s2.startswith("Checked, but the result was inconclusive"), s2)

# 2026-11-11: the settings save response and the Actions log echoed API keys in clear
conn = audit.db(); audit.load_settings(conn)
code, obj = audit.ui_settings_post(conn, {"set": {"SONARR_APIKEY": "topsecretvalue99"}}, "test")
acts = [r[0] for r in conn.execute("SELECT result FROM action WHERE kind='settings'")]
check("saving an API key never echoes it", code == 200 and "topsecretvalue99" not in json.dumps(obj) and not any("topsecretvalue99" in a for a in acts), (obj.get("saved"), acts[-1:] ))
check("settings list masks the key", all(not i["value"] or i["value"].startswith("••••") for i in audit.ui_settings_get()["items"] if i["kind"] == "secret"))

# 2026-11-11: staging copies ran the real job queue and the accept-language pass; JOBS=off must make the worker record jobs without running them
os.environ["JOBS"] = "off"
try:
    audit.enqueue(dict(type="verify", finding="x", app="sonarr", fid=1, path="/nope", label="Show S01E01", actor="test")); time.sleep(1.5)
    st_ = conn.execute("SELECT status FROM jobs ORDER BY id DESC LIMIT 1").fetchone(); check("JOBS=off: queued job is recorded as skipped, not run", st_ and st_[0] in ("skipped", "queued"), st_)
finally: os.environ.pop("JOBS", None)

# 2026-11-11: orphan scan flagged a file the arr imported two minutes after the listing; the end-of-run re-check must consult the arr live
check("orphan re-check against the arr exists in the run", "arr_has_now(full)" in open(os.path.join(ROOT, "audit.py")).read())

# 2026-11-11: a deploy swapped the container while a 6-file MP4 rewrite was in flight; /health must expose busy so the deploy script waits
h = audit.health_info(); check("health reports an in-flight counter for the deploy script", "busy" in h and isinstance(h["busy"], int), h.get("busy"))

# 2026-11-11: fuzzer - arr media info with an unparseable runTime crashed the whole check; None episode titles crashed TITLE_MISMATCH
check("hms('abc') is None, not an exception", audit.hms("abc") is None and audit.hms("0:42:00") == 2520)
it = dict(app="sonarr", kind="series", fid=1, parent_id=1, label="S S01E01", path="/data/x.mkv", size=1, orig="eng", intent=audit.intent_from_tags([]), title="Alpha Show Title", season=1, ep_titles=[None], quality={}, mi={}, expected=0, expected_lo=0, date_added=None, ep_ids=[1], n_eps=1, series_type=None, edge=False, slug="s", monitored=True,
          probe={"streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "tags": {"language": "eng"}}], "format": {"tags": {"title": "Completely Different Episode Name"}}}, probe_ok=True, disk_size=1, mtime=0)
try: codes = {c for c, _, _ in audit.check(it, set(), set())}; check("None episode title does not crash TITLE_MISMATCH", "TITLE_MISMATCH" in codes, codes)
except Exception as e: check("None episode title does not crash TITLE_MISMATCH", False, repr(e))

# 2026-11-11: a 6-file batch killed mid-way left a .fixtag.tmp file; the MP4 path must verify the swapped file and roll back otherwise (S7)
src = open(os.path.join(ROOT, "audit.py")).read(); check("MP4 rewrite verifies the swapped file against the verified copy (S7)", "sample_hash(path) != sig_tmp" in src and "original put back" in src)

# 2026-11-11: Verify refused movies ("verify supports series episodes only"); S1 compares a movie's dialogue with its synopsis instead
src = open(os.path.join(ROOT, "audit.py")).read(); check("content verification covers movies (S1)", "def verify_movie(" in src and "verify supports series episodes only" not in src)
# 2026-11-11: a viewer cookie must beat the 'container itself is admin' rule, or the viewer gate cannot be tested (and a viewer on the host would be an admin)
check("viewer cookie is checked before the localhost shortcut", src.index('u = session_user(conf, self.headers.get("Cookie"))') < src.index('if ip in ("127.0.0.1", "::1"): return True, "local", "admin"'))

# 2026-11-12: the first audit with Sportarr raised 36 "unknown language, no subtitles" rows for sports events and AUTO_WHISPER would have queued AI subtitles for each
check("sports are skipped by the language checks by default", "sportarr" in audit.SKIP_LANG_APPS and "sportarr" in audit.SKIP_RUNTIME_APPS, audit.SKIP_LANG_APPS)
src_lang = open(os.path.join(ROOT, "audit.py")).read()
check("the language block (NO_SUBS, UND_AUDIO_NO_SUBS, DUB_ONLY) sits behind SKIP_LANG_APPS", src_lang.index("if app not in SKIP_LANG_APPS and (a or mi):") < src_lang.index('F("UND_AUDIO_NO_SUBS"'))

# 2026-11-12: "Download a better copy" on part 3 of a Sportarr event failed with "no episode ids for this file": Sportarr links the event to part 1 only
ev = [{"id": 26, "seasonNumber": 2026, "episodeNumber": 32, "episodeFileId": 34, "hasFile": True}, {"id": 27, "seasonNumber": 2026, "episodeNumber": 33, "episodeFileId": 0, "hasFile": False}]
by_file = {34: [ev[0]]}
check("a Sportarr part file finds its event by season and number", [e["id"] for e in audit.eps_for_file({"id": 36, "seasonNumber": 2026, "episodeNumber": 32}, by_file, ev)] == [26])
check("the linked part still uses the link", [e["id"] for e in audit.eps_for_file({"id": 34, "seasonNumber": 2026, "episodeNumber": 32}, by_file, ev)] == [26])
check("a Sonarr file record (no episodeNumber) never guesses", audit.eps_for_file({"id": 99, "seasonNumber": 2026}, by_file, ev) == [])

# 2026-11-12: Sportarr reported episodeFileCount 0 for a series holding 29 files (145 GB); Auditarr skipped its file list and called all 29 "Unknown file" with Move to trash offered
check("a series with size on disk but a zero file count is still read", audit.series_has_files({"statistics": {"episodeFileCount": 0, "sizeOnDisk": 145170807220}}))
check("a truly empty series is still skipped", not audit.series_has_files({"statistics": {"episodeFileCount": 0, "sizeOnDisk": 0}}) and not audit.series_has_files({}))

# 2026-11-12: five MP4 files named .mkv failed "set default tracks" every night (mkvpropedit: not a Matroska file) and filled Today's error list
check("an MP4 inside a .mkv name is not sent to mkvpropedit", not audit.is_matroska({"probe": {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}}))
check("a real MKV, or a file not probed yet, still is", audit.is_matroska({"probe": {"format": {"format_name": "matroska,webm"}}}) and audit.is_matroska({}))
# 2026-11-12: 8 MP4-inside-.mkv episodes whose cached probe had streams but no format: is_matroska trusted the name, mkvpropedit failed,
# and the track-prefs pass retried every pass (88 "errors in 24 h" on the About tab from one season)
mp4 = os.path.join(TMP, "fake-mp4.mkv"); open(mp4, "wb").write(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
mkv = os.path.join(TMP, "real.mkv"); open(mkv, "wb").write(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
check("probe without a format: the file's first bytes decide (MP4 inside .mkv -> not Matroska)", not audit.is_matroska({"path": mp4, "probe": {"streams": []}}))
check("probe without a format: a real MKV header still counts as Matroska", audit.is_matroska({"path": mkv, "probe": {"streams": []}}))
try:
    conn = audit.db(); calls = []
    real_apply, real_setting, real_gate, real_plan = audit.apply_defaults, audit.setting, audit.gate, audit.plan_defaults
    audit.apply_defaults = lambda *a: (calls.append(a[0]), (False, "Error: not a Matroska file"))[1]
    audit.setting = lambda k: {"TRACK_PREFS": "on", "TRACK_PREFS_MAX": "50"}.get(k, real_setting(k)); audit.gate = lambda v, n: v
    audit.plan_defaults = lambda it: (1, 1, "audio default None->1 (eng)")
    item = {"path": mkv, "mtime": 1000.0, "missing": False, "intent": {"ignore": False}, "app": "sonarr", "fid": 1, "label": "Show S01E01", "probe": {"streams": [{"codec_type": "audio"}]}}
    audit.do_track_prefs(conn, [item]); audit.do_track_prefs(conn, [item]); audit.do_track_prefs(conn, [item])
    check("a failed default-flags edit is not retried on every pass (one try, then a day's wait)", len(calls) == 1, calls)
    conn.execute("UPDATE fixfail SET last=last-90000 WHERE kind='fixdefaults' AND path=?", (mkv,)); audit.do_track_prefs(conn, [item])
    check("after a day it is tried again", len(calls) == 2, calls)
    audit.do_track_prefs(conn, [dict(item, mtime=2000.0)])
    check("a changed file is tried again at once", len(calls) == 3, calls)
finally:
    audit.apply_defaults, audit.setting, audit.gate, audit.plan_defaults = real_apply, real_setting, real_gate, real_plan

# 2026-11-12: Today said "a self-test is failing / see the History tab" while every self-test passed; the failing part was the public demo canary, which neither the card nor the History tab named
src_t = open(os.path.join(ROOT, "audit.py")).read(); ui_t = open(os.path.join(ROOT, "ui.html")).read()
check("the self-test card names the public demo when that is what failed", "the nightly check of the public demo is failing" in src_t and "public {app_.capitalize()} demo" in src_t)
check("the History tab shows the public demo results", "Public demos (checked through their public addresses" in ui_t)

# 2026-11-12 (#30): Sportarr listed a series but none of its files; all 29 files of that folder became "unknown" with a bulk Move to trash
try:
    fold = {"/data/media/sports/Fight Series": ("sportarr", "Fight Series"), "/data/media/movies/Film (2020)": ("radarr", "Film")}
    sv, dt = audit.gap_detail("ORPHAN_FILE", "medium", "/data/media/sports/Fight Series/Season 2026/Night 1 Part 2.mkv", "2100 MB in sportarr root /data/media/sports - not managed by any arr", fold)
    check("unknown file inside a folder an arr lists -> low, marked as a reading gap", sv == "low" and audit.ARR_GAP in dt and "(/data/media/sports/Fight Series)" in dt, (sv, dt))
    sv2, dt2 = audit.gap_detail("ORPHAN_FILE", "medium", "/data/media/tv/Stray.mkv", "900 MB in sonarr root /data/media/tv - not managed by any arr", fold)
    check("a real stray outside any arr folder stays an ordinary unknown file", sv2 == "medium" and audit.ARR_GAP not in dt2, dt2)
    check("a duplicate is never re-labelled as a gap", audit.gap_detail("DUPLICATE_VIDEO", "medium", "/data/media/movies/Film (2020)/b.mkv", "x", fold) == ("medium", "x"))
    ps = audit.plain_sentence("ORPHAN_FILE", "low", "Night 1 Part 2.mkv", dt, "disk")
    check("gap sentence names the arr and says rescan first", ps.startswith("This file sits in a folder Sportarr lists") and "Rescan the folder" in ps, ps)
    c2 = audit.db(); now = time.time()
    c2.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)", ("gap1", "disk", 1, "ORPHAN_FILE", "low", "Night 1 Part 2.mkv", "/nonexistent/Fight Series/x.mkv", dt, now, now)); c2.commit()
    src = open(os.path.join(ROOT, "audit.py")).read()
    check("bulk endpoint refuses Move to trash for gap findings", 'body.get("action") == "quarantine" and is_gap(fr)' in src)
    check("is_gap reads the stored finding", audit.is_gap(audit.finding_row(c2, "gap1")))
except Exception as e: check("arr-gap case runs", False, repr(e))

# 2026-11-12: pressing Re-time / Make AI subtitles queued a job, but the row kept its buttons and looked untouched (owner: "I don't know if the button is doing its job")
try:
    P = "/data/media/anime/Show/Show - S00E30.mkv"
    fs_ = [dict(id="a", check="SUBS_OUT_OF_SYNC", path=P), dict(id="b", check="WRONG_LANGUAGE", path=P), dict(id="c", check="SUBS_OUT_OF_SYNC", path="/data/other.mkv"),
           dict(id="d", check="UND_AUDIO_NO_SUBS", path="/x.mkv"), dict(id="e", check="BAD_DURATION_HEADER", path="/y.mkv")]
    now_ = audit.time.time()
    audit.busy_marks(fs_, [("running", {"type": "whisper", "path": P}, now_ - 130), ("queued", {"type": "langdetect", "finding": "d", "path": "/x.mkv"}, now_)], {"e": ("fixing the time bar", now_)})
    B = {f["id"]: f.get("busy") for f in fs_}
    check("busy: a running AI-subtitles job marks the out-of-sync row of that file, with what and how long", B["a"] and B["a"]["what"] == "making AI subtitles" and B["a"]["state"] == "running" and B["a"]["mins"] == 2, B)
    check("busy: it leaves a different kind of problem on the same file alone", not B["b"], B)
    check("busy: and other files alone", not B["c"], B)
    check("busy: a job that names its finding marks that finding as waiting", B["d"] and B["d"]["state"] == "waiting", B)
    check("busy: a button press still running in the server marks its row", B["e"] and B["e"]["what"] == "fixing the time bar", B)
    seen = {}
    real = audit._ui_act
    audit._ui_act = lambda conn_, body, actor: (seen.setdefault("inflight", dict(audit.PRESSING)), (200, {"ok": True}))[1]
    try: audit.ui_act(None, {"id": "zz", "action": "subsync_fix"}, "test")
    finally: audit._ui_act = real
    check("busy: while a button press runs, the server lists it as in progress; afterwards it is gone", "zz" in seen.get("inflight", {}) and "zz" not in audit.PRESSING and seen["inflight"]["zz"][0] == "re-timing the subtitles", seen)
    audit.busy_marks([dict(id="q", check="NO_SUBS", path=P)], [("queued", {}, None), ("running", {"type": "nosuch"}, None)])
    check("busy: the deploy's request counter is still its own", audit.INFLIGHT == {"n": 0} or isinstance(audit.INFLIGHT.get("n"), int), audit.INFLIGHT)
    check("busy: odd jobs in the queue do not break the page state", True)
    ui = open(os.path.join(ROOT, "ui.html")).read()
    check("busy: the page lists rows being fixed apart and moves a pressed row there before the server answers", "drawBusy(" in ui and "LB[id]=" in ui and "list=all.filter(f=>!busyOf(f))" in ui)
except Exception as e: check("busy case runs", False, repr(e))

# 2026-11-12: AI subtitles landed for an episode whose built-in subtitles drift; the row kept the old verdict because nothing checked the timing again
try:
    c3 = audit.db(); P3 = "/data/media/anime/Show/Show - S00E30.mkv"
    c3.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES('sync1','sonarr','77','SUBS_OUT_OF_SYNC','medium','Show S00E30',?,'drift',0,0,NULL,0,1,0)", (P3,)); c3.commit()
    q3 = []; real_enq = audit.enqueue; audit.enqueue = q3.append
    try: n3 = audit.recheck_timing(c3, P3, "re-check after AI subtitles"); n0 = audit.recheck_timing(c3, "/data/other.mkv", "x")
    finally: audit.enqueue = real_enq
    check("AI subtitles landing on an out-of-sync file queue a timing check of it", n3 == 1 and q3 and q3[0]["type"] == "subsync" and str(q3[0]["fid"]) == "77" and q3[0]["path"] == P3, q3)
    check("files with no open timing finding are left alone", n0 == 0 and len(q3) == 1)
    check("the Whisper worker calls it when a subtitle lands", 'recheck_timing(conn, job["path"], "re-check after AI subtitles")' in open(os.path.join(ROOT, "audit.py")).read())
    c3.close()
except Exception as e: check("AI-subtitles re-check case runs", False, repr(e))

# #41 (2026-11-12): 'Ask Plex to rescan' and 'Ask the arr to rescan' ended with "clears on the next audit" - up to a day with no news. A follow-up now looks again.
try:
    c4 = audit.db(); P4 = "/data/media/tv/Show/Season 01/Show - S01E04.mkv"
    def add4(id_, code, app="sonarr"):
        c4.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES(?,?,?,?,'medium','Show S01E04',?,'x',0,0,NULL,0,1,0)", (id_, app, 44, code, P4)); c4.commit()
    add4("pm1", "PLEX_MISSING"); looks = []
    real_inv, real_lists = audit.plex_inventory, audit.arr_lists_path
    audit.plex_inventory = lambda: ({os.path.normpath(P4): 1} if len(looks) >= 2 else {}) if not looks.append(1) else {}
    r4 = audit._followup_job(dict(type="followup", what="plex", finding="pm1", app="sonarr", fid=44, path=P4, label="Show S01E04", after="the Plex rescan", waits=(0, 0, 0)), sleep=lambda s_: None)
    row4 = c4.execute("SELECT resolved_at FROM finding WHERE id='pm1'").fetchone()
    check("follow-up: once Plex lists the file, the row closes and Actions says it was verified", r4 == "verified" and row4[0] is not None and len(looks) == 2, (r4, row4, looks))
    check("follow-up: the proof is in the action log", any(k == "followup" and (res or "").startswith("verified: Plex lists the file now") for k, res in c4.execute("SELECT kind, result FROM action")))
    add4("pm2", "PLEX_MISSING"); audit.plex_inventory = lambda: {}
    r5 = audit._followup_job(dict(type="followup", what="plex", finding="pm2", app="sonarr", fid=44, path=P4, label="Show S01E04", waits=(0, 0)), sleep=lambda s_: None)
    check("follow-up: if Plex never lists it, the row stays and Actions says what is still missing", r5 == "not yet" and c4.execute("SELECT resolved_at FROM finding WHERE id='pm2'").fetchone()[0] is None
          and any(k == "followup" and "not yet: Plex still does not list it" in (res or "") for k, res in c4.execute("SELECT kind, result FROM action")), r5)
    audit.resolve_finding(c4, "pm2"); r6 = audit._followup_job(dict(type="followup", what="plex", finding="pm2", path=P4, waits=(0,)), sleep=lambda s_: None)
    check("follow-up: a row closed meanwhile (the audit, or a person) stops the looks", r6 == "already closed", r6)
    add4("or1", "ORPHAN_FILE", "disk"); audit.arr_lists_path = lambda apps, p_: "sonarr" if p_ == P4 else None; real_apps = audit.arr_apps; audit.arr_apps = lambda: {}
    r7 = audit._followup_job(dict(type="followup", what="arr", finding="or1", app="disk", fid=44, path=P4, waits=(0,)), sleep=lambda s_: None)
    check("follow-up: once the arr lists a rescanned file, its row closes", r7 == "verified" and c4.execute("SELECT resolved_at FROM finding WHERE id='or1'").fetchone()[0] is not None, r7)
    audit.plex_inventory, audit.arr_lists_path, audit.arr_apps = real_inv, real_lists, real_apps
    q4 = []; real_enq, real_px = audit.enqueue, audit.plex_refresh; audit.enqueue = q4.append; audit.plex_refresh = lambda p_: "plex refresh queued"
    try: add4("pm3", "PLEX_MISSING"); code4, obj4 = audit._ui_act(c4, {"id": "pm3", "action": "plexrefresh"}, "test")
    finally: audit.enqueue, audit.plex_refresh = real_enq, real_px
    check("Ask Plex to rescan on a 'not in Plex' row queues the follow-up and says so", code4 == 200 and q4 and q4[0]["type"] == "followup" and q4[0]["what"] == "plex" and q4[0]["finding"] == "pm3" and "looks again" in obj4["note"], (code4, obj4, q4))
    check("the follow-up waits on its own lane, so its pauses hold no subtitle job up", audit._lane({"type": "followup"}) is audit.FOLLOW_Q and audit.FOLLOW_Q is not audit.VERIFY_Q)
    check("while it waits the row shows under Being fixed now", audit.busy_marks([dict(id="pm3", check="PLEX_MISSING", path=P4)], [("queued", q4[0], None)])[0].get("busy", {}).get("what") == "checking that the fix worked")
    c4.close()
except Exception as e: check("follow-up case runs", False, repr(e))

# 2026-11-13: every audit closed every 'Subtitles out of sync' row (One Piece S00E30 closed at the second an audit started, 3.8 s late and unfixed),
# because the audit never makes such a row itself and its clean-up took "not seen this run" for "fixed"
try:
    import tempfile as _tf
    c5 = audit.db(); d5 = _tf.mkdtemp(); P5 = os.path.join(d5, "Show - S00E30.mkv"); open(P5, "wb").write(b"x")
    check("a timing row on the same, still-present file is kept by the audit's clean-up", audit.kept_for_its_job("SUBS_OUT_OF_SYNC", P5, P5))
    check("a timing row whose file was replaced (another path) is not kept", not audit.kept_for_its_job("SUBS_OUT_OF_SYNC", P5, P5 + ".new.mkv"))
    check("a timing row whose file is gone is not kept", not audit.kept_for_its_job("SUBS_OUT_OF_SYNC", P5 + ".gone", P5 + ".gone"))
    check("every other kind of row is cleaned up as before", not audit.kept_for_its_job("NO_SUBS", P5, P5) and not audit.kept_for_its_job("WRONG_LANGUAGE", P5, P5))
    src5 = open(os.path.join(ROOT, "audit.py")).read()
    check("the end-of-audit clean-up and the import re-check both ask who may close the row", 'not sweep_may_close("audit", code, fpath, cur_path.get((app, str(afid))))' in src5 and 'sweep_may_close("import", old_code, old_path, it.get("path"))' in src5 and "return not kept_for_its_job(code, f_path, cur_path)" in src5)
    t_run = 1_900_000_000.25
    c5.execute("INSERT INTO run(started, finished, files) VALUES(?, ?, 1)", (t_run, t_run + 100))
    c5.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES('sw1','sonarr',30,'SUBS_OUT_OF_SYNC','medium','Show S00E30',?,'3.8s late',0,0,?,0,1,0)", (P5, t_run))
    c5.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES('sw2','sonarr',31,'SUBS_OUT_OF_SYNC','medium','Show S00E31',?,'fixed',0,0,?,0,1,0)", (P5, t_run + 50)); c5.commit()
    q5 = []; real_enq = audit.enqueue; audit.enqueue = q5.append
    try: n5 = audit.recheck_swept_timing(c5, now=t_run + 3600); n6 = audit.recheck_swept_timing(c5, now=t_run + 3600)
    finally: audit.enqueue = real_enq
    check("one-time repair: a timing row closed at the second an audit started gets a fresh timing check", n5 == 1 and q5 and q5[0]["type"] == "subsync" and str(q5[0]["fid"]) == "30", (n5, q5))
    check("a row closed some other way (a fix, its own check) is left alone", all(str(j["fid"]) != "31" for j in q5))
    check("the repair runs only once", n6 == 0 and len(q5) == 1)
    c5.close()
except Exception as e: check("audit clean-up case runs", False, repr(e))

# #40 (lesson of #35): one back-off for every nightly fix - 1 day, 3 days, 7 days, then give up until the file changes, and Today says so
try:
    import tempfile as _tf
    c6 = audit.db(); d6 = _tf.mkdtemp(); P6 = os.path.join(d6, "Show - S01E01.mkv"); open(P6, "wb").write(b"x"); T = 2_000_000_000.0
    check("back-off: a file that never failed goes ahead", audit.fix_wait(c6, "fixheader", P6, now=T) is None)
    audit.fix_failed(c6, "fixheader", P6, "mkvmerge: broken", now=T)
    check("back-off: after the 1st failure it waits a day", audit.fix_wait(c6, "fixheader", P6, now=T + 3600) and audit.fix_wait(c6, "fixheader", P6, now=T + 86400 + 1) is None)
    audit.fix_failed(c6, "fixheader", P6, "mkvmerge: broken", now=T + 86400 + 1)
    check("back-off: after the 2nd it waits 3 days", audit.fix_wait(c6, "fixheader", P6, now=T + 86400 + 2 * 86400) and audit.fix_wait(c6, "fixheader", P6, now=T + 86400 + 3 * 86400 + 2) is None)
    audit.fix_failed(c6, "fixheader", P6, "mkvmerge: broken", now=T + 5 * 86400)
    check("back-off: after the 3rd it gives up, even a month later", (audit.fix_wait(c6, "fixheader", P6, now=T + 40 * 86400) or "").startswith("gave up after 3"))
    check("back-off: Today lists it once given up, with the error", [(k, p_) for k, p_, t, e in audit.gave_up(c6)] == [("fixheader", P6)])
    os.utime(P6, (T + 50 * 86400, T + 50 * 86400))
    check("back-off: a changed file starts over at once", audit.fix_wait(c6, "fixheader", P6, now=T + 50 * 86400) is None and not audit.gave_up(c6))
    audit.fix_failed(c6, "subtag", P6, "x", now=T); audit.fix_ok(c6, "subtag", P6)
    check("back-off: a success clears the record", audit.fix_wait(c6, "subtag", P6, now=T + 1) is None)
    check("back-off: one kind of fix waiting does not hold up another on the same file", (audit.fix_failed(c6, "whisper", P6, "x", now=T), audit.fix_wait(c6, "fixdefaults", P6, now=T + 1))[1] is None)
    for _ in range(3): audit.fix_failed(c6, "dedupe", P6 + ".gone", "x", mtime=1.0, now=T)
    check("back-off: a given-up file that is gone is dropped from Today", all(p_ != P6 + ".gone" for _, p_, _, _ in audit.gave_up(c6)))
    src6 = open(os.path.join(ROOT, "audit.py")).read()
    check("back-off: every nightly fix uses it (time bar, subtitle labels, default tracks, AI subtitles, duplicates)",
          all(f'fix_wait(conn, "{k}"' in src6 and f'fix_failed(conn, "{k}"' in src6 for k in ("fixheader", "subtag", "fixdefaults", "whisper", "dedupe")))
    check("back-off: the nightly time-bar repair skips waiting files instead of taking the 10 oldest every night", 'if not fix_wait(conn, "fixheader", r_[1] or "")][:10]' in src6)
    for _ in range(3): audit.fix_failed(c6, "fixheader", P6, "mkvmerge: broken", now=T + 60 * 86400)
    nd = [n for n in audit.today_summary(c6).get("needs", []) if n.get("check") == "GAVEUP"]
    check("back-off: Today says how many files the night gave up on, names one, and says it retries once the file changes", nd and nd[0]["title"].startswith("1 file(s): the night gave up after 3") and "Show - S01E01.mkv" in nd[0]["why"] and "once the file changes" in nd[0]["why"], nd)
    check("back-off: the Today notice is a Today-only kind, never a finding row", "GAVEUP" in audit.TODAY_ONLY and "GAVEUP" not in audit.ROW_MAKER)
    c6.close()
except Exception as e: check("back-off case runs", False, repr(e))

# 2026-11-13: the person re-timed the same five episodes five times each: Bazarr re-wrote the file, the numbers stayed the same, the button stayed
try:
    import tempfile as _tf
    c7 = audit.db(); T7 = time.time()
    check("re-timed lately: no Bazarr re-time yet -> not tried", not audit.bazarr_tried(c7, "sonarr", 701, T7))
    c7.execute("INSERT INTO action(ts, mode, kind, app, target, label, detail, result) VALUES(?,?,?,?,?,?,?,?)", (T7 - 3600, "on", "subsync-fix", "sonarr", "701", "Show S03E02", "Bazarr sync of x.en.srt by ui", "Bazarr re-timed it against the audio; the timing is checked again now"))
    c7.execute("INSERT INTO action(ts, mode, kind, app, target, label, detail, result) VALUES(?,?,?,?,?,?,?,?)", (T7 - 40 * 86400, "on", "subsync-fix", "sonarr", "702", "Show S03E03", "Bazarr sync of y.en.srt by ui", "asked Bazarr to re-time it"))
    check("re-timed lately: a Bazarr re-time an hour ago counts (number or text id)", audit.bazarr_tried(c7, "sonarr", 701, T7) and audit.bazarr_tried(c7, "sonarr", "701", T7))
    check("re-timed lately: one 40 days ago does not", not audit.bazarr_tried(c7, "sonarr", 702, T7))
    f7 = {"id": "rt1", "app": "sonarr", "fid": 701, "label": "Show S03E02", "path": "/x.mkv"}
    c7.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES('rt1','sonarr',701,'SUBS_OUT_OF_SYNC','medium','Show S03E02','/x.mkv',?,0,0,NULL,0,1,0)",
               ("external 'x.en.srt': subtitles drift: +0.4s at 25% -> +3.5s at 73% (3/3 samples) | fix: re-time in Bazarr | lang=en | src=sidecar:/x.en.srt | bazarr=tried",))
    code7, obj7 = audit.subsync_fix(c7, f7, {}, "test", {})
    check("a second Bazarr re-time is refused with the reason and what to do instead", code7 == 409 and "Find subtitles" in obj7.get("error", "") and "Make AI subtitles" in obj7.get("error", ""), (code7, obj7))
    pl7 = audit.plain_sentence("SUBS_OUT_OF_SYNC", "medium", "Show S03E02", c7.execute("SELECT detail FROM finding WHERE id='rt1'").fetchone()[0], "sonarr")
    check("…and the row says so in plain words", "already re-timed" in pl7, pl7)
    src7 = open(os.path.join(ROOT, "audit.py")).read()
    check("the timing check tags a file Bazarr already re-timed", 'bazarr_tried(conn, job["app"], job["fid"], now)' in src7 and '" | bazarr=tried" if tried' in src7)
    check("Bazarr's re-time is a queued job, so a group press never holds the page for minutes", 'enqueue(dict(type="bazarrsync"' in src7 and '"bazarrsync": _bazarrsync_job' in src7 and '"bazarrsync": ("Bazarr is re-timing the subtitles"' in src7)
    check("a drift the second look cannot confirm closes the row", 'resolve_finding(conn, fidh, "drift not confirmed by a second look")' in src7)
    check("AI subtitles for a whole show know each file, so its rows close and its timing is re-checked", 'path=ep_path.get(e["sonarrEpisodeId"])' in src7 and "path=None, actor=actor))" not in src7)
    ui7 = open(os.path.join(ROOT, "ui.html")).read()
    check("a show offers Make AI subtitles, Re-time and Check timing again for all its rows at once", all(f'data-bulk="{a}"' in ui7 for a in ("whisper", "subsync_fix", "subsync")))
    check("rows Bazarr already re-timed offer AI subtitles instead of Re-time", "src=embedded|bazarr=tried" in ui7 and "noRetime(f)?B('subsearch')+B('whisper','primary')" in ui7)
    q7 = []; real7 = audit.enqueue; audit.enqueue = q7.append
    try:
        d7 = _tf.mkdtemp(); P7 = os.path.join(d7, "a.mkv"); open(P7, "wb").write(b"x")
        c7.execute("UPDATE finding SET path=? WHERE id='rt1'", (P7,)); c7.commit()
        n7 = audit.recheck_open_timing(c7); n8 = audit.recheck_open_timing(c7)
    finally: audit.enqueue = real7
    check("one time: every open timing row gets a fresh check with the second look", n7 >= 1 and n8 == 0 and all(j["type"] == "subsync" for j in q7), (n7, n8, q7))
    c7.close()
except Exception as e: check("re-time case runs", False, repr(e))

conn.close(); failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)
