"""Q24: the fake stack answers in the shape of the real services, and the recorded shapes carry nothing private.
tests/shapes/ holds sanitised real answers (scripts/record_shapes.py). Run: python tests/shapes_check.py   Exit 1 on failure."""
import os, sys, json, re, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.join(ROOT, "tests"))
import mock_stack  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:220]}]"))
SH = mock_stack.SHAPES
files = sorted(os.path.join(r, f) for r, _, fs in os.walk(SH) for f in fs if f.endswith(".json") and f != "MANIFEST.json")
check("shapes are recorded", len(files) >= 40, len(files))
# ---- privacy: nothing that smells like a host, a person, a key or a real title may be in the repository
PRIVATE = re.compile(r"\b(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(\.\d{1,3})?\b|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|\.ts\.net|\.local\b|chrizantoz|shinra|capuya|[0-9a-f]{32}|Bearer ", re.I)
ALLOWED = re.compile(r"^(\d{4}-\d\d-\d\d.*|[\d.,:+-]+|\d+(\.\d+)*(-[A-Za-z0-9]+)?|true|false|REDACTED|[A-Za-z_][A-Za-z_0-9]* \d+|/data/.*|/(library|status|system|photo)/.*|[a-z0-9]+://.*|[A-Za-z][A-Za-z0-9_+.-]{0,24}|[A-Za-z0-9_-]+/[A-Za-z0-9_.+-]+|\d+x\d+|[a-z]{2,3}(/[a-z]{2,3})*|EAC3 Atmos|)$")
leaks, odd = [], set()
def walk(v, where):
    if isinstance(v, dict): [walk(x, where) for x in v.values()]
    elif isinstance(v, list): [walk(x, where) for x in v]
    elif isinstance(v, str):
        if PRIVATE.search(v): leaks.append((where, v[:80]))
        if not ALLOWED.match(v) and not re.fullmatch(r"[a-z]{2,3}(/[a-z]{2,3}){0,60}", v): odd.add(v[:60])
for f in files: walk(json.load(open(f))["body"], os.path.relpath(f, SH))
check("no address, host, name, e-mail or key in the recorded shapes", not leaks, leaks[:3])
check("every string is a placeholder, an enum, a date, a number or a path shape", not odd, sorted(odd)[:6])
# ---- the fake stack carries the real fields
import tempfile
T = tempfile.mkdtemp(prefix="auditarr-shapes-"); W = mock_stack.default_world(os.path.join(T, "tv"), os.path.join(T, "movies"))
for f in W["files"] + W["mfiles"]: os.makedirs(os.path.dirname(f["path"]), exist_ok=True); open(f["path"], "wb").close()   # the fake only lists files that exist
srv = mock_stack.start(W); base = f"http://127.0.0.1:{srv.server_address[1]}"
def get(path):
    req = urllib.request.Request(base + path, headers={"X-Api-Key": mock_stack.KEY})
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())
def keys_of(x): return set((x[0] if isinstance(x, list) and x else x).keys()) if isinstance(x, (list, dict)) else set()
for app, name, path in (("sonarr", "sonarr/series", "/sonarr/api/v3/series"), ("sonarr", "sonarr/episode", "/sonarr/api/v3/episode?seriesId=1"), ("sonarr", "sonarr/episodefile", "/sonarr/api/v3/episodefile?seriesId=1"),
                        ("radarr", "radarr/movie", "/radarr/api/v3/movie"), ("radarr", "radarr/moviefile", "/radarr/api/v3/moviefile?movieId=1")):
    real = keys_of(mock_stack.shape(name)); fake = keys_of(get(path)); missing = real - fake - mock_stack.DROP.get(name, set())
    check(f"fake {name} has every field the real one has", real and not missing, sorted(missing)[:8])
ef = get("/sonarr/api/v3/episodefile?seriesId=1")[0]; check("our test values still win over the sample (path, size, quality)", ef["size"] == 0 and ef["quality"]["quality"]["name"] == "Unknown" and "Alpha Show" in ef["path"], (ef["size"], ef["path"][-40:]))
check("real-only fields ride along with real-looking values", isinstance(ef.get("releaseType"), str) and isinstance(ef.get("customFormats"), list), (ef.get("releaseType"), ef.get("customFormats")))
srv.shutdown(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
