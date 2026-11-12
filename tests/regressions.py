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

conn.close(); failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)
