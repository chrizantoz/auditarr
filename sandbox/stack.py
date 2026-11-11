"""The playground's fake world: synthetic video files with real defects plus a fake Sonarr/Radarr/Bazarr/Plex/Whisper that answers for them.
Runs forever; the hourly reset recreates the containers, which rebuilds everything. Env: DATA (default /data), PORT (default 8765)."""
import os, sys, shutil, time, threading
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, "tests"))
import mock_stack  # noqa: E402
from synthlib import make  # noqa: E402
DATA = os.environ.get("DATA", "/data"); PORT = int(os.environ.get("PORT", "8765")); TV = os.path.join(DATA, "tv"); MOVIES = os.path.join(DATA, "movies")
for d in (TV, MOVIES):
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d, exist_ok=True)
W = mock_stack.default_world(TV, MOVIES)
make(W["files"][0]["path"], seconds=40, alang="eng")                 # S01E01 fine
make(W["files"][1]["path"], seconds=40, alang="fre")                 # S01E02 French audio on an English show -> Wrong language
make(W["files"][2]["path"], seconds=40, alang="jpn")                 # S01E03 Japanese, no subtitles -> No subtitles (AI subtitles work here)
make(W["mfiles"][0]["path"], seconds=40, audio=False)                # the movie has no audio track
os.utime(W["mfiles"][0]["path"], (time.time() - 3 * 86400,) * 2)    # old enough for the not-in-Plex rule
season = os.path.dirname(W["files"][0]["path"]); shutil.copy(W["files"][0]["path"], os.path.join(season, "Alpha.Show.S01E01.other-group.mkv"))   # a duplicate
shutil.copy(W["files"][0]["path"], os.path.join(TV, "Random.Home.Video.mkv"))                                                                # an orphan nobody manages
for f in W["files"] + W["mfiles"]: f["size"] = os.path.getsize(f["path"])
W["plex_missing"] = [W["mfiles"][0]["id"]]; W["plex_ondeck"] = [("sonarr", 102)]; W["plex_sessions"] = []
srv = mock_stack.start(W, port=PORT, host="0.0.0.0"); print("playground stack on", srv.server_address, flush=True)
threading.Event().wait()
