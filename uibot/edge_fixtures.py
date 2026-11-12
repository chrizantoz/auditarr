"""Edge-case worlds for the UI bots, derived from the base fixtures: empty library, one show with 2000 findings, every service down,
and API error states. Writes uibot/fixtures-edge/<name>/{state,history,settings,trash}.json plus edge.json naming the case.
Usage: python uibot/edge_fixtures.py"""
import copy, json, os
HERE = os.path.dirname(os.path.abspath(__file__)); SRC = os.path.join(HERE, "fixtures"); DST = os.path.join(HERE, "fixtures-edge")
base = {n: json.load(open(os.path.join(SRC, n + ".json"))) for n in ("state", "history", "settings", "trash")}
def write(name, d):
    os.makedirs(os.path.join(DST, name), exist_ok=True)
    for k, v in d.items(): json.dump(v, open(os.path.join(DST, name, k + ".json"), "w"))
    json.dump({"case": name}, open(os.path.join(DST, name, "edge.json"), "w"))

# 1. empty: a fresh install after its first run found nothing
e = copy.deepcopy(base); s = e["state"]
s.update(findings=[], counts={}, by_severity={k: 0 for k in ("critical", "high", "medium", "low")}, by_library={}, regrabs=[], actions=[], jobs={"active": {}, "recent": []}, whisper_live=[])
s["run"] = dict(s.get("run") or {}, files=0, new=0, resolved=0, files_by_app={"sonarr": 0, "radarr": 0, "whisparr": 0}); s["trash"] = {"files": 0, "gb": 0, "keep": {}}
s["today"] = {"since": "2026-01-01T05:00", "did": {}, "regrab_outcomes": {}, "errors": [], "needs": [], "new": 0, "resolved": 0, "active": 0, "trash": {}}
s["space"] = {"totals": {}, "watched_cold": [], "never_watched": [], "by_show": {}}; s["plan"] = {"gates": {}, "regrab": [], "ai_subtitles": [], "accept_language": [], "fix_headers": [], "verify": []}
e["trash"] = {"items": [], "keep": {}}; e["history"] = {"runs": [], "days": {}, "tests": {}}
write("empty", e)

# 1b. fresh: nothing connected at all - the setup wizard must take over
fr = copy.deepcopy(e); fr["state"]["setup"] = {"arrs": [], "bazarr": False, "plex": False, "first_run_done": False, "degraded": []}; fr["state"]["run"] = {}
write("fresh", fr)

# 1c. tidy: a well-kept library - two kinds of problem, a handful of rows, nothing urgent (this is what success looks like; checks must not assume mess)
t = copy.deepcopy(base); s = t["state"]; keep = [f for f in base["state"]["findings"] if f["check"] == "NO_SUBS"][:3] + [f for f in base["state"]["findings"] if f["check"] == "MISSING_EPISODE"][:3]
for f in keep: f["severity"] = "low" if f["check"] == "NO_SUBS" else "medium"
s["findings"] = keep; s["counts"] = {k: sum(1 for f in keep if f["check"] == k) for k in ("NO_SUBS", "MISSING_EPISODE")}
s["by_severity"] = {"critical": 0, "high": 0, "medium": sum(1 for f in keep if f["severity"] == "medium"), "low": sum(1 for f in keep if f["severity"] == "low")}
s["by_library"] = {}; s["today"] = dict(s.get("today") or {}, needs=[], errors=[])
write("tidy", t)

# 2. huge: one show with 2000 findings of one kind (row cap, grouping, performance)
h = copy.deepcopy(base); s = h["state"]; tpl = next(f for f in base["state"]["findings"] if f["app"] == "sonarr")
rows = []
for i in range(2000):
    f = dict(tpl); f["id"] = f"huge{i:05d}"; f["title"] = f"Giant Show S{i // 100 + 1:02d}E{i % 100 + 1:02d}"; f["check"] = "NO_SUBS"; f["severity"] = "medium"
    f["path"] = f"/data/media/tv/Giant Show (2020)/Season {i // 100 + 1:02d}/Giant Show - S{i // 100 + 1:02d}E{i % 100 + 1:02d}.mkv"; f["detail"] = "audio=['jpn'] embedded_subs=[] external=[] | Bazarr still searching"; f["plain"] = "No subtitles yet. Bazarr is still searching."; f["name"] = "No subtitles"; rows.append(f)
s["findings"] = rows + base["state"]["findings"][:50]; s["by_severity"] = {"critical": 0, "high": 0, "medium": 2000, "low": 50}; s["counts"] = {"NO_SUBS": 2000}
write("huge", h)

# 3. down: every service unreachable during the last run
d = copy.deepcopy(base); s = d["state"]
s["health"] = dict(s.get("health") or {}, status="ok", degraded=["bazarr", "plex", "whisparr"], deps={"bazarr": {"ok": False, "error": "URLError(ConnectionRefusedError(111))"}, "plex": {"ok": False, "error": "timeout"}, "whisparr": {"ok": False, "error": "HTTP 502"}, "sonarr": {"ok": True, "error": ""}})
s["today"] = dict(s.get("today") or {"did": {}, "needs": [], "errors": []}); s["today"]["needs"] = [dict(kind="health", sev="high", check="HEALTH", name="Service unreachable", title="bazarr, plex, whisparr could not be reached during the last run", why="the checks that need them were skipped")] + list(s["today"].get("needs", []))
s["whisper_live"] = [{"container": "subgen", "engine": "server CPU", "error": "connection refused"}]; s["whisper_engine"] = {"active": "server CPU · small model", "gpu_online": False, "note": "desktop GPU offline"}
write("down", d)

# 4. errors: endpoints that answer with error objects instead of data
x = copy.deepcopy(base); s = x["state"]
s["today"] = {"error": "today_summary failed: OperationalError('database is locked')"}; s["space"] = {"error": "Tautulli unreachable"}; s["plan"] = {"error": "plan failed"}
s["tests"] = {"ok": False, "selftest": {"ts": "2026-01-01T05:10", "passed": 30, "total": 33, "failed": [{"name": "settings save", "info": "HTTP 500"}]}, "uibot": {"ts": "2026-01-01T05:45", "passed": 40, "total": 47, "failed": ["tab trash visible"]}}
s["health"] = dict(s.get("health") or {}, status="stale", age_hours=41); x["history"]["tests"] = s["tests"]
write("errors", x)
# 5. arrgap: an arr lists a series folder but returned none of its files (Sportarr, 2026-11-12). Every file there looks unknown;
#    the page must never offer a bulk Move to trash for them (#30), and must still offer it for real strays elsewhere.
g = copy.deepcopy(base); s = g["state"]; tpl = next(f for f in base["state"]["findings"] if f["check"] == "ORPHAN_FILE"); rows = []
for i in range(6):
    f = dict(tpl); f["id"] = f"gap{i:03d}"; f["title"] = f"Fight Night {i + 1} Part {i % 3 + 1}.mkv"; f["severity"] = "low"; f["library"] = "Sports"
    f["path"] = f"/data/media/sports/Fight Series/Season 2026/Fight Night {i + 1} Part {i % 3 + 1}.mkv"
    f["detail"] = "2100 MB inside sportarr's folder for 'Fight Series' (/data/media/sports/Fight Series) but the arr did not list this file - probably a reading gap: rescan in sportarr first"; rows.append(f)
for i in range(3):
    f = dict(tpl); f["id"] = f"stray{i:03d}"; f["title"] = f"Stray Home Video {i + 1}.mkv"; f["path"] = f"/data/media/tv/Stray Home Video {i + 1}.mkv"; f["detail"] = "900 MB in sonarr root /data/media/tv - not managed by any arr"; rows.append(f)
s["findings"] = rows + [f for f in base["state"]["findings"] if f["check"] != "ORPHAN_FILE"][:40]
write("arrgap", g)
print("edge fixtures:", ", ".join(sorted(os.listdir(DST))))
