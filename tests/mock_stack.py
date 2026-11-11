"""A fake Sonarr + Radarr + Bazarr in one HTTP server, just enough for audit.py to run end to end against synthetic media.
Every write (POST/PUT/PATCH) is recorded in `server.calls` and visible at GET /__calls. Used by e2e_stack.py; usable by hand:
    python tests/mock_stack.py 8765   -> http://127.0.0.1:8765/sonarr , /radarr , /bazarr   (API key: test)"""
import json, os, sys, threading, datetime, random, socket, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

KEY = "test"
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
    return dict(tv_root=tv_root, movie_root=movie_root, series=series, episodes=eps, files=files, movies=movies, mfiles=mfiles, tags=[], wanted_eps=[103], wanted_movies=[], cmd_id=[100])

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
        public = (len(parts) <= 1) or parts[1:] in (["ping"], []) or (app == "bazarr" and len(parts) == 1)
        if not public and self.headers.get("X-Api-Key") != KEY and self.headers.get("X-API-KEY") != KEY: return self._send(401, {"error": "bad key"})
        if app in ("sonarr", "radarr", "whisparr"): return self._arr(app, method, parts[1:], q, body)
        if app == "bazarr": return self._bazarr(method, parts[1:], q, body)
        return self._send(404, {})
    SPEECH = [0.4, 1.9, 4.1, 5.0, 7.3, 8.1, 10.6, 12.2, 13.0, 15.5, 17.1, 19.4, 20.2, 22.8, 24.1, 26.6, 27.9, 29.5, 31.0, 33.4, 35.2, 37.7, 38.9, 41.3, 43.0, 45.6, 47.2, 49.9, 51.4, 53.8, 55.1, 57.6, 59.0]
    def _asr(self, parts, q):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n) if n else b""
        if parts != ["asr"] or (q.get("output") or [""])[0] != "json": return self._send(503, {"error": "mock whisper only answers json timing requests"})
        secs = max(0.0, (n - 300) / 32000.0)   # 16 kHz mono 16-bit wav -> seconds in the window
        self.server.calls.append(dict(app="whisper", method="POST", path="/asr", query=q, body={"seconds": round(secs, 1)}))
        return self._send(200, {"text": "la", "segments": [{"start": t, "end": t + 1.0, "text": "la la"} for t in self.SPEECH if t < secs - 0.5]})
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
        if p == "episodes" and method == "GET":
            sid = int((q.get("seriesid[]") or ["0"])[0]); return self._send(200, {"data": [dict(sonarrEpisodeId=e["id"], sonarrSeriesId=sid, season=e["seasonNumber"], episode=e["episodeNumber"], path=next((f["path"] for f in W["files"] if f["id"] == e["episodeFileId"]), None)) for e in W["episodes"] if e["seriesId"] == sid and e["hasFile"]]})
        if p == "episodes/subtitles" and method == "PATCH": return self._send(204)
        if p == "movies/subtitles" and method == "PATCH": return self._send(204)
        if p in ("episodes/history", "movies/history"): return self._send(200, {"data": []})
        if p == "subtitles" and method == "PATCH": return self._send(204)   # sync / translate / mods
        if p == "episodes" and q.get("episodeid[]"): return self._send(200, {"data": [{"sonarrEpisodeId": int(q["episodeid[]"][0]), "subtitles": []}]})
        if p == "movies" and q.get("radarrid[]"): return self._send(200, {"data": [{"radarrId": int(q["radarrid[]"][0]), "subtitles": []}]})
        if p.startswith("providers/"): return self._send(200, {"data": []} if method == "GET" else {})
        if p == "system/settings": return self._send(204 if method == "POST" else 200, None if method == "POST" else {})
        if p == "system/status": return self._send(200, {"data": {"bazarr_version": "1.4.0"}})
        return self._send(404, {})

def chaos(seed=11, p500=0.08, pslow=0.04, slow_s=2, vanish_app="radarr", vanish_from=2, vanish_len=12):
    """Settings for server.chaos: a share of requests fail with 500, a share answer slowly, and one app drops the connection for a window of requests."""
    return dict(rnd=random.Random(seed), n={}, p500=p500, pslow=pslow, slow_s=slow_s, vanish_app=vanish_app, vanish_from=vanish_from, vanish_len=vanish_len)
def start(world=None, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler); srv.world = world or default_world(); srv.calls = []; srv.chaos = None
    threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv

if __name__ == "__main__":
    s = start(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765); print("mock stack on", s.server_address); threading.Event().wait()
