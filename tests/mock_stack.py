"""A fake Sonarr + Radarr + Bazarr in one HTTP server, just enough for audit.py to run end to end against synthetic media.
Every write (POST/PUT/PATCH) is recorded in `server.calls` and visible at GET /__calls. Used by e2e_stack.py; usable by hand:
    python tests/mock_stack.py 8765   -> http://127.0.0.1:8765/sonarr , /radarr , /bazarr   (API key: test)"""
import json, os, sys, threading, datetime, random, socket, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

KEY = "test"
SHAPES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shapes")
def shape(name, default=None):
    """A recorded, sanitised answer of the real service (tests/shapes/<service>/<name>.json, made by scripts/record_shapes.py), or `default` when none is recorded."""
    try: return json.load(open(os.path.join(SHAPES, name + ".json")))["body"]
    except Exception: return default
DROP = {"sonarr/episode": {"runtime", "overview", "lastSearchTime", "finaleType"}, "sonarr/series": {"runtime", "statistics", "seasons", "overview", "images", "alternateTitles"}, "radarr/movie": {"runtime", "overview", "images", "alternateTitles", "statistics", "movieFile", "sizeOnDisk"},
        "sonarr/episodefile": {"mediaInfo", "languages", "quality", "size"}, "radarr/moviefile": {"mediaInfo", "languages", "quality", "size"}}   # measurements the hand-made world must own
def sample(name, where=None):
    """One recorded object from a shape file: the first row of data / MediaContainer.Metadata / MediaContainer.Directory, or the body itself."""
    b = shape(name)
    if b is None: return None
    if where: 
        for k in where: b = (b or {}).get(k) if isinstance(b, dict) else (b[k] if isinstance(b, list) and len(b) > k else None)
        return b
    for path in (("data", 0), ("MediaContainer", "Metadata", 0), ("MediaContainer", "Directory", 0), (0,)):
        x = b
        try:
            for k in path: x = x[k]
            if isinstance(x, dict): return x
        except Exception: continue
    return b if isinstance(b, dict) else None
def over(name, obj, where=None):
    """obj laid over the recorded sample `name` (deep-copied): real fields present, our values win."""
    t = sample(name, where)
    if not isinstance(t, dict): return obj
    out = json.loads(json.dumps(t)); out.update(obj); return out
def templated(name, obj):
    """The hand-made object laid over a real-shaped sample: every field the real service sends is present, our test values win."""
    t = shape(name); t = t[0] if isinstance(t, list) and t else t
    if not isinstance(t, dict): return obj
    out = {k: v for k, v in json.loads(json.dumps(t)).items() if k not in DROP.get(name, set())}; out.update(obj); return out
def iso(days_ago=0): return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

def default_world(tv_root="/data/tv", movie_root="/data/movies"):
    """One show with three files + one missing episode, one movie, matching the files e2e_stack.py generates."""
    show_dir = os.path.join(tv_root, "Alpha Show (2020)"); season = os.path.join(show_dir, "Season 01")
    files = [dict(id=11, seriesId=1, path=os.path.join(season, "Alpha Show (2020) - S01E01 - Pilot [WEBDL-1080p]-grp.mkv"), size=0, dateAdded=iso(40)),
             dict(id=12, seriesId=1, path=os.path.join(season, "Alpha Show (2020) - S01E02 - Second [WEBDL-1080p]-grp.mkv"), size=0, dateAdded=iso(40)),
             dict(id=13, seriesId=1, path=os.path.join(season, "Alpha Show (2020) - S01E03 - Third [WEBDL-1080p]-grp.mkv"), size=0, dateAdded=iso(40))]
    for f in files: f.update(quality={"quality": {"name": "Unknown", "resolution": 0}}, mediaInfo={}, languages=[])
    eps = [dict(id=101, seriesId=1, seasonNumber=1, episodeNumber=1, title="Pilot", hasFile=True, episodeFileId=11, monitored=True, airDateUtc=iso(60)),
           dict(id=102, seriesId=1, seasonNumber=1, episodeNumber=2, title="Second", hasFile=True, episodeFileId=12, monitored=True, airDateUtc=iso(53)),
           dict(id=103, seriesId=1, seasonNumber=1, episodeNumber=3, title="Third", hasFile=True, episodeFileId=13, monitored=True, airDateUtc=iso(46)),
           dict(id=104, seriesId=1, seasonNumber=1, episodeNumber=4, title="Fourth", hasFile=False, episodeFileId=0, monitored=True, airDateUtc=iso(30))]
    series = [dict(id=1, title="Alpha Show", titleSlug="alpha-show", path=show_dir, runtime=1, monitored=True, seriesType="standard", tags=[],
                   originalLanguage={"id": 1, "name": "English"}, statistics={"episodeFileCount": 3})]
    mdir = os.path.join(movie_root, "Beta Movie (2021)")
    movies = [dict(id=1, title="Beta Movie", year=2021, titleSlug="beta-movie", path=mdir, runtime=1, monitored=True, hasFile=True, isAvailable=True, tags=[],
                   originalLanguage={"id": 1, "name": "English"}, movieFile={"id": 21})]
    mfiles = [dict(id=21, movieId=1, path=os.path.join(mdir, "Beta Movie (2021) [WEBDL-1080p]-grp.mkv"), size=0, dateAdded=iso(40), quality={"quality": {"name": "Unknown", "resolution": 0}}, mediaInfo={}, languages=[])]
    series = [templated("sonarr/series", x) for x in series]; eps = [templated("sonarr/episode", x) for x in eps]; files = [templated("sonarr/episodefile", x) for x in files]
    movies = [templated("radarr/movie", x) for x in movies]; mfiles = [templated("radarr/moviefile", x) for x in mfiles]
    return dict(tv_root=tv_root, movie_root=movie_root, series=series, episodes=eps, files=files, movies=movies, mfiles=mfiles, tags=[], wanted_eps=[103], wanted_movies=[], cmd_id=[100],
                bazarr_history=[], plex_ondeck=[("sonarr", 102)], plex_sessions=[], plex_missing=[])   # Plex: S01E02 is next up on someone's Home; nobody is watching; plex_missing = file ids Plex has not indexed

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n) if n else b""
        try: return json.loads(raw) if raw and raw[:1] in b"{[" else (parse_qs(raw.decode()) if raw else {})
        except Exception: return {}
    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_PATCH(self): self._route("PATCH")
    def do_DELETE(self): self._route("DELETE")
    def _route(self, method):
        u = urlparse(self.path); q = parse_qs(u.query); parts = u.path.strip("/").split("/")
        if u.path == "/__calls": return self._send(200, self.server.calls)
        if u.path == "/__reset": self.server.calls.clear(); return self._send(200, {"ok": True})
        app = parts[0] if parts else ""
        if app == "whisper": return self._asr(parts[1:], q)   # fake Whisper: speech at fixed irregular times, so a shifted subtitle file is measurable
        body = self._body() if method != "GET" else None
        ch = getattr(self.server, "chaos", None)   # Q15: random 500s, slow answers, one service vanishing mid-run
        if ch and app in ("sonarr", "radarr", "whisparr", "bazarr"):
            ch["n"][app] = ch["n"].get(app, 0) + 1; r = ch["rnd"].random()
            if app == ch.get("vanish_app") and ch.get("vanish_from", 0) <= ch["n"][app] < ch.get("vanish_from", 0) + ch.get("vanish_len", 0):
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                return
            if r < ch.get("p500", 0.08): return self._send(500, {"error": "chaos"})
            if r < ch.get("p500", 0.08) + ch.get("pslow", 0.04): time.sleep(ch.get("slow_s", 2))
        if method != "GET": self.server.calls.append(dict(app=app, method=method, path="/" + "/".join(parts[1:]), query=q, body=body))
        public = (len(parts) <= 1) or parts[1:] in (["ping"], []) or (app == "bazarr" and len(parts) == 1) or app == "plex"
        if not public and self.headers.get("X-Api-Key") != KEY and self.headers.get("X-API-KEY") != KEY: return self._send(401, {"error": "bad key"})
        if app in ("sonarr", "radarr", "whisparr"): return self._arr(app, method, parts[1:], q, body)
        if app == "bazarr": return self._bazarr(method, parts[1:], q, body)
        if app == "plex": return self._plex(method, parts[1:], q)
        return self._send(404, {})
    SPEECH = [0.4, 1.9, 4.1, 5.0, 7.3, 8.1, 10.6, 12.2, 13.0, 15.5, 17.1, 19.4, 20.2, 22.8, 24.1, 26.6, 27.9, 29.5, 31.0, 33.4, 35.2, 37.7, 38.9, 41.3, 43.0, 45.6, 47.2, 49.9, 51.4, 53.8, 55.1, 57.6, 59.0]
    def _asr(self, parts, q):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n) if n else b""
        if parts == ["detect-language"]:   # which language is spoken: the world decides (default English)
            W = self.server.world; return self._send(200, {"detected_language": W.get("whisper_lang_name", "english"), "language_code": W.get("whisper_lang", "en")})
        if parts != ["asr"] or (q.get("output") or [""])[0] != "json": return self._send(503, {"error": "mock whisper only answers json timing requests"})
        secs = max(0.0, (n - 300) / 32000.0)   # 16 kHz mono 16-bit wav -> seconds in the window
        self.server.calls.append(dict(app="whisper", method="POST", path="/asr", query=q, body={"seconds": round(secs, 1)}))
        return self._send(200, {"text": "la", "segments": [{"start": t, "end": t + 1.0, "text": "la la"} for t in self.SPEECH if t < secs - 0.5]})
    def _bz_episode(self, e, sid):
        W = self.server.world; f = next((x for x in W["files"] if x["id"] == e["episodeFileId"]), None); path = f["path"] if f else None
        side = path and os.path.exists(os.path.splitext(path)[0] + ".en.srt")
        subs = [over("bazarr/episodes", dict(name="English", code2="en", code3="eng", path=os.path.splitext(path)[0] + ".en.srt", forced=False, hi=False, embedded_track_id=None), ("data", 0, "subtitles", 0))] if side else []
        missing = [] if side or e["id"] not in W["wanted_eps"] else [{"name": "English", "code2": "en", "code3": "eng", "forced": False, "hi": False}]
        return over("bazarr/episodes", dict(sonarrEpisodeId=e["id"], sonarrSeriesId=sid, season=e["seasonNumber"], episode=e["episodeNumber"], title=e["title"], path=path, monitored=True, subtitles=subs, missing_subtitles=missing, audio_language=[{"name": "English", "code2": "en", "code3": "eng"}], sceneName=None))
    def _plex(self, method, parts, q):
        """A fake Plex: sections, library contents built from the world's files, On Deck, sessions, metadata, refresh. Needs X-Plex-Token=test."""
        W = self.server.world
        if (q.get("X-Plex-Token") or [""])[0] != KEY: return self._send(401, {})
        path = "/" + "/".join(parts); title_of = lambda e: next((s["title"] for s in W["series"] if s["id"] == e["seriesId"]), "Show")
        def ep_item(e):
            f = next((x for x in W["files"] if x["id"] == e["episodeFileId"]), None)
            return over("plex/onDeck", dict(ratingKey=str(100000 + e["id"]), key=f"/library/metadata/{100000 + e['id']}", type="episode", title=e["title"], index=e["episodeNumber"], parentIndex=e["seasonNumber"], parentRatingKey=str(90000 + e["seriesId"]), grandparentRatingKey=str(80000 + e["seriesId"]), grandparentTitle=title_of(e), librarySectionID=1,
                                           Media=[{"id": e["id"], "duration": 1500000, "videoCodec": "h264", "audioCodec": "aac", "container": "mkv", "Part": [{"id": e["id"], "key": f"/library/parts/{e['id']}/file.mkv", "file": f["path"] if f else "", "size": 1, "container": "mkv"}]}] if f else []))
        def mv_item(m):
            f = next((x for x in W["mfiles"] if x["movieId"] == m["id"]), None)
            return over("plex/metadata_movie", dict(ratingKey=str(70000 + m["id"]), key=f"/library/metadata/{70000 + m['id']}", type="movie", title=m["title"], year=m.get("year"), librarySectionID=2,
                                                     Media=[{"id": 7000 + m["id"], "duration": 5400000, "videoCodec": "h264", "audioCodec": "aac", "container": "mkv", "Part": [{"id": 7000 + m["id"], "key": f"/library/parts/{7000 + m['id']}/file.mkv", "file": f["path"] if f else "", "size": 1, "container": "mkv"}]}] if f else []))
        eps = [e for e in W["episodes"] if e["hasFile"] and e["episodeFileId"] not in W["plex_missing"]]; movs = [m for m in W["movies"] if m.get("hasFile") and m.get("movieFile", {}).get("id") not in W["plex_missing"]]
        if path == "/identity": return self._send(200, over("plex/identity", {"MediaContainer": {"size": 0, "machineIdentifier": "mock-plex", "version": "1.40.0"}}) if sample("plex/identity") else {"MediaContainer": {"size": 0, "machineIdentifier": "mock-plex", "version": "1.40.0"}})
        if path == "/library/sections":
            dirs = [over("plex/sections", dict(key="1", type="show", title="TV", agent="tv.plex.agents.series", Location=[{"id": 1, "path": W["tv_root"]}])), over("plex/sections", dict(key="2", type="movie", title="Movies", agent="tv.plex.agents.movie", Location=[{"id": 2, "path": W["movie_root"]}]))]
            return self._send(200, {"MediaContainer": {"size": 2, "Directory": dirs}})
        if path in ("/library/sections/1/all", "/library/sections/2/all"):
            items = [ep_item(e) for e in eps] if path.startswith("/library/sections/1") else [mv_item(m) for m in movs]
            if path.startswith("/library/sections/1") and (q.get("type") or ["4"])[0] != "4": items = [over("plex/section_show_all", dict(ratingKey=str(80000 + s_["id"]), key=f"/library/metadata/{80000 + s_['id']}", type="show", title=s_["title"], librarySectionID=1)) for s_ in W["series"]]
            start = int((q.get("X-Plex-Container-Start") or ["0"])[0]); size = int((q.get("X-Plex-Container-Size") or ["3000"])[0])
            return self._send(200, {"MediaContainer": {"size": len(items[start:start + size]), "totalSize": len(items), "offset": start, "Metadata": items[start:start + size]}})
        if path.startswith("/library/sections/") and path.endswith("/refresh"): self.server.calls.append(dict(app="plex", method="GET", path=path, query=q, body=None)); return self._send(200, {})
        if path == "/library/onDeck":
            items = [ep_item(e) for a, fid in W["plex_ondeck"] for e in W["episodes"] if a == "sonarr" and e["id"] == fid]
            return self._send(200, {"MediaContainer": {"size": len(items), "Metadata": items}})
        if path == "/status/sessions":
            items = []
            for sess in W["plex_sessions"]:   # dict(app, id, user, offset)
                e = next((x for x in W["episodes"] if sess.get("app") == "sonarr" and x["id"] == sess.get("id")), None)
                if e: it = ep_item(e); it.update(viewOffset=sess.get("offset", 60000), User={"id": 1, "thumb": "", "title": sess.get("user", "someone")}, Player={"address": "127.0.0.1", "device": "TV", "platform": "Plex Web", "product": "Plex Web", "state": "playing", "title": "Living room"}, Session={"id": "s1", "bandwidth": 1000, "location": "lan"}); items.append(it)
            return self._send(200, {"MediaContainer": {"size": len(items), "Metadata": items}})
        if path.startswith("/library/metadata/"):
            rk = parts[2] if len(parts) > 2 else ""; kids = len(parts) > 3 and parts[3] == "children"
            try: n = int(rk)
            except ValueError: return self._send(404, {})
            if n >= 100000: e = next((x for x in W["episodes"] if x["id"] == n - 100000), None); return self._send(200 if e else 404, {"MediaContainer": {"size": 1, "Metadata": [dict(ep_item(e), lastViewedAt=None)]}} if e else {})
            if n >= 80000 and n < 90000:
                sid = n - 80000; eps_ = [ep_item(e) for e in eps if e["seriesId"] == sid]
                if kids: return self._send(200, {"MediaContainer": {"size": len(eps_), "Metadata": eps_}})
                return self._send(200, {"MediaContainer": {"size": 1, "Metadata": [over("plex/metadata_show", dict(ratingKey=rk, type="show", title=next((s_["title"] for s_ in W["series"] if s_["id"] == sid), "Show"), leafCount=len(eps_), viewedLeafCount=0, lastViewedAt=None))]}})
            if n >= 70000: m = next((x for x in W["movies"] if x["id"] == n - 70000), None); return self._send(200 if m else 404, {"MediaContainer": {"size": 1, "Metadata": [dict(mv_item(m), lastViewedAt=None)]}} if m else {})
            return self._send(404, {})
        return self._send(404, {})
    def _arr(self, app, method, parts, q, body):
        W = self.server.world
        if parts == ["ping"]: return self._send(200, {"status": "OK"})
        if parts == [] or parts == [""]:
            body_ = f"<!doctype html><html><head><title>{app.capitalize()}</title></head><body></body></html>".encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body_))); self.end_headers(); self.wfile.write(body_); return
        if parts[:2] != ["api", "v3"]: return self._send(404, {})
        p = "/".join(parts[2:])
        if p == "tag":
            if method == "POST": t = dict(id=len(W["tags"]) + 1, label=body.get("label")); W["tags"].append(t); return self._send(201, t)
            return self._send(200, W["tags"])
        if p == "rootfolder": return self._send(200, [{"path": W["tv_root"] if app != "radarr" else W["movie_root"]}])
        if p == "system/status": return self._send(200, {"version": "4.0.0.0", "appName": app.capitalize()})
        if p == "notification":
            W.setdefault("notifications", [])
            if method == "POST": n = dict(body, id=len(W["notifications"]) + 1); W["notifications"].append(n); return self._send(201, n)
            return self._send(200, W["notifications"])
        if p.startswith("notification/") and method == "PUT": return self._send(202, body)
        if p == "command":
            if method == "POST": W["cmd_id"][0] += 1; return self._send(201, dict(id=W["cmd_id"][0], name=body.get("name"), status="queued", body=body))
            return self._send(200, [])
        if p.startswith("queue"): return self._send(200, {"records": []})
        if p.startswith("history"): return self._send(200, [] if "since" in p else {"records": []})
        if p.startswith("blocklist"): return self._send(200, {"records": []})
        if p in ("series/editor", "movie/editor"): return self._send(202, body)
        if app == "radarr":
            if p == "movie": return self._send(200, W["movies"])
            if p.startswith("movie/"): m = next((x for x in W["movies"] if x["id"] == int(p.split("/")[1])), None); return self._send(200 if m else 404, m or {})
            if p == "moviefile": mid = int(q.get("movieId", ["0"])[0]); return self._send(200, [f for f in W["mfiles"] if f["movieId"] == mid])
            if p.startswith("moviefile/"): f = next((x for x in W["mfiles"] if x["id"] == int(p.split("/")[1])), None); return self._send(200 if f else 404, f or {})
            return self._send(404, {})
        if p == "series": return self._send(200, W["series"])
        if p.startswith("series/"): s = next((x for x in W["series"] if x["id"] == int(p.split("/")[1])), None); return self._send(200 if s else 404, s or {})
        if p == "episode": sid = int(q.get("seriesId", ["0"])[0]); return self._send(200, [e for e in W["episodes"] if e["seriesId"] == sid])
        if p == "episodefile": sid = int(q.get("seriesId", ["0"])[0]); return self._send(200, [f for f in W["files"] if f["seriesId"] == sid and os.path.exists(f["path"])])
        if p.startswith("episodefile/"):
            f = next((x for x in W["files"] if x["id"] == int(p.split("/")[1])), None)
            return self._send(200, f) if f and os.path.exists(f["path"]) else self._send(404, {})
        return self._send(404, {})
    def _bazarr(self, method, parts, q, body):
        W = self.server.world; p = "/".join(parts[1:]) if parts and parts[0] == "api" else "/".join(parts)
        if parts in ([], [""]):
            body_ = b"<!doctype html><html><head><title>Bazarr</title></head><body></body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body_))); self.end_headers(); self.wfile.write(body_); return
        if p == "episodes/wanted": return self._send(200, {"data": [{"sonarrEpisodeId": i} for i in W["wanted_eps"]]})
        if p == "movies/wanted": return self._send(200, {"data": [{"radarrId": i} for i in W["wanted_movies"]]})
        if p == "episodes" and method == "GET" and q.get("seriesid[]"):
            sid = int((q.get("seriesid[]") or ["0"])[0]); return self._send(200, {"data": [self._bz_episode(e, sid) for e in W["episodes"] if e["seriesId"] == sid and e["hasFile"]]})
        if p == "episodes/subtitles" and method == "PATCH": return self._send(204)
        if p == "movies/subtitles" and method == "PATCH": return self._send(204)
        if p in ("episodes/history", "movies/history"):
            eid = int((q.get("episodeid") or q.get("radarrid") or ["0"])[0]); rows = [h for h in W["bazarr_history"] if h.get("sonarrEpisodeId") == eid or h.get("radarrId") == eid]
            return self._send(200, {"data": rows, "total": len(rows)})
        if p == "subtitles" and method == "PATCH": return self._send(204)   # sync / translate / mods
        if p == "episodes" and q.get("episodeid[]"):
            eid = int(q["episodeid[]"][0]); e = next((x for x in W["episodes"] if x["id"] == eid), None); return self._send(200, {"data": [self._bz_episode(e, e["seriesId"])] if e else []})
        if p == "movies" and q.get("radarrid[]"): return self._send(200, {"data": [{"radarrId": int(q["radarrid[]"][0]), "subtitles": []}]})
        if p == "providers/episodes" and method == "GET":   # one whisperai candidate per episode, shaped like the real list
            eid = int((q.get("episodeid") or ["0"])[0]); return self._send(200, {"data": [over("bazarr/providers_episodes", dict(provider="whisperai", language="en", subtitle="whisperai-" + str(eid), release_info=["Whisper transcription"], score=100, orig_score=360, matches=["whisperai"], dont_matches=[]))]})
        if p == "providers/episodes" and method == "POST":   # the download: Bazarr answers at once and writes a history row when the transcription lands (here: immediately)
            eid = int((body.get("episodeid") or ["0"])[0]) if isinstance(body.get("episodeid"), list) else int(body.get("episodeid") or 0)
            e = next((x for x in W["episodes"] if x["id"] == eid), None); f = next((x for x in W["files"] if e and x["id"] == e["episodeFileId"]), None)
            if f: open(os.path.splitext(f["path"])[0] + ".en.srt", "w").write("1\n00:00:01,000 --> 00:00:02,000\n(whisper)\n")
            W["bazarr_history"].append(dict(id=len(W["bazarr_history"]) + 1, action=1, timestamp="just now", parsed_timestamp=datetime.datetime.now().strftime("%m/%d/%y %H:%M:%S"), provider="whisperai", language={"name": "English", "code2": "en", "code3": "eng", "forced": False, "hi": False},
                                            sonarrEpisodeId=eid, sonarrSeriesId=e["seriesId"] if e else 0, seriesTitle="Alpha Show", episodeTitle=e["title"] if e else "", episode_number=f"{e['seasonNumber']}x{e['episodeNumber']}" if e else "", score="100%", subtitles_path=(os.path.splitext(f["path"])[0] + ".en.srt") if f else "", description="downloaded from whisperai", matches=[], dont_matches=[], upgradable=False, blacklisted=False))
            return self._send(200, {})
        if p.startswith("providers/"): return self._send(200, {"data": []} if method == "GET" else {})
        if p == "system/settings": return self._send(204 if method == "POST" else 200, None if method == "POST" else {})
        if p == "system/status": return self._send(200, {"data": {"bazarr_version": "1.4.0"}})
        return self._send(404, {})

def chaos(seed=11, p500=0.08, pslow=0.04, slow_s=2, vanish_app="radarr", vanish_from=2, vanish_len=12):
    """Settings for server.chaos: a share of requests fail with 500, a share answer slowly, and one app drops the connection for a window of requests."""
    return dict(rnd=random.Random(seed), n={}, p500=p500, pslow=pslow, slow_s=slow_s, vanish_app=vanish_app, vanish_from=vanish_from, vanish_len=vanish_len)
def start(world=None, port=0, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), Handler); srv.world = world or default_world(); srv.calls = []; srv.chaos = None
    threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv

if __name__ == "__main__":
    s = start(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765); print("mock stack on", s.server_address); threading.Event().wait()
