"""F16 stuck downloads (#20): queue entries that sit for days, finished downloads the app cannot import, failed downloads and indexers the app
stopped using become rows; 'Take off the queue' tells the app to forget the download and NEVER deletes it from the download client or
blocklists it (the owner's choice (a), 2026-11-18). Against the fake stack (tests/mock_stack.py), in-process.
Run: python tests/stuck_downloads.py   Exit 1 on failure."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
T = tempfile.mkdtemp(prefix="auditarr-stuck-"); os.environ.update(CONFIG_DIR=T, REPORT_DIR=T, ARR_CONFIG_DIR=os.path.join(T, "none"), HOST_IP="", WHISPARR_APIKEY="", SPORTARR_APIKEY="", BAZARR_APIKEY="", PLEX_TOKEN="")
import mock_stack  # noqa: E402
W = mock_stack.stuck_world(mock_stack.default_world(os.path.join(T, "tv"), os.path.join(T, "movies")))
srv = mock_stack.start(W); M = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update(SONARR_URL=f"{M}/sonarr", SONARR_APIKEY="test", RADARR_URL=f"{M}/radarr", RADARR_APIKEY="test")
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:240]}]"))
conn = audit.db(); audit.load_settings(conn); now = time.time()
iso = mock_stack.iso; rec = mock_stack.queue_record

# ---- what counts as stuck (pure)
K = audit.queue_kind
check("a download still downloading is a download", K(dict(status="downloading", trackedDownloadState="downloading")) == "download")
check("a finished download waiting its normal turn is not stuck", K(dict(status="completed", trackedDownloadState="importPending", trackedDownloadStatus="ok")) is None)
check("a finished download the app cannot import is an import problem", K(dict(status="completed", trackedDownloadState="importBlocked", trackedDownloadStatus="warning")) == "import"
      and K(dict(status="completed", trackedDownloadState="importPending", trackedDownloadStatus="warning")) == "import")
check("a failed download is failed", K(dict(status="failed", trackedDownloadState="downloading")) == "failed" and K(dict(status="completed", trackedDownloadState="failedPending")) == "failed")
check("delayed, importing, imported and client-unavailable entries are never stuck", all(K(x) is None for x in (dict(status="delay"), dict(status="downloadClientUnavailable"), dict(status="completed", trackedDownloadState="importing"), dict(status="completed", trackedDownloadState="imported"))))

titles = {1: ("Alpha Show", "/data/tv/Alpha Show (2020)")}
recs = [rec("sonarr", id=1, seriesId=1, episodeId=104, downloadId="OLD", added=iso(5), episode={"seasonNumber": 1, "episodeNumber": 4}),   # 5 days, still downloading
        rec("sonarr", id=2, seriesId=1, episodeId=105, downloadId="NEW", added=iso(1)),                                                     # 1 day: not yet
        rec("sonarr", id=3, seriesId=1, episodeId=201, downloadId="PACK", added=iso(2), status="completed", trackedDownloadState="importPending", trackedDownloadStatus="warning", sizeleft=0,
            statusMessages=[{"title": "x", "messages": ["Episode has a TBA title and recently aired"]}], episode={"seasonNumber": 2, "episodeNumber": 1}),
        rec("sonarr", id=4, seriesId=1, episodeId=202, downloadId="PACK", added=iso(2), status="completed", trackedDownloadState="importPending", trackedDownloadStatus="warning", sizeleft=0, episode={"seasonNumber": 2, "episodeNumber": 2}),
        rec("sonarr", id=5, seriesId=1, episodeId=106, downloadId="FRESHIMP", added=iso(0.2), status="completed", trackedDownloadState="importBlocked", trackedDownloadStatus="warning"),   # 5 h: give it a day
        rec("sonarr", id=6, seriesId=1, episodeId=107, downloadId="DEAD", added=iso(4), status="warning", trackedDownloadStatus="warning", statusMessages=[{"title": "x", "messages": ["The download is stalled with no connections"]}])]
rows = {r[5].rsplit("download=", 1)[1]: r for r in audit.stuck_downloads("sonarr", recs, now, titles, {})}
check("a download not finished after 3 days is a row, one a day old is not", "OLD" in rows and "NEW" not in rows, sorted(rows))
check("…named after the show and episode, kept as Download stuck (medium)", rows.get("OLD", [0, "", "", ""])[1:4] == ("DOWNLOAD_STUCK", "medium", "Alpha Show S01E04"), rows.get("OLD"))
check("a season pack the app cannot import is ONE row naming both episodes", rows.get("PACK", [0, "", "", ""])[1:4] == ("IMPORT_STUCK", "medium", "Alpha Show S02E01/S02E02"), rows.get("PACK"))
check("an import problem only 5 hours old waits for the 24-hour mark", "FRESHIMP" not in rows)
check("a stalled download is high and says stalled", rows.get("DEAD", [0, "", ""])[2] == "high" and "stalled" in rows["DEAD"][5].split(" | ")[0], rows.get("DEAD"))
check("the app's own reason is kept in the details", "TBA title" in rows["PACK"][5], rows["PACK"][5][:200])
check("row ids are negative and stable, and never meet an episode or file id", all(r[0] < -audit.QUEUE_FID for r in rows.values()) and audit.stuck_downloads("sonarr", recs, now + 60, titles, {})[0][0] == rows["OLD"][0])
seen = {}; legacy = [dict(recs[0], added=None, downloadId="LEGACY")]
check("an app that sends no grab time is timed from the first audit that saw it", not audit.stuck_downloads("sonarr", legacy, now, titles, seen) and seen.get("sonarr|LEGACY") == now
      and audit.stuck_downloads("sonarr", legacy, now + 4 * 86400, titles, seen))
audit.ui_settings_post(conn, {"set": {"STUCK_DOWNLOAD_DAYS": "0", "STUCK_IMPORT_HOURS": "0"}}, "test")
check("0 turns each rule off", not audit.stuck_downloads("sonarr", recs, now, titles, {}))
audit.ui_settings_post(conn, {"set": {"STUCK_DOWNLOAD_DAYS": "7"}}, "test")
check("the day limit is a setting (7 days: the 5-day download is not stuck yet)", not audit.stuck_downloads("sonarr", recs, now, titles, {}))
audit.ui_settings_post(conn, {"reset": ["STUCK_DOWNLOAD_DAYS", "STUCK_IMPORT_HOURS"]}, "test")
ix = audit.indexer_rows("sonarr", [{"source": "IndexerLongTermStatusCheck", "type": "warning", "message": "Indexers unavailable due to failures for more than 6 hours: A, B"},
                                   {"source": "IndexerSearchCheck", "type": "warning", "message": "No indexers available with Automatic Search enabled"}, {"source": "UpdateCheck", "type": "warning", "message": "update"}])
check("indexer warnings become rows; other health warnings do not", [r[1:3] for r in ix] == [("INDEXER_DOWN", "medium"), ("INDEXER_DOWN", "high")], ix)
for code, det in (("DOWNLOAD_STUCK", rows["OLD"][5]), ("DOWNLOAD_STUCK", rows["DEAD"][5]), ("IMPORT_STUCK", rows["PACK"][5]), ("INDEXER_DOWN", ix[0][5]), ("INDEXER_DOWN", ix[1][5])):
    txt = audit.plain_sentence(code, "medium", "x", det, "sonarr")
    check(f"plain sentence for {code} says the download is kept and names no code", ("stays in your download client" in txt or code == "INDEXER_DOWN") and "_" not in txt and txt.endswith("."), txt)

# ---- a whole collect -> rows pass against the fake stack
apps = audit.arr_apps(); audit.collect(apps); got = []
asked = audit.stuck_pass(conn, apps, lambda *a: got.append(a))
check("the fake Radarr's blocked import becomes a 'Download not imported' row named after the movie", any(a[0] == "radarr" and a[2] == "IMPORT_STUCK" and a[4] == "Gamma Movie (2022)" for a in got), got)
check("the fake Sonarr's failing indexer becomes a row", any(a[0] == "sonarr" and a[2] == "INDEXER_DOWN" for a in got), got)
check("both apps answered both questions", {("sonarr", "queue"), ("radarr", "queue"), ("sonarr", "health"), ("radarr", "health")} <= asked, asked)
lk = dict(((a, f), ar) for a, f, p, ar in conn.execute("SELECT app, fid, plex, arr FROM links"))
check("each row links to the app's queue or indexer page", all((lk.get((a[0], a[1])) or "").endswith(("/activity/queue", "/settings/indexers")) for a in got), lk)

# ---- Take off the queue
row = next(a for a in got if a[0] == "radarr")
def plant(app, fid, code, label, detail, fid_h="stuck1"):
    conn.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES(?,?,?,?,'high',?,NULL,?,?,?,NULL,0,1,0)",
                 (fid_h, app, fid, code, label, detail, now, now)); conn.commit()
plant("radarr", row[1], row[2], row[4], row[6])
audit.READ_ONLY = True; srv.calls.clear()
try: code, obj = audit.ui_act(conn, {"id": "stuck1", "action": "unqueue"}, "test")
except audit.ReadOnly: code, obj = "refused", {}
check("read-only mode refuses it and sends nothing (a staged update cannot press it)", code == "refused" and not srv.calls and W["queue"]["radarr"], (code, obj, srv.calls))
audit.READ_ONLY = False
srv.calls.clear(); code, obj = audit.ui_act(conn, {"id": "stuck1", "action": "unqueue"}, "test")
dels = [c for c in srv.calls if c["method"] == "DELETE"]
check("Take off the queue sends one DELETE for that queue entry", code == 200 and len(dels) == 1 and dels[0]["app"] == "radarr" and dels[0]["path"] == "/api/v3/queue/5001", (code, obj, dels))
check("…with removeFromClient=false and blocklist=false: the download stays in the client", dels and dels[0]["query"].get("removeFromClient") == ["false"] and dels[0]["query"].get("blocklist") == ["false"], dels and dels[0]["query"])
check("no other write went out (no search, no blocklist, no client call)", len(srv.calls) == 1, srv.calls)
check("the app no longer lists it and the row is closed", not W["queue"]["radarr"] and conn.execute("SELECT resolved_at FROM finding WHERE id='stuck1'").fetchone()[0] is not None)
check("the answer says nothing was deleted and the download is still in the client", "Nothing was deleted" in obj.get("note", "") and "Deluge" in obj.get("note", ""), obj)
a_ = conn.execute("SELECT kind, result FROM action WHERE kind='unqueue' ORDER BY ts DESC LIMIT 1").fetchone()
check("Actions records it with the download left untouched", a_ and "left untouched in Deluge" in a_[1], a_)
check("Today counts it under what was done", audit.today_summary(conn)["did"].get("stuck downloads taken off the queue") == 1, audit.today_summary(conn)["did"])
plant("radarr", row[1], row[2], row[4], row[6], "stuck2"); srv.calls.clear()
code, obj = audit.ui_act(conn, {"id": "stuck2", "action": "unqueue"}, "test")
check("pressed again after it left the queue: nothing is sent, the row just closes", code == 200 and obj.get("already_gone") and not [c for c in srv.calls if c["method"] == "DELETE"], (code, obj))
W["queue"]["sonarr"] = [rec("sonarr", id=11, seriesId=1, episodeId=201, downloadId="PACK2", added=iso(5)), rec("sonarr", id=12, seriesId=1, episodeId=202, downloadId="PACK2", added=iso(5))]
plant("sonarr", -1, "DOWNLOAD_STUCK", "Alpha Show S02", "queue: not finished | queue=11 | download=PACK2", "stuck3"); srv.calls.clear()
code, obj = audit.ui_act(conn, {"id": "stuck3", "action": "unqueue"}, "test")
check("a season pack leaves the queue with one press (one DELETE drops every episode)", code == 200 and not W["queue"]["sonarr"] and len([c for c in srv.calls if c["method"] == "DELETE"]) == 1, (code, obj, srv.calls))
plant("sonarr", -5, "MISSING_EPISODE", "Alpha Show S01E04", "released 2026-10-12", "miss1")
code, obj = audit.ui_act(conn, {"id": "miss1", "action": "unqueue"}, "test")
check("the button is refused on any other kind of row", code == 400, (code, obj))
plant("sonarr", -1, "DOWNLOAD_STUCK", "x", "queue: not finished", "stuck4")
code, obj = audit.ui_act(conn, {"id": "stuck4", "action": "unqueue"}, "test")
check("a row that does not name its download is refused, nothing sent", code == 400, (code, obj))
src = open(os.path.join(ROOT, "audit.py")).read()
check("nothing in Auditarr ever asks an app to delete from the download client or to blocklist", "removeFromClient=true" not in src and "blocklist=true" not in src)
srv.shutdown()
bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
