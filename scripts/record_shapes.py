"""Q24: record what the real services answer, with every private detail replaced, so the fake stack can answer in the same shape.
Run inside the container (it uses the app's own connection settings):
    docker exec -i media-audit python3 - /config/shapes < scripts/record_shapes.py
Then copy the folder into tests/shapes/ of the repository. Titles, paths, names, hosts, keys and free text are replaced before anything is written;
ids, numbers, dates, flags and enumerations (quality names, codecs, languages, statuses) are kept, because that is what the app's code relies on."""
import os, re, sys, json, datetime
sys.path.insert(0, "/app"); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "shapes"
SECRET = re.compile(r"apikey|api_key|token|password|secret|auth|identifier|uuid", re.I)
KEEP_UNDER = {"quality", "language", "languages", "originallanguage", "revision", "mediainfo", "statistics", "ratings", "quality_profile", "profile"}   # the app reads these by value
SAFE = re.compile(r"^(\d{4}-\d\d-\d\d([T ][\d:.]+Z?([+-]\d\d:?\d\d)?)?|[\d.,:+-]+|true|false|[a-z]{2,3}(-[A-Za-z]{2})?|[A-Za-z][A-Za-z0-9_+.-]{0,24}|/(library|status|system|photo)/[A-Za-z0-9_/.=?&-]*|\d+x\d+|[A-Za-z0-9_-]+/[A-Za-z0-9_.+-]+)$")
RELEASE = re.compile(r"(?i)(\d{3,4}p|web-?dl|webrip|bluray|hdtv|x26[45]|hevc|h\.?26[45]|remux|dual|multi)")
counters = {}
def nick(kind, value):
    """Stable replacement: the same real value always becomes the same placeholder."""
    table = counters.setdefault(kind, {}); return table.setdefault(value, f"{kind} {len(table) + 1}")
def clean_path(p):
    """Keep the shape of a media path (show/season/episode or movie folder/file, extension), replace every name."""
    parts = [x for x in re.split(r"[/\\]", p) if x]; ext = os.path.splitext(parts[-1])[1].lower() if parts and "." in parts[-1] and len(os.path.splitext(parts[-1])[1]) <= 5 else ""
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", p); y = re.search(r"\((19|20)\d\d\)", p); year = y.group(0)[1:-1] if y else "2020"
    if m:
        show_dir = parts[-3] if len(parts) >= 3 else (parts[0] if parts else p); n = nick("Show", show_dir)
        return f"/data/tv/{n}/Season {int(m.group(1)):02d}/{n} - S{int(m.group(1)):02d}E{int(m.group(2)):02d} - Episode{ext or '.mkv'}"
    if len(parts) <= 1: return "/data/" + nick("folder", p).replace(" ", "")
    folder = parts[-2] if ext else parts[-1]; n = nick("Movie", folder)
    return f"/data/movies/{n} ({year})/{n}{ext}" if ext else f"/data/movies/{n} ({year})"
def scrub(v, key="", parent=""):
    """Whitelist: a string survives only when the app needs its value (quality/language/codec names, enums, dates, numbers, Plex api keys). Everything else becomes a stable placeholder."""
    k = (key or "").lower(); pk = (parent or "").lower()
    if isinstance(v, dict): return {kk: scrub(vv, kk, k) for kk, vv in v.items()}
    if isinstance(v, list):
        keep = v[:3] if len(v) > 3 and all(isinstance(x, dict) for x in v) else v[:12] if len(v) > 12 else v
        return [scrub(x, key, parent) for x in keep]
    if not isinstance(v, str): return v   # numbers, booleans, None: ids, dates, sizes, flags stay
    if SECRET.search(k): return "REDACTED"
    t = v.strip()
    if k in ("path", "relativepath", "originalfilepath", "file", "foldername", "mapped_path", "subtitles_path", "location", "rootfolderpath") or (t.startswith("/") and not t.startswith(("/library/", "/status/", "/system/", "/photo/")) and len(t) > 8): return clean_path(t)
    if k == "guid" or "://" in t: return re.sub(r"://.*", "://" + nick("ref", t).replace(" ", ""), t) if "://" in t else nick("guid", t)
    if pk in KEEP_UNDER or k in ("status", "type", "kind", "eventtype", "protocol", "state", "action", "mode", "codec", "videocodec", "audiocodec", "container", "resolution", "source", "code2", "code3", "format", "audiolanguages", "subtitles", "videocodec", "audiochannels", "scantype", "videodynamicrange", "videodynamicrangetype", "runtime", "containerformat", "quality", "language", "sortorder", "sortkey", "direction", "trackername", "series_type", "seriestype", "monitornewitems", "minimumavailability", "librarysectiontype", "ratingimage", "audienceratingimage", "contentrating", "originallyavailableat", "addedat", "updatedat", "scannedat", "releasedate", "version", "appname", "osname", "branch", "runtimeversion", "buildtime", "starttime", "timezone", "provider", "name" if pk in ("quality", "language", "languages", "originallanguage", "provider", "providers") else "-"):
        return t if not RELEASE.search(t) or pk in ("quality", "mediainfo") or k in ("quality", "resolution") else nick("release", t)
    if re.fullmatch(r"[0-9a-fA-F]{20,}", t): return nick("hex", t)
    if SAFE.match(t) and not RELEASE.search(t) and not re.search(r"[A-Z][a-z]+ [A-Z]", t): return t
    return nick(key or "text", t)
def save(service, name, data, endpoint):
    d = os.path.join(OUT, service); os.makedirs(d, exist_ok=True)
    body = scrub(data); json.dump({"_meta": {"endpoint": endpoint, "recorded": datetime.datetime.now().isoformat(timespec="minutes"), "note": "sanitised real answer; ids/dates/enums real, text replaced"}, "body": body}, open(os.path.join(d, name + ".json"), "w"), indent=1, ensure_ascii=False)
    print(f"  {service}/{name}: {type(body).__name__}{'[' + str(len(body)) + ']' if isinstance(body, list) else ''}")
def try_(fn, what):
    try: return fn()
    except Exception as e: print(f"  skip {what}: {e!r}"); return None

apps = audit.arr_apps(); print("recording", list(apps), "into", OUT)
for app, (base, key, kind) in apps.items():
    A = lambda p: audit.api(base, key, p)
    if kind == "series":
        series = try_(lambda: A("series"), "series") or []; pick = next((s for s in series if (s.get("statistics") or {}).get("episodeFileCount")), series[0] if series else None)
        save(app, "series", series[:3], "series")
        if pick:
            sid = pick["id"]; save(app, "series_one", try_(lambda: A(f"series/{sid}"), "series/id"), "series/{id}")
            eps = try_(lambda: A(f"episode?seriesId={sid}"), "episode") or []; save(app, "episode", eps[:6], "episode?seriesId=")
            efs = try_(lambda: A(f"episodefile?seriesId={sid}"), "episodefile") or []; save(app, "episodefile", efs[:3], "episodefile?seriesId=")
            if efs: fid = efs[0]["id"]; save(app, "episodefile_one", try_(lambda: A(f"episodefile/{fid}"), "episodefile/id"), "episodefile/{id}")
    else:
        movies = try_(lambda: A("movie"), "movie") or []; pick = next((m for m in movies if m.get("hasFile")), movies[0] if movies else None)
        save(app, "movie", movies[:3], "movie")
        if pick:
            mid = pick["id"]; save(app, "movie_one", try_(lambda: A(f"movie/{mid}"), "movie/id"), "movie/{id}")
            mfs = try_(lambda: A(f"moviefile?movieId={mid}"), "moviefile") or []; save(app, "moviefile", mfs[:3], "moviefile?movieId=")
            if mfs: fid = mfs[0]["id"]; save(app, "moviefile_one", try_(lambda: A(f"moviefile/{fid}"), "moviefile/id"), "moviefile/{id}")
    for name, p in (("tag", "tag"), ("rootfolder", "rootfolder"), ("queue", "queue?pageSize=5"), ("command", "command"), ("notification", "notification"), ("blocklist", "blocklist?pageSize=5"), ("system_status", "system/status"),
                    ("history_since", f"history/since?date={(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')}")):
        r = try_(lambda p=p: A(p), p)
        if r is not None: save(app, name, r, p.split("?")[0])
if audit.bazarr_conf()[1]:
    B = audit.bazarr_call; print("bazarr")
    sid = None
    try: sid = next(iter(apps.get("sonarr") and audit.api(apps["sonarr"][0], apps["sonarr"][1], "series") or []), {}).get("id")
    except Exception: pass
    for name, p in (("episodes_wanted", "episodes/wanted?length=5"), ("movies_wanted", "movies/wanted?length=5"), ("system_status", "system/status"), ("providers", "providers"), ("languages_profiles", "system/languages/profiles")):
        r = try_(lambda p=p: B(p), p)
        if r is not None: save("bazarr", name, r, p.split("?")[0])
    if sid:
        eps = try_(lambda: B(f"episodes?seriesid[]={sid}"), "episodes") or {}; save("bazarr", "episodes", eps, "episodes?seriesid[]=")
        rows = (eps or {}).get("data") or []; with_sub = next((e for e in rows if e.get("subtitles")), rows[0] if rows else None)
        if with_sub:
            eid = with_sub["sonarrEpisodeId"]
            for name, p in (("episodes_history", f"episodes/history?episodeid={eid}&start=0&length=20"), ("providers_episodes", f"providers/episodes?episodeid={eid}")):
                r = try_(lambda p=p: B(p, timeout=120), p)
                if r is not None: save("bazarr", name, r, p.split("?")[0])
    if "radarr" in apps:
        try:
            mv = next((m for m in audit.api(apps["radarr"][0], apps["radarr"][1], "movie") if m.get("hasFile")), None)
            if mv:
                r = try_(lambda: B(f"movies?radarrid[]={mv['id']}"), "movies"); r is not None and save("bazarr", "movies", r, "movies?radarrid[]=")
                r = try_(lambda: B(f"movies/history?radarrid={mv['id']}&start=0&length=20"), "movies/history"); r is not None and save("bazarr", "movies_history", r, "movies/history")
        except Exception as e: print("  skip bazarr movies:", repr(e))
if audit.plex_token():
    P = audit.plex_get; print("plex")
    for name, p in (("identity", "/identity"), ("sections", "/library/sections"), ("onDeck", "/library/onDeck"), ("sessions", "/status/sessions")):
        r = try_(lambda p=p: P(p, timeout=30), p)
        if r is not None: save("plex", name, r, p)
    secs = try_(lambda: P("/library/sections", timeout=30), "sections") or {}
    for s in (secs.get("MediaContainer") or {}).get("Directory") or []:
        if s.get("type") in ("show", "movie"):
            key_ = s["key"]; allr = try_(lambda: P(f"/library/sections/{key_}/all", timeout=60), "all") or {}
            mc = allr.get("MediaContainer") or {}; mc["Metadata"] = (mc.get("Metadata") or [])[:3]; save("plex", f"section_{s['type']}_all", {"MediaContainer": mc}, "/library/sections/{key}/all")
            first = (mc.get("Metadata") or [None])[0]
            if first:
                rk = first["ratingKey"]; save("plex", f"metadata_{s['type']}", try_(lambda: P(f"/library/metadata/{rk}", timeout=30), "metadata"), "/library/metadata/{rk}")
                if s["type"] == "show": save("plex", "metadata_show_children", try_(lambda: P(f"/library/metadata/{rk}/children", timeout=30), "children"), "/library/metadata/{rk}/children")
json.dump({"recorded": datetime.datetime.now().isoformat(timespec="minutes"), "placeholders": {k: len(v) for k, v in counters.items()}}, open(os.path.join(OUT, "MANIFEST.json"), "w"), indent=1)
print("done:", OUT)
