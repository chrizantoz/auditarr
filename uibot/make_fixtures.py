"""Refresh the UI-bot fixtures from a running Auditarr and sanitize them: show/movie names, file names, hosts and IPs are replaced
deterministically, structure and every field the page reads are kept. Safe to commit to a public repository.
Usage: python uibot/make_fixtures.py http://<auditarr-host>:3091 [out dir = uibot/fixtures]"""
import hashlib, json, os, re, sys, urllib.request

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3091").rstrip("/")
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(out, exist_ok=True)
def get(p):
    with urllib.request.urlopen(f"{base}/api/{p}", timeout=120) as r: return json.load(r)
data = {n: get(n) for n in ("state", "history", "settings", "trash")}

SHOW_RE = re.compile(r"^(.*?)(?: - )?(?:\s+S\d{1,2}E\d{1,3}.*|\s*\(\d{4}\).*)$")
def show_of(title):
    m = SHOW_RE.match(title or ""); s = (m.group(1) if m else title or "").strip(" -")
    return s
names = set()
for f in data["state"].get("findings", []): names.add(show_of(f.get("title", "")))
for r in data["state"].get("regrabs", []): names.add(show_of(r.get("label", "")))
for k in ("watched_cold", "never_watched"):
    for r in (data["state"].get("space") or {}).get(k, []): names.add(r.get("title", ""))
for a in data["state"].get("actions", []): names.add(show_of(a.get("title", "")))
names = sorted({n for n in names if len(n) >= 3}, key=len, reverse=True)
fake = {n: f"Show {int(hashlib.sha1(n.encode()).hexdigest()[:6], 16) % 900 + 100}" for n in names}
IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"); EPTITLE = re.compile(r"( - S\d{1,2}E\d{1,3}(?:-E\d{1,3})? - )(?:\d+ - )?([^\[\]/]+?)( \[|\.\w{2,4}$|$)")
def nopunct(n): return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", n)).strip()
def slug(n): return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")
PATTERNS = []
for n in names:
    for v, rep in ((n, fake[n]), (n.replace(": ", " - "), fake[n]), (n.replace(":", " -"), fake[n]), (n.replace(":", ""), fake[n]), (nopunct(n), fake[n]), (slug(n), slug(fake[n]))):
        if len(v) >= 4: PATTERNS.append((re.compile(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", re.I), rep))
PATTERNS.sort(key=lambda pr: -len(pr[0].pattern))
def clean(s):
    if not isinstance(s, str): return s
    for pat, rep in PATTERNS: s = pat.sub(rep, s)
    s = IP.sub("10.0.0.1", s); s = EPTITLE.sub(lambda m: m.group(1) + "Episode" + m.group(3), s)
    s = re.sub(r"server/[0-9a-f]{16,}", "server/0000000000000000", s); s = re.sub(r"https?://[\w.-]+(:\d+)?", lambda m: "http://example.local" + (m.group(1) or ""), s)
    return s
def walk(o):
    if isinstance(o, dict): return {clean(k): walk(v) for k, v in o.items()}
    if isinstance(o, list): return [walk(x) for x in o]
    return clean(o)
san = walk(data)
san["state"]["ignore_rules"] = "# one rule per line: <finding id> or CHECK_CODE: ^Show title\nTRUEHD_ONLY: \nINTERLACED: \n"
for n, d in san.items(): json.dump(d, open(os.path.join(out, n + ".json"), "w"), indent=0)
blob = json.dumps(san).lower(); left = [n for n in names if n.lower() in blob or nopunct(n).lower() in blob]
print(f"fixtures written to {out}: {len(san['state'].get('findings', []))} findings, {len(names)} titles replaced" + (f"; WARNING still present: {left[:5]}" if left else ""))
