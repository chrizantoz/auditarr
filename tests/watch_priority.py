"""F31 watch priority: what someone is watching in Plex (or has next up) gets its missing subtitles first.
Bazarr search right away, AI subtitles if Bazarr still has nothing WATCH_PRIORITY_MINUTES later, both queued ahead of every other job.
Run: python tests/watch_priority.py   Exit 1 on failure."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-watch-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]; os.environ["PLEX_PATH_MAP"] = "/data=/plexdata"
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))

# ---- parsing Plex's answers
sess = [dict(type="episode", grandparentTitle="Alpha Show", grandparentRatingKey=77, parentIndex=1, index=1, User={"title": "nyx"}, Media=[{"Part": [{"file": "/plexdata/tv/Alpha Show (2020)/Season 01/E01.mkv"}]}]),
        dict(type="movie", title="Beta Movie", ratingKey=9, Media=[{"Part": [{"file": "/plexdata/movies/Beta Movie (2021)/Beta.mkv"}]}]),
        dict(type="track", title="song")]
items = audit.plex_watch_items(sess, "watching")
check("parses episodes and movies, ignores music", [i["kind"] for i in items] == ["episode", "movie"])
check("maps Plex paths back to our paths", "/data/tv/Alpha Show (2020)/Season 01/E01.mkv" in items[0]["paths"] and items[0]["who"] == "nyx")
check("title matching ignores punctuation and year", audit._norm_title("Mushoku Tensei: Jobless Reincarnation (2021)") == audit._norm_title("mushoku tensei jobless reincarnation"))

# ---- a library with open no-subtitle findings
conn = audit.db(); now = time.time()
def seed(fid, app, afid, label, path, detail="audio=['jpn'] embedded_subs=[] external=[] | Bazarr still searching", code="NO_SUBS"):
    conn.execute("INSERT INTO finding(id, app, fid, check_code, severity, label, path, detail, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)", (fid, app, afid, code, "medium", label, path, detail, now - 3600, now))
seed("f2", "sonarr", 12, "Alpha Show S01E02", "/data/tv/Alpha Show (2020)/Season 01/E02.mkv")
seed("f3", "sonarr", 13, "Alpha Show S01E03", "/data/tv/Alpha Show (2020)/Season 01/E03.mkv")
seed("f9", "sonarr", 19, "Alpha Show S01E09", "/data/tv/Alpha Show (2020)/Season 01/E09.mkv")              # too far ahead
seed("fn", "sonarr", 21, "Alpha Show S02E01", "/data/tv/Alpha Show (2020)/Season 02/E01.mkv")              # start of next season: counts
seed("fo", "sonarr", 31, "Other Show S01E01", "/data/tv/Other Show (2019)/Season 01/E01.mkv")              # nobody watching
seed("fm", "radarr", 1, "Beta Movie (2021)", "/data/movies/Beta Movie (2021)/Beta.mkv")
seed("fw", "sonarr", 14, "Alpha Show S01E04", "/data/tv/Alpha Show (2020)/Season 01/E04.mkv", detail="audio=['jpn'] | not in Bazarr wanted list")   # language profile says no
conn.commit()
targets = audit.plex_watch_items(sess[:1], "watching") + audit.plex_watch_items(sess[1:2], "next up")
rows = conn.execute("SELECT id, app, fid, label, path, detail FROM finding WHERE resolved_at IS NULL").fetchall()
hits = audit.watch_matches(targets, rows)
check("hits: the next episodes of the show being watched, the start of its next season, the movie on deck", set(hits) == {("sonarr", 12), ("sonarr", 13), ("sonarr", 21), ("radarr", 1), ("sonarr", 14)}, sorted(hits))
check("episode far ahead and other shows are left alone", ("sonarr", 19) not in hits and ("sonarr", 31) not in hits)
check("watching beats next up", hits[("sonarr", 12)][0] == "watching" and hits[("radarr", 1)][0] == "next up")

# ---- the pass itself, with the arr/Bazarr layer faked
audit.arr_apps = lambda: {"sonarr": {"url": "x", "key": "k"}, "radarr": {"url": "x", "key": "k"}}
def fake_info(apps, app, afid, path): return dict(kind="series", parent_id=1, ep_ids=[100 + afid], path=path) if app == "sonarr" else dict(kind="movie", parent_id=afid, ep_ids=[], path=path)
audit.arr_file_info = fake_info; audit.bazarr_conf = lambda: ("http://bazarr", "key"); audit.bazarr_wanted = lambda: ({112, 113, 121}, {1})
audit.enqueue(dict(type="subsearch", fid="zz", app="sonarr", kind="series", parent_id=5, ep_ids=[5], path="/data/tv/x.mkv", label="Backlog S01E01", actor="test"))   # already waiting, normal priority
s1, w1 = audit.watch_priority_pass(conn, targets=targets, now=now)
check("first pass: a Bazarr search for every hit (not the one Bazarr does not want), no AI yet", (s1, w1) == (4, 0), (s1, w1))
first = audit.SUB_Q.queue[0]; check("watch-priority searches jump ahead of the waiting backlog job", first[0] == 1 and first[2]["actor"].startswith("watch priority"), first[:2])
up = audit.upcoming_map(conn); check("rows are marked for the page: watching vs next up", up.get(("sonarr", 12)) == "watching" and up.get(("radarr", 1)) == "next up", up)
acts = conn.execute("SELECT count(*) FROM action WHERE kind='watch-priority'").fetchone()[0]; check("each push is recorded as an action", acts == 4, acts)
s2, w2 = audit.watch_priority_pass(conn, targets=targets, now=now + 60)
check("a minute later: nothing new (Bazarr still has its time)", (s2, w2) == (0, 0), (s2, w2))
s3, w3 = audit.watch_priority_pass(conn, targets=targets, now=now + 21 * 60)
check("after WATCH_PRIORITY_MINUTES with Bazarr still wanting them: AI subtitles queued", (s3, w3) == (0, 4), (s3, w3))
wq = [it for it in audit.WHISPER_Q.queue]; check("AI jobs carry top priority and name the reason", wq and all(it[0] == 1 and "watch priority" in it[2]["actor"] for it in wq), [it[:2] for it in wq])
check("the item being watched is first in the AI queue", sorted(wq)[0][2]["label"] == "Alpha Show S01E02", sorted(wq)[0][2]["label"])
s4, w4 = audit.watch_priority_pass(conn, targets=targets, now=now + 30 * 60)
check("nothing is pushed twice", (s4, w4) == (0, 0), (s4, w4))
check("rows now carry the working flag (AI subtitles queued)", conn.execute("SELECT count(*) FROM finding WHERE detail LIKE '[working:%'").fetchone()[0] == 4)
# ---- off switch and dry run
def ui_set(**kv): conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)", (__import__("json").dumps(kv),)); conn.commit()   # what Settings → Save does
ui_set(WATCH_PRIORITY="off"); check("WATCH_PRIORITY=off does nothing", audit.watch_priority_pass(conn, targets=targets, now=now + 90000) == (0, 0))
ui_set(WATCH_PRIORITY="dryrun"); conn.execute("DELETE FROM kv WHERE k='watch_priority'"); conn.commit()
sd, wd = audit.watch_priority_pass(conn, targets=targets, now=now + 90000)
check("dry run records what it would do without queueing", sd == 4 and conn.execute("SELECT count(*) FROM action WHERE kind='watch-priority' AND mode='dryrun'").fetchone()[0] == 4, (sd, wd))
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
