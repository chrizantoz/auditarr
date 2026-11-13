#!/usr/bin/env python3
"""media-audit v2: finds library problems Healarr does not look for, and (optionally, gated) fixes them.

Sources: Sonarr / Radarr / Whisparr v3 APIs, ffprobe (incremental header read), sidecar subtitle files,
Bazarr wanted list, a walk of the arr root folders for orphans.

Write actions, each independently gated (off|dryrun|on):
  ARR_RESCAN   (default on)  ask the arr to rescan ONE series/movie whose stored media info is stale. Harmless.
  REMEDIATE    (default off) delete a bad file and ask the arr to search again. Also needs REMEDIATE_CONFIRM.
  PAPERCLIP    (default on if token mounted) file one issue per run with the new high findings for the media agent.

Checks (code -> meaning):
  MISSING_FILE, ZERO_SIZE, CORRUPT_HEADER, NO_AUDIO, NO_VIDEO
  WRONG_LANGUAGE    audio is neither the title's original language nor a preferred one
  DUB_NOT_WANTED    title tagged audit-lang-xxx in the arr but file has no xxx audio
  NO_SUBS           no preferred audio AND no preferred subtitle (embedded or sidecar)
  UND_AUDIO_NO_SUBS audio untagged and no subtitles anywhere (low)
  RUNTIME_SHORT / RUNTIME_LONG   vs expected runtime (multi-episode files summed)
  BAD_DURATION_HEADER  container says one length, the streams end at another (Plex shows the wrong runtime) -> 'fix header' remux
  RUNTIME_DISAGREE  ffprobe vs arr media info (stale scan)  -> triggers ARR_RESCAN
  RES_MISLABELED    picture smaller than the quality tag says
  AUDIO_OFFSET      audio start_time >0.3s from video
  SAMPLE_FILE       tiny file where a full episode/movie is expected
  TITLE_MISMATCH    embedded title tag looks like a different episode/movie (low)
  DV_PROFILE5       Dolby Vision profile 5, no HDR10 fallback -> green/purple on non-DV players
  DV_PROFILE7       Dolby Vision profile 7 (dual layer) -> most players fall back to HDR10 (low)
  HI10P             10-bit H.264 -> no hardware decode, Plex software-transcodes
  AV1               AV1 video (low)
  INTERLACED        interlaced video (low)
  MP4_IN_MKV        an MP4 file with a .mkv name: default tracks cannot be set, some players misread it -> 'Rewrap the file' (low)
  TRUEHD_ONLY       only TrueHD/DTS-HD audio, no lossy fallback track (low)
  DUB_ONLY          anime whose file has no Japanese / original-language audio track (dub-only release)
  MISSING_EPISODE / MISSING_MOVIE   monitored, released >= MISSING_MIN_DAYS ago, no file (medium within a year, low if older)
  ORPHAN_FILE       video file on disk that no arr knows about
  DUPLICATE_VIDEO   orphan whose SxxEyy matches a managed file in the same folder
  PLEX_MISSING      the arr has the file but Plex never indexed it (folder refresh is triggered automatically)

v2.3 additions: Plex folder refresh after every action, Plex-vs-arr reconciliation, tracked re-grabs (verify the replacement,
auto-restore the original after REGRAB_WAIT_DAYS), Bazarr subtitle search button, UI-saved settings (kv), run history, /health 503 when stale.
"""
import os, re, sys, json, time, sqlite3, hashlib, subprocess, threading, datetime, argparse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

E = os.environ.get
CONFIG_DIR   = E("CONFIG_DIR", "/config"); REPORT_DIR = os.path.join(CONFIG_DIR, "reports")
DB_PATH      = os.path.join(CONFIG_DIR, "state.db"); IGNORE_FILE = os.path.join(CONFIG_DIR, "ignore.txt")
ARR_DIR      = E("ARR_CONFIG_DIR", "/arr")
HOST_IP      = E("HOST_IP", "")   # the docker host where the arrs answer; blank = every address must be given explicitly (Setup tab or *_URL env)
PREF_AUDIO   = set(E("PREFERRED_AUDIO", "eng").split(",")); PREF_SUBS = set(E("PREFERRED_SUBS", "eng").split(","))
PROBE_WORKERS= int(E("PROBE_WORKERS", "4")); PROBE_MAX = int(E("PROBE_MAX", "20000")); PROBE_VERSION = 2
RUN_AT       = E("RUN_AT", "05:00"); HTTP_PORT = int(E("HTTP_PORT", "3091"))
NTFY_URL     = E("NTFY_URL", ""); NTFY_TOKEN_FILE = E("NTFY_TOKEN_FILE", ""); NTFY_MIN_SEV = E("NTFY_MIN_SEVERITY", "medium")
REPORT_URL   = E("REPORT_URL", f"http://{HOST_IP}:{HTTP_PORT}/latest.html")
SKIP_RUNTIME_APPS = set(E("SKIP_RUNTIME_APPS", "whisparr,sportarr").split(",")); SKIP_LANG_APPS = set(E("SKIP_LANG_APPS", "whisparr,sportarr").split(","))   # sports broadcasts carry no subtitles and rarely a language tag; the first Sportarr audit raised 36 language findings and would have queued AI subtitles for each   # sports events have no reliable expected runtime
SKIP_TITLE_APPS = set(E("SKIP_TITLE_APPS", "whisparr,sportarr").split(","))
SKIP_MISSING_APPS = set(E("SKIP_MISSING_APPS", "sportarr").split(","))   # a sports guide lists every game; "not downloaded" there is a choice, not a gap   # scene releases there carry site/ad titles
PLAYER_CHECKS = E("PLAYER_CHECKS", "1") == "1"; ORPHAN_SCAN = E("ORPHAN_SCAN", "1") == "1"
# --- write gates ---
ARR_RESCAN   = E("ARR_RESCAN", "on"); ARR_RESCAN_MAX = int(E("ARR_RESCAN_MAX", "40")); RESCAN_COOLDOWN_DAYS = 7
REMEDIATE    = E("REMEDIATE", "off"); REMEDIATE_CONFIRM = E("REMEDIATE_CONFIRM", "")
READ_ONLY    = E("READ_ONLY", "0") == "1"; WRITE_LEDGER = []   # staging: every attempt to change an arr, Bazarr, Plex or a media file is refused and counted
class ReadOnly(RuntimeError): pass
def guard_write(what):
    """Hard stop in READ_ONLY mode - independent of the per-feature gates, so a new nightly feature cannot leak writes from staging."""
    if not READ_ONLY: return
    WRITE_LEDGER.append((time.time(), str(what)[:160])); log(f"READ_ONLY: refused {what}")
    raise ReadOnly(f"read-only mode: {what}")
INFLIGHT = {"n": 0}   # UI actions being executed right now (a deploy waits for 0 so a remux is never killed half-way)
DEPS = {}   # service -> {"ok": bool, "error": str, "ts": float}; filled during a run, shown in /health and the Today panel
def note_flake(service, what, error):
    """Q21 flakiness ledger: a call that failed and then worked, or a service that was down and came back. Shown on the History tab."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=30); c.execute("CREATE TABLE IF NOT EXISTS flake(ts REAL, service TEXT, what TEXT, error TEXT)")
        c.execute("INSERT INTO flake VALUES(?,?,?,?)", (time.time(), str(service)[:60], str(what)[:120], str(error)[:160])); c.execute("DELETE FROM flake WHERE ts < ?", (time.time() - 30 * 86400,)); c.commit(); c.close()
    except Exception as e: log("flake ledger:", repr(e))
def dep_status(name, ok, error=""):
    was = DEPS.get(name)
    DEPS[name] = {"ok": bool(ok), "error": str(error)[:160], "ts": time.time()}
    if not ok: log(f"DEPENDENCY DOWN: {name} - {str(error)[:120]}")
    elif was and not was["ok"]: note_flake(name, "was unreachable, answers again", was.get("error", ""))
def degraded(): return sorted(k for k, v in DEPS.items() if not v["ok"])
TRUST_PROXY_AUTH = E("TRUST_PROXY_AUTH", "0") == "1"   # 1 = a Remote-User header from your reverse proxy (Authelia etc.) counts as a login
AUTH = {"loaded": False, "conf": None}
LOCAL_NO_LOGIN_ENV = E("LOCAL_NO_LOGIN", "")   # 1 = no sign-in for a browser on your own network that talks to the port directly; 0 = always sign in; empty = the Setup checkbox decides
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded", "CF-Connecting-IP", "CF-Ray", "True-Client-IP", "Via", "Tailscale-Funnel-Request")
def local_no_login(conn=None):
    """Is 'no sign-in on my own network' on? The env wins; otherwise the Setup checkbox (kv local_no_login)."""
    if LOCAL_NO_LOGIN_ENV in ("0", "1"): return LOCAL_NO_LOGIN_ENV == "1"
    c = conn or db()
    try: r = c.execute("SELECT v FROM kv WHERE k='local_no_login'").fetchone(); return bool(r and r[0] == "1")
    finally:
        if conn is None: c.close()
def direct_local(ip, headers):
    """A browser on your own network talking to the port itself: a private or tailnet address AND no header any reverse proxy, tunnel
    or CDN adds. Through a domain (NPM, Traefik, Caddy, Cloudflare, Tailscale Funnel) one of those headers is always there, so the
    domain keeps asking for the sign-in even though the proxy itself sits on a private address."""
    import ipaddress
    if any(headers.get(h) for h in PROXY_HEADERS): return False
    try: a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError: return False
    if getattr(a, "ipv4_mapped", None): a = a.ipv4_mapped
    return a.is_private or a.is_loopback or (a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"))
def auth_conf(conn=None):
    """{"user","salt","hash","secret"} once a password is set (Setup page or AUTH_USER/AUTH_PASSWORD env); None = open page (LAN model)."""
    if AUTH["loaded"]: return AUTH["conf"]
    c = conn or db()
    try:
        r = c.execute("SELECT v FROM kv WHERE k='auth'").fetchone(); conf = json.loads(r[0]) if r else None
        if conf is None and E("AUTH_USER") and E("AUTH_PASSWORD"): conf = save_account(c, E("AUTH_USER"), E("AUTH_PASSWORD"), persist=True)
    finally:
        if conn is None: c.close()
    AUTH["loaded"] = True; AUTH["conf"] = conf; return conf
def save_account(conn, user, password, persist=True, role="admin"):
    """Store an account (name, salt, password hash) and return the auth record. Named for what it returns: the record, never the password.
    role 'admin' sets the main account; role 'viewer' sets (or with an empty password removes) a read-only account (F9)."""
    import secrets
    old = AUTH.get("conf") or {}
    if role == "viewer":
        if not old: raise ValueError("set the main password first")
        conf = dict(old)
        if not password: conf.pop("viewer", None)
        else:
            if len(password) < 8: raise ValueError("password must be at least 8 characters")
            if (user or "viewer").strip()[:64] == old.get("user"): raise ValueError("the read-only account needs a different name from the main account")
            salt = secrets.token_hex(16); conf["viewer"] = {"user": (user or "viewer").strip()[:64], "salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()}
    else:
        if len(password or "") < 8: raise ValueError("password must be at least 8 characters")
        if old.get("viewer") and (user or "admin").strip()[:64] == old["viewer"].get("user"): raise ValueError("the main account needs a different name from the read-only account")
        salt = secrets.token_hex(16); h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
        conf = {"user": (user or "admin").strip()[:64], "salt": salt, "hash": h, "secret": old.get("secret") or secrets.token_hex(32), **({"viewer": old["viewer"]} if old.get("viewer") else {})}
    if persist: conn.execute("INSERT OR REPLACE INTO kv VALUES('auth',?)", (json.dumps(conf),)); conn.commit()
    AUTH["loaded"] = True; AUTH["conf"] = conf; return conf
def account_role(conf, user):
    """Which account a username names: 'admin', 'viewer' or None. Decided by the name alone, never by the password."""
    if not conf or not user: return None
    if user == conf.get("user"): return "admin"
    if user == (conf.get("viewer") or {}).get("user"): return "viewer"
    return None
def check_password(conf, user, password):
    """-> 'admin', 'viewer' or None."""
    import hmac
    for role, c in (("admin", conf), ("viewer", conf.get("viewer") or None)):
        if not c: continue
        h = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), c["salt"].encode(), 200_000).hex()
        if user == c["user"] and hmac.compare_digest(h, c["hash"]): return role
    return None
def session_cookie(conf, days=30, role="admin"):
    import hmac; exp = int(time.time()) + days * 86400; user = conf["user"] if role == "admin" else (conf.get("viewer") or {}).get("user", "viewer"); msg = f"{user}|{exp}|{role}"
    return f"{msg}|{hmac.new(conf['secret'].encode(), msg.encode(), 'sha256').hexdigest()}"
def session_user(conf, cookie_header):
    """-> (user, role) or None."""
    import hmac
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k != "auditarr_s" or v.count("|") != 3: continue
        user, exp, role, sig = v.split("|")
        if not hmac.compare_digest(sig, hmac.new(conf["secret"].encode(), f"{user}|{exp}|{role}".encode(), "sha256").hexdigest()) or int(exp) <= time.time(): continue
        if role == "admin" and user == conf["user"]: return user, "admin"
        if role == "viewer" and user == (conf.get("viewer") or {}).get("user"): return user, "viewer"
    return None
LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Auditarr - sign in</title>
<style>:root{--bg:#0e1412;--panel:#151d1a;--line:#243029;--fg:#e1e9e4;--mut:#8fa39a;--acc:#3dd6b4;--acc-ink:#062a22}@media (prefers-color-scheme: light){:root{--bg:#f3f6f4;--panel:#fff;--line:#d5e0da;--fg:#16211c;--mut:#4f6159;--acc:#0a6b56;--acc-ink:#fff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:26px 28px;width:min(360px,92vw)}
h1{font-size:20px;margin:0 0 14px;display:flex;gap:8px;align-items:center}h1 em{font-style:normal;color:var(--acc)}label{display:block;color:var(--mut);font-size:12px;margin:10px 0 3px}input{width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--line);color:var(--fg);padding:9px 10px;border-radius:8px;font-size:15px}
button{margin-top:16px;width:100%;background:var(--acc);color:var(--acc-ink);border:0;border-radius:8px;padding:10px;font-size:15px;font-weight:600;cursor:pointer}.err{color:#ff6f6f;margin:8px 0 0;font-size:13px}p.hint{color:var(--mut);font-size:12px;margin:12px 0 0}</style></head>
<body><form method="post" action="/login"><h1>&#128269; Audit<em>arr</em></h1><label>User</label><input name="user" autocomplete="username" autofocus><label>Password</label><input name="password" type="password" autocomplete="current-password">__ERR__<button>Sign in</button>
<p class="hint">Forgot it? Set a new one from the Docker host: <code>AUTH_USER</code> / <code>AUTH_PASSWORD</code> env plus <code>AUTH_RESET=1</code> once.</p></form></body></html>"""
def ledger_only(what):
    """Soft variant for best-effort side effects (pushes, Plex refresh): count it and let the caller return quietly."""
    if READ_ONLY: WRITE_LEDGER.append((time.time(), str(what)[:160])); log(f"READ_ONLY: skipped {what}")
    return READ_ONLY
REMEDIATE_CHECKS = set(E("REMEDIATE_CHECKS", "WRONG_LANGUAGE,RUNTIME_SHORT").split(","))
REMEDIATE_APPS = set(E("REMEDIATE_APPS", "sonarr,radarr").split(",")); REMEDIATE_MAX = int(E("REMEDIATE_MAX", "5"))
REMEDIATE_MIN_AGE_H = float(E("REMEDIATE_MIN_AGE_HOURS", "20")); REMEDIATE_FILE_MIN_AGE_H = float(E("REMEDIATE_FILE_MIN_AGE_HOURS", "24"))
PAPERCLIP    = E("PAPERCLIP", "off"); PAPERCLIP_URL = E("PAPERCLIP_URL", "http://paperclip:3100"); PAPERCLIP_TOKEN_FILE = E("PAPERCLIP_TOKEN_FILE", "/run/secrets/paperclip")
PAPERCLIP_COMPANY = E("PAPERCLIP_COMPANY", ""); PAPERCLIP_AGENT = E("PAPERCLIP_AGENT", ""); PAPERCLIP_MIN_SEV = E("PAPERCLIP_MIN_SEVERITY", "high")
PAPERCLIP_MAX_PER_DAY = 1
TRASH_DIR    = E("TRASH_DIR", "/data/media/.media-audit-trash"); TRASH_DAYS = int(E("TRASH_DAYS", "14"))   # re-grabbed files
TRASH_DAYS_DEDUPE = int(E("TRASH_DAYS_DEDUPE", "3"))                                                        # duplicates: the managed twin still exists
TRASH_DAYS_ARCHIVE = int(E("TRASH_DAYS_ARCHIVE", "30"))                                                      # archived (watched & cold) seasons
TRASH_KEEP = {"dedupe": TRASH_DAYS_DEDUPE, "regrab": TRASH_DAYS, "archive": TRASH_DAYS_ARCHIVE}
TAUTULLI_URL = E("TAUTULLI_URL", f"http://{HOST_IP}:8181" if HOST_IP else ""); TAUTULLI_INI = E("TAUTULLI_INI", os.path.join(ARR_DIR, "tautulli.ini"))
DEDUPE       = E("DEDUPE", "off"); DEDUPE_MAX = int(E("DEDUPE_MAX", "50")); DEDUPE_MIN_RUNS = int(E("DEDUPE_MIN_RUNS", "2"))
REMEDIATE_MIN_RUNS = int(E("REMEDIATE_MIN_RUNS", "2"))
# --- Plex ---
PLEX_URL = E("PLEX_URL", f"http://{HOST_IP}:32400" if HOST_IP else ""); PLEX_TOKEN_FILE = E("PLEX_TOKEN_FILE", os.path.join(ARR_DIR, "plex.xml"))
PLEX_PATH_MAP = E("PLEX_PATH_MAP", "/data/media=/data")   # audit-side prefix = plex-side prefix, comma-separated pairs
PLEX_REFRESH = E("PLEX_REFRESH", "on"); PLEX_CHECK = E("PLEX_CHECK", "on"); PLEX_MIN_AGE_H = float(E("PLEX_MIN_AGE_HOURS", "12")); PLEX_REFRESH_MAX = int(E("PLEX_REFRESH_MAX", "25"))
REGRAB_WAIT_DAYS = float(E("REGRAB_WAIT_DAYS", "4")); STALE_HOURS = float(E("STALE_HOURS", "30"))
SUBGEN_URLS = [u for u in E("SUBGEN_URLS", "http://whisper-router:9000,http://subgen-verify:9000,http://subgen:9000").split(",") if u]   # router = desktop GPU first, shinra CPU as backup
WHISPER_GPU_URL = E("WHISPER_GPU_URL", ""); WHISPER_GPU_NAME = E("WHISPER_GPU_NAME", "GPU transcriber"); WHISPER_CPU_NAME = E("WHISPER_CPU_NAME", "CPU transcriber")   # optional: a faster-whisper/subgen endpoint on a GPU box
ENGINE_NAMES = {"subgen": WHISPER_CPU_NAME, "subgen-verify": "CPU · content-check clips", "subgen-gpu (desktop)": WHISPER_GPU_NAME}
DOCKER_API = E("DOCKER_API", "http://dockersocket:2375")                      # wollomatic socket-proxy: GET /containers/(subgen|subgen-verify)/logs only
WHISPER_CONTAINERS = [c for c in E("WHISPER_CONTAINERS", "subgen,subgen-verify").split(",") if c]   # Whisper endpoints for 'verify content' (first reachable wins; the -verify one is a small dedicated instance)
PLEX_WEB = E("PLEX_WEB", "https://app.plex.tv/desktop/#!/server/{mid}/details?key=%2Flibrary%2Fmetadata%2F{rk}")   # or http://HOST:32400/web/index.html#!/server/{mid}/details?key=...
ARR_APPS = (("sonarr", 8989, "series"), ("radarr", 7878, "movie"), ("whisparr", 6969, "series"), ("sportarr", 1867, "series"))   # every arr-shaped app: name, default port, what it manages (Sportarr is a Sonarr build for sports)
ARR_KINDS = tuple(a[0] for a in ARR_APPS); OPTIONAL_ARRS = {"whisparr", "sportarr"}
ARR_LINK = {a: E(f"{a.upper()}_LINK_BASE") for a in ARR_KINDS}   # public URLs for the arr links, default = the API base
ALLOW_RUN_OVERRIDES = E("ALLOW_RUN_OVERRIDES", "1") == "1"   # GET /run?DEDUPE=on&DEDUPE_MAX=3 for one supervised run
OVERRIDABLE = {"REMEDIATE_CHECKS","DEDUPE","DEDUPE_MAX","DEDUPE_MIN_RUNS","REMEDIATE","REMEDIATE_MAX","REMEDIATE_MIN_RUNS","REMEDIATE_MIN_AGE_HOURS","REMEDIATE_FILE_MIN_AGE_HOURS","ARR_RESCAN","PAPERCLIP"}
# settings the UI may change persistently (stored in the kv table). Precedence: per-run /run?KEY= override > UI-saved > container env > default.
SETTABLE = {   # env name: (kind, default, help)
 "SONARR_URL": ("url", "", "Sonarr address, e.g. http://192.168.1.10:8989 (blank = SONARR_URL env or http://HOST_IP:8989)"), "SONARR_APIKEY": ("secret", "", "Sonarr API key (Settings > General); blank = env or the mounted config.xml"),
 "RADARR_URL": ("url", "", "Radarr address, e.g. http://192.168.1.10:7878"), "RADARR_APIKEY": ("secret", "", "Radarr API key; blank = env or the mounted config.xml"),
 "WHISPARR_URL": ("url", "", "Whisparr address (optional)"), "WHISPARR_APIKEY": ("secret", "", "Whisparr API key (optional)"),
 "SPORTARR_URL": ("url", "", "Sportarr address (optional)"), "SPORTARR_APIKEY": ("secret", "", "Sportarr API key (optional)"),
 "BAZARR_URL": ("url", "", "Bazarr address, e.g. http://192.168.1.10:6767"), "BAZARR_APIKEY": ("secret", "", "Bazarr API key (Settings > General); blank = env or the mounted config.yaml"),
 "PLEX_URL": ("url", "", "Plex address, e.g. http://192.168.1.10:32400"), "PLEX_TOKEN": ("secret", "", "Plex token (X-Plex-Token); blank = env or the mounted Preferences.xml"),
 "DEVICES": ("csv", "", "players you own, comma list of keys (ps5, shield, lg-tv, ...): compatibility notes then name the affected player or disappear; blank = generic notes"),
 "DEDUPE": ("mode", "off", "quarantine unmanaged duplicate files next to the arr-managed copy"),
 "DEDUPE_MAX": ("int", "50", "max duplicates quarantined per run"), "DEDUPE_MIN_RUNS": ("int", "2", "a duplicate must be seen this many runs first"),
 "REMEDIATE": ("mode", "off", "quarantine a bad file and have the arr search again (needs REMEDIATE_CONFIRM in the container env for 'on')"),
 "REMEDIATE_CHECKS": ("csv", "WRONG_LANGUAGE,RUNTIME_SHORT", "which high/critical checks remediation acts on"),
 "REMEDIATE_MAX": ("int", "5", "max files re-grabbed per run"), "REMEDIATE_MIN_RUNS": ("int", "2", "finding must persist this many runs"),
 "REMEDIATE_MIN_AGE_HOURS": ("num", "20", "finding must be at least this old"), "REMEDIATE_FILE_MIN_AGE_HOURS": ("num", "24", "file must have been imported at least this long ago"),
 "REGRAB_WAIT_DAYS": ("num", "4", "if nothing is imported this many days after a re-grab, restore the original from the trash"),
 "ARR_RESCAN": ("mode", "on", "ask the arr to rescan a series/movie whose media info is stale"),
 "PAPERCLIP": ("mode", "off", "file one read-only Paperclip issue per day with new high findings (needs a Paperclip instance; off by default)"), "PAPERCLIP_MIN_SEVERITY": ("sev", "high", "minimum severity handed to Paperclip"),
 "PLEX_REFRESH": ("onoff", "on", "tell Plex to rescan the folder after every file action"), "PLEX_CHECK": ("onoff", "on", "flag arr files that Plex has not indexed (PLEX_MISSING)"),
 "PLEX_MIN_AGE_HOURS": ("num", "12", "ignore files newer than this for PLEX_MISSING"),
 "MISSING_MIN_DAYS": ("int", "3", "flag monitored episodes/movies released at least this many days ago that still have no file (0 = off)"),
 "ANIME_ORIGINAL_AUDIO": ("onoff", "on", "flag anime files that have no Japanese (original-language) audio track as DUB_ONLY"),
 "AUTO_VERIFY": ("mode", "on", "nightly: run the content check on new runtime/title findings (max AUTO_VERIFY_MAX per night)"),
 "AUTO_VERIFY_MAX": ("int", "10", "max content checks queued per night"),
 "AUTO_WHISPER": ("mode", "on", "nightly: queue AI subtitles for files Bazarr has found nothing for in AUTO_WHISPER_DAYS days (NO_SUBS / UND_AUDIO_NO_SUBS; max AUTO_WHISPER_MAX per night)"),
 "AUTO_WHISPER_MAX": ("int", "10", "max AI-subtitle jobs queued per night"), "AUTO_WHISPER_DAYS": ("int", "3", "days a no-subtitles finding must stay open before AI subtitles are queued"),
 "AUTO_WHISPER_IMPORT_MINUTES": ("int", "0", "after a new download (import hook): if Bazarr still has no subtitles this many minutes later, make AI subtitles right away (0 = only the nightly AUTO_WHISPER_DAYS rule)"),
 "WATCH_PRIORITY": ("mode", "on", "every 5 minutes: whatever someone is watching in Plex right now, or has next up on their Home, gets its missing subtitles first - a Bazarr search straight away, then AI subtitles if Bazarr still has nothing WATCH_PRIORITY_MINUTES later, both ahead of every other job"),
 "WATCH_PRIORITY_MINUTES": ("int", "20", "how long Bazarr gets to find subtitles for something being watched before AI subtitles are made"),
 "AUTO_SUBSYNC": ("mode", "on", "nightly: check subtitle timing on files whose external subtitles changed in the last AUTO_SUBSYNC_DAYS days and on what people are watching - Whisper listens to three short samples and compares them with the subtitle times; out-of-sync files become findings with a Re-time button"),
 "AUTO_SUBSYNC_MAX": ("int", "6", "max subtitle timing checks per night (about a minute each on the GPU)"),
 "AUTO_AUDIO_LANG": ("mode", "on", "nightly: for files whose audio carries no language label (Unknown language rows), Whisper listens to a short sample and the audio is labelled with the language it hears - English means no subtitles are needed and the row clears; other languages get subtitles the usual way"),
 "AUTO_AUDIO_LANG_MAX": ("int", "10", "max spoken-language detections per night (about 20 seconds each on the GPU)"),
 "HARDSUB_CHECK": ("onoff", "on", "for files reported without subtitles: look at eight frames from the lower part of the picture for lettering painted into the video (burned-in subtitles); when found, the row says so instead of 'no subtitles' and offers a frame to look at"),
 "HARDSUB_MAX": ("int", "150", "max files whose frames are inspected per night (a few seconds each)"), "AUTO_SUBSYNC_DAYS": ("int", "7", "external subtitles newer than this many days get their timing checked"),
 "AUTO_ACCEPTLANG": ("mode", "on", "nightly: when every file of a season is in one language the arr did not expect, tag the title audit-lang-xxx (same as the accept-language button)"),
 "AUTO_ACCEPTLANG_MIN_FILES": ("int", "3", "a season needs at least this many files, all in the same language, before it is accepted automatically"),
 "AUTO_FIXHEADER": ("mode", "on", "nightly: repair files whose container length lies (lossless remux, original to trash; max 10 per night)"),
 "SUBTAG": ("mode", "on", "nightly: sniff untagged embedded subtitle tracks and write the language tag (mkv only, English/Japanese/Russian detected from the text)"),
 "SUBTAG_MAX": ("int", "100", "max files sub-tagged per run"),
 "TRASH_DAYS": ("int", "14", "keep re-grabbed/orphan files in the trash this many days"), "TRASH_DAYS_ARCHIVE": ("int", "30", "keep archived (watched & cold) seasons in the trash this many days"),
 "TRACK_PREFS": ("mode", "on", "nightly: set the default audio/subtitle flags in MKVs and Plex's remembered selection to what PREFER_DUBS, PREFERRED_AUDIO and PREFERRED_SUBS say"),
 "TRACK_PREFS_MAX": ("int", "1000", "max files whose MKV default flags are rewritten per night"),
 "PLEX_SELECT_MAX": ("int", "2000", "max Plex items whose selected audio/subtitle stream is set per night"),
 "WATCH_STATS": ("onoff", "on", "nightly: combine Tautulli plays, Plex watched counts and file sizes into the Space tab"),
 "COLD_DAYS": ("int", "180", "a fully watched season counts as cold when nobody played it for this many days"),
 "NEVER_WATCHED_DAYS": ("int", "365", "never-played titles are listed once they are this old"),
 "SPACE_MIN_GB": ("num", "3", "ignore seasons/movies smaller than this in the Space tab"), "TRASH_DAYS_DEDUPE": ("int", "3", "keep duplicates in the trash this many days"),
 "PREFERRED_AUDIO": ("csv", "eng", "your language(s) for audio, ISO 639-2 (first one = the one the night picks when PREFER_DUBS is on)"), "PREFERRED_SUBS": ("csv", "eng", "your language(s) for subtitles, ISO 639-2 (first one = the one the night picks)"),
 "PREFER_DUBS": ("onoff", "off", "how the night picks the default audio: off = every title in its original language with subtitles in your language (anime in Japanese, Korean drama in Korean); on = dubbed in your language whenever the file has it, original language only when it does not"),
 "NTFY_MIN_SEVERITY": ("sev", "medium", "push a notification for new findings at or above this severity"),
 "STALE_HOURS": ("num", "30", "/health reports 503 when the last run is older than this"),
}
_NAME_MAP = {"REMEDIATE_MIN_AGE_HOURS": "REMEDIATE_MIN_AGE_H", "REMEDIATE_FILE_MIN_AGE_HOURS": "REMEDIATE_FILE_MIN_AGE_H", "NTFY_MIN_SEVERITY": "NTFY_MIN_SEV",
             "PAPERCLIP_MIN_SEVERITY": "PAPERCLIP_MIN_SEV", "PLEX_MIN_AGE_HOURS": "PLEX_MIN_AGE_H"}
SETTINGS = {}   # UI-saved overrides (kv 'settings')
OVR = {}        # per-run overrides, set by /run?KEY=VAL, cleared after the run
def setting(name):
    """Effective raw string value of a settable knob."""
    if name in OVR: return str(OVR[name])
    if name in SETTINGS: return str(SETTINGS[name])
    v = E(name); return v if v is not None else SETTABLE[name][1]
def validate_setting(name, value):
    kind = SETTABLE[name][0]; value = str(value).strip()
    if kind == "mode" and value not in ("off", "dryrun", "on"): raise ValueError(f"{name}: must be off, dryrun or on")
    if kind == "onoff" and value not in ("off", "on"): raise ValueError(f"{name}: must be on or off")
    if kind == "sev" and value not in SEV_RANK: raise ValueError(f"{name}: must be low, medium, high or critical")
    if kind == "int":
        if not re.fullmatch(r"\d+", value): raise ValueError(f"{name}: must be a whole number")
    if kind == "num":
        try: float(value)
        except ValueError: raise ValueError(f"{name}: must be a number")
    if kind == "csv": value = ",".join(p.strip() for p in value.split(",") if p.strip()) or SETTABLE[name][1]
    if name == "DEVICES":
        unknown = [d for d in value.split(",") if d and d not in DEVICE_PROFILES]
        if unknown: raise ValueError(f"DEVICES: unknown player key(s) {unknown}; choose from {', '.join(DEVICE_PROFILES)}")
    if kind == "url" and value and not re.match(r"^https?://[^\s/]+", value): raise ValueError(f"{name}: must start with http:// or https:// and name a host")
    if kind == "url": value = value.rstrip("/")
    return value
def refresh_globals():
    """Re-derive the module globals the checks/actions read from the effective settings."""
    g = globals()
    for name, (kind, default, _) in SETTABLE.items():
        v = setting(name); tgt = _NAME_MAP.get(name, name)
        if name in ("PREFERRED_AUDIO", "PREFERRED_SUBS"): g["PREF_AUDIO" if name == "PREFERRED_AUDIO" else "PREF_SUBS"] = set(v.split(","))
        elif kind == "csv": g[tgt] = set(v.split(","))
        elif kind == "int": g[tgt] = int(v)
        elif kind == "num": g[tgt] = float(v)
        elif kind in ("url", "secret"):
            if v: g[tgt] = v   # saved connection details override the env; blank keeps the env / mounted-file fallback
        else: g[tgt] = v
    g["TRASH_KEEP"] = {"dedupe": g["TRASH_DAYS_DEDUPE"], "regrab": g["TRASH_DAYS"], "archive": g["TRASH_DAYS_ARCHIVE"]}
def load_settings(conn):
    row = conn.execute("SELECT v FROM kv WHERE k='settings'").fetchone(); SETTINGS.clear()
    try: SETTINGS.update(json.loads(row[0]) if row else {})
    except Exception as e: log("settings: bad json in kv, ignoring", e)
    refresh_globals()
def cfg(name, default):
    v = OVR.get(name, globals().get(name, default)); return v

SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# languages a release is realistically dubbed into; anything else on a single track is almost always a mis-tag by the release group
DUB_LANGS = {"eng","ger","fre","spa","por","ita","rus","ara","hin","chi","kor","jpn","fil","tha","vie","ind","tur","pol","dut","cze","hun","may","ukr"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx"}
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov", ".webm", ".mpg", ".mpeg", ".flv", ".m2ts"}
EXTRAS_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts", "trailers", "other", "specials-extras"}
LANG_NAME = {"English":"eng","Japanese":"jpn","Korean":"kor","Chinese":"chi","Mandarin":"chi","Cantonese":"chi","Thai":"tha","French":"fre","German":"ger",
 "Russian":"rus","Arabic":"ara","Spanish":"spa","Italian":"ita","Portuguese":"por","Hindi":"hin","Turkish":"tur","Polish":"pol","Dutch":"dut","Swedish":"swe",
 "Danish":"dan","Norwegian":"nor","Finnish":"fin","Hebrew":"heb","Filipino":"fil","Tagalog":"tgl","Vietnamese":"vie","Indonesian":"ind","Czech":"cze",
 "Hungarian":"hun","Greek":"gre","Ukrainian":"ukr","Persian":"per","Catalan":"cat","Icelandic":"ice","Malayalam":"mal","Tamil":"tam","Telugu":"tel","Bengali":"ben",
 "Romanian":"rum","Bulgarian":"bul","Croatian":"hrv","Serbian":"srp","Slovak":"slo","Slovenian":"slv","Lithuanian":"lit","Latvian":"lav","Estonian":"est","Malay":"may","Flemish":"dut"}
SYN = [("fre","fra"),("ger","deu"),("chi","zho"),("dut","nld"),("cze","ces"),("gre","ell"),("per","fas"),("ice","isl"),("rum","ron"),("slo","slk"),("may","msa"),
       ("fil","tgl"),("eng","en"),("jpn","ja"),("spa","es"),("fre","fr"),("ger","de"),("kor","ko"),("chi","zh")]
CHECK_NAMES = {"WRONG_LANGUAGE": "Wrong language", "DUB_NOT_WANTED": "Not the language you chose", "DUB_ONLY": "English dub only", "NO_SUBS": "No subtitles",
 "UND_AUDIO_NO_SUBS": "Unknown language, no subtitles", "MISSING_EPISODE": "Episode missing", "MISSING_MOVIE": "Movie missing", "MISSING_SCENE": "Scene missing",
 "MISSING_FILE": "File vanished", "ZERO_SIZE": "Empty file", "CORRUPT_HEADER": "Unreadable file", "NO_AUDIO": "No audio", "NO_VIDEO": "No video",
 "RUNTIME_SHORT": "Too short", "RUNTIME_LONG": "Too long", "RUNTIME_DISAGREE": "Length changed", "SAMPLE_FILE": "Sample, not the episode", "TITLE_MISMATCH": "Title names another episode",
 "DUPLICATE_VIDEO": "Duplicate file", "ORPHAN_FILE": "Unknown file", "BAD_DURATION_HEADER": "Wrong time bar", "AUDIO_OFFSET": "Audio starts late", "SUBS_OUT_OF_SYNC": "Subtitles out of sync", "JOBFAIL": "A job failed", "PLEX_MISSING": "Not in Plex",
 "RES_MISLABELED": "Lower resolution than labelled", "HI10P": "10-bit video (some players transcode)", "AV1": "AV1 video (some players transcode)", "INTERLACED": "Interlaced video", "MP4_IN_MKV": "File type does not match its name",
 "TRUEHD_ONLY": "Lossless-only audio", "DV_PROFILE5": "Dolby Vision without fallback", "DV_PROFILE7": "Dolby Vision dual layer", "SPACE": "Space to free", "UNFOUND": "Nothing found for weeks",
 "TRASH": "Trash purges soon", "SELFTEST": "Self-test failing", "HEALTH": "Audit not running"}
SEV_NAMES = {"critical": "Won't play", "high": "Wrong", "medium": "Waiting", "low": "FYI"}
# F4: what common players can direct-play. True = plays as-is, False = Plex has to transcode (or it fails). Edit freely; this is a best-effort table.
DEVICE_PROFILES = {   # key: (name, {capability: ok})
 "ps5":        ("PlayStation 5",          {"h264_10bit": False, "av1": False, "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
 "xbox":       ("Xbox Series",            {"h264_10bit": False, "av1": False, "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
 "shield":     ("NVIDIA Shield",          {"h264_10bit": False, "av1": False, "dv_p5": True,  "dv_p7": True,  "lossless_audio": True,  "interlaced": True}),
 "apple-tv":   ("Apple TV 4K",            {"h264_10bit": False, "av1": False, "dv_p5": True,  "dv_p7": True,  "lossless_audio": True,  "interlaced": True}),
 "google-tv":  ("Chromecast / Google TV", {"h264_10bit": False, "av1": True,  "dv_p5": True,  "dv_p7": True,  "lossless_audio": False, "interlaced": True}),
 "fire-tv":    ("Fire TV 4K",             {"h264_10bit": False, "av1": True,  "dv_p5": True,  "dv_p7": True,  "lossless_audio": False, "interlaced": True}),
 "roku":       ("Roku",                   {"h264_10bit": False, "av1": False, "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
 "lg-tv":      ("LG TV (webOS app)",      {"h264_10bit": False, "av1": True,  "dv_p5": True,  "dv_p7": True,  "lossless_audio": False, "interlaced": True}),
 "samsung-tv": ("Samsung TV (Tizen app)", {"h264_10bit": False, "av1": True,  "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
 "plex-web":   ("Plex Web / browser",     {"h264_10bit": False, "av1": True,  "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
 "desktop":    ("Plex Desktop / HTPC",    {"h264_10bit": True,  "av1": True,  "dv_p5": False, "dv_p7": True,  "lossless_audio": True,  "interlaced": True}),
 "phone":      ("Phone / tablet apps",    {"h264_10bit": False, "av1": False, "dv_p5": False, "dv_p7": False, "lossless_audio": False, "interlaced": True}),
}
COMPAT_CAP = {"HI10P": "h264_10bit", "AV1": "av1", "DV_PROFILE5": "dv_p5", "DV_PROFILE7": "dv_p7", "TRUEHD_ONLY": "lossless_audio", "INTERLACED": "interlaced"}
def devices_chosen():
    try: return [d for d in str(setting("DEVICES") or "").split(",") if d.strip() in DEVICE_PROFILES]
    except Exception: return []
def apply_device_profiles(out):
    """With players declared, a compatibility note is kept only if one of them cannot direct-play the file, and it names them; otherwise it is dropped."""
    devs = devices_chosen()
    if not devs: return out
    res = []
    for code, sev, detail in out:
        cap = COMPAT_CAP.get(code)
        if not cap: res.append((code, sev, detail)); continue
        hit = [DEVICE_PROFILES[d][0] for d in devs if not DEVICE_PROFILES[d][1].get(cap, True)]
        if not hit: continue
        res.append((code, "high" if code == "DV_PROFILE5" else "low", f"{detail} | affects: {', '.join(hit)}"))
    return res
def check_name(code): return CHECK_NAMES.get(code) or code.replace("_", " ").capitalize()
def canon(code):
    code = (code or "").strip().lower()
    for a, b in SYN:
        if code == b: return a
    return code
LANG_WORD = {}
for _n, _c in LANG_NAME.items(): LANG_WORD.setdefault(_c, _n)
LANG_WORD.update({"und": "an unknown language", "fil": "Filipino", "zxx": "no language"})
def lang_word(code): return LANG_WORD.get(code or "", code or "?")
def plain_sentence(code, sev, label, detail, app=None):
    """One sentence a person who has never heard of Sonarr can act on. The technical detail stays available underneath."""
    d = detail or ""
    def langs(key):
        m = re.search(key + r"=\[([^\]]*)\]", d)
        return [lang_word(x.strip("' ")) for x in m.group(1).split(",") if x.strip("' ")] if m else []
    au = ", ".join(langs("audio")); orig = (re.search(r"original=(\w{3})", d) or [None, None])[1]; want = lang_word(orig) if orig and orig != "eng" else "English"
    mins = re.search(r"file (\d+) min vs expected (\d+) min", d)
    if "two releases agree" in d: return "Two different downloads look the same here, so this is how the show is, not a broken file. Safe to ignore."
    if d.startswith("[working:"):
        what = d[9:d.find("]")].strip() if "]" in d else "work"
        return f"{what[0].upper()}{what[1:]}. This row clears by itself when it is done; nothing to do."
    if d.startswith("[verified"):
        v = d.split("]")[0][1:]; verdicts = (("CONTENT MATCHES", "Checked: the dialogue matches this episode. Safe to ignore."), ("CONSISTENT", "Checked: the dialogue fits this episode."),
            ("NOT A COPY", "Checked: this is not a copy of another episode."), ("PROBABLY CORRECT", "Checked: probably the right episode."), ("DUPLICATE CONTENT", "Checked: this file has the same dialogue as another episode. One of them is the wrong file."),
            ("MULTI-EPISODE", "Checked: the dialogue matches two different episodes, so this file is probably a double episode or mislabelled."), ("MISLABELED", "Checked: the dialogue belongs to a different episode than the name says."))
        return next((txt for key, txt in verdicts if key in v), "Checked, but the result was inconclusive. Open details for what was compared.")
    if code == "WRONG_LANGUAGE":
        if "100%" in d or "probably intended" in d: return f"The whole season is in {au or 'this language'}, so that is probably the show's real language, not a broken file."
        if "mis-tag" in d or "not a language anyone dubs" in d: return f"The audio is labelled {au}, which is almost certainly a wrong label, not a wrong language. 'Set audio language' fixes the label."
        return f"The audio is {au or 'in another language'} but this should be {want}. A re-download usually fixes it."
    if code == "DUB_NOT_WANTED":
        tagged = lang_word((re.search(r'audit-lang-(\w{3})', d) or [None, ''])[1])
        return f"This show is marked as {tagged}, but this whole season is in {au}. Probably fine, the show just switches language between seasons." if "simply in a different language" in d else f"You marked this show as {tagged} audio and this file has {au}."
    if code == "DUB_ONLY": return f"This anime only has {au or 'an English'} audio, no Japanese track. A re-download fetches a dual-audio release."
    if code in ("NO_SUBS", "UND_AUDIO_NO_SUBS") and "burned into the picture" in d: return "The subtitles are painted into the picture, so no player can switch them and the app cannot read their language. 'Show me a frame' lets you check which language they are."
    if code == "NO_SUBS": return ("No subtitles yet. Bazarr is still searching; if it finds nothing for a few days, AI subtitles are made automatically." if "still searching" in d
                                  else "No subtitles and Bazarr is not looking for any. 'Find subtitles' asks it to.") + (" There is an unlabelled subtitle track inside - 'Label subtitle tracks' may be all it needs." if "untagged text" in d else "")
    if code == "UND_AUDIO_NO_SUBS": return "The audio has no language label and there are no subtitles, so players cannot tell what it is. 'Detect the language' listens and labels it; English needs no subtitles."
    if code in ("MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE"):
        rel = re.search(r"released (\S+) \((\d+) days ago\)", d); q = "queued" in d.lower() or "downloading" in d.lower()
        return (f"Released {rel.group(2)} days ago and not downloaded yet. " if rel else "Released but not downloaded yet. ") + ("It is in the download queue now." if q else "Nothing found on your indexers so far. 'Search now' tries again.")
    who = (app or "").capitalize() or "The library manager"
    if code == "MISSING_FILE": return f"{who} still lists this file but it is not on disk." + (" A re-download is in progress." if "re-grab" in d else " A rescan will fix the record.")
    if code == "ZERO_SIZE": return "The file is empty (0 bytes). It needs re-downloading."
    if code == "CORRUPT_HEADER": return "The file cannot be read at all. It needs re-downloading."
    if code == "NO_AUDIO": return "There is no audio track in this file. It needs re-downloading."
    if code == "NO_VIDEO": return "There is no video track in this file. It needs re-downloading."
    if code == "SAMPLE_FILE": return "This is a tiny sample, not the real episode. It needs re-downloading."
    if code == "RUNTIME_SHORT": return (f"The file is {mins.group(1)} minutes but the episode should be about {mins.group(2)}. " if mins else "The file is much shorter than the episode should be. ") + ("Probably cut off." if sev != "low" else "The whole season is like this, so the listed length is probably wrong.")
    if code == "RUNTIME_LONG": return (f"The file is {mins.group(1)} minutes where about {mins.group(2)} were expected. " if mins else "The file is much longer than expected. ") + ("Premieres and finales are often extended, so this is probably fine." if sev == "low" else "Could be an extended cut, a double episode or the wrong content. 'Check the episode' tells you which.")
    if code == "RUNTIME_DISAGREE": return f"The file changed since {who} last looked at it. A rescan updates the record."
    if code == "TITLE_MISMATCH": return "The title written inside the file names a different episode. Often harmless (fansub naming), 'Check the episode' settles it."
    if code == "DUPLICATE_VIDEO": return "There are two files for this episode in the folder. Only one is in use; the other is wasting space."
    if code == "ORPHAN_FILE" and ARR_GAP in d:
        who_ = (re.search(r"inside (\w+)'s folder", d) or [None, "the arr"])[1]; who_ = who_[:1].upper() + who_[1:]
        return f"This file sits in a folder {who_} lists, but {who_} did not list the file itself. That is usually a reading gap, not a stray file: 'Rescan the folder' first. Move it to the trash only if the rescan does not adopt it."
    if code == "ORPHAN_FILE": return "A video file nobody manages: neither Sonarr nor Radarr knows about it, and Plex may not either."
    if code == "BAD_DURATION_HEADER": return "The file claims the wrong length, so players show a wrong time bar and may stop early. 'Fix the time bar' repairs it without re-downloading."
    if code == "SUBS_OUT_OF_SYNC":
        if "drift:" in d and "src=embedded" in d: return "The subtitles built into the file start close but slide further out as the file goes on. Bazarr cannot re-time a built-in track: 'Find subtitles' gets a separate file that fits, or 'Make AI subtitles' makes one."
        if "drift:" in d: return "The subtitles start close but slide further out as the file goes on. 'Re-time the subtitles' has Bazarr fit them to the audio again."
        m_ = re.search(r"(\d+(?:\.\d+)?)s (late|early)", d); secs = f"about {round(float(m_.group(1)))} second{'s' if round(float(m_.group(1))) != 1 else ''} {m_.group(2)}" if m_ else "out of step"
        return f"The subtitles run {secs} through the whole file. 'Re-time the subtitles' shifts them into place, no re-download."
    if code == "AUDIO_OFFSET": return "The audio starts a moment after the video." + (" That is intentional here (the video is cut at the same spot), nothing to do." if "intentional" in d else " 'Fix audio delay' lines it up without re-downloading.")
    if code == "PLEX_MISSING": return "The file is on disk but Plex has not picked it up. 'Refresh in Plex' makes it rescan the folder."
    if code == "RES_MISLABELED": return "The picture is lower resolution than the release claims."
    aff = (re.search(r"\| affects: (.+)$", d) or [None, None])[1]
    if aff and code in COMPAT_CAP:
        what = {"HI10P": "10-bit H.264 video", "AV1": "AV1 video", "DV_PROFILE5": "Dolby Vision without a normal fallback", "DV_PROFILE7": "dual-layer Dolby Vision", "TRUEHD_ONLY": "lossless-only audio", "INTERLACED": "interlaced video"}[code]
        return f"{what}: your {aff} cannot play it directly, so Plex transcodes for {'it' if ',' not in aff else 'them'}." + (" Re-download or expect green/purple colours there." if code == "DV_PROFILE5" else "")
    if code == "HI10P": return "10-bit H.264 video: normal for anime, but TVs and consoles cannot play it directly, so Plex transcodes."
    if code == "AV1": return "AV1 video: the PS5 and older TVs cannot play it directly, so Plex transcodes."
    if code == "MP4_IN_MKV": return "This video is an MP4 file under a Matroska name. It plays, but its default audio and subtitles cannot be set and some players misread it. 'Rewrap the file' fixes that without re-encoding."
    if code == "INTERLACED": return "Old-style interlaced video (TV broadcast). Plays fine; some players show comb lines."
    if code == "TRUEHD_ONLY": return "Only lossless audio tracks. Some players cannot play those and need a transcode."
    if code == "DV_PROFILE5": return "Dolby Vision without a normal fallback: looks green and purple on most players. Re-download."
    if code == "DV_PROFILE7": return "Dolby Vision dual layer: plays, most players just fall back to HDR10."
    return d[:160]
def canon_set(codes): return {canon(c) for c in codes if c and canon(c) not in ("", "zxx")}
def log(*a): print(datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)
ERRORS = []   # Q25: last 50 unexpected errors in memory (time, where, text, traceback); the full record is REPORT_DIR/errors.log
def note_error(*a, where=None):
    """Like log(), but when called inside an except block the full traceback is kept: appended to errors.log and to the About tab's Recent errors.
    Use it for failures that should not happen (a job crashing, a hook failing), not for expected conditions like a service being switched off."""
    log(*a); import traceback, sys as _sys
    et, ev, tb = _sys.exc_info(); text = " ".join(str(x) for x in a)
    trace = "".join(traceback.format_exception(et, ev, tb)) if ev else ""
    if where is None:
        try: where = _sys._getframe(1).f_code.co_name
        except Exception: where = ""
    ERRORS.append(dict(ts=time.time(), where=where, text=text[:300], trace=trace[-4000:])); del ERRORS[:-50]
    try:
        os.makedirs(REPORT_DIR, exist_ok=True); path = os.path.join(REPORT_DIR, "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000: os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f: f.write(f"=== {datetime.datetime.now().isoformat(timespec='seconds')} {where}: {text}\n{trace}\n")
    except Exception: pass
def err_result(e):
    """Inside `except Exception as e:` - record the traceback and return the 'ERROR …' text an action result expects."""
    import sys as _sys
    try: where = _sys._getframe(1).f_code.co_name
    except Exception: where = ""
    note_error(f"{where}: {e!r}", where=where); return f"ERROR {e!r}"
def errors_recent(hours=24, limit=20):
    """What went wrong lately: in-memory tracebacks plus the tail of errors.log (survives restarts), newest first."""
    out = list(ERRORS)
    try:
        path = os.path.join(REPORT_DIR, "errors.log")
        if os.path.exists(path):
            raw = open(path, encoding="utf-8", errors="replace").read()[-200_000:]
            seen = {(int(e["ts"]), e["text"]) for e in out}
            for block in ("\n" + raw).split("\n=== ")[1:]:
                head, _, trace = block.partition("\n"); ts_s, _, rest = head.partition(" "); where, _, text = rest.partition(": ")
                try: ts_ = datetime.datetime.fromisoformat(ts_s).timestamp()
                except Exception: continue
                if (int(ts_), text[:300]) in seen: continue
                out.append(dict(ts=ts_, where=where, text=text[:300], trace=trace[-4000:]))
    except Exception: pass
    cut = time.time() - hours * 3600
    out = [e for e in out if e["ts"] >= cut]; out.sort(key=lambda e: -e["ts"])
    return [dict(e, when=datetime.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds")) for e in out[:limit]]

PROGRESS = {"phase": "idle", "detail": "", "done": 0, "total": 0, "started": None, "updated": None, "recent": [], "actions": []}
def progress(phase=None, detail=None, done=None, total=None, recent=None, action=None):
    if phase is not None: PROGRESS["phase"] = phase
    if detail is not None: PROGRESS["detail"] = detail
    if done is not None: PROGRESS["done"] = done
    if total is not None: PROGRESS["total"] = total
    if recent is not None: PROGRESS["recent"] = (recent + PROGRESS["recent"])[:15]
    if action is not None: PROGRESS["actions"] = ([action] + PROGRESS["actions"])[:15]
    PROGRESS["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(REPORT_DIR, exist_ok=True); tmp = os.path.join(REPORT_DIR, "progress.json.tmp")
        json.dump(PROGRESS, open(tmp, "w")); os.replace(tmp, os.path.join(REPORT_DIR, "progress.json"))
    except Exception: pass

# ---------- arr clients ----------
def read_key(path):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(path).read()); return m.group(1) if m else None
def conn_value(name):
    """Connection detail: saved in the UI (setup wizard) > container env > None."""
    v = SETTINGS.get(name) if name in SETTABLE else None
    return str(v) if v else E(name)
def arr_apps():
    apps = {}
    for name, port, kind in ARR_APPS:
        url = conn_value(f"{name.upper()}_URL") or (f"http://{HOST_IP}:{port}" if HOST_IP else ""); key = conn_value(f"{name.upper()}_APIKEY")
        xml = os.path.join(ARR_DIR, f"{name}.xml")
        if not key and os.path.exists(xml): key = read_key(xml)
        if key and url: apps[name] = (url.rstrip("/"), key, kind)
        elif key: log(f"{name}: no address - set {name.upper()}_URL or HOST_IP, or use the Setup page")
    return apps
def api(base, key, path, method="GET", body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    if (method or "GET").upper() not in ("GET", "HEAD") or data is not None: guard_write(f"{method or 'POST'} {base}/api/v3/{path}")
    req = urllib.request.Request(f"{base}/api/v3/{path}", data=data, method=method, headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(); return json.loads(raw) if raw.strip() else None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:   # includes HTTPError; one retry for reads only - a write is never repeated blindly
        transient = (not isinstance(e, urllib.error.HTTPError)) or e.code >= 500
        if data is not None or (method or "GET").upper() not in ("GET", "HEAD") or not transient: raise
        time.sleep(2)
        with urllib.request.urlopen(req, timeout=timeout) as r: raw = r.read()
        note_flake(base.rstrip("/").rsplit("/", 1)[-1] if base.count("/") > 2 else base, path.split("?")[0], repr(e)); return json.loads(raw) if raw.strip() else None
def bazarr_conf():
    yml = os.path.join(ARR_DIR, "bazarr.yaml"); key = conn_value("BAZARR_APIKEY")
    if not key and os.path.exists(yml):
        try:
            import yaml; key = (yaml.safe_load(open(yml)).get("auth") or {}).get("apikey")
        except Exception as e: log("bazarr yaml:", e)
    return (conn_value("BAZARR_URL") or (f"http://{HOST_IP}:6767" if HOST_IP else "")).rstrip("/"), key
TWO_LETTER = {"eng":"en","jpn":"ja","spa":"es","fre":"fr","ger":"de","kor":"ko","chi":"zh","por":"pt","ita":"it","rus":"ru","ara":"ar","hin":"hi","dut":"nl","pol":"pl","tur":"tr",
              "swe":"sv","nor":"no","dan":"da","fin":"fi","heb":"he","tha":"th","vie":"vi","ind":"id","cze":"cs","hun":"hu","gre":"el","ukr":"uk","rum":"ro","bul":"bg","fil":"tl","may":"ms"}
def bazarr_wanted():
    base, key = bazarr_conf()
    if not key: return set(), set()
    eps, movs = set(), set()
    try:
        for path, out, k in (("episodes", eps, "sonarrEpisodeId"), ("movies", movs, "radarrId")):
            req = urllib.request.Request(f"{base}/api/{path}/wanted?start=0&length=-1", headers={"X-API-KEY": key})
            with urllib.request.urlopen(req, timeout=120) as r:
                for row in json.load(r).get("data", []): out.add(row.get(k))
        dep_status("bazarr", True)
    except Exception as e: log("bazarr wanted:", e); dep_status("bazarr", False, repr(e))
    return eps, movs
def hms(s):
    """'0:42:00' / '42:00' / '2520' -> seconds; anything unparseable -> None (arr media info is not always clean)."""
    if not s: return None
    try: p = [int(float(x)) for x in str(s).split(":")]
    except (TypeError, ValueError): return None
    while len(p) < 3: p.insert(0, 0)
    return p[0]*3600 + p[1]*60 + p[2]

# ---------- Plex ----------
_PLEX = {"sections": None, "ts": 0}; PLEX_PENDING = set(); PLEX_KEYS = {}   # path -> (ratingKey, seasonKey, showKey, sectionKey)
def plex_token():
    t = conn_value("PLEX_TOKEN")
    if t: return t
    if os.path.exists(PLEX_TOKEN_FILE):
        m = re.search(r'PlexOnlineToken="([^"]+)"', open(PLEX_TOKEN_FILE).read()); return m.group(1) if m else None
    return None
def plex_get(path, params=None, timeout=60):
    tok = plex_token()
    if not tok: raise RuntimeError("no Plex token (mount Preferences.xml at /arr/plex.xml or set PLEX_TOKEN)")
    from urllib.parse import urlencode
    q = dict(params or {}); q["X-Plex-Token"] = tok
    req = urllib.request.Request(f"{PLEX_URL}{path}?{urlencode(q)}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(); return json.loads(raw) if raw.strip() else {}
def _plex_maps(): return [(a.rstrip("/"), b.rstrip("/")) for a, b in (p.split("=", 1) for p in PLEX_PATH_MAP.split(",") if "=" in p)]
def to_plex(p):
    for a, b in _plex_maps():
        if p == a or p.startswith(a + "/"): return b + p[len(a):]
    return p
def from_plex(p):
    for a, b in _plex_maps():
        if p == b or p.startswith(b + "/"): return a + p[len(b):]
    return p
def plex_sections(force=False):
    if _PLEX["sections"] is None or force or time.time() - _PLEX["ts"] > 3600:
        d = plex_get("/library/sections").get("MediaContainer", {}).get("Directory", []) or []
        _PLEX["sections"] = [dict(key=s["key"], type=s["type"], title=s["title"], locations=[os.path.normpath(l["path"]) for l in s.get("Location", [])]) for s in d]; _PLEX["ts"] = time.time()
    return _PLEX["sections"]
def plex_sections_for(path):
    pp = os.path.normpath(to_plex(path)); return [s for s in plex_sections() if any(pp == l or pp.startswith(l + os.sep) for l in s["locations"])]
def compute_upcoming(conn, items, ahead=6):
    """F2: files people are about to watch. Plex On Deck gives the next episode per show; the following `ahead` episodes of that show (and the deck item itself) are 'upcoming'."""
    try: deck = plex_get("/library/onDeck").get("MediaContainer", {}).get("Metadata", []) or []
    except Exception as e: log("on deck:", repr(e)); return 0
    pos = {}; movies = set()
    for m in deck:
        if m.get("type") == "episode" and m.get("grandparentRatingKey"): pos[str(m["grandparentRatingKey"])] = (int(m.get("parentIndex") or 0), int(m.get("index") or 0))
        elif m.get("type") == "movie": movies.add(str(m.get("ratingKey")))
    rows = []
    for it in items:
        k = PLEX_KEYS.get(os.path.normpath(it.get("path") or "")) if it.get("path") else None
        if not k: continue
        rk, _, show_rk, _ = k
        if it["kind"] == "movie" and str(rk) in movies: rows.append((it["app"], it["fid"], it["label"], time.time())); continue
        if str(show_rk) not in pos: continue
        m = re.search(r"S(\d+)E(\d+)", it["label"] or "")
        if not m: continue
        s_, e_ = int(m.group(1)), int(m.group(2)); ds, de = pos[str(show_rk)]
        if (s_ == ds and de <= e_ <= de + ahead) or (s_ == ds + 1 and e_ <= 3): rows.append((it["app"], it["fid"], it["label"], time.time()))
    conn.execute("DELETE FROM upcoming WHERE why IS NULL OR why='next up'"); conn.executemany("INSERT OR REPLACE INTO upcoming(app,fid,label,ts,why) VALUES(?,?,?,?,'next up')", rows); conn.commit()
    log(f"up next: {len(pos)} shows on deck in Plex, {len(rows)} files flagged as upcoming"); return len(rows)
def upcoming_map(conn):
    """(app, fid) -> 'watching' (playing in Plex right now) or 'next up' (On Deck)."""
    try: return {(a, f): (w or "next up") for a, f, w in conn.execute("SELECT app, fid, why FROM upcoming")}
    except Exception: return {}
def upcoming_set(conn):
    try: return {(a, f) for a, f in conn.execute("SELECT app, fid FROM upcoming")}
    except Exception: return set()
SCORE_W = {"critical": 10.0, "high": 4.0, "medium": 1.0, "low": 0.1}
def library_scores(conn):
    """F3: one number per library: 100 minus the weighted open findings per 100 files. 100 = nothing open; a critical costs 10 points per 100 files."""
    try: files = json.loads((conn.execute("SELECT v FROM kv WHERE k='files_by_lib'").fetchone() or ["{}"])[0])
    except Exception: files = {}
    out = {}
    for app, path, sev in conn.execute("SELECT app, path, severity FROM finding WHERE resolved_at IS NULL"):
        lib = lib_of(path, app); e = out.setdefault(lib, {"open": {k: 0 for k in SCORE_W}, "weighted": 0.0}); e["open"][sev] = e["open"].get(sev, 0) + 1; e["weighted"] += SCORE_W.get(sev, 0)
    res = {}
    for lib, n in files.items():
        e = out.get(lib, {"open": {k: 0 for k in SCORE_W}, "weighted": 0.0}); n = n or 1
        res[lib] = dict(files=n, open=e["open"], score=round(max(0.0, 100 - e["weighted"] * 100 / n), 1))
    return res
def plex_refresh(path):
    """Partial scan of the folder holding `path` in every Plex section that covers it. Returns a one-line description."""
    if setting("PLEX_REFRESH") != "on": return "plex refresh off"
    if ledger_only(f"plex refresh {path}"): return "read-only: plex refresh skipped"
    if not plex_token(): return "plex: no token"
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    try: secs = plex_sections_for(folder)
    except Exception as e: return f"plex: ERROR {e!r}"
    if not secs: return "plex: no section covers this folder"
    out = []
    for s in secs:
        try: plex_get(f"/library/sections/{s['key']}/refresh", {"path": to_plex(folder)}, timeout=30); out.append(s["title"])
        except Exception as e: out.append(f"{s['title']} ERROR {e!r}"); log("plex refresh:", repr(e))
    return "plex refresh: " + ", ".join(out)
def queue_plex(path):
    """Collect folders touched during a run; flushed once at the end so a 50-file dedupe is 1 scan per folder, not 50."""
    if path: PLEX_PENDING.add(path if os.path.isdir(path) else os.path.dirname(path))
def flush_plex(conn):
    if not PLEX_PENDING: return 0
    folders = sorted(PLEX_PENDING)[:PLEX_REFRESH_MAX]; res = [plex_refresh(f) for f in folders]
    record_action(conn, "on", "plex-refresh", "plex", len(folders), f"{len(folders)} folder(s)", "; ".join(os.path.basename(f) for f in folders)[:300], "; ".join(sorted(set(res)))[:300])
    PLEX_PENDING.clear(); return len(folders)
def plex_machine_id():
    if not _PLEX.get("mid"): _PLEX["mid"] = plex_get("/identity").get("MediaContainer", {}).get("machineIdentifier", "")
    return _PLEX["mid"]
def plex_link(rk):
    try: return PLEX_WEB.format(mid=plex_machine_id(), rk=rk) if rk else None
    except Exception: return None
def plex_inventory():
    """{audit-side file path: Plex ratingKey} for every file Plex has indexed, across every show/movie section. Raises on failure."""
    paths = {}; PLEX_KEYS.clear()
    for s in plex_sections(force=True):
        typ = 4 if s["type"] == "show" else 1 if s["type"] == "movie" else None
        if typ is None: continue
        start = 0
        while True:
            mc = plex_get(f"/library/sections/{s['key']}/all", {"type": typ, "X-Plex-Container-Start": start, "X-Plex-Container-Size": 3000}, timeout=300).get("MediaContainer", {})
            md = mc.get("Metadata") or []
            for m in md:
                for media in m.get("Media") or []:
                    for part in media.get("Part") or []:
                        if part.get("file"): np_ = os.path.normpath(from_plex(part["file"])); paths[np_] = m.get("ratingKey"); PLEX_KEYS[np_] = (m.get("ratingKey"), m.get("parentRatingKey"), m.get("grandparentRatingKey"), s["key"])
            start += len(md)
            if not md or start >= int(mc.get("totalSize") or 0): break
    return paths

def intent_from_tags(tag_labels):
    """audit-ignore / audit-lang-xxx / audit-nosubs / audit-noremediate set on the series or movie in the arr."""
    it = {"ignore": False, "lang": None, "nosubs": False, "noremediate": False}
    for t in tag_labels:
        t = t.lower().strip()
        if t == "audit-ignore": it["ignore"] = True
        elif t.startswith("audit-lang-"): it["lang"] = canon(t[len("audit-lang-"):])
        elif t == "audit-nosubs": it["nosubs"] = True
        elif t == "audit-noremediate": it["noremediate"] = True
    return it

MISSING = []   # filled by collect(): dicts(app, kind, id, parent_id, label, path(series/movie folder), released, days, last_search)
MISSING_EV = {}   # app -> {"queue": {id: txt}, "grab": {id: date}, "fail": {id: date}, "block": {id: n}}
LAST_SEARCH = {}  # app -> {episode/movie id: 'YYYY-MM-DD'}
ARR_FOLDERS = {}  # series/movie folder an arr lists -> (app, title), files or not; an "unknown file" inside one is more likely a reading gap than an orphan (#30)
def search_evidence(base, key, now):
    """What the arr has actually done about its missing items: queue, grabs / failed downloads in the last 30 days, blocklist. 3 calls per app."""
    ev = {"queue": {}, "grab": {}, "fail": {}, "block": {}, "cmd": {}}
    try:
        waiting = [c for c in api(base, key, "command") or [] if c.get("status") in ("queued", "started")]
        for pos, c in enumerate(waiting):
            b = c.get("body") or {}; what = ("search" if "Search" in c["name"] else "rescan" if "Rescan" in c["name"] else None)
            if not what: continue
            for k in (b.get("episodeIds") or b.get("movieIds") or []): ev["cmd"].setdefault(k, f"{what} {c['status']} in the arr ({pos} commands ahead)")
    except Exception as e: log("evidence commands:", repr(e))
    try:
        for r in (api(base, key, "queue?pageSize=1000&includeUnknownSeriesItems=false&includeUnknownMovieItems=false") or {}).get("records", []):
            k = r.get("episodeId") or r.get("movieId"); pct = 100 - round(100 * (r.get("sizeleft") or 0) / (r.get("size") or 1))
            if k: ev["queue"][k] = f"{r.get('status')} {pct}%" + (f" ({r.get('trackedDownloadState')})" if r.get("trackedDownloadState") else "")
    except Exception as e: log("evidence queue:", repr(e))
    try:
        since = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in api(base, key, f"history/since?date={since}") or []:
            k = r.get("episodeId") or r.get("movieId"); d = (r.get("date") or "")[:10]
            if not k: continue
            if r.get("eventType") == "grabbed": ev["grab"][k] = max(ev["grab"].get(k, ""), d)
            elif r.get("eventType") == "downloadFailed": ev["fail"][k] = max(ev["fail"].get(k, ""), d)
    except Exception as e: log("evidence history:", repr(e))
    try:
        for r in (api(base, key, "blocklist?pageSize=1000") or {}).get("records", []):
            for k in (r.get("episodeIds") or ([r.get("movieId")] if r.get("movieId") else [])): ev["block"][k] = ev["block"].get(k, 0) + 1
    except Exception as e: log("evidence blocklist:", repr(e))
    return ev
def arr_status_line(app, ids):
    """One line of evidence for episode/movie ids: queue > last grab (+failed) > last search > never."""
    ev = MISSING_EV.get(app, {}); ls = LAST_SEARCH.get(app, {}); out = []
    for i in ids:
        q = ev.get("queue", {}).get(i); g = ev.get("grab", {}).get(i); f = ev.get("fail", {}).get(i); b = ev.get("block", {}).get(i, 0); c = ev.get("cmd", {}).get(i)
        line = (f"downloading now: {q}" if q else c if c else f"last grab {g}" + (f" FAILED {f}" if f and f >= g else "") if g else f"last searched {ls[i]}, nothing found" if ls.get(i) else "never searched by the arr")
        if b: line += f", {b} blocklisted"
        out.append(line)
    return "; ".join(dict.fromkeys(out)) or "no search evidence"
def collect(apps, only=None):
    """only={(app, parent_id)}: build items for those titles alone (import hook) without touching the run-wide MISSING/LAST_SEARCH state."""
    items = []; roots = {}; now = datetime.datetime.now(datetime.timezone.utc); min_days = 0 if only else int(setting("MISSING_MIN_DAYS") or 0)
    if only is None: MISSING.clear(); MISSING_EV.clear(); LAST_SEARCH.clear(); ARR_FOLDERS.clear()
    for app, (base, key, kind) in apps.items():
        try:
            tags = {t["id"]: t["label"] for t in api(base, key, "tag")}
            roots[app] = [r["path"] for r in api(base, key, "rootfolder")]
            if kind == "series":
                for s in api(base, key, "series"):
                    if only is not None and (app, s["id"]) not in only: continue
                    if s.get("path"): ARR_FOLDERS[os.path.normpath(s["path"])] = (app, s.get("title") or "")
                    intent = intent_from_tags(tags.get(t, "") for t in s.get("tags", []))
                    has_files = series_has_files(s)
                    if not has_files and not (min_days and s.get("monitored")): continue
                    files = api(base, key, f"episodefile?seriesId={s['id']}") if has_files else []; eps_by_file = {}; season_last = {}
                    all_eps = api(base, key, f"episode?seriesId={s['id']}")
                    for e in all_eps:
                        sn = e.get("seasonNumber", 0); season_last[sn] = max(season_last.get(sn, 0), e.get("episodeNumber", 0))
                        if e.get("lastSearchTime"): LAST_SEARCH.setdefault(app, {})[e["id"]] = e["lastSearchTime"][:10]
                        if e.get("hasFile") and e.get("episodeFileId"): eps_by_file.setdefault(e["episodeFileId"], []).append(e)
                        elif min_days and s.get("monitored") and e.get("monitored") and not e.get("hasFile") and sn > 0 and (e.get("airDateUtc") or e.get("releaseDate")) and not intent["ignore"]:
                            try: aired = datetime.datetime.fromisoformat(e["airDateUtc"].replace("Z", "+00:00")) if e.get("airDateUtc") else datetime.datetime.fromisoformat(e["releaseDate"]).replace(tzinfo=datetime.timezone.utc); days = (now - aired).days
                            except ValueError: continue
                            label = f"{s['title']} S{sn:02d}E{e.get('episodeNumber', 0):02d}" if e.get("episodeNumber") is not None else f"{s['title']} - {e.get('title') or '?'} ({aired.date().isoformat()})"
                            if days >= min_days: MISSING.append(dict(app=app, kind="series", id=e["id"], parent_id=s["id"], label=label, path=s.get("path"), released=aired.date().isoformat(), days=days, ep_title=e.get("title") or "", last_search=(e.get("lastSearchTime") or "")[:10], slug=s.get("titleSlug")))
                    if not has_files: continue
                    for f in files:
                        eps = sorted(eps_for_file(f, eps_by_file, all_eps), key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)))
                        edge = bool(eps) and (eps[0].get("seasonNumber") or 0) > 0 and any(e.get("episodeNumber") in (1, season_last.get(e.get("seasonNumber", 0))) for e in eps)
                        label = s["title"] + (" " + "/".join(f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in eps) if eps else "")
                        srt = s.get("runtime") or 0
                        exp = sum((e.get("runtime") or srt) for e in eps) * 60 if eps else srt * 60
                        exp_lo = sum(min(e.get("runtime") or srt, srt) if srt else (e.get("runtime") or 0) for e in eps) * 60 if eps else srt * 60
                        items.append(dict(app=app, kind=kind, fid=f["id"], parent_id=s["id"], label=label, path=f.get("path"), size=f.get("size") or 0,
                            orig=canon(LANG_NAME.get((s.get("originalLanguage") or {}).get("name"), "")), intent=intent, title=s["title"], season=(eps[0].get("seasonNumber") if eps else None),
                            ep_titles=[e.get("title") or "" for e in eps], quality=f.get("quality", {}).get("quality", {}), mi=f.get("mediaInfo") or {},
                            expected=exp, expected_lo=exp_lo, date_added=f.get("dateAdded"), ep_ids=[e["id"] for e in eps], n_eps=len(eps), series_type=s.get("seriesType"),
                            edge=edge, slug=s.get("titleSlug"), monitored=bool(s.get("monitored")) and all(e.get("monitored") for e in eps)))
            else:
                for m in api(base, key, "movie"):
                    if only is not None and (app, m["id"]) not in only: continue
                    if m.get("path"): ARR_FOLDERS[os.path.normpath(m["path"])] = (app, m.get("title") or "")
                    intent = intent_from_tags(tags.get(t, "") for t in m.get("tags", []))
                    if m.get("lastSearchTime"): LAST_SEARCH.setdefault(app, {})[m["id"]] = m["lastSearchTime"][:10]
                    if not m.get("hasFile"):
                        if min_days and m.get("monitored") and m.get("isAvailable") and not intent["ignore"]:
                            rel = m.get("digitalRelease") or m.get("physicalRelease") or m.get("inCinemas")
                            try: rd = datetime.datetime.fromisoformat(rel.replace("Z", "+00:00")) if rel else None
                            except ValueError: rd = None
                            if rd and (now - rd).days >= min_days: MISSING.append(dict(app=app, kind="movie", id=m["id"], parent_id=m["id"], label=f"{m['title']} ({m.get('year')})", path=m.get("path"), released=rd.date().isoformat(), days=(now - rd).days, ep_title="", last_search=(m.get("lastSearchTime") or "")[:10], slug=m.get("titleSlug")))
                        continue
                    for f in api(base, key, f"moviefile?movieId={m['id']}"):
                        items.append(dict(app=app, kind=kind, fid=f["id"], parent_id=m["id"], label=f"{m['title']} ({m.get('year')})", path=f.get("path"),
                            size=f.get("size") or 0, orig=canon(LANG_NAME.get((m.get("originalLanguage") or {}).get("name"), "")), intent=intent, title=m["title"], season=None,
                            ep_titles=[], quality=f.get("quality", {}).get("quality", {}), mi=f.get("mediaInfo") or {}, expected=(m.get("runtime") or 0) * 60,
                            date_added=f.get("dateAdded"), ep_ids=[], n_eps=0, slug=m.get("titleSlug"), monitored=bool(m.get("monitored"))))
            n = sum(1 for i in items if i["app"] == app); log(f"{app}: {n} files"); progress(detail=f"{app}: {n} files listed")
            if only is None: MISSING_EV[app] = search_evidence(base, key, now)
            dep_status(app, True)
        except Exception as e:
            log(f"{app}: API error {e!r} - skipped this run"); roots.pop(app, None); dep_status(app, False, repr(e))
    return items, roots

# ---------- state ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=60); c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS probe(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, probed_at REAL, ok INTEGER, data TEXT);
    CREATE TABLE IF NOT EXISTS finding(id TEXT PRIMARY KEY, app TEXT, fid INTEGER, check_code TEXT, severity TEXT, label TEXT, path TEXT,
        detail TEXT, first_seen REAL, last_seen REAL, resolved_at REAL, notified INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS run(started REAL, finished REAL, files INTEGER, probed INTEGER, active INTEGER, new INTEGER, resolved INTEGER);
    CREATE TABLE IF NOT EXISTS action(ts REAL, mode TEXT, kind TEXT, app TEXT, target TEXT, label TEXT, detail TEXT, result TEXT);
    CREATE TABLE IF NOT EXISTS rescan_log(app TEXT, parent_id INTEGER, ts REAL, PRIMARY KEY(app, parent_id));
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS upcoming(app TEXT, fid INTEGER, label TEXT, ts REAL, PRIMARY KEY(app, fid));
    CREATE TABLE IF NOT EXISTS flake(ts REAL, service TEXT, what TEXT, error TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(probe)")]
    if "ver" not in cols: c.execute("ALTER TABLE probe ADD COLUMN ver INTEGER DEFAULT 1")
    fcols = [r[1] for r in c.execute("PRAGMA table_info(finding)")]
    if "seen_runs" not in fcols: c.execute("ALTER TABLE finding ADD COLUMN seen_runs INTEGER DEFAULT 1")
    if "handed_off" not in fcols: c.execute("ALTER TABLE finding ADD COLUMN handed_off INTEGER DEFAULT 0")
    rcols = [r[1] for r in c.execute("PRAGMA table_info(run)")]
    if "extra" not in rcols: c.execute("ALTER TABLE run ADD COLUMN extra TEXT")
    ucols = [r[1] for r in c.execute("PRAGMA table_info(upcoming)")]
    if ucols and "why" not in ucols: c.execute("ALTER TABLE upcoming ADD COLUMN why TEXT DEFAULT 'next up'")
    c.execute("CREATE TABLE IF NOT EXISTS links(app TEXT, fid INTEGER, plex TEXT, arr TEXT, PRIMARY KEY(app, fid))")
    c.execute("CREATE TABLE IF NOT EXISTS subtag_seen(path TEXT PRIMARY KEY, mtime REAL, result TEXT, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, ts REAL, job TEXT, status TEXT, done_at REAL)")
    jcols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
    if "started_at" not in jcols: c.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")
    c.execute("CREATE TABLE IF NOT EXISTS realdur(path TEXT PRIMARY KEY, mtime REAL, dur REAL, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS trackprefs(path TEXT PRIMARY KEY, mtime REAL, ts REAL, note TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS hardsub(path TEXT PRIMARY KEY, mtime REAL, verdict TEXT, score REAL, best_t REAL, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS plexsel(rk TEXT PRIMARY KEY, ts REAL, audio_id TEXT, sub_id TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS watch(app TEXT, parent_id INTEGER, season INTEGER, title TEXT, library TEXT, files INTEGER, bytes INTEGER, leaf INTEGER, viewed INTEGER,
        plays INTEGER, last_played REAL, added REAL, status TEXT, show_rk TEXT, item_rk TEXT, updated REAL)""")
    wcols = [r[1] for r in c.execute("PRAGMA table_info(watch)")]
    if "show_last" not in wcols: c.execute("ALTER TABLE watch ADD COLUMN show_last REAL")
    c.execute("""CREATE TABLE IF NOT EXISTS regrab(id INTEGER PRIMARY KEY, ts REAL, app TEXT, kind TEXT, parent_id INTEGER, fid INTEGER, ep_ids TEXT, label TEXT,
        orig_path TEXT, trash_dest TEXT, code TEXT, status TEXT, note TEXT, checked_at REAL, actor TEXT)""")
    c.commit(); return c
def record_regrab(conn, app, kind, parent_id, fid, ep_ids, label, orig_path, dest, code, actor):
    conn.execute("INSERT INTO regrab(ts,app,kind,parent_id,fid,ep_ids,label,orig_path,trash_dest,code,status,note,actor) VALUES(?,?,?,?,?,?,?,?,?,?,'pending','waiting for the arr to import a replacement',?)",
                 (time.time(), app, kind, parent_id, fid, json.dumps(ep_ids or []), label, orig_path, dest, code, actor)); conn.commit()
def sample_hash(path, chunk=4 << 20):
    """Cheap fingerprint of a large media file: size + sha256 of the first, middle and last 4 MB (the MP4 index lives at one end)."""
    h = hashlib.sha256(); size = os.path.getsize(path); h.update(str(size).encode())
    with open(path, "rb") as fh:
        for off in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            fh.seek(off); h.update(fh.read(chunk))
    return h.hexdigest()
def restore_file(dest, orig):
    """Move a quarantined file (and sidecars) back to its original path."""
    guard_write(f"restore {dest}")
    if os.path.exists(orig): raise FileExistsError(orig)
    os.makedirs(os.path.dirname(orig), exist_ok=True); os.rename(dest, orig)
    stem = os.path.splitext(os.path.basename(dest))[0]
    for n in os.listdir(os.path.dirname(dest)):
        if n.startswith(stem + ".") and n != os.path.basename(dest):
            try: os.rename(os.path.join(os.path.dirname(dest), n), os.path.join(os.path.dirname(orig), n))
            except OSError: pass
    with open(os.path.join(TRASH_DIR, "LOG.txt"), "a") as f: f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\trestore\t{dest}\t->\t{orig}\n")
def check_regrabs(conn, apps, items, found, started):
    """Close the loop on every pending re-grab: did the arr import a replacement, and is it actually better?
    found = {(app, fid): set(check codes)} from this run. Returns human lines for the notification."""
    rows = conn.execute("SELECT id,ts,app,kind,parent_id,fid,ep_ids,label,orig_path,trash_dest,code FROM regrab WHERE status='pending'").fetchall()
    if not rows: return []
    by_parent = {}
    for it in items: by_parent.setdefault((it["app"], it["parent_id"]), []).append(it)
    out = []; wait = float(cfg("REGRAB_WAIT_DAYS", REGRAB_WAIT_DAYS))
    for rid, ts, app, kind, pid, fid, ep_ids, label, orig, dest, code in rows:
        eps = set(json.loads(ep_ids or "[]")); cur = None
        for it in by_parent.get((app, pid), []):
            if kind == "movie" or (eps & set(it["ep_ids"])): cur = it; break
        status = note = None; norm = os.path.normpath(orig or "")
        if cur and not cur.get("missing") and cur.get("path"):
            same_path = os.path.normpath(cur["path"]) == norm
            trash_exists = bool(dest and os.path.isfile(dest))      # a restore moves the trash copy back, so if it is still there the arr imported something
            try: identical = trash_exists and os.path.getsize(dest) == (cur.get("disk_size") or -1)
            except OSError: identical = False
            replaced = (not same_path) or (cur.get("mtime") or 0) > ts or trash_exists
            if replaced:
                codes = found.get((app, cur["fid"]), set()); same_rel = " (the arr re-downloaded the identical release - block that group or pick another manually)" if identical else ""
                if code in codes: status, note = "same_defect", f"replacement {os.path.basename(cur['path'])} still has {code}{same_rel}; original stays in the trash until purge"
                else: status, note = "imported", f"replacement {os.path.basename(cur['path'])} passes {code}" + (f" (other findings: {', '.join(sorted(codes))})" if codes else " - clean")
            elif same_path and os.path.exists(cur["path"]): status, note = "restored", "original is back in place (restored by hand or via the UI)"
        if status is None and (started - ts) > wait * 86400:
            if dest and os.path.isfile(dest) and not os.path.exists(orig):
                try:
                    restore_file(dest, orig); conn.execute("DELETE FROM probe WHERE path=?", (orig,)); resc = rescan_for_path(apps, orig)
                    status, note = "restored", f"nothing imported in {wait:g} days - original restored from the trash; {resc}"
                except Exception as e: status, note = "restore_failed", err_result(e)
            elif os.path.exists(orig): status, note = "restored", "original is back in place"
            else: status, note = "lost", "nothing imported and the quarantined copy is gone (purged or moved by hand)"
        if status:
            conn.execute("UPDATE regrab SET status=?, note=?, checked_at=? WHERE id=?", (status, note, started, rid))
            record_action(conn, "on", "regrab-check", app, fid, label, f"{code} re-grab outcome", f"{status}: {note}"); out.append(f"{label}: {status} - {note}")
            queue_plex((cur or {}).get("path") or orig)
    conn.commit(); return out
def record_action(conn, mode, kind, app, target, label, detail, result):
    if str(result).startswith("ERROR") and sys.exc_info()[1] is not None: note_error(f"{kind} {label}: {str(result)[:160]}")
    conn.execute("INSERT INTO action VALUES(?,?,?,?,?,?,?,?)", (time.time(), mode, kind, app, str(target), label, detail, result)); conn.commit()
    msg = f"[{mode}] {kind} {app} {label}: {detail} -> {result}"; log("ACTION", msg); progress(action=msg)

# ---------- ffprobe ----------
PROBE_ENTRIES = ("format=duration,size,format_name:format_tags=title:"
    "stream=index,codec_type,codec_name,profile,pix_fmt,color_transfer,field_order,start_time,width,height,channels,disposition:"
    "stream_tags=language,title:stream_side_data=side_data_type,dv_profile,dv_bl_signal_compatibility_id")
def ffprobe(path):
    try: p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", PROBE_ENTRIES, "-of", "json", path], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired: return False, {"error": "ffprobe timeout"}
    if p.returncode != 0: return False, {"error": (p.stderr or "ffprobe failed").strip()[:300]}
    try: return True, json.loads(p.stdout)
    except Exception as e: return False, {"error": f"bad ffprobe json {e}"}
def probe_all(conn, items):
    todo = []; cache = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT path,size,mtime,ver FROM probe")}
    for it in items:
        p = it["path"]
        if not p: continue
        try: st = os.stat(p)
        except FileNotFoundError: it["missing"] = True; continue
        except OSError as e: it["stat_err"] = str(e); continue
        it["disk_size"] = st.st_size; it["mtime"] = st.st_mtime; c = cache.get(p)
        if c is None or c[0] != st.st_size or abs((c[1] or 0) - st.st_mtime) > 1 or (c[2] or 1) != PROBE_VERSION: todo.append(it)
    todo = todo[:PROBE_MAX]
    log(f"ffprobe: {len(todo)} new/changed files (workers={PROBE_WORKERS})"); progress("ffprobe", f"{len(todo)} new/changed files", 0, len(todo))
    done = 0
    def work(it): ok, data = ffprobe(it["path"]); return it, ok, data
    with ThreadPoolExecutor(PROBE_WORKERS) as ex:
        for it, ok, data in ex.map(work, todo):
            conn.execute("INSERT OR REPLACE INTO probe(path,size,mtime,probed_at,ok,data,ver) VALUES(?,?,?,?,?,?,?)", (it["path"], it["disk_size"], it["mtime"], time.time(), int(ok), json.dumps(data), PROBE_VERSION))
            done += 1
            if done % 25 == 0: progress(done=done, detail=os.path.basename(it["path"]))
            if done % 200 == 0: conn.commit(); log(f"  probed {done}/{len(todo)}")
    conn.commit()
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path,ok,data FROM probe")}
    for it in items:
        r = rows.get(it.get("path"))
        if r: it["probe_ok"] = bool(r[0]); it["probe"] = json.loads(r[1])
    return len(todo)
def external_subs(path):
    d, base = os.path.split(path); stem = os.path.splitext(base)[0].lower(); out = set()
    try: names = os.listdir(d)
    except OSError: return out
    for n in names:
        ln = n.lower(); ext = os.path.splitext(ln)[1]
        if ext in SUB_EXT and ln.startswith(stem):
            rest = ln[len(stem):-len(ext)].strip(".").split("."); tags = [t for t in rest if t not in ("hi", "sdh", "forced", "cc", "default")]
            out.add(canon(tags[0]) if tags and len(tags[0]) in (2, 3) else "und")
    return out

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
EN_WORDS = {"the", "you", "and", "that", "what", "this", "with", "have", "just", "your", "they", "about", "don't", "it's", "i'm", "we're", "going", "right", "know", "there", "think"}
def sniff_sub(path, stream_index, seconds=300):
    """Sample a text subtitle stream and guess (language, cues_per_min). Language from script + English stop-word density."""
    o = subprocess.run(["ffmpeg", "-v", "error", "-t", str(seconds), "-i", path, "-map", f"0:{stream_index}", "-f", "srt", "-"], capture_output=True, text=True, timeout=240).stdout
    cues = [b for b in o.split("\n\n") if "-->" in b]
    text = " ".join(re.sub(r"<[^>]*>|\{[^}]*\}|\\N", " ", "\n".join(b.split("\n")[2:])) for b in cues)
    letters = sum(ch.isalpha() for ch in text) or 1; words = re.findall(r"[A-Za-z']+", text.lower())
    kana = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", text)); cyr = len(re.findall(r"[\u0400-\u04ff]", text))
    ascii_ratio = sum(ch.isascii() and ch.isalpha() for ch in text) / letters; en = sum(1 for w in words if w in EN_WORDS) / (len(words) or 1)
    if len(cues) < 5: lang = None
    elif kana / letters > 0.2: lang = "jpn"
    elif cyr / letters > 0.3: lang = "rus"
    elif ascii_ratio > 0.9 and en > 0.06: lang = "eng"
    else: lang = None
    return lang, len(cues) / (seconds / 60)
def is_signs(st):
    """Forced / signs & songs tracks are not dialogue subtitles."""
    return bool((st.get("disposition") or {}).get("forced")) or bool(re.search(r"sign|song|forced", (st.get("tags") or {}).get("title") or "", re.I))
def tag_subs(path, streams=None):
    """Tag every untagged text subtitle track in an mkv with the language sniffed from its text. Returns (changes, note)."""
    if not path.lower().endswith(".mkv"): return [], "not an mkv"
    ok, pr = ffprobe(path)
    if not ok: return [], f"ffprobe failed: {pr}"
    subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle"]; cmd = ["mkvpropedit", path]; changes = []
    for n_sub, st in enumerate(subs, 1):
        if canon((st.get("tags") or {}).get("language", "und") or "und") not in ("und", "") or st.get("codec_name") not in TEXT_SUB_CODECS: continue
        lang, per_min = sniff_sub(path, st["index"])
        if not lang: changes.append(f"s{n_sub}: language unclear, left alone"); continue
        cmd += ["--edit", f"track:s{n_sub}", "--set", f"language={lang}"]; what = f"s{n_sub}={lang}"
        if per_min < 4 and not is_signs(st): cmd += ["--set", "flag-forced=1"]; what += " (sparse -> forced/signs)"
        changes.append(what)
    if len(cmd) == 2: return [], "; ".join(changes) or "no untagged text subtitle tracks"
    guard_write(f"{cmd[0]} on a media file"); p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if p.returncode not in (0, 1): return [], f"mkvpropedit failed: {(p.stderr or p.stdout)[-200:]}"
    return [c for c in changes if "=" in c], "; ".join(changes)
REALDUR_CACHE = {}   # path -> (mtime, dur), loaded per run from the realdur table
REALDUR_BUDGET = {"left": 0}   # new end-of-stream probes allowed this run (each costs ~10 s on shfs)
REALDUR_MAX = int(E("REALDUR_MAX", "40"))
# ---------- F32: subtitles painted into the picture ----------
HARDSUB_CACHE = {}; HARDSUB_BUDGET = {"left": 0}
def frame_band(path, t, w=320):
    """The middle 70 % of the lower 28 % of the picture at time t, small and grayscale (raw bytes, width, height). Corner logos fall outside it."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-vf", f"crop=iw*0.7:ih*0.28:iw*0.15:ih*0.72,scale={w}:-2,format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=60).stdout
    return out, w, (len(out) // w if w else 0)
def text_score(buf, w, h):
    """How much the band looks like subtitle lettering: bright pixels with a dark pixel right beside them (outlined type), dense over a run of rows. -> (density, (top, bottom))"""
    if not h: return 0.0, (0, 0)
    rows = []
    for y in range(h):
        r = buf[y * w:(y + 1) * w]; hits = 0
        for x in range(1, w - 1):
            if r[x] > 190 and (r[x - 1] < 80 or r[x + 1] < 80): hits += 1
        rows.append(hits)
    best = 0.0; where = (0, 0)
    for y0 in range(h):
        acc = 0
        for y1 in range(y0, min(h, y0 + max(6, int(h * 0.5)))):
            acc += rows[y1]
            if y1 - y0 >= 4:
                d = acc / ((y1 - y0 + 1) * w)
                if d > best: best, where = d, (y0, y1)
    return best, where
def hardsub_verdict(path, dur, mtime=None, threshold=0.03, frames=8):
    """Eight frames between 12 % and 88 % of the file: lettering in at least three of them, in about the same rows, means burned-in subtitles.
    -> ('burned' | 'clean' | 'unknown', best score, time of the strongest frame). Cached per file + mtime; budgeted per night."""
    key = os.path.normpath(path); mt = mtime or (os.path.getmtime(path) if os.path.exists(path) else 0)
    hit = HARDSUB_CACHE.get(key)
    if hit and abs(hit[0] - mt) < 1: return hit[1], hit[2], hit[3]
    if HARDSUB_BUDGET["left"] <= 0 or not dur or dur < 20: return "unknown", 0.0, 0.0
    HARDSUB_BUDGET["left"] -= 1; scores = []
    try:
        for i in range(frames):
            t = dur * (0.12 + 0.76 * i / (frames - 1)); b, w, h = frame_band(path, t); sc, where = text_score(b, w, h); scores.append((sc, t, where))
    except Exception as e: log("hardsub:", repr(e)); return "unknown", 0.0, 0.0
    pos = [x for x in scores if x[0] >= threshold]
    same_band = len(pos) >= 3 and (max(p[2][0] for p in pos) - min(p[2][0] for p in pos)) <= 12
    verdict = "burned" if same_band else "clean"; best = max(scores, key=lambda x: x[0])
    HARDSUB_CACHE[key] = (mt, verdict, best[0], best[1]); return verdict, best[0], best[1]
def load_hardsub(conn):
    HARDSUB_CACHE.clear(); HARDSUB_CACHE.update({r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute("SELECT path, mtime, verdict, score, best_t FROM hardsub")}); HARDSUB_BUDGET["left"] = int(setting("HARDSUB_MAX")) if setting("HARDSUB_CHECK") == "on" else 0
def flush_hardsub(conn):
    conn.executemany("INSERT OR REPLACE INTO hardsub VALUES(?,?,?,?,?,?)", [(p_, v[0], v[1], v[2], v[3], time.time()) for p_, v in HARDSUB_CACHE.items()]); conn.commit()
def real_duration(path, mtime=None, conn=None):
    """Where the streams actually end (last audio/video packet pts), independent of the container's duration field. Seeks to the end; cached per file+mtime."""
    try: mt = mtime if mtime is not None else os.stat(path).st_mtime
    except OSError: mt = 0
    c = REALDUR_CACHE.get(path)
    if c and abs(c[0] - mt) < 1 and c[1]: return c[1]
    if RUN_LOCK.locked():                                  # inside the nightly run: respect the per-run budget (each probe ~10 s)
        if REALDUR_BUDGET["left"] <= 0: return None
        REALDUR_BUDGET["left"] -= 1
    best = 0.0
    for sel in ("a:0", "v:0"):
        try:
            o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries", "packet=pts_time", "-of", "csv=p=0", "-read_intervals", "99999", path], capture_output=True, text=True, timeout=120).stdout.strip().splitlines()
            if o: best = max(best, float(o[-1]))
        except Exception: pass
    REALDUR_CACHE[path] = (mt, best or None)
    if conn is not None:
        try: conn.execute("INSERT OR REPLACE INTO realdur VALUES(?,?,?,?)", (path, mt, best or None, time.time()))
        except Exception: pass
    return best or None
# ---------- checks ----------
def intro_boundary(path, off, window=0.35):
    """Is there a cut to/from black in the picture at ~`off` seconds? If the audio delay equals the length of the opening
    ident/logo segment, the source's audio track simply starts after it and the delay is intentional (seen on HBO Max via AMZN:
    FLUX and Kitsune both carry 4.107 s on The Pitt, video black boundary at 4.004 s). Returns the boundary time or None."""
    try:
        o = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-t", str(off + 3), "-i", path, "-map", "0:v:0", "-vf", "blackdetect=d=0.2:pix_th=0.10", "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", o.stderr):
            for b in (float(m.group(1)), float(m.group(2))):
                if abs(b - off) <= window: return b
    except Exception as e: log("intro_boundary:", repr(e))
    return None
def quality_floor(res): return {2160: (3000, 2000), 1080: (1800, 1000), 720: (1200, 690)}.get(res)
def norm_tokens(s): return {t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if len(t) > 2 and t not in ("the", "and", "episode", "part")}
def season_runtime_profile(items):
    """(app,parent,season) -> share of files whose ffprobe duration is < 0.6 of expected. High share = TVDB runtime is wrong for that season."""
    import collections; per = collections.defaultdict(list)
    for it in items:
        if it["kind"] != "series" or not it.get("expected_lo"): continue
        pr = it.get("probe") or {}
        try: dur = float((pr.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError): dur = 0
        if dur: per[(it["app"], it["parent_id"], it.get("season"))].append(dur < it["expected_lo"] * 0.6)
    return {k: (sum(v) / len(v), len(v)) for k, v in per.items()}
def series_has_files(s):
    """Whether to ask for a series' files. Sportarr's summary can say 0 files while it holds them (seen 2026-11-12 on a 145 GB series),
    and skipping the file list then turns every file of that series into an 'unknown file', so any size on disk counts too."""
    st = s.get("statistics") or {}
    return bool(st.get("episodeFileCount") or st.get("sizeOnDisk"))

def eps_for_file(f, eps_by_file, all_eps):
    """The episodes a file holds. Sonarr links each episode to one file; Sportarr splits an event into part files (pt1, pt2, pt3)
    and links the event to the first part only, so the other parts are matched by the season and episode number on the file record."""
    eps = eps_by_file.get(f["id"]) or []
    if not eps and f.get("episodeNumber") is not None:
        eps = [e for e in all_eps if e.get("seasonNumber") == f.get("seasonNumber") and e.get("episodeNumber") == f.get("episodeNumber")]
    return eps

def series_language_profile(items):
    """(app,parent_id) -> (dominant audio-language set, share of files having it). Uses ffprobe tags, falls back to arr."""
    import collections; per = collections.defaultdict(list)
    for it in items:
        if it["kind"] != "series": continue
        pr = it.get("probe") or {}; a = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"] if it.get("probe_ok") else []
        langs = canon_set((s.get("tags") or {}).get("language", "und") or "und" for s in a) if a else canon_set((it["mi"].get("audioLanguages") or "").split("/"))
        per[(it["app"], it["parent_id"])].append(frozenset(langs - {"und"}))
        per[(it["app"], it["parent_id"], it.get("season"))].append(frozenset(langs - {"und"}))
    out = {}
    for k, sets in per.items():
        c = collections.Counter(sets); top, n = c.most_common(1)[0]; out[k] = (set(top), n / len(sets), len(sets))
    return out
def check(it, bz_eps, bz_movs, profile=None, rt_profile=None):
    out = []; F = lambda code, sev, detail: out.append((code, sev, detail))
    app, mi, q, intent = it["app"], it["mi"], it["quality"], it["intent"]
    it["needs_rescan"] = False
    if intent["ignore"]: return out
    if it.get("missing"): F("MISSING_FILE", "high", "path not found on disk (arr still lists it)"); it["needs_rescan"] = True; return out
    if it.get("disk_size") == 0: F("ZERO_SIZE", "critical", "0-byte file"); return out
    pr = it.get("probe"); ok = it.get("probe_ok")
    if pr is not None and not ok: F("CORRUPT_HEADER", "high", f"ffprobe: {pr.get('error','?')} (Healarr should pick this up)"); return out
    streams = (pr or {}).get("streams", []) if ok else []; fmt = (pr or {}).get("format", {}) if ok else {}
    v = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    a = [s for s in streams if s.get("codec_type") == "audio"]; sub_embedded = [s for s in streams if s.get("codec_type") == "subtitle"]
    if ok:
        if not v: F("NO_VIDEO", "critical", "no video stream")
        if not a: F("NO_AUDIO", "critical", "no audio stream")
    elif mi and (mi.get("audioStreamCount") or 0) == 0: F("NO_AUDIO", "critical", "arr mediaInfo: 0 audio streams")
    audio = canon_set((s.get("tags") or {}).get("language", "und") or "und" for s in a) if (ok and a) else canon_set((mi.get("audioLanguages") or "").split("/"))
    subs = canon_set((s.get("tags") or {}).get("language", "und") or "und" for s in sub_embedded if not is_signs(s)) if ok else canon_set((mi.get("subtitles") or "").split("/"))
    und_text = [s for s in sub_embedded if canon((s.get("tags") or {}).get("language", "und") or "und") in ("und", "") and s.get("codec_name") in TEXT_SUB_CODECS and not is_signs(s)] if ok else []
    it["und_subs"] = len(und_text)
    ext = external_subs(it["path"]) if it.get("path") else set(); subs_all = subs | ext
    if ok and a and (not mi or not mi.get("audioLanguages")) and (audio - {"und"}): it["needs_rescan"] = True   # arr never saw the languages
    if app not in SKIP_LANG_APPS and (a or mi):
        real = audio - {"und"}; acceptable = PREF_AUDIO | ({it["orig"]} if it["orig"] else set()) | ({"jpn"} if it.get("series_type") == "anime" else set())
        if intent["lang"]:
            if real and intent["lang"] not in real:
                sdom, sshare, sn = (profile or {}).get((app, it["parent_id"], it.get("season")), (set(), 0, 0))
                if it["kind"] == "series" and sn >= 3 and sshare >= 0.8 and sdom == real: F("DUB_NOT_WANTED", "low", f"tagged audit-lang-{intent['lang']} but audio={sorted(real)} - {sshare:.0%} of this season's {sn} files are {sorted(real)}: this season is simply in a different language than the tag")
                else: F("DUB_NOT_WANTED", "medium", f"tagged audit-lang-{intent['lang']} but audio={sorted(real)}")
        elif real and not (real & acceptable):
            dom, share, n = (profile or {}).get((app, it["parent_id"]), (set(), 0, 0))
            sdom, sshare, sn = (profile or {}).get((app, it["parent_id"], it.get("season")), (set(), 0, 0))
            if it["kind"] == "series" and ((sn >= 3 and sshare >= 0.8 and sdom == real) or (n >= 3 and share >= 0.8 and dom == real)):
                scope = f"this season's {sn}" if (sn >= 3 and sshare >= 0.8 and sdom == real) else f"this series' {n}"
                F("WRONG_LANGUAGE", "low", f"audio={sorted(real)} original={it['orig'] or '?'} - but {max(sshare, share):.0%} of {scope} files are {sorted(real)}, so probably intended / metadata wrong. Tag the series audit-lang-{sorted(real)[0]} to accept, or fix its language in the arr")
            elif not (real & DUB_LANGS):
                F("WRONG_LANGUAGE", "low", f"audio tagged {sorted(real)} (not a language anyone dubs into) with eng subs={bool(subs_all & PREF_SUBS)} - almost certainly a mis-tag by the release group; re-grabbing fetches the same file. Tag the series audit-ignore or fix the tag with mkvpropedit")
            else:
                F("WRONG_LANGUAGE", "high", f"audio={sorted(real)} original={it['orig'] or '?'} wanted={sorted(acceptable)}" + (f" - rest of series is {sorted(dom)}" if dom else ""))
        is_anime = it.get("series_type") == "anime" or lib_of(it.get("path") or "", app) == "Anime"
        if is_anime and setting("ANIME_ORIGINAL_AUDIO") == "on" and not intent["lang"] and it["orig"] != "eng":
            accept_oa = {"jpn"} | ({it["orig"]} if it["orig"] else set())
            if real and not (real & accept_oa):
                F("DUB_ONLY", "medium", f"anime with audio={sorted(real)} and no {'/'.join(sorted(accept_oa))} track - dubbed-only release; re-grab fetches a Japanese-audio or dual-audio copy (Sonarr's profile already scores 'Dubs Only' -10000)")
        hunting = (it["kind"] == "series" and any(e in bz_eps for e in it["ep_ids"])) or (it["kind"] == "movie" and it["parent_id"] in bz_movs)
        if not intent["nosubs"]:
            has_pref_audio = bool(audio & PREF_AUDIO); has_pref_sub = bool(subs_all & PREF_SUBS) or ("und" in ext)
            burned = ""
            if real and not has_pref_audio and not has_pref_sub or (not real and audio and not subs_all):
                try: hv = hardsub_verdict(it["path"], float(((it.get("probe") or {}).get("format") or {}).get("duration") or 0), it.get("mtime")) if it.get("path") and os.path.exists(it["path"]) else ("unknown", 0, 0)
                except Exception: hv = ("unknown", 0, 0)
                if hv[0] == "burned": burned = f" | subtitles burned into the picture (language unknown; seen at {hv[2]:.0f}s)"
            if real and not has_pref_audio and not has_pref_sub:
                F("NO_SUBS", "low" if burned else ("medium" if hunting else "high"), f"audio={sorted(real)} embedded_subs={sorted(subs)[:6]} external={sorted(ext)}" + burned + (f" | {len(und_text)} untagged text subtitle track(s) inside - 'tag subs' reads the text and labels it (nightly SUBTAG does this too)" if und_text else "") + (" | Bazarr still searching" if hunting else " | not in Bazarr wanted list"))
            elif not real and audio and not subs_all:
                F("UND_AUDIO_NO_SUBS", "low", "audio language untagged, no subtitles anywhere" + (" | Bazarr searching" if hunting else "") + burned)
    dur = None
    try: dur = float(fmt["duration"]) if fmt.get("duration") else None
    except (TypeError, ValueError, KeyError): pass
    mi_rt = hms(mi.get("runTime"))
    if dur is None: dur = mi_rt
    exp = it.get("expected") or 0; exp_lo = it.get("expected_lo") or exp
    _share, _n = (rt_profile or {}).get((app, it["parent_id"], it.get("season")), (0, 0))
    _short_hi = dur and exp_lo and dur < exp_lo * (0.6 if it["kind"] == "series" else 0.72) and exp_lo - dur > 240 and not (it["kind"] == "series" and _n >= 3 and _share >= 0.8)
    _long = dur and exp and dur > exp * 1.4 and dur - exp > 480 and not it.get("edge")
    if dur and exp and app not in SKIP_RUNTIME_APPS and it.get("path") and ok and (_long or _short_hi):
        rd = real_duration(it["path"], it.get("mtime"))
        if rd and abs(rd - dur) > 60 and (abs(rd - exp) <= max(240, 0.4 * exp) or abs(rd - dur) > 0.25 * dur):   # only when the header actually misleads
            F("BAD_DURATION_HEADER", "medium", f"container header says {dur/60:.0f} min but the streams end at {rd/60:.0f} min (expected {exp/60:.0f}) - the header is wrong, Plex shows the wrong length and seeking misbehaves; 'fix header' rewrites it losslessly")
            dur = rd; it["real_dur"] = rd; mi_rt = None   # the arr's runtime comes from the same bad header - no separate RUNTIME_DISAGREE
        elif rd and abs(rd - dur) > 60: dur = rd; mi_rt = None   # small header slop: judge the runtime by where the streams really end
    if dur and exp and app not in SKIP_RUNTIME_APPS:
        if exp > 600 and (dur < 180 or (it.get("disk_size") or it["size"]) < 120e6):
            F("SAMPLE_FILE", "high", f"{(it.get('disk_size') or it['size'])/1e6:.0f} MB, {dur/60:.1f} min where {exp/60:.0f} min expected - sample/stub?")
        elif dur < exp_lo * (0.6 if it["kind"] == "series" else 0.72) and exp_lo - dur > 240:
            share, n = (rt_profile or {}).get((app, it["parent_id"], it.get("season")), (0, 0))
            if it["kind"] == "series" and n >= 3 and share >= 0.8:
                F("RUNTIME_SHORT", "low", f"file {dur/60:.0f} min vs expected {exp_lo/60:.0f} min - but {share:.0%} of this season's {n} files are short the same way, so the metadata runtime is wrong, not the files")
            else:
                F("RUNTIME_SHORT", "high", f"file {dur/60:.0f} min vs expected {exp_lo/60:.0f} min ({it['n_eps'] or 1} ep) - truncated or wrong content?" + (" (rest of season is full length)" if n >= 3 else ""))
        elif dur > exp * 1.4 and dur - exp > 480:
            if it.get("edge"): F("RUNTIME_LONG", "low", f"file {dur/60:.0f} min vs expected {exp/60:.0f} min - season premiere/finale, extended episodes are normal there")
            else: F("RUNTIME_LONG", "medium", f"file {dur/60:.0f} min vs expected {exp/60:.0f} min ({it['n_eps'] or 1} ep) - extended cut / double episode / wrong content?")
    if dur and mi_rt and abs(dur - mi_rt) > max(120, 0.1 * dur):
        F("RUNTIME_DISAGREE", "medium", f"ffprobe {dur/60:.0f} min vs arr mediaInfo {mi_rt/60:.0f} min - file changed since arr scanned it"); it["needs_rescan"] = True
    w = h = 0
    if v: w, h = int(v[0].get("width") or 0), int(v[0].get("height") or 0)
    if not w:
        m = re.match(r"(\d+)x(\d+)", mi.get("resolution") or "")
        if m: w, h = int(m.group(1)), int(m.group(2))
    fl = quality_floor(q.get("resolution"))
    if w and fl and w < fl[0] and h < fl[1]: F("RES_MISLABELED", "medium", f"{w}x{h} but tagged {q.get('name')}")
    if ok and v and a:
        try:
            vs = float(v[0].get("start_time") or 0); off = max(abs(float(s.get("start_time") or 0) - vs) for s in a)
            if off > 1.5:
                b = intro_boundary(it["path"], off) if it.get("path") else None
                if b is not None: F("AUDIO_OFFSET", "low", f"audio delay {off:.2f}s matches the end of the intro segment at {b:.2f}s - intentional: the source's audio starts after the logo, players that honour timestamps are in sync. Do not 'fix sync'; if one player is off, it is that player's transcode path")
                else: F("AUDIO_OFFSET", "low", f"audio track starts {off:.2f}s after/before video with no matching cut in the picture - spot-check sync on a direct-play device; 'fix sync' removes the delay losslessly")
        except (TypeError, ValueError): pass
    # embedded title vs expected title
    ttl = ((fmt.get("tags") or {}).get("title") or "").strip()
    if ok and ttl and len(norm_tokens(ttl)) >= 3 and app not in SKIP_TITLE_APPS:
        want = norm_tokens(it["title"]) | set().union(*[norm_tokens(t) for t in it["ep_titles"]]) if it["ep_titles"] else norm_tokens(it["title"])
        got = norm_tokens(ttl); fname = norm_tokens(os.path.basename(it["path"] or ""))
        if want and not (got & want) and len(got & fname) / len(got) < 0.3:
            F("TITLE_MISMATCH", "low", f"embedded title '{ttl[:60]}' does not match '{it['title']}'" + (f" / {(it['ep_titles'][0] or '')[:40]}" if it["ep_titles"] and it["ep_titles"][0] else ""))
    # the container behind the name (2026-11-12: MP4 releases saved as .mkv failed the default-flags edit every pass)
    if ok and (it.get("path") or "").lower().endswith(".mkv") and not is_matroska(it):
        fmt_ = str(((pr or {}).get("format") or {}).get("format_name") or "mov,mp4")
        F("MP4_IN_MKV", "low", f"container is {'MP4' if fmt_.startswith('mov') else fmt_.split(',')[0]} under a .mkv name - default tracks cannot be set and some players misread it; 'Rewrap the file' puts it in a real Matroska file without re-encoding")
    # player compatibility
    if PLAYER_CHECKS and ok and v:
        v0 = v[0]; codec = (v0.get("codec_name") or "").lower(); pix = v0.get("pix_fmt") or ""
        for sd in v0.get("side_data_list") or []:
            if "DOVI" in (sd.get("side_data_type") or "").replace(" ", "").upper():
                prof = sd.get("dv_profile")
                if prof == 5: F("DV_PROFILE5", "high", "Dolby Vision profile 5 (IPT, no HDR10 base layer) - green/purple on non-DV players, Plex cannot tone-map it")
                elif prof == 7: F("DV_PROFILE7", "low", f"Dolby Vision profile 7 (dual layer, compat id {sd.get('dv_bl_signal_compatibility_id')}) - most players fall back to HDR10")
        if codec == "h264" and "10" in pix: F("HI10P", "medium", f"10-bit H.264 ({pix}) - no hardware decode on any player, Plex software-transcodes")
        if codec == "av1": F("AV1", "low", "AV1 video - PS5 / older Shield / older TVs need a transcode")
        if (v0.get("field_order") or "progressive") not in ("progressive", "unknown", ""): F("INTERLACED", "low", f"interlaced video ({v0.get('field_order')})")
        def lossless(st): return (st.get("codec_name") or "") in ("truehd", "mlp") or ((st.get("codec_name") == "dts") and "MA" in (st.get("profile") or ""))
        if a and all(lossless(s) for s in a):
            F("TRUEHD_ONLY", "low", f"only lossless audio ({', '.join(sorted({(s.get('profile') or s.get('codec_name') or '?') for s in a}))}) - no AC3/EAC3 fallback track, PS5/Shield transcode")
    return apply_device_profiles(out)

def orphan_scan(items, roots):
    """Walk the arr root folders for video files no arr manages."""
    known = {os.path.normpath(it["path"]) for it in items if it.get("path")}; known_dirs = {}
    for it in items:   # only files that are really there can be a 'twin' - an arr entry whose file is gone is a stale record, not a duplicate
        if it.get("path") and not it.get("missing"): known_dirs.setdefault(os.path.dirname(os.path.normpath(it["path"])), []).append(os.path.basename(it["path"]).lower())
    out = []; seen_roots = set(); n = 0
    for app, rs in roots.items():
        for root in rs:
            root = os.path.normpath(root)
            if root in seen_roots or not os.path.isdir(root): continue
            seen_roots.add(root); progress("orphans", f"walking {root}")
            for d, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if x.lower() not in EXTRAS_DIRS and not x.startswith(".")]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXT or f.startswith("."): continue
                    full = os.path.normpath(os.path.join(d, f)); n += 1
                    if full in known: continue
                    lf = f.lower()
                    if "-trailer" in lf or re.search(r"\b(trailer|featurette|deleted scene|behind the scenes)\b", lf): continue
                    try: size = os.path.getsize(full)
                    except OSError: size = 0
                    m = re.search(r"s(\d{1,2})e(\d{1,3})", lf); code = "ORPHAN_FILE"; sev = "medium"
                    if m and any(re.search(rf"s0*{int(m.group(1))}e0*{int(m.group(2))}\b", k) for k in known_dirs.get(d, [])): code, sev = "DUPLICATE_VIDEO", "medium"
                    if "sample" in lf or size < 50e6: sev = "low"
                    if code == "ORPHAN_FILE" and m and any(re.search(rf"s0*{int(m.group(1))}e0*{int(m.group(2))}\b", k) for k in {os.path.basename(it["path"]).lower() for it in items if it.get("path") and it.get("missing") and os.path.dirname(os.path.normpath(it["path"])) == d}):
                        out.append((code, "low", full, f"{size/1e6:.0f} MB - the arr's record for this episode points at a file that is gone (re-grab in progress / import not finished); this is probably the replacement - leave it, the arr will adopt it on rescan")); continue
                    out.append((code, sev, full, f"{size/1e6:.0f} MB in {app} root {root} - not managed by any arr" + (" (same episode as a managed file)" if code == "DUPLICATE_VIDEO" else "")))
    log(f"orphan scan: {n} video files on disk, {len(out)} unmanaged"); return out

ARR_GAP = "the arr did not list this file"   # marker in the detail: no bulk Move to trash, Rescan in the arr comes first (#30)
def arr_folder_of(path, folders=None):
    """The (folder, app, title) of the series/movie folder an arr lists that holds `path`, or None."""
    folders = ARR_FOLDERS if folders is None else folders; d = os.path.dirname(os.path.normpath(path))
    while d and d != os.path.dirname(d):
        if d in folders: return (d,) + tuple(folders[d])
        d = os.path.dirname(d)
    return None
def gap_detail(code, sev, full, detail, folders=None):
    """An unknown file inside a folder an arr lists as a series or movie: when the arr answers incompletely (Sportarr, 2026-11-12)
    every file of that series looks unmanaged. Say so, and keep the finding low until a rescan settles it."""
    hit = arr_folder_of(full, folders) if code == "ORPHAN_FILE" else None
    if not hit: return sev, detail
    folder, app, title = hit
    return "low", f"{detail.split(' in ')[0]} inside {app}'s folder for '{title}' ({folder}) but {ARR_GAP} - probably a reading gap: rescan in {app} first"
def is_gap(f): return f.get("check") == "ORPHAN_FILE" and ARR_GAP in (f.get("detail") or "")

# ---------- quarantine ----------
def quarantine(conn, path, why, kind="regrab", sidecars=True):
    """Move a file (and its sidecars) into TRASH_DIR/<kind>/<date>/<relative path>. Same share -> rename, no copy. Returns dest."""
    guard_write(f"move to trash {path}")
    rel = os.path.relpath(path, "/data"); dest = os.path.join(TRASH_DIR, kind, datetime.date.today().isoformat(), rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    moved = []
    d, base = os.path.split(path); stem = os.path.splitext(base)[0]
    for n in os.listdir(d):
        if n == base or (sidecars and n.startswith(stem + ".") and os.path.splitext(n)[1].lower() in SUB_EXT | {".nfo", ".jpg", ".png"}):
            src = os.path.join(d, n); dst = os.path.join(os.path.dirname(dest), n)
            try: os.rename(src, dst)
            except OSError:
                import shutil; shutil.move(src, dst)
            try: os.utime(dst, None)                       # stamp the quarantine time; purge must never see the original mtime
            except OSError: pass
            moved.append(n)
    with open(os.path.join(TRASH_DIR, "LOG.txt"), "a") as f: f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{why}\t{path}\t->\t{dest}\n")
    return dest, moved
def backup_state(conn, keep=14):
    """Consistent nightly snapshot of state.db (+ ignore.txt) into /config/backups so the appdata backup grabs a clean copy."""
    try:
        d = os.path.join(CONFIG_DIR, "backups"); os.makedirs(d, exist_ok=True); stamp = datetime.date.today().isoformat()
        dst = sqlite3.connect(os.path.join(d, f"state-{stamp}.db")); conn.backup(dst); dst.close()
        if os.path.exists(IGNORE_FILE): import shutil; shutil.copy(IGNORE_FILE, os.path.join(d, f"ignore-{stamp}.txt"))
        for f in sorted(x for x in os.listdir(d) if x.startswith("state-"))[:-keep]:
            os.remove(os.path.join(d, f)); ig = os.path.join(d, f.replace("state-", "ignore-").replace(".db", ".txt"))
            if os.path.exists(ig): os.remove(ig)
        return os.path.join(d, f"state-{stamp}.db")
    except Exception as e: log("backup:", repr(e)); return None
def purge_trash(conn):
    if not os.path.isdir(TRASH_DIR): return 0
    today = datetime.date.today(); n = 0; freed = 0
    for d, dirs, files in os.walk(TRASH_DIR, topdown=False):
        parts = os.path.relpath(d, TRASH_DIR).split(os.sep)
        kind, day = (parts[0], parts[1]) if len(parts) > 1 and parts[0] in TRASH_KEEP else ("regrab", parts[0])   # legacy <date>/ dirs = 14 days
        cutoff_day = (today - datetime.timedelta(days=TRASH_KEEP.get(kind, TRASH_DAYS))).isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day >= cutoff_day: continue      # purge by quarantine date folder only
        for f in files:
            p = os.path.join(d, f)
            if f == "LOG.txt": continue
            try:
                st = os.stat(p)
                if os.path.realpath(p).startswith(os.path.realpath(TRASH_DIR) + os.sep): os.remove(p); n += 1; freed += st.st_size
            except OSError as e: log("purge:", p, e)
        if d != TRASH_DIR and not os.listdir(d):
            try: os.rmdir(d)
            except OSError: pass
    if n: record_action(conn, "on", "purge", "trash", TRASH_DIR, f"{n} files", f"past retention (dedupe {TRASH_DAYS_DEDUPE} d, regrab {TRASH_DAYS} d)", f"freed {freed/1e9:.1f} GB")
    return n
def trash_stats():
    tot = 0; n = 0
    for d, _, files in os.walk(TRASH_DIR) if os.path.isdir(TRASH_DIR) else []:
        for f in files:
            try: tot += os.path.getsize(os.path.join(d, f)); n += 1
            except OSError: pass
    return n, tot

# ---------- actions ----------
def gate(mode_env, name):
    m = (mode_env or "off").lower()
    if m not in ("off", "dryrun", "on"): log(f"{name}: bad mode {m!r}, treating as off"); m = "off"
    return m
def do_rescans(conn, apps, items):
    mode = gate(cfg("ARR_RESCAN", ARR_RESCAN), "ARR_RESCAN")
    if mode == "off": return 0
    now = time.time(); recent = {(r[0], r[1]) for r in conn.execute("SELECT app,parent_id FROM rescan_log WHERE ts>?", (now - RESCAN_COOLDOWN_DAYS * 86400,))}
    targets = {}
    for it in items:
        if it.get("needs_rescan") and (it["app"], it["parent_id"]) not in recent and it["app"] in apps: targets.setdefault((it["app"], it["parent_id"]), it)
    done = 0
    for (app, pid), it in list(targets.items())[:ARR_RESCAN_MAX]:
        base, key, kind = apps[app]; cmd = {"name": "RescanSeries", "seriesId": pid} if kind == "series" else {"name": "RescanMovie", "movieId": pid}
        if mode == "dryrun": record_action(conn, "dryrun", "rescan", app, pid, it["title"], json.dumps(cmd), "would POST /command"); continue
        try:
            r = api(base, key, "command", "POST", cmd, timeout=60); record_action(conn, "on", "rescan", app, pid, it["title"], json.dumps(cmd), f"command id {(r or {}).get('id')} {(r or {}).get('status')}")
            conn.execute("INSERT OR REPLACE INTO rescan_log VALUES(?,?,?)", (app, pid, now)); conn.commit(); done += 1
        except Exception as e: record_action(conn, "on", "rescan", app, pid, it["title"], json.dumps(cmd), err_result(e))
    if len(targets) > ARR_RESCAN_MAX: log(f"rescan: {len(targets) - ARR_RESCAN_MAX} more deferred to next run")
    return done
def do_dedupe(conn, items, run_started):
    """Quarantine unmanaged duplicate video files that sit next to the arr-managed copy of the same episode."""
    mode = gate(cfg("DEDUPE", DEDUPE), "DEDUPE"); mx = int(cfg("DEDUPE_MAX", DEDUPE_MAX)); min_runs = int(cfg("DEDUPE_MIN_RUNS", DEDUPE_MIN_RUNS))
    if mode == "off": return 0
    managed = {}
    for it in items:
        if it.get("path") and not it.get("missing"): managed.setdefault(os.path.dirname(os.path.normpath(it["path"])), []).append(it)
    rows = conn.execute("SELECT id,path,label,seen_runs,detail FROM finding WHERE resolved_at IS NULL AND check_code='DUPLICATE_VIDEO' ORDER BY first_seen").fetchall()
    done = 0; skipped = 0
    for fid, path, label, seen_runs, detail in rows:
        if done >= mx: break
        if (seen_runs or 1) < min_runs: skipped += 1; continue
        m = re.search(r"s(\d{1,2})e(\d{1,3})", os.path.basename(path).lower())
        if not m or not os.path.exists(path): skipped += 1; continue
        twins = [it for it in managed.get(os.path.dirname(os.path.normpath(path)), [])
                 if re.search(rf"s0*{int(m.group(1))}e0*{int(m.group(2))}\b", os.path.basename(it["path"]).lower()) and os.path.normpath(it["path"]) != os.path.normpath(path)]
        twin = next((t for t in twins if t.get("probe_ok") and (t.get("disk_size") or 0) > 0), None)
        if not twin: record_action(conn, mode, "dedupe", "disk", fid, label, "no healthy managed twin found - left alone", "skipped"); skipped += 1; continue
        try: dsize = os.path.getsize(path)
        except OSError: dsize = 0
        plan = f"quarantine {os.path.basename(path)} ({dsize/1e6:.0f} MB); keeping managed {os.path.basename(twin['path'])} ({(twin.get('disk_size') or 0)/1e6:.0f} MB, {twin['app']} {twin['label']})"
        if mode == "dryrun": record_action(conn, "dryrun", "dedupe", "disk", fid, label, plan, "would move to trash"); done += 1; continue
        try:
            dest, moved = quarantine(conn, path, f"dedupe twin={twin['path']}", "dedupe"); queue_plex(path)
            record_action(conn, "on", "dedupe", "disk", fid, label, plan, f"moved {len(moved)} file(s) to {dest}"); done += 1
            conn.execute("UPDATE finding SET resolved_at=? WHERE id=?", (time.time(), fid))
        except Exception as e: record_action(conn, "on", "dedupe", "disk", fid, label, plan, err_result(e))
    if skipped: log(f"dedupe: {skipped} duplicates skipped (not seen {min_runs}+ runs, missing, or no healthy twin)")
    return done
def do_subtag(conn, items, found):
    """Label untagged embedded subtitle tracks so Plex/Bazarr/this audit can see them. Only touches metadata, never the streams.
    Only files whose checks complained about subtitles; each file+mtime is judged once (subtag_seen)."""
    mode = gate(setting("SUBTAG"), "SUBTAG"); mx = int(setting("SUBTAG_MAX"))
    if mode == "off": return 0
    seen = {r[0]: r[1] for r in conn.execute("SELECT path, mtime FROM subtag_seen")}; done = 0; looked = 0
    for it in items:
        if done >= mx or looked >= mx * 3: break
        if not it.get("und_subs") or not (it.get("path") or "").lower().endswith(".mkv") or it["intent"]["ignore"]: continue
        if not (found.get((it["app"], it["fid"]), set()) & {"NO_SUBS", "UND_AUDIO_NO_SUBS"}): continue
        if abs((seen.get(it["path"]) or 0) - (it.get("mtime") or 0)) < 1: continue      # already judged this exact file
        looked += 1
        if mode == "dryrun": record_action(conn, "dryrun", "subtag", it["app"], it["fid"], it["label"], f"{it['und_subs']} untagged subtitle track(s)", "would sniff + tag"); done += 1; continue
        try:
            changes, note = tag_subs(it["path"])
            if changes:
                conn.execute("DELETE FROM probe WHERE path=?", (it["path"],)); it["needs_rescan"] = True; queue_plex(it["path"]); done += 1
                conn.execute("UPDATE finding SET resolved_at=? WHERE resolved_at IS NULL AND app=? AND fid=? AND check_code IN ('NO_SUBS','UND_AUDIO_NO_SUBS')", (time.time(), it["app"], it["fid"]))
            record_action(conn, "on", "subtag", it["app"], it["fid"], it["label"], f"{it['und_subs']} untagged subtitle track(s)", note[:200])
            conn.execute("INSERT OR REPLACE INTO subtag_seen VALUES(?,?,?,?)", (it["path"], os.stat(it["path"]).st_mtime if os.path.exists(it["path"]) else it.get("mtime"), note[:200], time.time()))
        except Exception as e: record_action(conn, "on", "subtag", it["app"], it["fid"], it["label"], "subtitle tagging", err_result(e))
    conn.commit(); return done
def do_remediation(conn, apps, items, run_started):
    mode = gate(cfg("REMEDIATE", REMEDIATE), "REMEDIATE"); mx = int(cfg("REMEDIATE_MAX", REMEDIATE_MAX))
    min_runs = int(cfg("REMEDIATE_MIN_RUNS", REMEDIATE_MIN_RUNS)); min_age = float(cfg("REMEDIATE_MIN_AGE_HOURS", REMEDIATE_MIN_AGE_H)); file_age = float(cfg("REMEDIATE_FILE_MIN_AGE_HOURS", REMEDIATE_FILE_MIN_AGE_H))
    if mode == "off": return 0
    if mode == "on" and REMEDIATE_CONFIRM != "I_UNDERSTAND_FILES_WILL_BE_DELETED":
        log("REMEDIATE=on but REMEDIATE_CONFIRM not set - downgrading to dryrun"); mode = "dryrun"
    cands = []
    prior = {(a_, l_) for a_, l_ in conn.execute("SELECT app, label FROM regrab WHERE status='same_defect' OR ts > ?", (run_started - 30 * 86400,))}   # one automatic attempt per item, never after two releases agreed
    for fid, app, afid, code, sev, label, path, first_seen, seen_runs in conn.execute(
            "SELECT id,app,fid,check_code,severity,label,path,first_seen,seen_runs FROM finding WHERE resolved_at IS NULL AND severity IN ('high','critical')"):
        allowed = set(str(cfg("REMEDIATE_CHECKS", ",".join(REMEDIATE_CHECKS))).split(",")) if "REMEDIATE_CHECKS" in OVR else REMEDIATE_CHECKS
        if code not in allowed or app not in REMEDIATE_APPS or app not in apps or (app, label) in prior: continue
        if (run_started - first_seen) < min_age * 3600 or (seen_runs or 1) < min_runs: continue        # must persist across runs
        it = next((x for x in items if x["app"] == app and x["fid"] == afid), None)
        if not it or it["intent"]["noremediate"] or not it["monitored"] or it.get("missing"): continue
        try: added = datetime.datetime.fromisoformat((it.get("date_added") or "").replace("Z", "+00:00")).timestamp()
        except ValueError: added = 0
        if added and (run_started - added) < file_age * 3600: continue                      # brand-new import, let Healarr/arr settle
        cands.append((it, code, label, path))
    acted = 0
    for it, code, label, path in cands[:mx]:
        app = it["app"]; base, key, kind = apps[app]
        rescan = {"name": "RescanSeries", "seriesId": it["parent_id"]} if kind == "series" else {"name": "RescanMovie", "movieId": it["parent_id"]}
        search = {"name": "EpisodeSearch", "episodeIds": it["ep_ids"]} if kind == "series" else {"name": "MoviesSearch", "movieIds": [it["parent_id"]]}
        plan = f"quarantine {os.path.basename(path or '')} -> {app} {rescan['name']} -> {search['name']}"
        if mode == "dryrun": record_action(conn, "dryrun", "regrab", app, it["fid"], label, f"{code}: {plan}", "would do"); continue
        if kind == "series" and not it["ep_ids"]: record_action(conn, "on", "regrab", app, it["fid"], label, plan, "SKIPPED: no episode ids"); continue
        if not path or not os.path.exists(path): record_action(conn, "on", "regrab", app, it["fid"], label, plan, "SKIPPED: file not on disk"); continue
        try:
            dest, moved = quarantine(conn, path, f"regrab {code} {app}:{label}", "regrab"); queue_plex(path)
            record_regrab(conn, app, kind, it["parent_id"], it["fid"], it["ep_ids"], label, path, dest, code, "nightly")
            r1 = api(base, key, "command", "POST", rescan, timeout=60)
            r2 = api(base, key, "command", "POST", search, timeout=60)
            record_action(conn, "on", "regrab", app, it["fid"], label, f"{code}: {plan}", f"moved {len(moved)} file(s) to {dest}; rescan cmd {(r1 or {}).get('id')}, search cmd {(r2 or {}).get('id')}"); acted += 1
        except Exception as e: record_action(conn, "on", "regrab", app, it["fid"], label, f"{code}: {plan}", err_result(e))
    if len(cands) > mx: log(f"remediate: {len(cands) - mx} more candidates deferred (REMEDIATE_MAX={mx})")
    return acted
def paperclip_token():
    if not os.path.exists(PAPERCLIP_TOKEN_FILE): return None
    raw = open(PAPERCLIP_TOKEN_FILE).read().strip()
    try: return list(json.loads(raw)["credentials"].values())[0]["token"]
    except Exception: return raw
def do_paperclip(conn, new, actions_summary, active):
    mode = gate(cfg("PAPERCLIP", PAPERCLIP), "PAPERCLIP")
    if mode == "off" or not PAPERCLIP_COMPANY: return None
    tok = paperclip_token()
    if not tok: log("paperclip: no token file - skipped"); return None
    rows = conn.execute("SELECT id,severity,check_code,label,detail FROM finding WHERE resolved_at IS NULL AND handed_off=0").fetchall()
    notable = [(r[1], r[2], r[3], r[4]) for r in rows if SEV_RANK.get(r[1], 0) >= SEV_RANK.get(PAPERCLIP_MIN_SEV, 2)]
    ids = [r[0] for r in rows if SEV_RANK.get(r[1], 0) >= SEV_RANK.get(PAPERCLIP_MIN_SEV, 2)]
    if not notable: return None
    today = datetime.date.today().isoformat(); row = conn.execute("SELECT v FROM kv WHERE k='paperclip_last'").fetchone()
    if row and row[0] == today: log("paperclip: already filed an issue today - skipped"); return None
    notable.sort(key=lambda n: -SEV_RANK.get(n[0], 0))
    import collections; per_check = collections.Counter(c for _, c, _, _ in notable)
    body = [f"media-audit has **{len(notable)} {PAPERCLIP_MIN_SEV}+ finding(s) not yet handed off** from the nightly library audit ({active} findings active in total, most are low/medium FYI).", "",
            f"Full report: {REPORT_URL}  (JSON: {REPORT_URL.rsplit('/',1)[0]}/latest.json - filter `findings[].check`)", "",
            "| check | new | meaning |", "|---|---|---|"]
    meaning = {"WRONG_LANGUAGE": "audio is not the original language nor English", "NO_SUBS": "non-English audio and no English subs anywhere",
               "RUNTIME_SHORT": "file much shorter than the episode/movie runtime (truncated or wrong content)", "RUNTIME_LONG": "file much longer (double episode / extended / wrong content)",
               "SAMPLE_FILE": "tiny file where a full episode is expected", "MISSING_FILE": "arr lists a file that is not on disk", "ZERO_SIZE": "0-byte file",
               "CORRUPT_HEADER": "ffprobe cannot read it", "NO_AUDIO": "no audio stream", "NO_VIDEO": "no video stream", "DV_PROFILE5": "Dolby Vision p5, no HDR10 fallback"}
    body += [f"| {c} | {n} | {meaning.get(c, '')} |" for c, n in per_check.most_common()]
    body += ["", "Examples (first 25, rest in the report):", "", "| severity | check | title | detail |", "|---|---|---|---|"]
    body += [f"| {s} | {c} | {l} | {d.replace('|','/')[:140]} |" for s, c, l, d in notable[:25]]
    if actions_summary: body += ["", "Actions media-audit already took this run:", ""] + [f"- {a}" for a in actions_summary[:20]]
    body += ["", "**What to do (READ-ONLY):** verify each finding (ffprobe the file, compare with the arr's file info) and comment which are real and which "
             "are false positives, with one line of evidence each. Do NOT delete, mark-failed, re-grab or move any file: media-audit does its own "
             "remediation through a quarantine, and a second actor deleting files causes loops (this happened on 2026-11-10). If something needs a "
             "change you cannot make read-only (profile, tag, metadata language), ask the owner in a comment and mark the issue done."]
    payload = {"title": f"media-audit {today}: {len(notable)} new library finding(s)", "description": "\n".join(body), "status": "todo", "priority": "medium"}
    if PAPERCLIP_AGENT: payload["assigneeAgentId"] = PAPERCLIP_AGENT
    if mode == "dryrun": record_action(conn, "dryrun", "paperclip", "paperclip", PAPERCLIP_COMPANY[:8], payload["title"], f"{len(notable)} findings", "would POST issue"); return None
    try:
        guard_write("POST paperclip issue"); req = urllib.request.Request(f"{PAPERCLIP_URL}/api/companies/{PAPERCLIP_COMPANY}/issues", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r: res = json.load(r)
        ident = res.get("identifier") or res.get("id")
        conn.execute("INSERT OR REPLACE INTO kv VALUES('paperclip_last',?)", (today,))
        conn.executemany("UPDATE finding SET handed_off=1 WHERE id=?", [(i,) for i in ids]); conn.commit()
        record_action(conn, "on", "paperclip", "paperclip", PAPERCLIP_COMPANY[:8], payload["title"], f"{len(notable)} findings", f"issue {ident}"); return ident
    except Exception as e:
        body_err = getattr(e, "read", lambda: b"")()[:200]
        record_action(conn, "on", "paperclip", "paperclip", PAPERCLIP_COMPANY[:8], payload["title"], f"{len(notable)} findings", f"ERROR {e!r} {body_err!r}"); return None

# ---------- ignore list ----------
def load_ignores():
    rules = []
    if os.path.exists(IGNORE_FILE):
        for line in open(IGNORE_FILE):
            line = re.split(r"(?:^|\s+)#", line.strip(), 1)[0].strip()      # drop trailing "  # comment"
            if not line: continue
            code, _, pat = line.partition(":"); rules.append((code.strip().upper(), re.compile(pat.strip(), re.I) if pat.strip() else None))
    return rules
def ignored(rules, code, label, fid_hash):
    for c, pat in rules:
        if c.lower() == fid_hash: return True
        if c in (code, "*") and (pat is None or pat.search(label)): return True
    return False

def store_links(conn, apps, items, inv):
    """Deep links per file: Plex episode/movie page (by ratingKey) and the title's page in its arr."""
    rows = []
    for it in items:
        base = (ARR_LINK.get(it["app"]) or apps.get(it["app"], ("",))[0] or "").rstrip("/")
        arr = f"{base}/{'series' if it['kind'] == 'series' else 'movie'}/{it['slug']}" if base and it.get("slug") else None
        rows.append((it["app"], it["fid"], plex_link(inv.get(os.path.normpath(it["path"] or ""))) if inv else None, arr))
    for mi_ in MISSING:   # missing items: link to the title's page in its arr (no Plex item exists yet)
        base = (ARR_LINK.get(mi_["app"]) or apps.get(mi_["app"], ("",))[0] or "").rstrip("/")
        if base and mi_.get("slug"): rows.append((mi_["app"], -mi_["id"], None, f"{base}/{'series' if mi_['kind'] == 'series' else 'movie'}/{mi_['slug']}"))
    if inv:   # unmanaged files too (orphans), keyed the same way upsert() keys them
        for path, rk in inv.items(): rows.append(("disk", int(hashlib.sha1(path.encode()).hexdigest()[:8], 16), plex_link(rk), None))
    conn.executemany("INSERT OR REPLACE INTO links VALUES(?,?,?,?)", rows); conn.commit()
# ---------- track preferences (default audio / subtitle) ----------
def wanted_langs(it):
    """(audio languages in order of preference, subtitle language). Rule: a title plays in its ORIGINAL language with English
    subtitles - Korean drama in Korean, Japanese anime in Japanese, Chinese donghua in Chinese (Japanese dub as second choice) -
    and English-original titles in English. An audit-lang-xxx tag on the title overrides everything."""
    orig = it.get("orig") or ""; is_anime = it.get("series_type") == "anime" or lib_of(it.get("path") or "", it["app"]) == "Anime"
    mine = (canon(setting("PREFERRED_AUDIO").split(",")[0].strip()) or "eng"); subs = (canon(setting("PREFERRED_SUBS").split(",")[0].strip()) or "eng")
    tag = (it.get("intent") or {}).get("lang")
    if tag: prefs = [tag]
    elif setting("PREFER_DUBS") == "on": prefs = list(dict.fromkeys([l for l in (mine, orig if orig else None, "jpn" if is_anime else None) if l]))   # my language first, the original as the fallback
    elif is_anime: prefs = list(dict.fromkeys([l for l in (orig if orig not in (mine, "") else None, "jpn") if l]))
    elif orig and orig != mine: prefs = [orig]
    else: prefs = [mine]
    return (prefs, subs)
def _sub_rank(st):
    t = ((st.get("tags") or {}).get("title") or "").lower(); d = st.get("disposition") or {}
    return (1 if (d.get("forced") or "forced" in t or "sign" in t or "song" in t) else 0) + (1 if ("sdh" in t or "cc" in t or d.get("hearing_impaired")) else 0) + (1 if "weeb" in t or "honorific" in t else 0)
def plan_defaults(it):
    """Which audio/subtitle track should be default. Returns (audio_pos, sub_pos, reason) with 1-based positions among audio / subtitle tracks, or None."""
    pr = it.get("probe") or {}
    if not it.get("probe_ok") or not pr: return None
    a = [st for st in pr.get("streams", []) if st.get("codec_type") == "audio"]; subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle"]
    want_a, want_s = wanted_langs(it)
    ap = None
    for w in want_a:
        cands = [i for i, st in enumerate(a, 1) if canon((st.get("tags") or {}).get("language", "und") or "und") == w and not re.search(r"descri|commentary", (st.get("tags") or {}).get("title") or "", re.I)]
        if cands: ap = cands[0]; break
    sp = None
    cands = [(_sub_rank(st), i) for i, st in enumerate(subs, 1) if canon((st.get("tags") or {}).get("language", "und") or "und") == want_s and st.get("codec_name") in TEXT_SUB_CODECS | {"hdmv_pgs_subtitle", "dvd_subtitle"}]
    if cands: sp = sorted(cands)[0][1]
    cur_a = next((i for i, st in enumerate(a, 1) if (st.get("disposition") or {}).get("default")), None); cur_s = next((i for i, st in enumerate(subs, 1) if (st.get("disposition") or {}).get("default")), None)
    if ap is None and sp is None: return None
    if (ap is None or ap == cur_a) and (sp is None or sp == cur_s): return None
    return (ap, sp, f"audio default {cur_a}->{ap} ({want_a[0]}), subs default {cur_s}->{sp} ({want_s})")
MKV_MAGIC = b"\x1a\x45\xdf\xa3"   # every Matroska file starts with the EBML header id
def is_matroska(it):
    """True unless the probe or the file's first bytes say the container is something else (a .mkv name is not proof: some releases
    are MP4 inside). The probe's format can be missing (older cached probes keep only the streams), so the file itself decides then;
    with neither (file unreadable, not probed yet) the name is trusted."""
    fmt = str(((it.get("probe") or {}).get("format") or {}).get("format_name") or "")
    if fmt: return "matroska" in fmt
    try:
        with open(it.get("path") or "", "rb") as fh: return fh.read(4) == MKV_MAGIC
    except OSError: return True
def apply_defaults(path, ap, sp, n_audio, n_subs):
    cmd = ["mkvpropedit", path]
    if ap is not None:
        for i in range(1, n_audio + 1): cmd += ["--edit", f"track:a{i}", "--set", f"flag-default={1 if i == ap else 0}"]
    if sp is not None:
        for i in range(1, n_subs + 1): cmd += ["--edit", f"track:s{i}", "--set", f"flag-default={1 if i == sp else 0}"]
    guard_write(f"{cmd[0]} on a media file"); p = subprocess.run(cmd, capture_output=True, text=True, timeout=300); return p.returncode in (0, 1), (p.stderr or p.stdout)[-160:]
def do_track_prefs(conn, items):
    mode = gate(setting("TRACK_PREFS"), "TRACK_PREFS")
    if mode == "off": return 0, 0
    mx = int(setting("TRACK_PREFS_MAX")); seen = {r[0]: r[1] for r in conn.execute("SELECT path, mtime FROM trackprefs")}; done = 0; planned = 0
    failed = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, mtime, ts FROM trackprefs WHERE note LIKE 'failed:%'")}   # a failure waits a day (or a changed file) before the next try
    for it in items:
        if done >= mx: break
        pth = it.get("path") or ""
        if not pth.lower().endswith(".mkv") or it.get("missing") or it["intent"]["ignore"] or it["app"] == "whisparr": continue
        if pth in failed:
            if abs((failed[pth][0] or 0) - (it.get("mtime") or 0)) < 1 and failed[pth][1] > time.time() - 86400: continue
        elif abs((seen.get(pth) or 0) - (it.get("mtime") or 0)) < 1: continue
        if not is_matroska(it):   # an MP4 saved with a .mkv name: mkvpropedit cannot edit it, so remember and skip instead of failing every night
            conn.execute("INSERT OR REPLACE INTO trackprefs VALUES(?,?,?,?)", (pth, it.get("mtime"), time.time(), "skipped: MP4 inside a .mkv name")); continue
        plan = plan_defaults(it)
        if not plan:
            conn.execute("INSERT OR REPLACE INTO trackprefs VALUES(?,?,?,?)", (pth, it.get("mtime"), time.time(), "ok")); continue
        ap, sp, why = plan; planned += 1
        if mode == "dryrun": record_action(conn, "dryrun", "fixdefaults", it["app"], it["fid"], it["label"], why, "would set flags"); done += 1; continue
        pr = it.get("probe") or {}; na = sum(1 for st in pr.get("streams", []) if st.get("codec_type") == "audio"); ns = sum(1 for st in pr.get("streams", []) if st.get("codec_type") == "subtitle")
        ok, msg = apply_defaults(pth, ap, sp, na, ns)
        if ok:
            try: mt = os.stat(pth).st_mtime
            except OSError: mt = it.get("mtime")
            conn.execute("INSERT OR REPLACE INTO trackprefs VALUES(?,?,?,?)", (pth, mt, time.time(), why)); conn.execute("DELETE FROM probe WHERE path=?", (pth,)); queue_plex(pth); done += 1
            record_action(conn, "on", "fixdefaults", it["app"], it["fid"], it["label"], why, "mkvpropedit ok")
        else:
            record_action(conn, "on", "fixdefaults", it["app"], it["fid"], it["label"], why, f"ERROR {msg}")
            conn.execute("INSERT OR REPLACE INTO trackprefs VALUES(?,?,?,?)", (pth, it.get("mtime"), time.time(), f"failed: {msg[-120:]}"))
    conn.commit()
    if done or planned: log(f"track prefs: {done} files' default flags set ({planned} needed it)")
    return done, planned
def do_plex_select(conn, items):
    """Make Plex (owner account) pick the wanted audio/subtitle stream per episode/movie. Budgeted; remembered per item."""
    if gate(setting("TRACK_PREFS"), "TRACK_PREFS") != "on" or not PLEX_KEYS or not plex_token(): return 0
    mx = int(setting("PLEX_SELECT_MAX")); seen = {r[0] for r in conn.execute("SELECT rk FROM plexsel")}; looked = 0; changed = 0
    for it in items:
        if looked >= mx: break
        k = PLEX_KEYS.get(os.path.normpath(it.get("path") or "")); rk = k[0] if k else None
        if not rk or rk in seen or it.get("missing") or it["app"] == "whisparr": continue
        looked += 1
        if looked % 50 == 0: progress(detail=f"Plex track selection: {looked} of up to {mx} items checked, {changed} switched"); conn.commit()   # heartbeat + release the write lock
        try:
            md = (plex_get(f"/library/metadata/{rk}").get("MediaContainer", {}).get("Metadata") or [{}])[0]; part = (md.get("Media") or [{}])[0].get("Part", [{}])[0]; streams = part.get("Stream", [])
        except Exception: continue
        want_a, want_s = wanted_langs(it); aud = [st for st in streams if st.get("streamType") == 2]; sub = [st for st in streams if st.get("streamType") == 3]
        pick_a = next((st for w in want_a for st in aud if canon(st.get("languageCode") or "und") == w and not re.search(r"descri|commentary", st.get("displayTitle") or "", re.I)), None)
        def _srank(st): t_ = (st.get("displayTitle") or "").lower(); return (1 if ("forced" in t_ or st.get("forced")) else 0) + (1 if "sdh" in t_ else 0)
        cands = sorted([(_srank(st), st) for st in sub if canon(st.get("languageCode") or "und") == want_s], key=lambda x: x[0])
        pick_s = cands[0][1] if cands else None
        cur_a = next((st for st in aud if st.get("selected")), None); cur_s = next((st for st in sub if st.get("selected")), None)
        need = (pick_a and (not cur_a or cur_a["id"] != pick_a["id"])) or (pick_s and (not cur_s or cur_s["id"] != pick_s["id"]))
        if need and part.get("id"):
            try:
                from urllib.parse import urlencode
                q = {"allParts": 1, "X-Plex-Token": plex_token()}
                if pick_a: q["audioStreamID"] = pick_a["id"]
                if pick_s: q["subtitleStreamID"] = pick_s["id"]
                guard_write("PUT plex track selection"); urllib.request.urlopen(urllib.request.Request(f"{PLEX_URL}/library/parts/{part['id']}?{urlencode(q)}", method="PUT"), timeout=20).read(); changed += 1
            except Exception as e: log("plex select:", repr(e)); continue
        conn.execute("INSERT OR REPLACE INTO plexsel VALUES(?,?,?,?)", (rk, time.time(), str(pick_a["id"]) if pick_a else None, str(pick_s["id"]) if pick_s else None))
    conn.commit()
    if looked: record_action(conn, "on", "plexselect", "plex", looked, f"{changed} items", f"checked {looked} Plex items' selected tracks", f"{changed} switched to the wanted audio/subtitle")
    log(f"plex select: {looked} items checked, {changed} switched"); return changed
# ---------- Tautulli / watch statistics ----------
def tautulli_key():
    k = E("TAUTULLI_APIKEY")
    if k: return k
    try:
        m = re.search(r"^api_key\s*=\s*(\S+)", open(TAUTULLI_INI).read(), re.M); return m.group(1) if m else None
    except OSError: return None
def tautulli(cmd, **params):
    from urllib.parse import urlencode
    k = tautulli_key()
    if not k: raise RuntimeError("no Tautulli api key (mount config.ini at /arr/tautulli.ini or set TAUTULLI_APIKEY)")
    with urllib.request.urlopen(f"{TAUTULLI_URL}/api/v2?{urlencode(dict(apikey=k, cmd=cmd, **params))}", timeout=120) as r:
        j = json.load(r)["response"]
        if j.get("result") != "success": raise RuntimeError(j.get("message") or "tautulli error")
        return j.get("data")
def watch_stats(conn, items, run_started):
    """Per season (series) / per movie: size from our items, owner's watched counts from Plex, everyone's plays from Tautulli."""
    if setting("WATCH_STATS") != "on" or not PLEX_KEYS: return 0
    cold_days = int(setting("COLD_DAYS")); never_days = int(setting("NEVER_WATCHED_DAYS")); now = run_started
    groups = {}
    for it in items:
        if it["app"] not in ("sonarr", "radarr") or not it.get("path") or it.get("missing"): continue
        key = (it["app"], it["parent_id"], it.get("season") if it["kind"] == "series" else None)
        g = groups.setdefault(key, dict(title=it["title"], library=lib_of(it["path"], it["app"]), files=0, bytes=0, added=None, keys=None, kind=it["kind"]))
        g["files"] += 1; g["bytes"] += it.get("disk_size") or it["size"] or 0
        try: ad = datetime.datetime.fromisoformat((it.get("date_added") or "").replace("Z", "+00:00")).timestamp()
        except ValueError: ad = None
        if ad and (g["added"] is None or ad < g["added"]): g["added"] = ad
        if not g["keys"]: g["keys"] = PLEX_KEYS.get(os.path.normpath(it["path"]))
    # Plex: owner's viewed counts per season (one call per show) / per movie
    plex_seasons = {}; show_cache = {}; show_last = {}
    for key, g in groups.items():
        k = g["keys"]
        if not k: continue
        if g["kind"] == "series" and k[2] and k[2] not in show_cache:
            try:
                mc_ = plex_get(f"/library/metadata/{k[2]}/children").get("MediaContainer", {}); show_cache[k[2]] = {c["ratingKey"]: c for c in mc_.get("Metadata", [])}
                show_last[k[2]] = (plex_get(f"/library/metadata/{k[2]}").get("MediaContainer", {}).get("Metadata") or [{}])[0].get("lastViewedAt")
            except Exception: show_cache[k[2]] = {}
        if g["kind"] == "series" and k[1]: plex_seasons[key] = show_cache.get(k[2], {}).get(k[1], {})
        elif g["kind"] == "movie" and k[0]:
            try: plex_seasons[key] = (plex_get(f"/library/metadata/{k[0]}").get("MediaContainer", {}).get("Metadata") or [{}])[0]
            except Exception: plex_seasons[key] = {}
    # Tautulli: plays by everyone, per season (per show call) / per movie (per section call)
    tplays = {}
    try:
        for sec in {g["keys"][3] for g in groups.values() if g["keys"]}:
            typ = next((s_["type"] for s_ in plex_sections() if str(s_["key"]) == str(sec)), "show")
            if typ == "movie":
                for row in (tautulli("get_library_media_info", section_id=sec, length=10000) or {}).get("data", []): tplays[str(row.get("rating_key"))] = (row.get("play_count") or 0, row.get("last_played"))
        for show_rk in {g["keys"][2] for g in groups.values() if g["keys"] and g["kind"] == "series" and g["keys"][2]}:
            sec = next((g["keys"][3] for g in groups.values() if g["keys"] and g["keys"][2] == show_rk), None)
            try:
                for row in (tautulli("get_library_media_info", section_id=sec, rating_key=show_rk, length=500) or {}).get("data", []): tplays[str(row.get("rating_key"))] = (row.get("play_count") or 0, row.get("last_played"))
            except Exception as e: log("tautulli show", show_rk, repr(e)); break
    except Exception as e: log("tautulli:", repr(e))
    rows = []
    for key, g in groups.items():
        k = g["keys"] or (None, None, None, None); pm = plex_seasons.get(key, {}) or {}
        leaf = int(pm.get("leafCount") or (1 if g["kind"] == "movie" else 0)); viewed = int(pm.get("viewedLeafCount") or (pm.get("viewCount") or 0 if g["kind"] == "movie" else 0))
        tp = tplays.get(str(k[1] if g["kind"] == "series" else k[0]), (0, None)); plays, last = tp[0], tp[1]
        last_ts = max([x for x in (last, pm.get("lastViewedAt")) if x] or [0]); last_ts = float(last_ts) if last_ts else None
        watched = leaf > 0 and (viewed >= leaf or plays >= leaf)
        if watched and (last_ts is None or now - last_ts > cold_days * 86400): status = "watched_cold"
        elif not viewed and not plays and g["added"] and now - g["added"] > never_days * 86400: status = "never_watched"
        else: status = "active"
        rows.append((key[0], key[1], key[2], g["title"], g["library"], g["files"], g["bytes"], leaf, viewed, plays, last_ts, g["added"], status, k[2], k[1] if g["kind"] == "series" else k[0], now, float(show_last.get(k[2]) or 0) or None))
    conn.execute("DELETE FROM watch"); conn.executemany("INSERT INTO watch(app,parent_id,season,title,library,files,bytes,leaf,viewed,plays,last_played,added,status,show_rk,item_rk,updated,show_last) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows); conn.commit()
    try:
        h0 = (tautulli("get_history", length=1, order_column="date", order_dir="asc") or {}).get("data") or []
        if h0: conn.execute("INSERT OR REPLACE INTO kv VALUES('tautulli_since',?)", (datetime.datetime.fromtimestamp(h0[0]["date"]).date().isoformat(),)); conn.commit()
    except Exception: pass
    cold = sum(r[6] for r in rows if r[12] == "watched_cold"); never = sum(r[6] for r in rows if r[12] == "never_watched")
    log(f"watch stats: {len(rows)} seasons/movies - watched & cold {cold/1e9:.0f} GB, never watched {never/1e9:.0f} GB"); return len(rows)
def space_view(conn):
    min_b = float(setting("SPACE_MIN_GB")) * 1e9; out = {"watched_cold": [], "never_watched": [], "totals": {}}
    apps = arr_apps()
    row_ = conn.execute("SELECT v FROM kv WHERE k='tautulli_since'").fetchone(); out["tautulli_since"] = row_[0] if row_ else None
    for r in conn.execute("SELECT app,parent_id,season,title,library,files,bytes,leaf,viewed,plays,last_played,added,status,show_rk,item_rk,show_last FROM watch WHERE status IN ('watched_cold','never_watched') ORDER BY bytes DESC"):
        app, pid, season, title, lib, files, b, leaf, viewed, plays, last, added, status, show_rk, item_rk, show_last = r
        if b < min_b: continue
        base = (ARR_LINK.get(app) or apps.get(app, ("",))[0] or "").rstrip("/")
        out[status].append(dict(id=f"{app}|{pid}|{'' if season is None else season}", app=app, parent_id=pid, season=season, title=title, library=lib, files=files, gb=round(b / 1e9, 1), leaf=leaf, viewed=viewed, plays=plays,
                                last_played=datetime.datetime.fromtimestamp(last).date().isoformat() if last else None, added=datetime.datetime.fromtimestamp(added).date().isoformat() if added else None,
                                show_last=datetime.datetime.fromtimestamp(show_last).date().isoformat() if show_last else None,
                                plex=plex_link(item_rk), arr=f"{base}/{'series' if season is not None else 'movie'}/{pid}" if base else None))
    for k in ("watched_cold", "never_watched"): out["totals"][k] = dict(n=len(out[k]), gb=round(sum(x["gb"] for x in out[k]), 1))
    out["by_show"] = {t: dict(last=datetime.datetime.fromtimestamp(l).date().isoformat() if l else None, plays=p_) for t, l, p_ in conn.execute("SELECT title, max(last_played), sum(plays) FROM watch GROUP BY title")}
    return out
def ui_archive(conn, body, actor):
    """{id: 'app|parent|season'} -> unmonitor that season/movie in its arr and move its files to the trash (archive retention)."""
    apps = arr_apps(); app, pid, season = (body.get("id") or "||").split("|"); pid = int(pid or 0); season = int(season) if season not in ("", None) else None
    if app not in apps: return 400, {"error": "unknown app"}
    base, key, kind = apps[app]; moved = 0; freed = 0
    if kind == "series":
        srs = api(base, key, f"series/{pid}")
        for se in srs.get("seasons", []):
            if se.get("seasonNumber") == season: se["monitored"] = False
        api(base, key, f"series/{pid}", "PUT", srs)
        files = [f for f in api(base, key, f"episodefile?seriesId={pid}") if f.get("seasonNumber") == season]
        label = f"{srs['title']} S{season:02d}"
    else:
        mv = api(base, key, f"movie/{pid}"); mv["monitored"] = False; api(base, key, f"movie/{pid}", "PUT", mv)
        files = api(base, key, f"moviefile?movieId={pid}"); label = f"{mv['title']} ({mv.get('year')})"
    for f in files:
        pth = f.get("path")
        if pth and os.path.exists(pth):
            try: freed += os.path.getsize(pth)
            except OSError: pass
            quarantine(conn, pth, f"archive {label} by {actor}", "archive"); moved += 1; queue_plex(pth)
    try: api(base, key, "command", "POST", {"name": "RescanSeries", "seriesId": pid} if kind == "series" else {"name": "RescanMovie", "movieId": pid}, timeout=60)
    except Exception as e: log("archive rescan:", repr(e))
    flush_plex(conn); conn.execute("DELETE FROM watch WHERE app=? AND parent_id=? AND (season IS ? OR season=?)", (app, pid, season, season)); conn.commit()
    record_action(conn, "on", "archive", app, pid, label, f"unmonitored + archived by {actor}", f"{moved} file(s), {freed/1e9:.1f} GB -> trash/archive ({TRASH_DAYS_ARCHIVE} d)")
    refresh_reports(conn); return 200, {"ok": True, "moved": moved, "gb": round(freed / 1e9, 1), "note": f"{label}: unmonitored in {app}, {moved} file(s) / {freed/1e9:.1f} GB in the trash for {TRASH_DAYS_ARCHIVE} days"}
# ---------- reporting ----------
LIBRARY_MAP = [(a.rstrip("/"), b) for a, b in (x.split("=", 1) for x in E("LIBRARY_MAP", "").split(",") if "=" in x)]   # optional "/data/media/kids=Kids,..."
_LIB_NAMES = {"tv": "TV", "xxx": "XXX", "movies": "Movies", "movie": "Movies", "anime": "Anime", "sports": "Sports", "kids": "Kids", "music": "Music"}
def lib_of(path, app=None):
    """Library category from the top folder under the media root: Anime / TV / Movies / XXX / Sports ... (whisparr files land in XXX by path)."""
    for a, b in LIBRARY_MAP:
        if path and (path == a or path.startswith(a + "/")): return b
    m = re.match(r"^/data/(?:media/)?([^/]+)/", path or "")
    if m and m.group(1) and not m.group(1).startswith("."): return _LIB_NAMES.get(m.group(1).lower(), m.group(1).title())
    return {"whisparr": "XXX", "radarr": "Movies", "sonarr": "TV", "sportarr": "Sports"}.get(app or "", "Other")
def write_reports(conn, run_info):
    os.makedirs(REPORT_DIR, exist_ok=True)
    rows = conn.execute("SELECT id,app,check_code,severity,label,path,detail,first_seen,last_seen,fid FROM finding WHERE resolved_at IS NULL ORDER BY "
                        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, check_code, label").fetchall()
    acts = conn.execute("SELECT ts,mode,kind,app,label,detail,result FROM action ORDER BY ts DESC LIMIT 100").fetchall()
    now = time.time(); by_code = {}; data = []
    links = {(a, f): (p, ar) for a, f, p, ar in conn.execute("SELECT app,fid,plex,arr FROM links")}; up = upcoming_map(conn)
    for r in rows:
        d = dict(id=r[0], app=r[1], check=r[2], name=check_name(r[2]), severity=r[3], sev_name=SEV_NAMES.get(r[3], r[3]), plain=plain_sentence(r[2], r[3], r[4], r[6], r[1]), title=r[4], path=r[5], detail=r[6], first_seen=datetime.datetime.fromtimestamp(r[7]).isoformat(timespec="minutes"), age_days=round((now - r[7]) / 86400, 1), library=lib_of(r[5], r[1]), upcoming=(r[1], r[9]) in up, watching=up.get((r[1], r[9])) == "watching", plex=links.get((r[1], r[9]), (None, None))[0], arr=links.get((r[1], r[9]), (None, None))[1])
        data.append(d); by_code.setdefault(r[2], []).append(d)
    act_list = [dict(ts=datetime.datetime.fromtimestamp(a[0]).isoformat(timespec="minutes"), mode=a[1], kind=a[2], app=a[3], title=a[4], detail=a[5], result=a[6]) for a in acts]
    json.dump(dict(generated=datetime.datetime.now().isoformat(timespec="seconds"), run=run_info, counts={k: len(v) for k, v in by_code.items()},
                   scores=library_scores(conn), by_severity={s: sum(1 for d in data if d["severity"] == s) for s in SEV_RANK}, by_library={l: sum(1 for d in data if d["library"] == l) for l in sorted({d["library"] for d in data})}, findings=data, actions=act_list),
              open(os.path.join(REPORT_DIR, "latest.json"), "w"), indent=1)
    md = [f"# media-audit report - {datetime.datetime.now():%Y-%m-%d %H:%M}", "",
          f"Files checked: {run_info['files']} | ffprobed this run: {run_info['probed']} | active findings: {len(data)} | new: {run_info['new']} | resolved: {run_info['resolved']} | rescans: {run_info.get('rescans',0)} | dedupes: {run_info.get('dedupes',0)} | regrabs: {run_info.get('regrabs',0)} | trash: {run_info.get('trash',{}).get('files',0)} files / {run_info.get('trash',{}).get('gb',0)} GB (kept {run_info.get('trash',{}).get('keep_days','?')} days in {run_info.get('trash',{}).get('dir','')})", "",
          "| check | count |", "|---|---|"] + [f"| {k} | {len(v)} |" for k, v in sorted(by_code.items(), key=lambda kv: -len(kv[1]))] + [""]
    for code, lst in sorted(by_code.items(), key=lambda kv: -SEV_RANK.get(kv[1][0]["severity"], 0)):
        md += [f"## {code} ({lst[0]['severity']}) - {len(lst)}", ""] + [f"- **{d['title']}** [{d['app']}] - {d['detail']}  \n  `{d['path']}` (since {d['first_seen']})" for d in lst] + [""]
    if act_list: md += ["## Actions (latest 100)", ""] + [f"- {a['ts']} [{a['mode']}] {a['kind']} {a['app']} **{a['title']}** - {a['detail']} → {a['result']}" for a in act_list] + [""]
    md += ["Silence a finding: add a line to ignore.txt - `CHECK_CODE`, `CHECK_CODE: title-regex`, or the finding id. Per-title intent via arr tags: audit-ignore, audit-lang-xxx, audit-nosubs, audit-noremediate.", ""]
    open(os.path.join(REPORT_DIR, "latest.md"), "w").write("\n".join(md))
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    html = ["<!doctype html><meta charset=utf-8><title>media-audit report</title><style>body{font:14px system-ui;margin:24px;background:#111;color:#ddd}a{color:#9cf}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #333;padding:4px 8px;text-align:left;vertical-align:top}th{color:#fff}code{color:#9cf;font-size:12px}.critical{color:#f66}.high{color:#fa5}.medium{color:#fd6}.low{color:#9c9}summary{cursor:pointer;font-size:16px;margin:12px 0}</style>",
            f"<h1>media-audit report</h1><p>{esc(run_info.get('finished',''))} - files {run_info['files']}, probed {run_info['probed']}, active {len(data)}, new {run_info['new']}, resolved {run_info['resolved']}, rescans {run_info.get('rescans',0)}, dedupes {run_info.get('dedupes',0)}, regrabs {run_info.get('regrabs',0)}, trash {run_info.get('trash',{}).get('files',0)} files / {run_info.get('trash',{}).get('gb',0)} GB - gates {esc(json.dumps(run_info.get('gates',{})))} - <a href=/>live</a> <a href=latest.json>json</a> <a href=latest.md>markdown</a></p>"]
    for code, lst in sorted(by_code.items(), key=lambda kv: -SEV_RANK.get(kv[1][0]["severity"], 0)):
        html.append(f"<details {'open' if SEV_RANK.get(lst[0]['severity'],0)>=1 else ''}><summary class={lst[0]['severity']}>{code} - {len(lst)} ({lst[0]['severity']})</summary><table><tr><th>title</th><th>app</th><th>detail</th><th>since</th><th>id</th></tr>")
        html += [f"<tr><td>{esc(d['title'])}<br><code>{esc(d['path'])}</code></td><td>{d['app']}</td><td>{esc(d['detail'])}</td><td>{d['first_seen'][:10]}</td><td><code>{d['id'][:10]}</code></td></tr>" for d in lst]
        html.append("</table></details>")
    if act_list:
        html.append(f"<details open><summary>Actions taken - latest {len(act_list)}</summary><table><tr><th>when</th><th>mode</th><th>what</th><th>title</th><th>detail</th><th>result</th></tr>")
        html += [f"<tr><td>{a['ts']}</td><td>{a['mode']}</td><td>{a['kind']} {a['app']}</td><td>{esc(a['title'])}</td><td>{esc(a['detail'])}</td><td>{esc(a['result'])}</td></tr>" for a in act_list]
        html.append("</table></details>")
    open(os.path.join(REPORT_DIR, "latest.html"), "w").write("\n".join(html))
    hist = os.path.join(REPORT_DIR, "history"); os.makedirs(hist, exist_ok=True)
    open(os.path.join(hist, f"{datetime.datetime.now():%Y-%m-%d_%H%M}.md"), "w").write("\n".join(md))
    for old in sorted(os.listdir(hist))[:-60]: os.remove(os.path.join(hist, old))
def page_url(tab=""): return (E("PUBLIC_URL") or REPORT_URL.rsplit("/", 1)[0]).rstrip("/") + "/" + (f"#{tab}" if tab else "")
def ntfy(title, body, priority="default", tags="clapper", click=None):
    if not NTFY_URL: return
    headers = {"Title": title, "Priority": priority, "Tags": tags, "Click": click or page_url("findings")}
    if NTFY_TOKEN_FILE and os.path.exists(NTFY_TOKEN_FILE): headers["Authorization"] = "Bearer " + open(NTFY_TOKEN_FILE).read().strip()
    if ledger_only("ntfy push"): return
    try: urllib.request.urlopen(urllib.request.Request(NTFY_URL, body.encode(), headers), timeout=20).read()
    except Exception as e: log("ntfy error:", e)

# ---------- one run ----------
def run_once():
    started = time.time(); os.makedirs(CONFIG_DIR, exist_ok=True); conn = db(); load_settings(conn)
    log(f"run start (ARR_RESCAN={cfg('ARR_RESCAN',ARR_RESCAN)} DEDUPE={cfg('DEDUPE',DEDUPE)} REMEDIATE={cfg('REMEDIATE',REMEDIATE)} PAPERCLIP={cfg('PAPERCLIP',PAPERCLIP)} PLEX={setting('PLEX_CHECK')}/{setting('PLEX_REFRESH')} overrides={OVR} saved={SETTINGS})")
    PROGRESS["actions"] = []; PROGRESS["recent"] = []; progress("listing", "asking Sonarr/Radarr/Whisparr for every file", 0, 0); PROGRESS["started"] = datetime.datetime.now().isoformat(timespec="seconds")
    apps = arr_apps(); items, roots = collect(apps)
    if not items: log("nothing collected - aborting run"); progress("failed", "nothing collected"); return
    probed = probe_all(conn, items)
    bz_eps, bz_movs = bazarr_wanted(); rules = load_ignores()
    seen = set(); new = []
    def upsert(app, fid, code, sev, label, path, detail):
        fidh = hashlib.sha1(f"{app}|{fid}|{code}".encode()).hexdigest()[:16]
        if ignored(rules, code, label, fidh): return
        seen.add(fidh)
        if upsert_finding(conn, fidh, app, fid, code, sev, label, path, detail, started, count_run=True) == "new": new.append((sev, code, label, detail)); progress(recent=[f"[{sev}] {code}: {label} - {detail}"])
    progress("checking", "running checks", 0, len(items)); profile = series_language_profile(items); rt_profile = season_runtime_profile(items)
    found = {}; REALDUR_CACHE.clear(); REALDUR_CACHE.update({r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, mtime, dur FROM realdur")}); REALDUR_BUDGET["left"] = REALDUR_MAX; load_hardsub(conn)
    for n_it, it in enumerate(items):
        if n_it % 500 == 0: progress(done=n_it)
        if n_it % 300 == 0:
            conn.executemany("INSERT OR REPLACE INTO realdur VALUES(?,?,?,?)", [(p_, v[0], v[1], time.time()) for p_, v in REALDUR_CACHE.items()]); conn.commit()   # release the write lock now and then so UI actions can get through
        res = check(it, bz_eps, bz_movs, profile, rt_profile); found[(it["app"], it["fid"])] = {c for c, _, _ in res}
        for code, sev, detail in res: upsert(it["app"], it["fid"], code, sev, it["label"], it["path"], detail)
    pend = {os.path.normpath(r[0]): r for r in conn.execute("SELECT orig_path, ts, code, ep_ids, kind, parent_id, actor FROM regrab WHERE status='pending'")}
    wait_d = float(cfg("REGRAB_WAIT_DAYS", REGRAB_WAIT_DAYS)); n_pend = 0
    for it in items:
        r = pend.get(os.path.normpath(it.get("path") or "")) if it.get("missing") else None
        if not r: continue
        ids = json.loads(r[3] or "[]") if r[4] == "series" else [r[5]]
        detail = (f"quarantined by re-grab on {datetime.datetime.fromtimestamp(r[1]).strftime('%Y-%m-%d %H:%M')} ({r[2]}, {r[6]}) - {it['app']} is replacing it: {arr_status_line(it['app'], ids)}"
                  f" | original auto-restores if nothing imports within {wait_d:g} days")
        fidh = hashlib.sha1(f"{it['app']}|{it['fid']}|MISSING_FILE".encode()).hexdigest()[:16]
        conn.execute("UPDATE finding SET severity='low', detail=? WHERE id=? AND resolved_at IS NULL", (detail, fidh)); n_pend += 1
    if n_pend: conn.commit(); log(f"missing-file: {n_pend} are pending re-grabs (annotated, low)")
    n_sd = 0   # learn from re-grabs: when the replacement shows the same finding, two releases agree -> it is the show, not the file
    for app_, label_, code_ in conn.execute("SELECT DISTINCT app, label, code FROM regrab WHERE status='same_defect'").fetchall():
        n_sd += conn.execute("UPDATE finding SET severity='low', detail=? || ' || ' || detail WHERE app=? AND label=? AND check_code=? AND resolved_at IS NULL AND detail NOT LIKE '[two releases agree%'",
                             (f"[two releases agree: the re-grabbed replacement has the same {code_} - probably not a defect; re-grab hidden, ignore it if you agree]", app_, label_, code_)).rowcount
    if n_sd: conn.commit(); log(f"same-defect: {n_sd} finding(s) downgraded to low (two releases agree)")
    for mi_ in MISSING:
        if mi_["app"] in SKIP_MISSING_APPS: continue
        code = "MISSING_SCENE" if mi_["app"] == "whisparr" else "MISSING_EPISODE" if mi_["kind"] == "series" else "MISSING_MOVIE"
        q = MISSING_EV.get(mi_["app"], {}).get("queue", {}).get(mi_["id"])
        sev = "low" if (q or mi_["app"] == "whisparr" or mi_["days"] > 365) else "medium"
        status = arr_status_line(mi_["app"], [mi_["id"]])
        hint = "" if q else (" - older than a year, may simply not exist on your indexers" if mi_["days"] > 365 else " - press 'search' to look again (Huntarr/RSS also keep trying)")
        upsert(mi_["app"], -mi_["id"], code, sev, mi_["label"], mi_["path"], f"released {mi_['released']} ({mi_['days']} days ago), monitored, no file | {status}" + (f" - '{mi_['ep_title'][:50]}'" if mi_["ep_title"] and mi_["kind"] == "series" and "S" in mi_["label"].split()[-1] else "") + hint)
    if MISSING: log(f"missing: {len(MISSING)} monitored items released >= {setting('MISSING_MIN_DAYS')} days ago without a file")
    if ORPHAN_SCAN:
        folders = {}   # series/movie folder -> (app, kind, parent_id), so an "orphan" can be re-checked live against its arr (imports that landed during this run)
        for it in items:
            if not it.get("path"): continue
            d1_ = os.path.dirname(os.path.normpath(it["path"])); folders.setdefault(d1_, (it["app"], it["kind"], it["parent_id"]))
            if it["kind"] == "series": folders.setdefault(os.path.dirname(d1_), (it["app"], it["kind"], it["parent_id"]))
        fresh = {}
        def arr_has_now(full):
            d1_ = os.path.dirname(full); hit = folders.get(d1_) or folders.get(os.path.dirname(d1_))
            if not hit or hit[0] not in apps: return False
            app_, kind_, pid_ = hit
            if (app_, pid_) not in fresh:
                try:
                    base_, key_, _ = apps[app_]; rows_ = api(base_, key_, f"episodefile?seriesId={pid_}" if kind_ == "series" else f"moviefile?movieId={pid_}") or []
                    fresh[(app_, pid_)] = {os.path.normpath(r_.get("path") or "") for r_ in rows_}
                except Exception: fresh[(app_, pid_)] = set()
            return os.path.normpath(full) in fresh[(app_, pid_)]
        for code, sev, full, detail in orphan_scan(items, roots):
            if arr_has_now(full): log(f"orphan scan: {os.path.basename(full)[:70]} - the arr imported it while this run was going; not an orphan"); continue
            sev, detail = gap_detail(code, sev, full, detail)
            upsert("disk", int(hashlib.sha1(full.encode()).hexdigest()[:8], 16), code, sev, os.path.basename(full), full, detail)
    # Plex reconciliation: files the arr has but Plex never indexed
    skip_codes = set(); plex_missing_dirs = []
    if setting("PLEX_CHECK") == "on" and plex_token():
        try:
            progress("plex", "reading the Plex library inventory"); inv = plex_inventory(); locs = [l for s in plex_sections() for l in s["locations"]]; min_age = float(cfg("PLEX_MIN_AGE_HOURS", PLEX_MIN_AGE_H)) * 3600
            for it in items:
                p = it.get("path")
                if not p or it.get("missing") or it["intent"]["ignore"] or (started - (it.get("mtime") or started)) < min_age: continue
                pp = os.path.normpath(to_plex(p))
                if not any(pp.startswith(l + os.sep) for l in locs) or os.path.normpath(p) in inv: continue
                upsert(it["app"], it["fid"], "PLEX_MISSING", "medium", it["label"], p, "the arr has this file but Plex has not indexed it - folder refresh is triggered automatically; if it persists, check the Plex scanner log / file name / permissions")
                plex_missing_dirs.append(os.path.dirname(p))
            log(f"plex: {len(inv)} files indexed, {len(plex_missing_dirs)} arr files not in Plex")
            store_links(conn, apps, items, inv); dep_status("plex", True); compute_upcoming(conn, items)
        except Exception as e: log("plex inventory failed - PLEX_MISSING skipped this run:", repr(e)); skip_codes.add("PLEX_MISSING"); dep_status("plex", False, repr(e))
    else: store_links(conn, apps, items, {})
    conn.executemany("INSERT OR REPLACE INTO realdur VALUES(?,?,?,?)", [(p_, v[0], v[1], time.time()) for p_, v in REALDUR_CACHE.items()]); conn.commit()
    answered = {it["app"] for it in items} | ({"disk"} if ORPHAN_SCAN and roots else set()); resolved = 0
    for fid, app, code in conn.execute("SELECT id, app, check_code FROM finding WHERE resolved_at IS NULL").fetchall():
        if fid not in seen and app in answered and code not in skip_codes: conn.execute("UPDATE finding SET resolved_at=? WHERE id=?", (started, fid)); resolved += 1
    conn.commit()
    progress("actions", "rescans / remediation / handoff")
    regrab_outcomes = check_regrabs(conn, apps, items, found, started); subtags = do_subtag(conn, items, found)
    rescans = do_rescans(conn, apps, items); dedupes = do_dedupe(conn, items, started); regrabs = do_remediation(conn, apps, items, started); purged = purge_trash(conn)
    for d in plex_missing_dirs: queue_plex(d)
    # night automation: fix lying headers (lossless), queue content checks for new runtime/title findings
    auto_fixed = 0
    if gate(setting("AUTO_FIXHEADER"), "AUTO_FIXHEADER") != "off":
        for fid_, in conn.execute("SELECT id FROM finding WHERE resolved_at IS NULL AND check_code='BAD_DURATION_HEADER' ORDER BY first_seen LIMIT 10").fetchall():
            if setting("AUTO_FIXHEADER") == "dryrun": record_action(conn, "dryrun", "fixheader", "nightly", fid_, finding_row(conn, fid_)["label"], "would repair the container header", "dryrun"); continue
            try:
                code_, obj_ = ui_act(conn, {"id": fid_, "action": "fixheader"}, "nightly"); auto_fixed += 1 if code_ == 200 else 0
                if code_ != 200: record_action(conn, "on", "fixheader", "nightly", fid_, finding_row(conn, fid_)["label"], "auto repair", f"not done: {obj_.get('error', '')[:120]}")
            except Exception as e: log("auto fixheader:", repr(e))
    auto_verified = 0
    if gate(setting("AUTO_VERIFY"), "AUTO_VERIFY") == "on" and apps:
        up_ = upcoming_set(conn); cand_ = conn.execute("SELECT id, app, fid, label, path FROM finding WHERE resolved_at IS NULL AND check_code IN ('RUNTIME_LONG','RUNTIME_SHORT','TITLE_MISMATCH') AND detail NOT LIKE '[verified%' AND app IN ('sonarr','whisparr','radarr') ORDER BY first_seen DESC LIMIT 300").fetchall()
        cand_.sort(key=lambda r_: 0 if (r_[1], r_[2]) in up_ else 1)   # F2: what is about to be watched gets checked first
        for fid_, app_, afid_, label_, path_ in cand_[:int(setting("AUTO_VERIFY_MAX"))]:
            if fid_ in {json.loads(r[0]).get("finding") for r in conn.execute("SELECT job FROM jobs WHERE status IN ('queued','running')")}: continue
            it_ = next((x for x in items if x["app"] == app_ and x["fid"] == afid_), None)
            if not it_ or not it_["ep_ids"] or not it_.get("path") or it_.get("missing"): continue
            enqueue(dict(type="verify", finding=fid_, app=app_, fid=afid_, path=it_["path"], parent_id=it_["parent_id"], ep_ids=it_["ep_ids"], label=label_, actor="nightly")); auto_verified += 1
    if auto_fixed or auto_verified: log(f"night automation: {auto_fixed} headers repaired, {auto_verified} content checks queued")
    try: auto_sync = do_auto_subsync(conn, apps, items); auto_sync and log(f"night automation: {auto_sync} subtitle timing check(s) queued")
    except Exception as e: note_error("nightly auto (subsync) failed:", repr(e))
    try: auto_al = do_auto_audiolang(conn); auto_al and log(f"night automation: {auto_al} spoken-language detection(s) queued")
    except Exception as e: note_error("nightly auto (audio language) failed:", repr(e))
    try: auto_lang = do_auto_acceptlang(conn, apps); auto_ai = do_auto_whisper(conn, apps); (auto_lang or auto_ai) and log(f"night automation: {auto_lang} language(s) accepted, {auto_ai} AI-subtitle job(s) queued")
    except Exception as e: log("nightly auto (acceptlang/whisper) failed:", repr(e))
    try: progress("tracks", "default audio/subtitle flags + Plex selection"); do_track_prefs(conn, items); do_plex_select(conn, items)
    except Exception as e: log("track prefs failed:", repr(e))
    plex_refreshed = flush_plex(conn)
    try: progress("watch", "Tautulli + Plex watch statistics"); watch_stats(conn, items, started)
    except Exception as e: log("watch stats failed:", repr(e))
    active = conn.execute("SELECT count(*) FROM finding WHERE resolved_at IS NULL").fetchone()[0]
    acts = [f"{r[0]} {r[1]} {r[2]} {r[3]}: {r[4]} -> {r[5]}" for r in conn.execute("SELECT mode,kind,app,label,detail,result FROM action WHERE ts>=?", (started,))]
    issue = do_paperclip(conn, new, acts, active)
    finished = time.time()
    by_sev = {s: n for s, n in conn.execute("SELECT severity, count(*) FROM finding WHERE resolved_at IS NULL GROUP BY severity")}
    t_files, t_bytes = trash_stats()
    extra = dict(by_severity=by_sev, rescans=rescans, dedupes=dedupes, regrabs=regrabs, purged=purged, plex_refreshed=plex_refreshed, trash_gb=round(t_bytes / 1e9, 1), duration_s=round(finished - started),
                 modes=dict(DEDUPE=cfg("DEDUPE", DEDUPE), REMEDIATE=cfg("REMEDIATE", REMEDIATE)))
    conn.execute("INSERT INTO run(started,finished,files,probed,active,new,resolved,extra) VALUES(?,?,?,?,?,?,?,?)", (started, finished, len(items), probed, active, len(new), resolved, json.dumps(extra))); conn.commit()
    info = dict(started=datetime.datetime.fromtimestamp(started).isoformat(timespec="seconds"), finished=datetime.datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
                files=len(items), probed=probed, active=active, new=len(new), resolved=resolved, rescans=rescans, dedupes=dedupes, regrabs=regrabs, purged=purged, paperclip_issue=issue, duration_s=round(finished - started),
                plex_refreshed=plex_refreshed, regrab_outcomes=len(regrab_outcomes), files_by_app={a: sum(1 for it in items if it["app"] == a) for a in sorted({it["app"] for it in items})},
                trash=dict(files=t_files, gb=round(t_bytes / 1e9, 1), dir=TRASH_DIR, keep_days=f"dedupe {TRASH_DAYS_DEDUPE} / regrab {TRASH_DAYS}"),
                gates=dict(ARR_RESCAN=cfg("ARR_RESCAN", ARR_RESCAN), DEDUPE=cfg("DEDUPE", DEDUPE), REMEDIATE=cfg("REMEDIATE", REMEDIATE), PAPERCLIP=cfg("PAPERCLIP", PAPERCLIP),
                           PLEX_CHECK=setting("PLEX_CHECK"), PLEX_REFRESH=setting("PLEX_REFRESH"), overrides=dict(OVR), saved=dict(SETTINGS)))
    conn.execute("INSERT OR REPLACE INTO kv VALUES('last_run_info',?)", (json.dumps(info),)); conn.commit()
    fbl = {}
    for it in items: fbl[lib_of(it.get("path") or "", it["app"])] = fbl.get(lib_of(it.get("path") or "", it["app"]), 0) + 1
    conn.execute("INSERT OR REPLACE INTO kv VALUES('files_by_lib',?)", (json.dumps(fbl),)); sc = library_scores(conn)
    try: hist = json.loads((conn.execute("SELECT v FROM kv WHERE k='scores_history'").fetchone() or ["[]"])[0])
    except Exception: hist = []
    hist = (hist + [dict(ts=datetime.datetime.fromtimestamp(finished).isoformat(timespec="minutes"), scores={k: v["score"] for k, v in sc.items()})])[-180:]
    conn.execute("INSERT OR REPLACE INTO kv VALUES('scores_history',?)", (json.dumps(hist),)); conn.commit(); log("library scores: " + ", ".join(f"{k} {v['score']}" for k, v in sorted(sc.items())))
    write_reports(conn, info); bk = backup_state(conn)
    if bk:   # prove the backup is usable: open it read-only, integrity-check it, count what is inside
        try:
            b_ = sqlite3.connect(f"file:{bk}?mode=ro", uri=True); ic_ = b_.execute("PRAGMA integrity_check").fetchone()[0]; nf_ = b_.execute("SELECT count(*) FROM finding").fetchone()[0]; b_.close()
            conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('backup_last',?)", (json.dumps(dict(ts=datetime.datetime.now().isoformat(timespec="minutes"), file=os.path.basename(bk), ok=ic_ == "ok", integrity=ic_[:80], findings=nf_, mb=round(os.path.getsize(bk) / 1e6, 1))),)); conn.commit()
            log(f"backup: {os.path.basename(bk)} integrity={ic_} findings={nf_}")
        except Exception as e: log("backup proof:", repr(e))
    if bk: log(f"state backup: {bk}")
    notable = [n for n in new if SEV_RANK.get(n[0], 0) >= SEV_RANK.get(NTFY_MIN_SEV, 1)]
    if SCHEDULED_RUN.get("flag"):
        SCHEDULED_RUN["flag"] = False
        try:
            t_ = today_summary(conn, started - 26 * 3600); body_ = digest_text(t_) + f"\n{REPORT_URL.rsplit('/', 1)[0]}/"
            ntfy(f"Auditarr daily: {len(t_['needs'])} need you · {sum(t_['did'].values())} fixes overnight", body_, "high" if any(x["sev"] in ("high", "critical") for x in t_["needs"]) else "default")
        except Exception as e: log("digest:", repr(e))
    elif notable or regrabs or regrab_outcomes or (dedupes and cfg("DEDUPE", DEDUPE) == "on"):
        notable.sort(key=lambda n: -SEV_RANK.get(n[0], 0)); lines = [f"[{s}] {c}: {l} - {d}"[:160] for s, c, l, d in notable[:12]]
        if len(notable) > 12: lines.append(f"... and {len(notable)-12} more")
        if regrabs: lines.append(f"re-grabbed {regrabs} file(s) - see Actions in the report")
        lines += [f"re-grab result: {o}"[:200] for o in regrab_outcomes[:6]]
        if dedupes and cfg("DEDUPE", DEDUPE) == "on": lines.append(f"quarantined {dedupes} duplicate file(s) - restorable for {TRASH_DAYS_DEDUPE} days")
        if issue: lines.append(f"Paperclip issue {issue} filed")
        lines.append(REPORT_URL)
        ntfy(f"media-audit: {len(notable)} new finding(s), {active} active", "\n".join(lines), "high" if any(n[0] == "critical" for n in notable) else "default")
    log(f"run done in {info['duration_s']}s: files={len(items)} probed={probed} active={active} new={len(new)} resolved={resolved} rescans={rescans} dedupes={dedupes} regrabs={regrabs} regrab_checks={len(regrab_outcomes)} subtags={subtags} plex_refreshed={plex_refreshed} purged={purged} issue={issue}")
    progress("done", f"files={len(items)} probed={probed} active={active} new={len(new)} resolved={resolved} rescans={rescans} dedupes={dedupes} regrabs={regrabs} trash={info['trash']['files']} files/{info['trash']['gb']} GB", len(items), len(items))
    OVR.clear(); refresh_globals()
    conn.close()
    threading.Thread(target=run_selftest, daemon=True).start()   # nightly self-test right after the audit


# ---------- on-demand helpers for the UI API ----------
def last_run_info(conn):
    row = conn.execute("SELECT v FROM kv WHERE k='last_run_info'").fetchone()
    return json.loads(row[0]) if row else dict(files=0, probed=0, new=0, resolved=0, finished="", trash={})
def refresh_reports(conn):
    try: write_reports(conn, last_run_info(conn))
    except Exception as e: log("refresh_reports:", repr(e))
def arr_file_info(apps, app, fid, path=None):
    """Everything an action needs about one arr file, fetched live. Falls back to a path lookup when the file id is stale
    (the arr replaced/re-imported the file since the last audit)."""
    base, key, kind = apps[app]
    def by_id():
        if kind == "series":
            f = api(base, key, f"episodefile/{fid}"); return f, f["seriesId"]
        f = api(base, key, f"moviefile/{fid}"); return f, f["movieId"]
    try: f, pid = by_id()
    except urllib.error.HTTPError as e:
        if e.code != 404 or not path: raise
        f = pid = None; np = os.path.normpath(path)
        if kind == "series":
            for srs in api(base, key, "series"):
                if srs.get("path") and np.startswith(os.path.normpath(srs["path"]) + os.sep):
                    pid = srs["id"]; f = next((x for x in api(base, key, f"episodefile?seriesId={pid}") if os.path.normpath(x.get("path", "")) == np), None); break
        else:
            for mv in api(base, key, "movie"):
                if mv.get("path") and np.startswith(os.path.normpath(mv["path"]) + os.sep):
                    pid = mv["id"]; f = next((x for x in api(base, key, f"moviefile?movieId={pid}") if os.path.normpath(x.get("path", "")) == np), None); break
        if not f: raise LookupError("the arr no longer has this file (replaced or removed since the last audit) - run the audit again to refresh")
    if kind == "series":
        srs = api(base, key, f"series/{pid}"); all_eps = api(base, key, f"episode?seriesId={pid}")
        eps = eps_for_file(f, {f["id"]: [e for e in all_eps if e.get("episodeFileId") == f["id"]]}, all_eps)
        return dict(app=app, kind=kind, fid=f["id"], parent_id=pid, path=f.get("path"), ep_ids=[e["id"] for e in eps], title=srs["title"],
                    orig=canon(LANG_NAME.get((srs.get("originalLanguage") or {}).get("name"), "")), monitored=srs.get("monitored"))
    mv = api(base, key, f"movie/{pid}")
    return dict(app=app, kind=kind, fid=f["id"], parent_id=pid, path=f.get("path"), ep_ids=[], title=mv["title"],
                orig=canon(LANG_NAME.get((mv.get("originalLanguage") or {}).get("name"), "")), monitored=mv.get("monitored"))
def rescan_for_path(apps, path):
    """Find which series/movie owns `path` and ask its arr to rescan it. Returns description."""
    for app, (base, key, kind) in apps.items():
        try:
            if kind == "series":
                for s in api(base, key, "series"):
                    if s.get("path") and os.path.normpath(path).startswith(os.path.normpath(s["path"]) + os.sep):
                        r = api(base, key, "command", "POST", {"name": "RescanSeries", "seriesId": s["id"]}, timeout=60); return f"{app} RescanSeries {s['title']} (cmd {(r or {}).get('id')})"
            else:
                for m in api(base, key, "movie"):
                    if m.get("path") and os.path.normpath(path).startswith(os.path.normpath(m["path"]) + os.sep):
                        r = api(base, key, "command", "POST", {"name": "RescanMovie", "movieId": m["id"]}, timeout=60); return f"{app} RescanMovie {m['title']} (cmd {(r or {}).get('id')})"
        except Exception as e: log("rescan_for_path:", app, repr(e))
    return "no arr owns this path"
def arr_lists_path(apps, path):
    """#41: which arr now lists `path` as one of its files ('sonarr', ...), or None. Asks only the series/movie whose folder holds it."""
    n = os.path.normpath(path)
    for app, (base, key, kind) in apps.items():
        try:
            if kind == "series":
                for s in api(base, key, "series"):
                    if s.get("path") and n.startswith(os.path.normpath(s["path"]) + os.sep):
                        if any(os.path.normpath(f.get("path") or "") == n for f in api(base, key, f"episodefile?seriesId={s['id']}") or []): return app
            else:
                for m in api(base, key, "movie"):
                    if m.get("path") and n.startswith(os.path.normpath(m["path"]) + os.sep):
                        if os.path.normpath(((m.get("movieFile") or {}).get("path")) or "") == n: return app
        except Exception as e: log("arr_lists_path:", app, repr(e))
    return None
def finding_row(conn, fid):
    r = conn.execute("SELECT id,app,fid,check_code,severity,label,path,detail FROM finding WHERE id=?", (fid,)).fetchone()
    return dict(zip(("id", "app", "fid", "check", "severity", "label", "path", "detail"), r)) if r else None
def mark_working(conn, what, path=None, app=None, afid=None, codes=("NO_SUBS", "UND_AUDIO_NO_SUBS")):
    """Flag open findings as 'being worked on' (prefix in detail): they leave Needs-you and lose their buttons until the job reports back."""
    pre = f"[working: {what} {datetime.datetime.now():%H:%M}] || "; q = f"UPDATE finding SET detail = ? || detail WHERE resolved_at IS NULL AND detail NOT LIKE '[working:%' AND check_code IN ({','.join('?' * len(codes))})"; args = [pre, *codes]
    if path: q += " AND path=?"; args.append(path)
    if app is not None: q += " AND app=?"; args.append(app)
    if afid is not None: q += " AND fid=?"; args.append(afid)
    n = conn.execute(q, args).rowcount; conn.commit(); return n
def clear_working(conn, path=None, app=None, afid=None):
    """Remove the 'being worked on' flag (the job failed or gave up); the row returns to normal."""
    q = "SELECT id, detail FROM finding WHERE resolved_at IS NULL AND detail LIKE '[working:%'"; args = []
    if path: q += " AND path=?"; args.append(path)
    if app is not None: q += " AND app=?"; args.append(app)
    if afid is not None: q += " AND fid=?"; args.append(afid)
    rows = conn.execute(q, args).fetchall()
    for fid_, d_ in rows: conn.execute("UPDATE finding SET detail=? WHERE id=?", (d_.split("] || ", 1)[1] if "] || " in d_ else d_, fid_))
    if rows: conn.commit()
    return len(rows)
def upsert_finding(conn, fidh, app, fid, code, sev, label, path, detail, started, count_run=False):
    """Write a finding a check just produced. -> 'new' | 'kept' | 'skipped'. A finding resolved at or after `started` was fixed while this check
    was running (a button pressed mid-audit): the check's older view must not re-open it - the next check decides."""
    row = conn.execute("SELECT first_seen, resolved_at FROM finding WHERE id=?", (fidh,)).fetchone()
    if row and row[1] is not None and float(row[1]) >= float(started) - 1: return "skipped"
    if row is None or row[1] is not None:
        conn.execute("INSERT OR REPLACE INTO finding(id,app,fid,check_code,severity,label,path,detail,first_seen,last_seen,resolved_at,notified,seen_runs,handed_off) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,1,0)", (fidh, app, fid, code, sev, label, path, detail, started, started)); return "new"
    conn.execute(f"UPDATE finding SET last_seen=?, detail=?, severity=?, label=?, path=?{', seen_runs=seen_runs+1' if count_run else ''} WHERE id=?", (started, detail, sev, label, path, fidh)); return "kept"
def resolve_finding(conn, fid, note=None):
    conn.execute("UPDATE finding SET resolved_at=? WHERE id=?", (time.time(), fid)); conn.commit()
def add_ignore_rule(line):
    line = line.strip(); cut = line.find(" #"); key = (line[:cut] if cut >= 0 else line).strip()   # comment after the rule; plain split, no regex on user text
    if os.path.exists(IGNORE_FILE) and any(re.split(r"(?:^|\s+)#", l.strip(), 1)[0].strip() == key for l in open(IGNORE_FILE)): return   # already there
    with open(IGNORE_FILE, "a") as f: f.write(line.rstrip() + "\n")
def ui_ignore(conn, body, actor):
    """{id} -> silence this finding; {code, show} -> silence that check for every title starting with show."""
    if body.get("id"):
        f = finding_row(conn, body["id"])
        if not f: return 404, {"error": "finding not found"}
        add_ignore_rule(f"{f['id']}   # {f['check']} {f['label']}"); resolve_finding(conn, f["id"])
        record_action(conn, "on", "ignore", f["app"], f["id"], f["label"], f"{f['check']} silenced by {actor}", "ignore.txt"); n = 1
    elif body.get("code") and body.get("show"):
        pat = "^" + re.escape(body["show"]); add_ignore_rule(f"{body['code'].upper()}: {pat}")
        ids = [r[0] for r in conn.execute("SELECT id FROM finding WHERE resolved_at IS NULL AND check_code=? AND label LIKE ?", (body["code"].upper(), body["show"] + "%"))]
        for i in ids: resolve_finding(conn, i)
        record_action(conn, "on", "ignore", "ui", body["code"], body["show"], f"{body['code']} silenced for show by {actor}", f"{len(ids)} findings"); n = len(ids)
    elif body.get("code"):   # whole check, every title
        add_ignore_rule(f"{body['code'].upper()}   # every title, silenced via UI")
        ids = [r[0] for r in conn.execute("SELECT id FROM finding WHERE resolved_at IS NULL AND check_code=?", (body["code"].upper(),))]
        for i in ids: resolve_finding(conn, i)
        record_action(conn, "on", "ignore", "ui", body["code"], "every title", f"{body['code']} silenced everywhere by {actor}", f"{len(ids)} findings"); n = len(ids)
    else: return 400, {"error": "need id or code+show"}
    refresh_reports(conn); return 200, {"ok": True, "silenced": n}
def accept_language(conn, apps, app, info, lang, label_, actor):
    """Tag the title audit-lang-<lang> in its arr and clear the language findings of that title. Shared by the accept-language button and the nightly pass."""
    base, key, kind = apps[app]; tagl = f"audit-lang-{lang}"
    tag = next((t for t in api(base, key, "tag") if t["label"].lower() == tagl), None) or api(base, key, "tag", "POST", {"label": tagl})
    ed = {"tags": [tag["id"]], "applyTags": "add"}; ed["seriesIds" if kind == "series" else "movieIds"] = [info["parent_id"]]
    api(base, key, "series/editor" if kind == "series" else "movie/editor", "PUT", ed)
    show = label_.rsplit(" S", 1)[0] if kind == "series" else label_
    ids = [r[0] for r in conn.execute("SELECT id FROM finding WHERE resolved_at IS NULL AND app=? AND check_code IN ('WRONG_LANGUAGE','DUB_NOT_WANTED','DUB_ONLY') AND label LIKE ?", (app, show + "%"))]
    for i in ids: resolve_finding(conn, i)
    record_action(conn, "on", "acceptlang", app, info["parent_id"], show, f"tag {tagl} added in {app} by {actor}", f"{len(ids)} finding(s) cleared; future files with {lang} audio are accepted (DUB_NOT_WANTED if a file lacks it)")
    return tagl, show, len(ids)
def do_auto_acceptlang(conn, apps, plan=None):
    """Low WRONG_LANGUAGE where every file of the season agrees on one language: the arr's metadata is wrong, not the files - tag the title like the accept-language button does."""
    mode = gate(setting("AUTO_ACCEPTLANG"), "AUTO_ACCEPTLANG")
    if mode == "off" or not apps: return 0
    done = set(); n = 0; mn = int(setting("AUTO_ACCEPTLANG_MIN_FILES"))
    for fid_, app_, afid_, label_, path_, detail_ in conn.execute("SELECT id, app, fid, label, path, detail FROM finding WHERE resolved_at IS NULL AND check_code='WRONG_LANGUAGE' AND severity='low' AND app IN ('sonarr','radarr') ORDER BY label").fetchall():
        m = re.search(r"audio=\['(\w{3})'\] .*?100% of this season's (\d+) files are \['(\w{3})'\]", detail_ or "")
        if not m or m.group(1) != m.group(3) or int(m.group(2)) < mn or app_ not in apps: continue
        show = label_.rsplit(" S", 1)[0] if apps[app_][2] == "series" else label_
        if (app_, show) in done: continue
        done.add((app_, show))
        if plan is not None: plan.append(dict(label=show, why=f"whole season is {lang_word(m.group(1))} - accept it for the show")); n += 1; continue
        if mode == "dryrun": record_action(conn, "dryrun", "acceptlang", app_, afid_, show, f"would tag audit-lang-{m.group(1)} (the whole season is {m.group(1)})", "dryrun"); n += 1; continue
        try: accept_language(conn, apps, app_, arr_file_info(apps, app_, afid_, path_), m.group(1), label_, "nightly"); n += 1
        except Exception as e: log("auto-acceptlang:", show, repr(e))
    if n and plan is None: log(f"auto-acceptlang: {n} title(s) {'would be ' if mode == 'dryrun' else ''}tagged - whole seasons in one language")
    return n
def do_auto_whisper(conn, apps, plan=None):
    """Bazarr has had its chance: queue AI subtitles for no-subtitle findings older than AUTO_WHISPER_DAYS (same job the per-row button starts)."""
    mode = gate(setting("AUTO_WHISPER"), "AUTO_WHISPER")
    if mode == "off" or not apps or not bazarr_conf()[1]: return 0
    mx = int(setting("AUTO_WHISPER_MAX")); days = float(setting("AUTO_WHISPER_DAYS")); now = time.time(); n = 0
    recent = {l for (l,) in conn.execute("SELECT label FROM action WHERE kind='whisper' AND ts > ?", (now - 7 * 86400,))}   # one try a week per title (errors included)
    up_ = upcoming_set(conn); rows_ = conn.execute("SELECT id, app, fid, label, path, detail FROM finding WHERE resolved_at IS NULL AND check_code IN ('NO_SUBS','UND_AUDIO_NO_SUBS') AND app IN ('sonarr','radarr') AND first_seen < ? ORDER BY first_seen", (now - days * 86400,)).fetchall()
    rows_.sort(key=lambda r_: 0 if (r_[1], r_[2]) in up_ else 1)   # F2: subtitles for what is about to be watched come first
    for fid_, app_, afid_, label_, path_, detail_ in rows_:
        if n >= mx: break
        if label_ in recent or app_ not in apps or "not in Bazarr wanted list" in (detail_ or ""): continue   # not wanted = language-profile problem, Whisper cannot land it
        try: info = arr_file_info(apps, app_, afid_, path_)
        except Exception: continue
        if plan is not None: plan.append(dict(label=label_, why="Bazarr found nothing for days - make AI subtitles")); n += 1; continue
        if mode == "dryrun": record_action(conn, "dryrun", "whisper", app_, afid_, label_, "would queue AI subtitles (Bazarr found nothing)", "dryrun"); n += 1; continue
        job = dict(type="whisper", kind=info["kind"], app=app_, label=label_, path=info["path"], actor="nightly")
        if info["kind"] == "series":
            if not info["ep_ids"]: continue
            job.update(seriesid=info["parent_id"], episodeid=info["ep_ids"][0])
        else: job.update(radarrid=info["parent_id"])
        enqueue(job); n += 1
    if n and plan is None: log(f"auto-whisper: {n} AI-subtitle job(s) {'would be ' if mode == 'dryrun' else ''}queued - Bazarr found nothing for {days:g}+ days")
    return n
def plan_tonight(conn):
    """What the next nightly run would do on its own, from the current findings - the same selection code the night uses, nothing executed."""
    apps = arr_apps(); now = time.time(); out = {"gates": {k: setting(k) for k in ("REMEDIATE", "AUTO_WHISPER", "AUTO_ACCEPTLANG", "AUTO_FIXHEADER", "AUTO_VERIFY", "DEDUPE")}}
    w = []; do_auto_whisper(conn, apps, plan=w); out["ai_subtitles"] = w
    sc = []; do_auto_subsync(conn, apps, None, plan=sc); out["sync_checks"] = sc
    al = []; do_auto_audiolang(conn, plan=al); out["audio_lang"] = al
    a = []; do_auto_acceptlang(conn, apps, plan=a); out["accept_language"] = a
    out["fix_headers"] = [dict(label=l, why="repair the wrong time bar") for (l,) in conn.execute("SELECT label FROM finding WHERE resolved_at IS NULL AND check_code='BAD_DURATION_HEADER' ORDER BY first_seen LIMIT 10")] if gate(setting("AUTO_FIXHEADER"), "AUTO_FIXHEADER") != "off" else []
    out["verify"] = [dict(label=l, why=f"check it is the right {'movie' if a_ == 'radarr' else 'episode'} ({check_name(c)})") for l, c, a_ in conn.execute("SELECT label, check_code, app FROM finding WHERE resolved_at IS NULL AND check_code IN ('RUNTIME_LONG','RUNTIME_SHORT','TITLE_MISMATCH') AND detail NOT LIKE '[verified%' AND app IN ('sonarr','whisparr','radarr') ORDER BY first_seen DESC LIMIT ?", (int(setting("AUTO_VERIFY_MAX")),))] if gate(setting("AUTO_VERIFY"), "AUTO_VERIFY") == "on" else []
    rg = []
    if gate(setting("REMEDIATE"), "REMEDIATE") != "off":
        allowed = set(setting("REMEDIATE_CHECKS").split(",")); min_age = float(setting("REMEDIATE_MIN_AGE_HOURS")); min_runs = int(setting("REMEDIATE_MIN_RUNS"))
        prior = {(a_, l_) for a_, l_ in conn.execute("SELECT app, label FROM regrab WHERE status='same_defect' OR ts > ?", (now - 30 * 86400,))}
        for app_, label_, code_, fs_, runs_ in conn.execute("SELECT app, label, check_code, first_seen, seen_runs FROM finding WHERE resolved_at IS NULL AND severity IN ('high','critical')"):
            if code_ in allowed and app_ in REMEDIATE_APPS and (now - fs_) >= min_age * 3600 and (runs_ or 1) >= min_runs and (app_, label_) not in prior: rg.append(dict(label=label_, why=f"{check_name(code_)} - download a better copy"))
    out["regrab"] = rg[:int(setting("REMEDIATE_MAX"))]; out["regrab_mode"] = setting("REMEDIATE")
    return out
def _probe(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(); ct = r.headers.get("Content-Type") or ""
        return json.loads(raw) if "json" in ct or raw[:1] in b"{[" else raw.decode("utf-8", "replace")
SERVICE_NAMES = {"sonarr": "Sonarr", "radarr": "Radarr", "whisparr": "Whisparr", "sportarr": "Sportarr", "bazarr": "Bazarr", "plex": "Plex"}
KEY_PLACE = {"plex": "Plex Web → open any item → ⋯ → Get info → View XML; the X-Plex-Token is at the end of that page's address.", "bazarr": "Bazarr → Settings → General → Security → API Key."}
def plain_error(kind, e):
    """U15: turn an exception from talking to a service into (what happened, what to do), in plain words."""
    name = SERVICE_NAMES.get(kind, kind); where = KEY_PLACE.get(kind, f"{name} → Settings → General → Security → API Key.")
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403): return (f"{name} rejected the key." if kind != "plex" else "Plex rejected the token."), f"Copy it again from {where} Paste it here and press Test."
        if e.code == 404: return f"The address answers, but not as {name}.", f"Check the port, and whether {name} runs under a URL base (for example http://host:8989/sonarr)."
        if e.code >= 500: return f"{name} answered with a server error ({e.code}).", f"Open {name} in your browser. If it works there, try again in a minute."
        return f"{name} answered HTTP {e.code}.", "Check the address and the key."
    if isinstance(e, urllib.error.URLError):
        r = getattr(e, "reason", e); rs = str(r).lower()
        if "refused" in rs: return "Nothing is listening at that address.", "Check host and port. From inside Docker, 'localhost' is the Auditarr container itself: use the server's IP, or the container name when both are on the same Docker network."
        if "name or service" in rs or "nodename" in rs or "getaddrinfo" in rs or "not known" in rs: return "That host name cannot be found.", "Use the IP address, or the container name if both run on the same Docker network."
        if "timed out" in rs or isinstance(r, TimeoutError): return "That address did not answer in time.", "A firewall, a VLAN, or the wrong port. Try opening the address in a browser from this server."
        if "certificate" in rs or "ssl" in rs: return "The https certificate is not trusted here.", "Use http:// on your own network, or the exact https name that is on the certificate."
        if "no route" in rs or "unreachable" in rs: return "No route to that address.", "Is it on another VLAN or a Docker network Auditarr is not part of?"
        return "Cannot connect to that address.", "Check the address, and that the service is running."
    if isinstance(e, TimeoutError) or type(e).__name__ == "timeout": return "That address did not answer in time.", "A firewall, a VLAN, or the wrong port."
    return f"Unexpected answer from {name}.", repr(e)[:120]
def discover_services(extra=None):
    """U14 setup auto-discovery: look for Sonarr, Radarr, Whisparr, Bazarr and Plex on the Docker network and the LAN, and say whether a key was found in mounted config files."""
    ports = {"sonarr": 8989, "radarr": 7878, "whisparr": 6969, "sportarr": 1867, "bazarr": 6767, "plex": 32400}
    hosts = []
    if HOST_IP: hosts.append(HOST_IP)
    try:   # the container's default gateway is the Docker host on a bridge network
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000" and len(f[2]) == 8: hosts.append(".".join(str(int(f[2][i:i + 2], 16)) for i in (6, 4, 2, 0))); break
    except Exception: pass
    hosts += ["host.docker.internal"]
    cands = []
    for h in dict.fromkeys(hosts):
        for k, p in ports.items(): cands.append((k, f"http://{h}:{p}"))
    for k, p in ports.items(): cands.append((k, f"http://{k}:{p}"))   # compose-style container names
    for u in (extra or []):
        u = u.strip().rstrip("/")
        if u: cands += [(k, u) for k in ports]
    for u in (E("DISCOVER_EXTRA") or "").split(","):
        u = u.strip().rstrip("/")
        if u: cands += [(k, u) for k in ports]
    def fetch(u, accept="*/*"):
        req = urllib.request.Request(u, headers={"User-Agent": "auditarr", "Accept": accept})
        with urllib.request.urlopen(req, timeout=2.5) as r: return r.status, r.read(4096).decode("utf-8", "replace")
    def probe(item):
        kind, base = item
        try:
            if kind == "plex":
                st, body = fetch(f"{base}/identity")
                return dict(kind=kind, url=base) if "MediaContainer" in body and "machineIdentifier" in body else None
            if kind == "bazarr":
                st, body = fetch(f"{base}/")
                return dict(kind=kind, url=base) if re.search(r"<title>\s*Bazarr", body, re.I) else None
            st, body = fetch(f"{base}/ping")
            if '"status"' not in body.lower() and "ok" not in body.lower(): return None
            st2, html = fetch(f"{base}/")
            m = re.search(r"<title>\s*(Sonarr|Radarr|Whisparr)", html, re.I); found = (m.group(1).lower() if m else None)
            if found and found != kind: return None
            return dict(kind=kind, url=base, sure=bool(m))
        except Exception: return None
    found = {}
    with ThreadPoolExecutor(16) as ex:
        for r in ex.map(probe, cands):
            if r and r["kind"] not in found: found[r["kind"]] = r
    out = []
    for kind, r in found.items():
        if kind in ARR_KINDS: key = bool(conn_value(f"{kind.upper()}_APIKEY")) or (os.path.exists(os.path.join(ARR_DIR, f"{kind}.xml")) and bool(read_key(os.path.join(ARR_DIR, f"{kind}.xml"))))
        elif kind == "bazarr": key = bool(bazarr_conf()[1])
        else: key = bool(plex_token())
        out.append(dict(kind=kind, name=SERVICE_NAMES[kind], url=r["url"], key_found=bool(key)))
    return dict(found=out, scanned=len(cands), hosts=list(dict.fromkeys(hosts)))
def test_connection(kind, url, key):
    """One real request to a service with the given address and key. Returns what a person needs to know: reachable?, version, root folders and whether this container can see them, and on failure what happened and what to do."""
    url = (url or "").rstrip("/"); out = {"kind": kind, "ok": False}
    if not url: out["error"] = "No address yet."; out["fix"] = f"Type the address you open {SERVICE_NAMES.get(kind, kind)} at in your browser, for example http://192.168.1.10:{ {'sonarr': 8989, 'radarr': 7878, 'whisparr': 6969, 'bazarr': 6767, 'plex': 32400}[kind] if kind in SERVICE_NAMES else 80}."; return out
    try:
        if kind in ARR_KINDS:
            if not key: out["error"] = "No API key yet."; out["fix"] = f"Copy it from {KEY_PLACE.get(kind, SERVICE_NAMES[kind] + ' → Settings → General → Security → API Key.')}"; return out
            st = _probe(f"{url}/api/v3/system/status", {"X-Api-Key": key}); out.update(ok=True, version=st.get("version"), name=(st.get("instanceName") if kind == "sportarr" else None) or st.get("appName") or kind)   # Sportarr answers "Sonarr" as appName
            roots = [r.get("path") for r in (_probe(f"{url}/api/v3/rootfolder", {"X-Api-Key": key}) or []) if isinstance(r, dict)]
            out["roots"] = [{"path": r, "visible": bool(r) and os.path.isdir(r)} for r in roots]
        elif kind == "bazarr":
            if not key: out["error"] = "No API key yet."; out["fix"] = f"Copy it from {KEY_PLACE['bazarr']}"; return out
            st = _probe(f"{url}/api/system/status", {"X-API-KEY": key}); d = st.get("data", st) if isinstance(st, dict) else {}
            out.update(ok=True, version=d.get("bazarr_version") or d.get("version"), name="Bazarr")
        elif kind == "plex":
            if not key: out["error"] = "No token yet."; out["fix"] = KEY_PLACE["plex"]; return out
            idn = _probe(f"{url}/identity?X-Plex-Token={key}", {"Accept": "application/json"}); mc = idn.get("MediaContainer", {}) if isinstance(idn, dict) else {}
            out.update(ok=True, version=mc.get("version"), name="Plex", machine=mc.get("machineIdentifier"))
            secs = _probe(f"{url}/library/sections?X-Plex-Token={key}", {"Accept": "application/json"}); locs = []
            for sct in (secs.get("MediaContainer", {}).get("Directory", []) if isinstance(secs, dict) else []):
                for l in sct.get("Location", []): locs.append({"section": sct.get("title"), "path": l.get("path"), "visible": os.path.isdir(l.get("path") or ""), "mapped": any(os.path.isdir((l.get("path") or "").replace(b_, a_, 1)) for a_, b_ in _plex_maps() if (l.get("path") or "").startswith(b_))})
            out["locations"] = locs
        else: out["error"] = f"unknown service {kind}"
    except Exception as e: out["error"], out["fix"] = plain_error(kind, e)
    return out
def ui_setup(conn):
    """What the setup page shows: per service where its details come from and whether it answers, which media folders are visible, and whether a first run happened."""
    apps = arr_apps(); bz_url, bz_key = bazarr_conf(); services = []
    for kind in ARR_KINDS + ("bazarr", "plex"):
        if kind == "bazarr": url, key = bz_url, bz_key
        elif kind == "plex": url, key = PLEX_URL, plex_token()
        else: url, key = (apps.get(kind) or (None, None, None))[:2]
        src = "saved in the setup page" if SETTINGS.get(f"{kind.upper()}_URL") or SETTINGS.get(f"{kind.upper()}_APIKEY" if kind != "plex" else "PLEX_TOKEN") else ("container env" if E(f"{kind.upper()}_URL") or E(f"{kind.upper()}_APIKEY") or E("PLEX_TOKEN") else ("mounted config file" if key else "not configured"))
        t = test_connection(kind, url, key) if key else {"kind": kind, "ok": False, "error": "not configured" if kind not in OPTIONAL_ARRS else "optional, not configured"}
        t.update(url=url or "", has_key=bool(key), source=src, optional=kind in OPTIONAL_ARRS); services.append(t)
    runs = conn.execute("SELECT count(*) FROM run WHERE finished IS NOT NULL").fetchone()[0]
    required_ok = all(x["ok"] for x in services if x["kind"] in ("sonarr", "radarr")) and any(x["ok"] for x in services if x["kind"] in ("sonarr", "radarr"))
    roots = [r for x in services for r in x.get("roots", [])]
    return dict(services=services, first_run_done=runs > 0, runs=runs, complete=required_ok and all(r["visible"] for r in roots) if roots else required_ok,
                media_visible=all(r["visible"] for r in roots) if roots else None, roots=roots, data_dir="/data", hint="Mount the folder that holds your media at the same path the arrs use (e.g. -v /mnt/user/media:/data/media), or the audit cannot open the files.")
def hook_token(conn):
    r = conn.execute("SELECT v FROM kv WHERE k='webhook_token'").fetchone()
    if r: return r[0]
    import secrets; t = secrets.token_hex(16); conn.execute("INSERT OR REPLACE INTO kv VALUES('webhook_token',?)", (t,)); conn.commit(); return t
def hook_url(app, conn): return f"{(E('PUBLIC_URL') or f'http://{HOST_IP}:{HTTP_PORT}').rstrip('/')}/webhook/{app}?token={hook_token(conn)}"
def hook_status(conn):
    """Does each arr have the Auditarr webhook connection, pointing at the right URL?"""
    apps = arr_apps(); out = []
    for app, (base, key, kind) in apps.items():
        try:
            mine = [n for n in api(base, key, "notification") or [] if (n.get("name") or "").lower() == "auditarr"]
            url = next((f.get("value") for n in mine for f in n.get("fields", []) if f.get("name") == "url"), None)
            out.append(dict(app=app, connected=bool(mine), url_ok=url == hook_url(app, conn), url=url, wants=hook_url(app, conn).split("?")[0]))
        except Exception as e: out.append(dict(app=app, connected=False, error=repr(e)[:120]))
    return out
def ensure_hook(conn, app):
    """Create (or fix) the 'Auditarr' webhook connection in that arr: on import and on upgrade, POST to /webhook/<app>."""
    apps = arr_apps()
    if app not in apps: return 404, {"error": f"{app} is not connected"}
    base, key, kind = apps[app]; url = hook_url(app, conn)
    body = {"name": "Auditarr", "implementation": "Webhook", "configContract": "WebhookSettings", "onGrab": False, "onDownload": True, "onUpgrade": True, "onRename": False,
            "onHealthIssue": False, "onApplicationUpdate": False, "includeHealthWarnings": False, "tags": [], "fields": [{"name": "url", "value": url}, {"name": "method", "value": 1}, {"name": "username", "value": ""}, {"name": "password", "value": ""}]}
    if kind == "series": body.update(onSeriesDelete=False, onEpisodeFileDelete=False, onEpisodeFileDeleteForUpgrade=False)
    else: body.update(onMovieDelete=False, onMovieFileDelete=False, onMovieFileDeleteForUpgrade=False)
    mine = [n for n in api(base, key, "notification") or [] if (n.get("name") or "").lower() == "auditarr"]
    if mine: body["id"] = mine[0]["id"]; r = api(base, key, f"notification/{mine[0]['id']}", "PUT", body); what = "updated"
    else: r = api(base, key, "notification", "POST", body); what = "created"
    record_action(conn, "on", "hook", app, (r or {}).get("id"), app, f"import hook {what}", url.split("?")[0])
    return 200, {"ok": True, "what": what, "url": url.split("?")[0], "note": f"{app.capitalize()} now tells Auditarr about every import and upgrade; the new file is checked within a minute"}
def whisper_followup(app, afid, path, label, minutes, since=0):
    """F14: a new download had no subtitles; Bazarr has had `minutes` to find some. If it still lists the item as wanted, make AI subtitles now instead of waiting for the nightly rule."""
    conn = db()
    try:
        load_settings(conn)
        if gate(setting("AUTO_WHISPER"), "AUTO_WHISPER") != "on" or READ_ONLY: return
        apps = arr_apps()
        if app not in apps: return
        open_ = conn.execute("SELECT 1 FROM finding WHERE resolved_at IS NULL AND app=? AND fid=? AND check_code IN ('NO_SUBS','UND_AUDIO_NO_SUBS')", (app, afid)).fetchone()
        if not open_: log(f"import follow-up: {label} has subtitles now, nothing to do"); return
        if conn.execute("SELECT 1 FROM action WHERE kind='whisper' AND label=? AND ts > ?", (label, since)).fetchone(): log(f"import follow-up: AI subtitles were already tried for {label} since it arrived"); return
        try: info = arr_file_info(apps, app, afid, path)
        except Exception as e: log("import follow-up:", repr(e)); return
        bz_eps, bz_movs = bazarr_wanted()
        wanted = (info["kind"] == "series" and any(e in bz_eps for e in info["ep_ids"])) or (info["kind"] == "movie" and info["parent_id"] in bz_movs)
        if not wanted: log(f"import follow-up: Bazarr does not want subtitles for {label} (language profile) - leaving it"); return
        job = dict(type="whisper", kind=info["kind"], app=app, label=label, path=info["path"], actor="import follow-up")
        if info["kind"] == "series":
            if not info["ep_ids"]: return
            job.update(seriesid=info["parent_id"], episodeid=info["ep_ids"][0])
        else: job.update(radarrid=info["parent_id"])
        enqueue(job); record_action(conn, "on", "import-check", app, afid, label, "AI subtitles queued after import", f"Bazarr still had nothing {minutes} min after the download")
        log(f"import follow-up: AI subtitles queued for {label} (Bazarr found nothing in {minutes} min)")
    except Exception as e: note_error("import follow-up failed:", repr(e))
    finally: conn.close()
# ---------- F31: subtitles first for what people are watching ----------
def plex_watch_items(meta, why):
    """Plex metadata rows (sessions or On Deck) -> what matters for matching: title, show key, season/episode, file paths, who."""
    out = []
    for m in meta or []:
        paths = {os.path.normpath(from_plex(p["file"])) for md in (m.get("Media") or []) for p in (md.get("Part") or []) if p.get("file")}
        who = (m.get("User") or {}).get("title", "")
        if m.get("type") == "episode": out.append(dict(why=why, kind="episode", title=m.get("grandparentTitle") or "", show_rk=str(m.get("grandparentRatingKey") or ""), s=int(m.get("parentIndex") or 0), e=int(m.get("index") or 0), paths=paths, who=who))
        elif m.get("type") == "movie": out.append(dict(why=why, kind="movie", title=m.get("title") or "", show_rk="", s=0, e=0, paths=paths, who=who))
    return out
def watch_targets():
    """What is playing in Plex right now (sessions) and what is next up on people's Home (On Deck)."""
    out = []
    for path, why in (("/status/sessions", "watching"), ("/library/onDeck", "next up")):
        try: out += plex_watch_items((plex_get(path, timeout=20).get("MediaContainer") or {}).get("Metadata") or [], why)
        except Exception as e: log(f"watch priority: {path}: {e!r}")
    return out
def _norm_title(t): return re.sub(r"[^a-z0-9]+", "", re.sub(r"\(\d{4}\)", "", (t or "").lower()))
def watch_matches(targets, rows, ahead=6):
    """rows = open no-subtitle findings (id, app, fid, label, path, detail) -> {(app, fid): (why, label, path, finding id)}.
    A hit is the file itself, the next `ahead` episodes of a show someone is in, or the first 3 of its following season. 'watching' beats 'next up'."""
    out = {}
    for t in targets:
        for fid_, app_, afid_, label_, path_, detail_ in rows:
            key = (app_, afid_); hit = os.path.normpath(path_ or "") in t["paths"]
            if not hit and t["kind"] == "episode":
                m = re.search(r"^(.*?)\s+S(\d+)E(\d+)", label_ or "")
                if m and _norm_title(m.group(1)) == _norm_title(t["title"]):
                    s_, e_ = int(m.group(2)), int(m.group(3)); hit = (s_ == t["s"] and t["e"] <= e_ <= t["e"] + ahead) or (s_ == t["s"] + 1 and e_ <= 3)
            if hit and (key not in out or (t["why"] == "watching" and out[key][0] != "watching")): out[key] = (t["why"], label_, path_, fid_)
    return out
def watch_priority_pass(conn, targets=None, now=None, max_searches=10, max_whispers=5):
    """Every few minutes. For no-subtitle files someone is watching or about to watch: a Bazarr search now, then AI subtitles if Bazarr still has
    nothing WATCH_PRIORITY_MINUTES later - both queued ahead of every other job. Returns (searches queued, AI jobs queued)."""
    load_settings(conn); mode = gate(setting("WATCH_PRIORITY"), "WATCH_PRIORITY")
    if mode == "off" or READ_ONLY or E("JOBS", "on") == "off": return (0, 0)
    now = now or time.time(); mins = int(setting("WATCH_PRIORITY_MINUTES") or 0)
    if targets is None:
        if not plex_token(): return (0, 0)
        targets = watch_targets()
    rows = conn.execute("SELECT id, app, fid, label, path, detail FROM finding WHERE resolved_at IS NULL AND check_code IN ('NO_SUBS','UND_AUDIO_NO_SUBS') AND app IN ('sonarr','radarr') AND detail NOT LIKE '%not in Bazarr wanted list%'").fetchall()
    hits = watch_matches(targets, rows) if targets and rows else {}
    try: st = json.loads((conn.execute("SELECT v FROM kv WHERE k='watch_priority'").fetchone() or ["{}"])[0])
    except Exception: st = {}
    st = {k: v for k, v in st.items() if now - float(v.get("ts", 0)) < 86400}   # a title is remembered for a day, then it may be pushed again
    conn.execute("DELETE FROM upcoming WHERE why='watching'")
    for (app_, afid_), (why, label_, path_, fid_) in hits.items(): conn.execute("INSERT OR REPLACE INTO upcoming(app,fid,label,ts,why) VALUES(?,?,?,?,?)", (app_, afid_, label_, now, why))
    conn.commit()
    searches = whispers = 0; apps = arr_apps() if hits else {}; bz_key = bazarr_conf()[1] if hits else None
    order = sorted(hits.items(), key=lambda kv_: 0 if kv_[1][0] == "watching" else 1)
    for (app_, afid_), (why, label_, path_, fid_) in order:
        k = f"{app_}:{afid_}"; e = st.setdefault(k, dict(ts=now, why=why, searched=0, whisper=0, label=label_))
        if app_ not in apps or not bz_key: continue
        try: info = arr_file_info(apps, app_, afid_, path_)
        except Exception: continue
        if not e["searched"]:
            if searches >= max_searches: continue
            if mode == "dryrun": record_action(conn, "dryrun", "watch-priority", app_, afid_, label_, f"{why} in Plex: would ask Bazarr now", "dryrun"); e["searched"] = now; searches += 1; continue
            enqueue(dict(type="subsearch", prio=1, fid=fid_, app=app_, kind=info["kind"], parent_id=info["parent_id"], ep_ids=info["ep_ids"], path=info["path"], label=label_, actor=f"watch priority ({why})"))
            record_action(conn, "on", "watch-priority", app_, afid_, label_, f"{why} in Plex: Bazarr search now, AI subtitles in {mins} min if it finds nothing", "search queued ahead of the line")
            e["searched"] = now; searches += 1
        elif not e["whisper"] and now - float(e["searched"]) >= mins * 60:
            if whispers >= max_whispers or gate(setting("AUTO_WHISPER"), "AUTO_WHISPER") != "on": continue
            if conn.execute("SELECT 1 FROM action WHERE kind='whisper' AND label=? AND ts > ?", (label_, float(e["searched"]) - 60)).fetchone(): e["whisper"] = now; continue
            bz_eps, bz_movs = bazarr_wanted()
            if not ((info["kind"] == "series" and any(x in bz_eps for x in info["ep_ids"])) or (info["kind"] == "movie" and info["parent_id"] in bz_movs)): e["whisper"] = now; continue
            if mode == "dryrun": record_action(conn, "dryrun", "watch-priority", app_, afid_, label_, f"{why} in Plex: would make AI subtitles", "dryrun"); e["whisper"] = now; whispers += 1; continue
            job = dict(type="whisper", prio=1, kind=info["kind"], app=app_, label=label_, path=info["path"], actor=f"watch priority ({why})")
            if info["kind"] == "series":
                if not info["ep_ids"]: continue
                job.update(seriesid=info["parent_id"], episodeid=info["ep_ids"][0])
            else: job.update(radarrid=info["parent_id"])
            enqueue(job); record_action(conn, "on", "watch-priority", app_, afid_, label_, f"{why} in Plex: Bazarr found nothing in {mins} min", "AI subtitles queued ahead of the line"); e["whisper"] = now; whispers += 1
    conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('watch_priority', ?)", (json.dumps(st),)); conn.commit()
    if hits: refresh_reports(conn)
    if searches or whispers: log(f"watch priority: {len(hits)} title(s) being watched or next up without subtitles - {searches} Bazarr search(es), {whispers} AI-subtitle job(s) queued first")
    return (searches, whispers)
def audit_one(app, parent_id, path, reason):
    """The import hook: list that one title, probe its new file, run the checks, store findings, push if something is seriously wrong."""
    time.sleep(8)   # let the arr finish renaming / Plex start scanning
    if RUN_LOCK.locked(): log(f"import hook: nightly run in progress, {app} {parent_id} will be covered by it"); return
    conn = db()
    try:
        apps = arr_apps()
        if app not in apps: return
        items, _ = collect({app: apps[app]}, only={(app, int(parent_id))})
        if not items: log(f"import hook: nothing listed for {app} {parent_id}"); return
        prev = dict(PROGRESS); probe_all(conn, items)
        for k_, v_ in prev.items(): PROGRESS[k_] = v_
        bz_eps, bz_movs = bazarr_wanted(); rules = load_ignores(); load_hardsub(conn); profile = series_language_profile(items); rt_profile = season_runtime_profile(items); started = time.time(); out = []
        targets = [it for it in items if not path or os.path.normpath(it.get("path") or "") == os.path.normpath(path)] or items
        for it in targets:
            for code, sev, detail in check(it, bz_eps, bz_movs, profile, rt_profile):
                fidh = hashlib.sha1(f"{app}|{it['fid']}|{code}".encode()).hexdigest()[:16]
                if ignored(rules, code, it["label"], fidh): continue
                upsert_finding(conn, fidh, app, it["fid"], code, sev, it["label"], it["path"], detail, started)
                out.append((sev, code, it["label"], detail))
        flush_hardsub(conn)
        for it in targets:   # an import re-check also clears this file's earlier findings that are gone now (a replaced or re-tagged file), instead of leaving them until the night
            still = {c for _, c, l, _ in out if l == it["label"]}
            for old_id, old_code in conn.execute("SELECT id, check_code FROM finding WHERE resolved_at IS NULL AND app=? AND fid=?", (app, it["fid"])).fetchall():
                if old_code not in still and old_code not in ("PLEX_MISSING", "MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE"): resolve_finding(conn, old_id, "cleared by the import re-check")
        conn.commit(); label = targets[0]["label"] if targets else str(parent_id)
        bad = [x for x in out if x[0] in ("high", "critical")]
        record_action(conn, "on", "import-check", app, parent_id, label, f"checked right after import ({reason})", (f"{len(out)} finding(s): " + ", ".join(sorted({check_name(c) for _, c, _, _ in out}))) if out else "clean")
        refresh_reports(conn)
        if bad: ntfy("Auditarr: a new download has a problem", "\n".join(f"{l}: {plain_sentence(c, sv, l, d, app)}" for sv, c, l, d in bad[:4]), "high", "warning", click=page_url("findings"))
        log(f"import hook: {label} -> {len(out)} finding(s), {len(bad)} serious")
        mins = int(setting("AUTO_WHISPER_IMPORT_MINUTES") or 0)   # F14: give Bazarr a short head start, then make AI subtitles the same day
        if mins > 0 and gate(setting("AUTO_WHISPER"), "AUTO_WHISPER") == "on" and any(c in ("NO_SUBS", "UND_AUDIO_NO_SUBS") for _, c, _, _ in out):
            it0 = targets[0]; t_ = threading.Timer(mins * 60, whisper_followup, (app, it0["fid"], it0["path"], it0["label"], mins, time.time())); t_.daemon = True; t_.start()
            log(f"import hook: {label} has no subtitles - AI subtitles in {mins} min unless Bazarr finds some")
    except Exception as e: note_error("import hook failed:", repr(e))
    finally: conn.close()
def ui_search_show(conn, body, actor):
    """{show} -> one search command per arr covering every open missing episode/movie/scene of that title."""
    show = (body.get("show") or "").strip(); apps = arr_apps()
    if not show: return 400, {"error": "need show"}
    rows = [r for r in conn.execute("SELECT app, fid, label FROM finding WHERE resolved_at IS NULL AND check_code IN ('MISSING_EPISODE','MISSING_MOVIE','MISSING_SCENE') AND label LIKE ?", (show + "%",)) if r[0] in apps and int(r[1]) < 0]
    if not rows: return 404, {"error": "no open missing items for that title"}
    out = {}
    for app_ in sorted({r[0] for r in rows}):
        base, key, kind = apps[app_]; ids = [-int(r[1]) for r in rows if r[0] == app_][:100]
        cmd = {"name": "EpisodeSearch", "episodeIds": ids} if kind == "series" else {"name": "MoviesSearch", "movieIds": ids}
        r = api(base, key, "command", "POST", cmd, timeout=60); out[app_] = {"items": len(ids), "command": (r or {}).get("id")}
        record_action(conn, "on", "search", app_, len(ids), show, f"{cmd['name']} for every missing item via UI by {actor}", f"{len(ids)} item(s); command {(r or {}).get('id')} {(r or {}).get('status')}")
    return 200, {"ok": True, "searched": out, "note": f"{sum(v['items'] for v in out.values())} missing item(s) searched - results land in the arr's queue; titles that stay unfound show up in the Today digest"}
def remux_to_mkv(conn, f, apps, actor):
    """MP4_IN_MKV: rewrap an MP4 that carries a .mkv name into a real Matroska file with mkvmerge (no re-encode; MP4 text subtitles become
    SRT tracks). The new file is built beside the old one and checked (Matroska, same video and audio tracks, same length) before the
    original moves to the trash; any failure leaves the original untouched."""
    import shutil
    path = f["path"]
    if not path or not path.lower().endswith(".mkv"): return 400, {"error": "Rewrap the file is for files with a .mkv name"}
    if not os.path.exists(path): return 404, {"error": "file not on disk"}
    ok, pr = ffprobe(path)
    if not ok: return 500, {"error": "ffprobe failed", "detail": pr}
    if is_matroska({"path": path, "probe": pr}):
        resolve_finding(conn, f["id"]); refresh_reports(conn); return 200, {"ok": True, "already_gone": True, "note": "already a real MKV; finding cleared"}
    size = os.path.getsize(path)
    if shutil.disk_usage(os.path.dirname(path)).free < size * 1.1 + 2e9: return 507, {"error": f"not enough free space next to the file for a {size / 1e9:.1f} GB copy"}
    kinds = lambda p_: {k: sum(1 for s_ in p_.get("streams", []) if s_.get("codec_type") == k) for k in ("video", "audio", "subtitle")}
    k0 = kinds(pr); dur0 = float((pr.get("format") or {}).get("duration") or 0)
    tmp = path[:-4] + ".remux.tmp.mkv"
    try:
        guard_write("mkvmerge remux"); p = subprocess.run(["mkvmerge", "-o", tmp, path], capture_output=True, text=True, timeout=3600)
        if p.returncode not in (0, 1): raise RuntimeError((p.stderr or p.stdout)[-300:])
        ok2, pr2 = ffprobe(tmp)
        if not ok2 or "matroska" not in str((pr2.get("format") or {}).get("format_name") or ""): raise RuntimeError("the new file is not a Matroska file")
        k1 = kinds(pr2); dur1 = float((pr2.get("format") or {}).get("duration") or 0)
        if (k1["video"], k1["audio"]) != (k0["video"], k0["audio"]): raise RuntimeError(f"tracks changed: {k0['video']} video + {k0['audio']} audio before, {k1['video']} + {k1['audio']} after")
        if dur0 and abs(dur1 - dur0) > 2: raise RuntimeError(f"length changed: {dur0 / 60:.1f} min before, {dur1 / 60:.1f} min after")
    except Exception as e:
        try: os.remove(tmp)
        except OSError: pass
        record_action(conn, "on", "remuxmkv", f["app"], f["fid"], f["label"], f"mkvmerge remux via UI by {actor}", f"ERROR {str(e)[-160:]}")
        return 500, {"error": f"remux failed, original untouched: {e}"}
    lost = k0["subtitle"] - k1["subtitle"]
    dest, moved = quarantine(conn, path, f"ui remuxmkv by {actor}: MP4 inside .mkv rewrapped as Matroska", "regrab", sidecars=False); os.rename(tmp, path)
    try: st0 = os.stat(dest); os.chmod(path, st0.st_mode & 0o777); os.chown(path, st0.st_uid, st0.st_gid)
    except OSError: pass
    conn.execute("DELETE FROM probe WHERE path=?", (path,)); conn.execute("DELETE FROM trackprefs WHERE path=?", (path,)); resolve_finding(conn, f["id"])
    resc = rescan_for_path(apps, path) if f["app"] in apps else ""; plx = plex_refresh(path)
    what = f"{k1['video']} video, {k1['audio']} audio, {k1['subtitle']} subtitle track(s)" + (f"; {lost} subtitle track(s) MKV cannot hold were left out" if lost > 0 else "")
    record_action(conn, "on", "remuxmkv", f["app"], f["fid"], f["label"], f"mkvmerge remux via UI by {actor}: {what}", f"original -> {dest}; {resc}; {plx}")
    refresh_reports(conn)
    return 200, {"ok": True, "note": f"now a real MKV ({what}); default tracks are set on the next audit; original in the trash for {TRASH_DAYS} days"}
PRESSING = {}   # finding id -> (what, started): a button press the server is still working on (re-time, rewrap, fix the time bar ... run inside the request)
SUB_CODES = ("NO_SUBS", "UND_AUDIO_NO_SUBS", "SUBS_OUT_OF_SYNC")
JOB_BUSY = {"whisper": ("making AI subtitles", SUB_CODES), "subsearch": ("searching for subtitles", SUB_CODES), "subsync": ("checking the subtitle timing", ("SUBS_OUT_OF_SYNC",)),
            "langdetect": ("listening to the audio", None), "verify": ("checking what the file really is", None), "followup": ("checking that the fix worked", None)}
def _jload(raw):
    try: v = json.loads(raw); return v if isinstance(v, dict) else {}
    except Exception: return {}
def busy_marks(findings, active_jobs, inflight=None):
    """2026-11-12: a row whose fix was queued kept its buttons, so a press looked like it did nothing. Each finding a job is running or waiting
    for gets busy={what, state, mins}: the page moves it to 'Being fixed now'. A job names its finding (langdetect, verify) or its file plus the kinds
    of problem it can clear (subtitle jobs). active_jobs: [(status, job dict, started_at or ts)]."""
    now = time.time(); by_id = {}; by_path = {}
    for st_, jb, t0 in active_jobs:
        what, codes = JOB_BUSY.get(jb.get("type"), (None, None))
        if not what: continue
        mark = dict(what=what, state="running" if st_ == "running" else "waiting", mins=int((now - (t0 or now)) / 60))
        if jb.get("finding"): by_id.setdefault(jb["finding"], mark)
        elif codes and jb.get("path"):
            prev = by_path.get(os.path.normpath(jb["path"]))
            if not prev or (prev[0]["state"] == "waiting" and mark["state"] == "running"): by_path[os.path.normpath(jb["path"])] = (mark, codes)
    for f in findings:
        m = (inflight or {}).get(f.get("id"))
        if m: f["busy"] = dict(what=m[0], state="running", mins=int((now - m[1]) / 60)); continue
        if f.get("id") in by_id: f["busy"] = by_id[f["id"]]; continue
        pm = by_path.get(os.path.normpath(f.get("path") or "")) if f.get("path") else None
        if pm and f.get("check") in pm[1]: f["busy"] = pm[0]
    return findings
def ui_act(conn, body, actor):
    """Every button press on a row: while it runs, the row shows as 'Being fixed now' for anyone who loads the page."""
    fid = body.get("id"); PRESSING[fid] = (ACT_WORDS.get(body.get("action"), "working on it"), time.time())
    try: return _ui_act(conn, body, actor)
    finally: PRESSING.pop(fid, None)
ACT_WORDS = {"subsync_fix": "re-timing the subtitles", "remuxmkv": "rewrapping the file", "fixheader": "fixing the time bar", "fixsync": "fixing the audio delay",
             "fixtag": "labelling the audio", "regrab": "asking for a better copy", "subtag": "labelling the subtitles", "quarantine": "moving it to the trash"}
def _ui_act(conn, body, actor):
    fid = body.get("id"); action = body.get("action"); f = finding_row(conn, fid) if fid else None
    if not f: return 404, {"error": "finding not found"}
    apps = arr_apps(); path = f["path"]
    if action == "quarantine":
        if f["check"] not in ("DUPLICATE_VIDEO", "ORPHAN_FILE", "SAMPLE_FILE") and f["app"] == "disk": return 400, {"error": "quarantine is for unmanaged files; use regrab for arr files"}
        if not path or not os.path.exists(path):
            resolve_finding(conn, fid); refresh_reports(conn); return 200, {"ok": True, "already_gone": True, "note": "file was already moved/removed; finding cleared"}
        if f["app"] != "disk":
            return 400, {"error": "this file is managed by an arr - use regrab so the arr knows"}
        if f["check"] == "DUPLICATE_VIDEO":
            m = re.search(r"s(\d{1,2})e(\d{1,3})", os.path.basename(path).lower()); d = os.path.dirname(path)
            twins = [n_ for n_ in os.listdir(d) if n_ != os.path.basename(path) and m and re.search(rf"s0*{int(m.group(1))}e0*{int(m.group(2))}\b", n_.lower()) and os.path.splitext(n_)[1].lower() in VIDEO_EXT] if os.path.isdir(d) else []
            if not twins: resolve_finding(conn, fid); refresh_reports(conn); return 409, {"error": "refusing: no other copy of this episode exists in the folder any more - this file is probably the replacement the arr just imported. Finding cleared; run the audit again."}
        dest, moved = quarantine(conn, path, f"ui {f['check']} by {actor}", "dedupe" if f["check"] == "DUPLICATE_VIDEO" else "regrab"); plx = plex_refresh(path)
        resolve_finding(conn, fid); record_action(conn, "on", "quarantine", "disk", fid, f["label"], f"{f['check']} by {actor}", f"moved {len(moved)} -> {dest}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "dest": dest, "plex": plx}
    if action == "regrab":
        if f["app"] not in apps: return 400, {"error": f"{f['app']} not an arr"}
        if int(f["fid"]) < 0: return 400, {"error": "nothing to re-grab: this item has no file yet - use search"}
        try: info = arr_file_info(apps, f["app"], f["fid"], f["path"])
        except LookupError as e:
            resolve_finding(conn, fid); refresh_reports(conn); return 409, {"error": str(e)}
        base, key, kind = apps[f["app"]]
        if not info["path"] or not os.path.exists(info["path"]): return 404, {"error": "file not on disk (arr path)"}
        if kind == "series" and not info["ep_ids"]: return 400, {"error": "no episode ids for this file"}
        dest, moved = quarantine(conn, info["path"], f"ui regrab {f['check']} by {actor}", "regrab")
        record_regrab(conn, f["app"], kind, info["parent_id"], info["fid"], info["ep_ids"], f["label"], info["path"], dest, f["check"], actor)
        rescan = {"name": "RescanSeries", "seriesId": info["parent_id"]} if kind == "series" else {"name": "RescanMovie", "movieId": info["parent_id"]}
        search = {"name": "EpisodeSearch", "episodeIds": info["ep_ids"]} if kind == "series" else {"name": "MoviesSearch", "movieIds": [info["parent_id"]]}
        r1 = api(base, key, "command", "POST", rescan, timeout=60); r2 = api(base, key, "command", "POST", search, timeout=60); plx = plex_refresh(info["path"])
        resolve_finding(conn, fid); record_action(conn, "on", "regrab", f["app"], f["fid"], f["label"], f"{f['check']} via UI by {actor}: quarantine -> {rescan['name']} -> {search['name']}", f"moved {len(moved)} -> {dest}; cmds {(r1 or {}).get('id')},{(r2 or {}).get('id')}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "dest": dest, "plex": plx, "note": f"tracked: if nothing is imported in {cfg('REGRAB_WAIT_DAYS', REGRAB_WAIT_DAYS):g} days the original comes back automatically"}
    if action == "whisper":
        if f["app"] not in ("sonarr", "radarr"): return 400, {"error": "AI subtitles go through Bazarr, which only handles Sonarr and Radarr"}
        if not bazarr_conf()[1]: return 400, {"error": "no Bazarr API key (mount config.yaml at /arr/bazarr.yaml)"}
        try: info = arr_file_info(apps, f["app"], f["fid"], f["path"])
        except LookupError as e:
            resolve_finding(conn, fid); refresh_reports(conn); return 409, {"error": str(e)}
        job = dict(type="whisper", kind=info["kind"], app=f["app"], label=f["label"], path=info["path"], actor=actor)
        if info["kind"] == "series":
            if not info["ep_ids"]: return 400, {"error": "no episode id for this file"}
            job.update(seriesid=info["parent_id"], episodeid=info["ep_ids"][0])
        else: job.update(radarrid=info["parent_id"])
        enqueue(job); return 200, {"ok": True, "queued": True, "note": f"AI subtitles queued ({WHISPER_Q.qsize()} ahead) - progress shows in the header, the result in Actions"}
    if action == "arrrescan":
        if f["app"] != "disk" or not path: return 400, {"error": "rescan in the arr is for unknown files"}
        r = rescan_for_path(apps, path); record_action(conn, "on", "rescan", "disk", fid, f["label"], f"{f['check']} via UI by {actor}", r)
        if r == "no arr owns this path": return 409, {"error": "no arr lists a folder that holds this file any more"}
        enqueue(dict(type="followup", what="arr", finding=fid, app="disk", fid=f["fid"], path=path, label=f["label"], after="the arr rescan", actor=actor))
        return 200, {"ok": True, "queued": True, "note": f"{r} - Auditarr looks again in a minute or two: the row closes once the arr lists the file, or Actions says it did not"}
    if action == "plexrefresh":
        if not path: return 400, {"error": "no path"}
        plx = plex_refresh(path); record_action(conn, "on", "plex-refresh", "plex", fid, f["label"], f"{f['check']} via UI by {actor}", plx)
        if f["check"] == "PLEX_MISSING": enqueue(dict(type="followup", what="plex", finding=fid, app=f["app"], fid=f["fid"], path=path, label=f["label"], after="the Plex rescan", actor=actor))
        return 200, {"ok": True, "plex": plx, "queued": f["check"] == "PLEX_MISSING", "note": "Plex is rescanning that folder; Auditarr looks again in a minute or two: the row closes once Plex lists the file, or Actions says it did not" if f["check"] == "PLEX_MISSING" else "Plex is rescanning that folder"}
    if action == "subsearch":
        if f["app"] not in ("sonarr", "radarr"): return 400, {"error": "Bazarr only handles Sonarr and Radarr"}
        if not bazarr_conf()[1]: return 400, {"error": "no Bazarr API key (mount config.yaml at /arr/bazarr.yaml)"}
        try: info = arr_file_info(apps, f["app"], f["fid"], f["path"])
        except LookupError as e:
            resolve_finding(conn, fid); refresh_reports(conn); return 409, {"error": str(e)}
        enqueue(dict(type="subsearch", fid=fid, app=f["app"], kind=info["kind"], parent_id=info["parent_id"], ep_ids=info["ep_ids"], path=info["path"], label=f["label"], actor=actor))
        return 200, {"ok": True, "queued": True, "note": f"Bazarr is searching ({SUB_Q.qsize()} in queue) - the result shows up in Actions"}
    if action == "fixtag":
        lang = canon(body.get("lang") or "")
        if not re.fullmatch(r"[a-z]{3}", lang or ""): return 400, {"error": "lang must be a 3-letter ISO 639-2 code, e.g. jpn"}
        ext = os.path.splitext(path or "")[1].lower()
        if ext not in (".mkv", ".mp4", ".m4v", ".mov"): return 400, {"error": "tag fixing works on .mkv and .mp4 (AVI has no language field - use ignore)"}
        if not os.path.exists(path): return 404, {"error": "file not on disk"}
        ok, pr = ffprobe(path)
        if not ok: return 500, {"error": "ffprobe failed", "detail": pr}
        audio = [st for st in pr.get("streams", []) if st.get("codec_type") == "audio"]
        if not audio: return 400, {"error": "no audio streams"}
        # only retag tracks whose current tag is missing/und or an implausible dub language; never touch a plausible second language
        targets = [i + 1 for i, st in enumerate(audio) if canon((st.get("tags") or {}).get("language", "und") or "und") in ("und", "") or canon((st.get("tags") or {}).get("language")) not in DUB_LANGS]
        if not targets: return 400, {"error": "every audio track already has a plausible language tag; refusing to overwrite"}
        if ext == ".mkv":
            cmd = ["mkvpropedit", path]
            for t in targets: cmd += ["--edit", f"track:a{t}", "--set", f"language={lang}"]
            guard_write(f"{cmd[0]} on a media file"); p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if p.returncode not in (0, 1): return 500, {"error": "mkvpropedit failed", "detail": (p.stderr or p.stdout)[-400:]}
        else:   # MP4: lossless remux with the language metadata set, swap in, original to the trash
            tmp = path[:-len(ext)] + ".fixtag.tmp" + ext; cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy", "-movflags", "+faststart"]
            for t in targets: cmd += [f"-metadata:s:a:{t-1}", f"language={lang}"]
            guard_write(f"{cmd[0]} remux"); p = subprocess.run(cmd + [tmp], capture_output=True, text=True, timeout=3600)
            ok2, pr2 = ffprobe(tmp) if p.returncode == 0 else (False, {})
            if not ok2 or abs(float((pr2.get("format") or {}).get("duration") or 0) - float((pr.get("format") or {}).get("duration") or 0)) > 2:
                try: os.remove(tmp)
                except OSError: pass
                return 500, {"error": "mp4 remux failed, original untouched", "detail": (p.stderr or p.stdout)[-300:]}
            sig_tmp = sample_hash(tmp); dest, _ = quarantine(conn, path, f"ui fixtag mp4 remux by {actor}", "regrab", sidecars=False); os.rename(tmp, path)
            try: st0 = os.stat(dest); os.chmod(path, st0.st_mode & 0o777); os.chown(path, st0.st_uid, st0.st_gid)
            except OSError: pass
            ok3, pr3 = ffprobe(path); n_streams = len((pr3 or {}).get("streams", [])) if ok3 else 0   # S7: prove the file that now sits at the path is the one we verified
            if not ok3 or n_streams < len(pr.get("streams", [])) or sample_hash(path) != sig_tmp:
                try: restore_file(dest, path) if not os.path.exists(path) else (os.replace(dest, path))
                except Exception as e2: log("fixtag mp4: rollback failed:", repr(e2))
                record_action(conn, "on", "fixtag", f["app"], f["fid"], f["label"], f"mp4 remux by {actor}", f"REFUSED: the swapped file did not match the verified copy (streams {n_streams}, probe ok={ok3}) - original put back")
                return 500, {"error": "the rewritten file did not verify after the swap; the original was put back. Nothing changed. (This protects against a storage hiccup; try again later or report it.)"}
        conn.execute("DELETE FROM probe WHERE path=?", (path,)); resolve_finding(conn, fid)
        resc = rescan_for_path(apps, path) if f["app"] in apps else ""; plx = plex_refresh(path)
        record_action(conn, "on", "fixtag", f["app"], f["fid"], f["label"], f"set audio track(s) {targets} language={lang} via UI by {actor}", f"{'mkvpropedit' if ext == '.mkv' else 'lossless mp4 remux'} rc={p.returncode}; {resc}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "tracks": targets, "lang": lang, "rescan": f"{resc}; {plx}"}
    if action == "search":
        if f["check"] not in ("MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE") or f["app"] not in apps: return 400, {"error": "search is for missing episodes/movies"}
        base, key, kind = apps[f["app"]]; tid = -int(f["fid"])
        cmd = {"name": "EpisodeSearch", "episodeIds": [tid]} if kind == "series" else {"name": "MoviesSearch", "movieIds": [tid]}
        r = api(base, key, "command", "POST", cmd, timeout=60)
        record_action(conn, "on", "search", f["app"], f["fid"], f["label"], f"{cmd['name']} via UI by {actor}", f"command {(r or {}).get('id')} {(r or {}).get('status')}")
        return 200, {"ok": True, "note": f"{f['app']} is searching now - the finding clears on the next run once a file is imported"}
    if action == "acceptlang":
        if f["app"] not in apps: return 400, {"error": "accept-language works on arr-managed titles"}
        lang = canon(body.get("lang") or (re.search(r"audio=\['(\w{3})", f["detail"] or "") or [None, ""])[1])
        if not re.fullmatch(r"[a-z]{3}", lang or ""): return 400, {"error": "lang must be a 3-letter ISO 639-2 code, e.g. fil"}
        try: info = arr_file_info(apps, f["app"], f["fid"], f["path"])
        except LookupError as e: return 409, {"error": str(e)}
        label, show, n_ = accept_language(conn, apps, f["app"], info, lang, f["label"], actor)
        refresh_reports(conn); return 200, {"ok": True, "tag": label, "cleared": n_, "note": f"{show}: {lang} audio accepted from now on ({n_} findings cleared)"}
    if action == "verify":
        if f["app"] not in apps: return 400, {"error": "verify works on arr-managed files"}
        try: info = arr_file_info(apps, f["app"], f["fid"], f["path"])
        except LookupError as e: return 409, {"error": str(e)}
        if info["kind"] == "series" and not info["ep_ids"]: return 400, {"error": "verify needs the episode list from the arr"}
        if not info["path"] or not os.path.exists(info["path"]): return 404, {"error": "file not on disk"}
        enqueue(dict(type="verify", finding=fid, app=f["app"], fid=info["fid"], path=info["path"], parent_id=info["parent_id"], ep_ids=info["ep_ids"], label=f["label"], actor=actor))
        return 200, {"ok": True, "queued": True, "note": "verifying: 3 dialogue samples vs the episode descriptions (movies: the synopsis) - seconds with subtitles, ~1-2 min via Whisper; result appears on the row and in Actions"}
    if action == "subtag":
        if not path or not os.path.exists(path): return 404, {"error": "file not on disk"}
        changes, note = tag_subs(path)
        if not changes: return 400, {"error": note}
        conn.execute("DELETE FROM probe WHERE path=?", (path,)); resolve_finding(conn, fid)
        resc = rescan_for_path(apps, path) if f["app"] in apps else ""; plx = plex_refresh(path)
        record_action(conn, "on", "subtag", f["app"], f["fid"], f["label"], f"tag subs via UI by {actor}", f"{note}; {resc}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "changes": changes, "note": note}
    if action == "fixheader":
        if not path or not path.lower().endswith(".mkv"): return 400, {"error": "header fixing works on .mkv only"}
        if not os.path.exists(path): return 404, {"error": "file not on disk"}
        ok, pr = ffprobe(path)
        if not ok: return 500, {"error": "ffprobe failed", "detail": pr}
        hdr = float((pr.get("format") or {}).get("duration") or 0); rd = real_duration(path)
        if not rd or abs(rd - hdr) <= 60: return 400, {"error": f"header ({hdr/60:.1f} min) already matches the streams ({(rd or 0)/60:.1f} min) - nothing to fix"}
        tmp = path[:-4] + ".fixheader.tmp.mkv"; dropped = []
        try:
            # which streams still have packets past the real audio/video end? (one end-window probe covers every stream)
            ends = {}; sub_idx = {st["index"] for st in pr.get("streams", []) if st.get("codec_type") == "subtitle"}
            for i_ in sorted(sub_idx):   # full scan of each subtitle track (small): the culprit is usually one cue with a huge duration, not the last cue
                mx = 0.0
                for line in subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(i_), "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=300).stdout.splitlines():
                    try: a_, b_ = (line.split(",") + ["0"])[:2]; mx = max(mx, float(a_ or 0) + float(b_ or 0))
                    except ValueError: pass
                if mx: ends[i_] = mx
            bad_subs = sorted(i_ for i_, t_ in ends.items() if i_ in sub_idx and t_ > rd + 5)
            if bad_subs:   # mkvmerge track ids follow the same order as ffprobe stream indexes for mkv input
                lang_of = {st["index"]: (st.get("tags") or {}).get("language", "und") for st in pr.get("streams", [])}
                name_of = {st["index"]: (st.get("tags") or {}).get("title", "") for st in pr.get("streams", [])}
                codec_of = {st["index"]: st.get("codec_name") for st in pr.get("streams", [])}
                extra = []; repaired = []; really_drop = []
                for i_ in bad_subs:
                    srt = f"{tmp}.{i_}.srt"; limit = rd + 5; clamp_to = rd + 2
                    if codec_of.get(i_) in ("ass", "ssa"):
                        try:   # keep the styling: extract the native ASS, fix the Dialogue timing lines, mux back
                            assf = f"{tmp}.{i_}.ass"
                            x_ = subprocess.run(["mkvextract", "tracks", path, f"{i_}:{assf}"], capture_output=True, text=True, timeout=600)
                            if x_.returncode not in (0, 1): raise RuntimeError((x_.stderr or x_.stdout)[-200:])
                            def _at(h): hh, mm, ss = h.strip().split(":"); return int(hh) * 3600 + int(mm) * 60 + float(ss)
                            def _fmt(t_): return f"{int(t_)//3600}:{(int(t_)%3600)//60:02d}:{t_%60:05.2f}"
                            out_lines = []; cut = 0
                            for ln in open(assf, encoding="utf-8-sig", errors="replace").read().splitlines():
                                if ln.startswith(("Dialogue:", "Comment:")):
                                    parts = ln.split(",", 9)
                                    if len(parts) >= 3:
                                        try: st_, en_ = _at(parts[1]), _at(parts[2])
                                        except ValueError: out_lines.append(ln); continue
                                        if st_ > limit: cut += 1; continue
                                        if en_ > limit: parts[2] = _fmt(clamp_to); cut += 1; ln = ",".join(parts)
                                out_lines.append(ln)
                            open(assf, "w", encoding="utf-8").write("\n".join(out_lines) + "\n")
                            extra += ["--language", f"0:{lang_of.get(i_, 'und')}"] + (["--track-name", f"0:{name_of[i_]}"] if name_of.get(i_) else []) + [assf]
                            repaired.append(f"{lang_of.get(i_, '?')} ASS track ({cut} cue(s) past the end fixed)")
                        except Exception as e_: really_drop.append(i_); dropped.append(f"{lang_of.get(i_, '?')} ASS track (cues to {ends[i_]/60:.0f} min, could not repair: {e_})")
                    elif codec_of.get(i_) in ("subrip", "webvtt", "mov_text", "text"):
                        try:
                            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", f"0:{i_}", "-f", "srt", srt], capture_output=True, text=True, timeout=600)
                            blocks = open(srt, encoding="utf-8", errors="replace").read().split("\n\n"); keep = []; cut = 0
                            def _t(h): hh, mm, rest = h.split(":"); ss, ms = rest.replace(".", ",").split(","); return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000
                            for b in blocks:
                                m_ = re.search(r"(\d\d:\d\d:\d\d[,.]\d+) --> (\d\d:\d\d:\d\d[,.]\d+)", b)
                                if not m_: continue
                                if _t(m_.group(1)) > rd + 5: cut += 1; continue
                                if _t(m_.group(2)) > rd + 5:   # clamp a cue that runs past the end
                                    e_ = rd + 2; b = b.replace(m_.group(2), f"{int(e_)//3600:02d}:{(int(e_)%3600)//60:02d}:{int(e_)%60:02d},{int((e_%1)*1000):03d}"); cut += 1
                                keep.append(b.strip())
                            if not keep: raise RuntimeError("no cues left")
                            open(srt, "w", encoding="utf-8").write("\n\n".join(f"{k}\n" + (blk.split("\n", 1)[1] if blk.split("\n", 1)[0].strip().isdigit() else blk) for k, blk in enumerate(keep, 1)) + "\n")
                            extra += ["--language", f"0:{lang_of.get(i_, 'und')}"] + (["--track-name", f"0:{name_of[i_]}"] if name_of.get(i_) else []) + [srt]
                            repaired.append(f"{lang_of.get(i_, '?')} subtitle track ({cut} cue(s) past the end fixed)")
                        except Exception as e_: really_drop.append(i_); dropped.append(f"{lang_of.get(i_, '?')} subtitle track (cues up to {ends[i_]/60:.0f} min, could not repair: {e_})")
                    else: really_drop.append(i_); dropped.append(f"{lang_of.get(i_, '?')} {codec_of.get(i_)} subtitle track (cues up to {ends[i_]/60:.0f} min)")
                cmd = ["mkvmerge", "-o", tmp, "--subtitle-tracks", "!" + ",".join(str(i_) for i_ in bad_subs), path] + extra
                dropped = repaired + dropped
            else:          # audio/video themselves overrun: cut the container at the real end (+2 s)
                end = f"{int(rd + 2) // 3600:02d}:{(int(rd + 2) % 3600) // 60:02d}:{int(rd + 2) % 60:02d}"
                cmd = ["mkvmerge", "-o", tmp, "--split", f"parts:-{end}", path]
            guard_write(f"{cmd[0]} remux"); p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if p.returncode not in (0, 1): raise RuntimeError((p.stderr or p.stdout)[-400:])
            ok2, pr2 = ffprobe(tmp); hdr2 = float((pr2.get("format") or {}).get("duration") or 0) if ok2 else 0
            if not ok2 or abs(hdr2 - rd) > 60: raise RuntimeError(f"remux header is {hdr2/60:.1f} min, streams end at {rd/60:.1f} - still wrong, kept original")
            rd2 = real_duration(tmp, 0)
            if not rd2 or abs(rd2 - rd) > 5: raise RuntimeError(f"trimmed file ends at {(rd2 or 0)/60:.1f} min instead of {rd/60:.1f} - kept original")
            if len([x for x in pr2["streams"] if x.get("codec_type") in ("audio", "video")]) != len([x for x in pr["streams"] if x.get("codec_type") in ("audio", "video")]): raise RuntimeError("stream count changed")
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return 500, {"error": f"remux failed, original untouched: {e}"}
        for g_ in __import__("glob").glob(tmp + ".*.srt") + __import__("glob").glob(tmp + ".*.ass"):
            try: os.remove(g_)
            except OSError: pass
        dest, moved = quarantine(conn, path, f"ui fixheader by {actor}: header {hdr/60:.1f} min -> {rd/60:.1f} min", "regrab", sidecars=False); os.rename(tmp, path)
        try: st0 = os.stat(dest); os.chmod(path, st0.st_mode & 0o777); os.chown(path, st0.st_uid, st0.st_gid)
        except OSError: pass
        conn.execute("DELETE FROM probe WHERE path=?", (path,)); resolve_finding(conn, fid)
        resc = rescan_for_path(apps, path) if f["app"] in apps else ""; plx = plex_refresh(path)
        what = ("repaired/dropped " + ", ".join(dropped)) if dropped else "trimmed container at the real end"
        record_action(conn, "on", "fixheader", f["app"], f["fid"], f["label"], f"mkvmerge remux: header {hdr/60:.1f} -> {rd/60:.1f} min via UI by {actor} ({what})", f"original -> {dest}; {resc}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "header_min": round(hdr / 60, 1), "real_min": round(rd / 60, 1), "note": f"header rewritten ({hdr/60:.0f} -> {rd/60:.0f} min, {what}); original in the trash for {TRASH_DAYS} days"}
    if action == "remuxmkv":
        return remux_to_mkv(conn, f, apps, actor)
    if action == "langdetect":
        if not path or not os.path.exists(path): return 404, {"error": "file not on disk"}
        if not SUBGEN_URLS: return 400, {"error": "no Whisper endpoint configured (SUBGEN_URLS)"}
        enqueue(dict(type="langdetect", finding=fid, app=f["app"], fid=f["fid"], path=path, label=f["label"], actor=actor, prio=3))
        return 200, {"ok": True, "queued": True, "note": "listening to a short sample (about 20 seconds on the GPU); the audio is labelled with the language heard, and the row clears if it is English"}
    if action == "subsync":
        if f["app"] not in ARR_KINDS: return 400, {"error": "subtitle timing checks work on arr-managed files"}
        if not path or not os.path.exists(path): return 404, {"error": "file not on disk"}
        enqueue(dict(type="subsync", fid=f["fid"], app=f["app"], path=path, label=f["label"], actor=actor, prio=3))
        return 200, {"ok": True, "queued": True, "note": "checking subtitle timing: three short samples through Whisper, about a minute; the result appears on the row and in Actions"}
    if action == "subsync_fix": return subsync_fix(conn, f, apps, actor, body)
    if action == "fixsync":
        """Lossless mkvmerge remux that removes a container-level audio delay (the AUDIO_OFFSET case). Original goes to the trash."""
        if not path or not path.lower().endswith(".mkv"): return 400, {"error": "sync fixing works on .mkv only"}
        if not os.path.exists(path): return 404, {"error": "file not on disk"}
        ok, pr = ffprobe(path)
        if not ok: return 500, {"error": "ffprobe failed", "detail": pr}
        v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
        a = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"]
        if not v or not a: return 400, {"error": "need a video and an audio stream"}
        try: mk = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True, timeout=120).stdout); aids = [t["id"] for t in mk.get("tracks", []) if t.get("type") == "audio"]
        except Exception as e: return 500, {"error": f"mkvmerge -J failed: {e!r}"}
        if len(aids) != len(a): return 500, {"error": f"track count mismatch (ffprobe {len(a)} audio, mkvmerge {len(aids)})"}
        vs = float(v[0].get("start_time") or 0); shifts = {}
        for tid, s in zip(aids, a):
            ms = round((float(s.get("start_time") or 0) - vs) * 1000)
            if abs(ms) >= 500: shifts[tid] = ms
        if not shifts: return 400, {"error": "audio and video already start together (offset < 0.5 s) - nothing to fix"}
        b = intro_boundary(path, max(abs(v) for v in shifts.values()) / 1000)
        if b is not None and not body.get("force"): return 409, {"error": f"refusing: the {max(abs(v) for v in shifts.values())/1000:.2f}s delay matches a picture cut at {b:.2f}s, so it is intentional (audio starts after the intro logo). Removing it would put every direct-play device out of sync. If a player is off, fix the player (enable Direct Play / Direct Stream) instead."}
        tmp = path[:-4] + ".fixsync.tmp.mkv"; cmd = ["mkvmerge", "-o", tmp]
        for tid, ms in shifts.items(): cmd += ["--sync", f"{tid}:{-ms}"]
        cmd.append(path)
        try:
            guard_write(f"{cmd[0]} remux"); p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if p.returncode not in (0, 1): raise RuntimeError((p.stderr or p.stdout)[-400:])
            ok2, pr2 = ffprobe(tmp)
            if not ok2: raise RuntimeError(f"remuxed file unreadable: {pr2}")
            v2 = [s for s in pr2["streams"] if s.get("codec_type") == "video"]; a2 = [s for s in pr2["streams"] if s.get("codec_type") == "audio"]
            vs2 = float(v2[0].get("start_time") or 0); left = max(abs(float(s.get("start_time") or 0) - vs2) for s in a2)
            if left > 0.5 or len(a2) != len(a): raise RuntimeError(f"remux left an offset of {left:.2f}s / track count changed")
            d_old = float((pr.get("format") or {}).get("duration") or 0); d_new = float((pr2.get("format") or {}).get("duration") or 0)
            if d_old and abs(d_new - d_old) > 10: raise RuntimeError(f"duration changed {d_old:.0f}s -> {d_new:.0f}s")
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return 500, {"error": f"remux failed, original untouched: {e}"}
        dest, moved = quarantine(conn, path, f"ui fixsync {f['check']} by {actor}: audio shifted {shifts} ms", "regrab", sidecars=False)
        os.rename(tmp, path)
        try: st0 = os.stat(dest); os.chmod(path, st0.st_mode & 0o777); os.chown(path, st0.st_uid, st0.st_gid)   # look exactly like the original to Plex/arr
        except OSError: pass
        conn.execute("DELETE FROM probe WHERE path=?", (path,)); resolve_finding(conn, fid)
        resc = rescan_for_path(apps, path) if f["app"] in apps else ""; plx = plex_refresh(path)
        record_action(conn, "on", "fixsync", f["app"], f["fid"], f["label"], f"mkvmerge remux, audio shifted by {({k: -v for k, v in shifts.items()})} ms via UI by {actor}", f"original -> {dest}; {resc}; {plx}")
        refresh_reports(conn); return 200, {"ok": True, "shifted_ms": {str(k): -v for k, v in shifts.items()}, "dest": dest, "note": f"original kept in the trash for {TRASH_DAYS} days - play it now; if it got worse, restore it from the Trash tab"}
    return 400, {"error": f"unknown action {action}"}
# ---------- Bazarr subtitle search (sequential background queue so a bulk click does not hammer Bazarr) ----------
import queue
SUB_Q = queue.PriorityQueue(); VERIFY_Q = queue.PriorityQueue(); WHISPER_Q = queue.PriorityQueue(); FOLLOW_Q = queue.PriorityQueue()
def _lane(job): return {"verify": VERIFY_Q, "whisper": WHISPER_Q, "subsync": VERIFY_Q, "langdetect": VERIFY_Q, "followup": FOLLOW_Q}.get(job.get("type"), SUB_Q)   # follow-ups wait on their own lane, so their pauses hold nothing else up   # three lanes: Bazarr searches never wait behind Whisper
import itertools; _SEQ = itertools.count()
def _put(job): _lane(job).put((int(job.get("prio", 5)), next(_SEQ), job))   # lower runs first: 1 = someone is watching this (F31), 5 = everything else; ties keep arrival order
def _subsearch(job):
    base, key = bazarr_conf(); lang2 = TWO_LETTER.get(sorted(PREF_SUBS)[0], sorted(PREF_SUBS)[0][:2]); results = []
    from urllib.parse import urlencode
    targets = [("episodes", {"seriesid": job["parent_id"], "episodeid": e}) for e in job["ep_ids"]] if job["kind"] == "series" else [("movies", {"radarrid": job["parent_id"]})]
    for ep, ids in targets:
        q = urlencode({**ids, "language": lang2, "forced": "false", "hi": "false"})
        guard_write(f"PATCH bazarr {ep}/subtitles"); req = urllib.request.Request(f"{base}/api/{ep}/subtitles?{q}", method="PATCH", headers={"X-API-KEY": key})
        try:
            with urllib.request.urlopen(req, timeout=300) as r: results.append(f"HTTP {r.status}")
        except urllib.error.HTTPError as e: results.append(f"HTTP {e.code} {e.read()[:80].decode('utf-8','replace')}".strip())
        except Exception as e: results.append(err_result(e))
    conn = db()
    try:
        got = external_subs(job["path"]) if job.get("path") else set(); ok = bool(got & PREF_SUBS) or "und" in got
        plx = ""
        if ok: resolve_finding(conn, job["fid"]); plx = plex_refresh(job["path"])
        record_action(conn, "on", "subsearch", job["app"], job["fid"], job["label"], f"Bazarr search ({lang2}) by {job['actor']}",
                      (f"subtitle found: {','.join(sorted(got))}; {plx}" if ok else "no subtitle found yet (Bazarr keeps searching on its schedule)") + " | " + "; ".join(results)[:160])
        refresh_reports(conn)
    finally: conn.close()
def bazarr_call(path, data=None, method=None, timeout=900):
    """Bazarr REST helper (form-encoded like its UI). Returns parsed JSON or None."""
    from urllib.parse import urlencode
    base, key = bazarr_conf()
    body = urlencode(data, doseq=True).encode() if data is not None else None
    if body is not None or (method or "GET").upper() not in ("GET", "HEAD"): guard_write(f"{method or 'POST'} bazarr {path.split('?')[0]}")
    req = urllib.request.Request(f"{base}/api/{path}", data=body, method=method or ("POST" if body is not None else "GET"), headers={"X-API-KEY": key, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(); return json.loads(raw) if raw.strip() else None
def _whisper_job(job):
    """Ask Bazarr for the whisperai candidate of one episode/movie and download it (this is the slow part: Whisper runs on the whole episode)."""
    kind = job["kind"]; t0 = time.time()
    hist_path = f"episodes/history?episodeid={job.get('episodeid')}&start=0&length=20" if kind == "series" else f"movies/history?radarrid={job.get('radarrid')}&start=0&length=20"
    def whisper_done_since(ts):
        try: rows = (bazarr_call(hist_path, timeout=60) or {}).get("data", [])
        except Exception: return False
        def _ts(r_):   # Bazarr's 'timestamp' is prose ("7 minutes ago"); 'parsed_timestamp' is '%m/%d/%y %H:%M:%S' local time
            for v, fmt in ((r_.get("parsed_timestamp"), "%m/%d/%y %H:%M:%S"), (r_.get("timestamp"), None)):
                if v is None: continue
                try: return datetime.datetime.strptime(str(v), fmt).timestamp() if fmt else float(v)
                except (TypeError, ValueError): continue
            return 0
        return any(r.get("provider") == "whisperai" and _ts(r) >= ts - 5 for r in rows)
    def whisper_rows():
        """Identity of every whisperai history row right now: Bazarr may live in another time zone, so 'landed' means a row that was not there before, not a clock comparison."""
        try: rows = (bazarr_call(hist_path, timeout=60) or {}).get("data", [])
        except Exception: return None
        return {(r.get("id"), r.get("parsed_timestamp") or r.get("timestamp"), r.get("subtitles_path")) for r in rows if r.get("provider") == "whisperai"}
    try:
        if whisper_done_since(time.time() - 86400): result = "skipped - a Whisper subtitle was already made for this in the last 24 h"
        else:
            rows = bazarr_call(f"providers/episodes?episodeid={job['episodeid']}" if kind == "series" else f"providers/movies?radarrid={job['radarrid']}", timeout=900)
            rows = rows if isinstance(rows, list) else (rows or {}).get("data", [])
            w = next((r for r in rows if r.get("provider") == "whisperai"), None)
            if not w: result = "Bazarr offered no whisperai candidate (is the provider enabled / subgen up?)"
            else:
                form = {"hi": "false", "forced": "false", "original_format": "false", "provider": "whisperai", "subtitle": w["subtitle"]}
                before = whisper_rows() or set()
                if kind == "series": form.update(seriesid=job["seriesid"], episodeid=job["episodeid"]); bazarr_call("providers/episodes", form, "POST", timeout=600)
                else: form.update(radarrid=job["radarrid"]); bazarr_call("providers/movies", form, "POST", timeout=600)
                # Bazarr answers immediately and transcribes in the background: wait for the history entry so jobs run one at a time
                deadline = time.time() + 50 * 60; ok = False
                while time.time() < deadline:
                    time.sleep(10); now_rows = whisper_rows()
                    if (now_rows is not None and now_rows - before) or whisper_done_since(t0): ok = True; break
                result = (f"whisper subtitle landed after {(time.time()-t0)/60:.1f} min" if ok else "submitted to Bazarr, still transcribing after 50 min (check Bazarr history later)") + f" ({w.get('release_info', [''])[0] if w.get('release_info') else 'translate/transcribe'})"
    except Exception as e: result = err_result(e)
    conn = db()
    try:
        record_action(conn, "on", "whisper", job["app"], job.get("episodeid") or job.get("radarrid"), job["label"], f"Whisper subtitles via Bazarr by {job['actor']}", result[:200])
        if job.get("path"):
            if result.startswith("whisper subtitle") or result.startswith("skipped - a Whisper subtitle was already made"):
                for (fid_,) in conn.execute("SELECT id FROM finding WHERE resolved_at IS NULL AND path=? AND check_code IN ('NO_SUBS','UND_AUDIO_NO_SUBS')", (job["path"],)).fetchall(): resolve_finding(conn, fid_, "AI subtitles made")
                recheck_timing(conn, job["path"], "re-check after AI subtitles")
                conn.execute("DELETE FROM probe WHERE path=?", (job["path"],)); conn.commit(); refresh_reports(conn)
            elif not result.startswith("submitted to Bazarr"): clear_working(conn, path=job["path"]); refresh_reports(conn)
            plex_refresh(job["path"])
    finally: conn.close()
def recheck_timing(conn, path, why):
    """2026-11-12: AI subtitles landed for a file whose subtitles were out of sync, and the row stayed open with the old verdict. A new external
    subtitle is what the timing check judges first, so check again: the row clears if the new file fits."""
    rows = conn.execute("SELECT app, fid, label FROM finding WHERE resolved_at IS NULL AND path=? AND check_code='SUBS_OUT_OF_SYNC'", (path,)).fetchall()
    for app_, afid_, label_ in rows: enqueue(dict(type="subsync", fid=afid_, app=app_, path=path, label=label_, actor=why, prio=3))
    return len(rows)
def ui_whisper(conn, body, actor):
    """{show, season?} -> queue a Whisper subtitle for every episode of that show (or season); {movie} for a Radarr title."""
    apps = arr_apps(); q = (body.get("show") or body.get("movie") or "").strip().lower(); season = body.get("season")
    if not q: return 400, {"error": "need show or movie"}
    if not bazarr_conf()[1]: return 400, {"error": "no Bazarr API key"}
    jobs = []
    if body.get("movie"):
        base, key, _ = apps["radarr"]; m = next((x for x in api(base, key, "movie") if x["title"].lower().startswith(q)), None)
        if not m: return 404, {"error": f"no Radarr movie starting with {q!r}"}
        jobs.append(dict(type="whisper", kind="movie", app="radarr", radarrid=m["id"], label=f"{m['title']} ({m.get('year')})", path=(m.get("movieFile") or {}).get("path"), actor=actor))
    else:
        base, key, _ = apps["sonarr"]; srs = next((x for x in api(base, key, "series") if x["title"].lower().startswith(q)), None)
        if not srs: return 404, {"error": f"no Sonarr series starting with {q!r}"}
        eps = bazarr_call(f"episodes?seriesid[]={srs['id']}").get("data", [])
        for e in sorted(eps, key=lambda e: (e["season"], e["episode"])):
            if season not in (None, "", 0) and int(e["season"]) != int(season): continue
            if not e.get("path"): continue
            jobs.append(dict(type="whisper", kind="series", app="sonarr", seriesid=srs["id"], episodeid=e["sonarrEpisodeId"], label=f"{srs['title']} S{e['season']:02d}E{e['episode']:02d}", path=None, actor=actor))
    if not jobs: return 404, {"error": "nothing matched (no files in Bazarr for that selection)"}
    ahead = SUB_Q.qsize()
    for j in jobs: enqueue(j)
    record_action(conn, "on", "whisper", jobs[0]["app"], len(jobs), jobs[0]["label"].rsplit(" S", 1)[0] if jobs[0]["kind"] == "series" else jobs[0]["label"], f"queued by {actor}", f"{len(jobs)} episode(s)/movie(s) queued; ~12 min each on CPU"); refresh_reports(conn)
    return 200, {"ok": True, "queued": len(jobs), "note": f"{len(jobs)} queued" + (f" behind {ahead} other job(s)" if ahead else "") + "; results appear in Actions as 'whisper'"}
def enqueue(job):
    if job.get("type") == "whisper" and job.get("path"):
        try:
            c_ = db()
            if mark_working(c_, "AI subtitles queued", path=job["path"]): refresh_reports(c_)
            c_.close()
        except Exception as e: log("mark working:", repr(e))
    """Persist then queue. Jobs survive restarts (re-queued by requeue_jobs at startup)."""
    conn = db()
    try: cur = conn.execute("INSERT INTO jobs(ts, job, status) VALUES(?,?,'queued')", (time.time(), json.dumps(job))); conn.commit(); job["_id"] = cur.lastrowid
    finally: conn.close()
    _put(job)
def requeue_jobs():
    if E("JOBS", "on") == "off": log("JOBS=off: queued subtitle/verify jobs are left for production (staging never talks to Bazarr/Whisper)"); return
    conn = db()
    try: rows = conn.execute("SELECT id, job FROM jobs WHERE status IN ('queued','running') ORDER BY id").fetchall()
    finally: conn.close()
    for jid, raw in rows:
        try: j = json.loads(raw); j["_id"] = jid; _put(j)
        except Exception: pass
    if rows: log(f"re-queued {len(rows)} subtitle job(s) from before the restart")
# ---------- verify content: does the dialogue match the episode? ----------
STOP = {"the", "and", "that", "this", "with", "have", "from", "they", "their", "them", "will", "what", "when", "where", "which", "while", "about", "after", "before", "into", "than", "then",
        "there", "here", "been", "being", "were", "was", "are", "his", "her", "she", "him", "you", "your", "but", "not", "for", "who", "how", "all", "one", "two", "out", "has", "had", "can",
        "episode", "part", "season", "series", "anime", "show", "story", "it's", "don't", "i'm", "we're", "that's", "just", "like", "know", "want", "going", "really", "very", "also", "only", "more", "some", "any", "get", "got", "let", "now", "yes", "okay", "right", "well", "come", "back", "take", "make", "made", "think", "thing", "things", "something", "someone", "because", "could", "would", "should", "even", "still", "much", "many", "most", "other", "over", "under", "again", "away", "down", "off", "own", "too", "why", "did", "does", "doing", "done", "say", "said", "tell", "told", "see", "saw", "look", "looks", "looking", "way", "time", "day", "night", "man", "girl", "boy", "people", "life", "world", "new", "old", "good", "bad", "big", "little", "long", "first", "last", "next", "every", "each"}
def _toks(text): return [t for t in re.findall(r"[a-z][a-z'\-]{2,}", (text or "").lower()) if t not in STOP]
def _names(text): return {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text or "")} - STOP
def _dialogue_sample(path, start, dur, sub_stream=None, sidecar=None, lang2="en", translate=False, audio_stream=None):
    """Text spoken between start and start+dur: embedded English subs, else sidecar srt, else Whisper via subgen."""
    if sub_stream is not None:
        o = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(dur), "-i", path, "-map", f"0:{sub_stream}", "-f", "srt", "-"], capture_output=True, text=True, timeout=180).stdout
        txt = " ".join(re.sub(r"<[^>]*>|\{[^}]*\}|\\N", " ", "\n".join(b.split("\n")[2:])) for b in o.split("\n\n") if "-->" in b)
        if len(txt) > 80: return txt, "subs"
    if sidecar and os.path.exists(sidecar):
        out = []
        for b in open(sidecar, errors="replace").read().split("\n\n"):
            m = re.search(r"(\d\d):(\d\d):(\d\d)[,.]\d+ -->", b)
            if m and start <= int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) <= start + dur: out.append(" ".join(b.split("\n")[2:]))
        txt = re.sub(r"<[^>]*>", " ", " ".join(out))
        if len(txt) > 80: return txt, "sidecar"
    wav = os.path.join(CONFIG_DIR, f"verify-{os.getpid()}-{int(start)}.wav")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", path] + (["-map", f"0:{audio_stream}"] if audio_stream is not None else []) + ["-vn", "-ac", "1", "-ar", "16000", wav], capture_output=True, timeout=300)
        import uuid; bnd = uuid.uuid4().hex; body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + open(wav, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
        q = f"task={'translate' if translate else 'transcribe'}&output=txt&encode=false" + (f"&language={lang2}" if lang2 else "")
        last = None
        for base_url in SUBGEN_URLS:
            try:
                req = urllib.request.Request(f"{base_url.rstrip('/')}/asr?{q}", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
                with urllib.request.urlopen(req, timeout=900) as r: txt = r.read().decode("utf-8", "replace")
                return txt, "whisper"
            except Exception as e: last = e; log("verify whisper:", base_url, repr(e))
        raise last or RuntimeError("no whisper endpoint")
    finally:
        try: os.remove(wav)
        except OSError: pass
def _shingles(text, k=5):
    w = re.findall(r"[a-z']+", (text or "").lower()); return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}
def _file_sub_text(path):
    """Full English (or untagged) text subtitle of a file as plain text, or '' if none. Picks the dialogue-densest track."""
    ok, pr = ffprobe(path)
    if not ok: return ""
    subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle" and st.get("codec_name") in TEXT_SUB_CODECS and canon((st.get("tags") or {}).get("language", "und") or "und") in ("eng", "und") and not is_signs(st)]
    best = ""
    for st in subs:
        o = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{st['index']}", "-f", "srt", "-"], capture_output=True, text=True, timeout=300).stdout
        if o.count("-->") > best.count("-->"): best = o
    if best:
        o = best
        return " ".join(re.sub(r"<[^>]*>|\{[^}]*\}|\\N", " ", "\n".join(b.split("\n")[2:])) for b in o.split("\n\n") if "-->" in b)
    stem = os.path.splitext(path)[0]; sc = next((c for c in (stem + ".en.srt", stem + ".eng.srt", stem + ".srt") if os.path.exists(c)), None)
    if sc: return re.sub(r"<[^>]*>", " ", " ".join(" ".join(b.split("\n")[2:]) for b in open(sc, errors="replace").read().split("\n\n") if "-->" in b))
    return ""
def verify_movie(app, fid, path, parent_id, apps):
    """S1: a movie's dialogue against its own synopsis and two dozen decoy synopses from the same library. No neighbours to cross-check, so the verdicts are 'matches', 'mislabeled' or 'inconclusive'."""
    base, key, _ = apps[app]
    try: me = api(base, key, f"movie/{parent_id}"); allm = api(base, key, "movie") or []
    except Exception as e: return "could not read the movie from the arr", repr(e)
    if not (me.get("overview") or "").strip(): return "no synopsis in the arr to compare against", ""
    import random; rnd = random.Random(int(parent_id)); others = [m for m in allm if m.get("id") != me.get("id") and (m.get("overview") or "").strip()]; rnd.shuffle(others)
    cands = [me] + others[:24]; import math
    docs = {m["id"]: (set(_toks(m.get("title", "") + " " + m.get("overview", ""))), _names(m.get("title", "") + " " + m.get("overview", ""))) for m in cands}
    N = len(docs); df = {}
    for toks, names in docs.values():
        for t in toks | names: df[t] = df.get(t, 0) + 1
    idf = {t: math.log((N + 1) / (c + 0.5)) for t, c in df.items()}
    def score(st_, sn, d): hits = (st_ & d[0]) | (sn & d[1]); return sum(idf.get(t, 0) * (2 if t in d[1] else 1) for t in hits) / (len(d[0] | d[1]) ** 0.5 or 1)
    ok, pr = ffprobe(path)
    if not ok: return "ffprobe failed", str(pr)
    dur = float((pr.get("format") or {}).get("duration") or 0); rd = real_duration(path)
    if rd and abs(rd - dur) > 60: dur = rd
    subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle" and st.get("codec_name") in TEXT_SUB_CODECS and canon((st.get("tags") or {}).get("language", "und") or "und") in ("eng", "und", "")]
    sub_stream = subs[0]["index"] if subs else None; stem = os.path.splitext(path)[0]; sidecar = next((c for c in (stem + ".en.srt", stem + ".eng.srt", stem + ".srt") if os.path.exists(c)), None)
    a = [st for st in pr.get("streams", []) if st.get("codec_type") == "audio"]
    a_pick = next((st for st in a if canon((st.get("tags") or {}).get("language", "und") or "und") == "eng" and not re.search(r"descri|commentary", (st.get("tags") or {}).get("title") or "", re.I)), a[0] if a else None)
    alang = canon((a_pick.get("tags") or {}).get("language", "und") or "und") if a_pick else "und"; audio_stream = a_pick["index"] if a_pick else None
    lang2 = TWO_LETTER.get(alang) if alang not in ("und", "") else None; translate = alang not in ("eng", "und", "")
    lines = []; picks = []
    for frac in (0.15, 0.5, 0.85):
        start = max(0, dur * frac - 45)
        try: txt, how = _dialogue_sample(path, start, 90, sub_stream, sidecar, lang2, translate, audio_stream)
        except Exception as e: lines.append(f"{int(frac*100)}%: sample failed {e!r}"); continue
        if how == "whisper": how = f"whisper on {alang} audio" + (" translated" if translate else "")
        st_, sn = set(_toks(txt)), _names(txt)
        if len(st_) < 15: lines.append(f"{int(frac*100)}%: too little dialogue ({how})"); continue
        scored = sorted([(score(st_, sn, d), mid) for mid, d in docs.items()], key=lambda x: -x[0]); best, second = scored[0], (scored[1] if len(scored) > 1 else (0, None))
        bm = next(m for m in cands if m["id"] == best[1]); conf = best[0] / (second[0] or 0.01); picks.append((bm["id"], conf))
        lines.append(f"{int(frac*100)}% ({how}): best '{bm.get('title','')[:30]}' score {best[0]:.2f}, next {second[0]:.2f}")
    if not picks: return "inconclusive - no usable dialogue", " | ".join(lines)
    strong = [p_ for p_ in picks if p_[1] >= 2.0]; own = me["id"]
    if strong and all(p_[0] == own for p_ in strong): return f"CONTENT MATCHES {me.get('title')} (clear at {len(strong)}/3 points)", " | ".join(lines)
    wrong = [p_ for p_ in strong if p_[0] != own]
    if len(wrong) >= 2 and len({p_[0] for p_ in wrong}) == 1:
        t_ = next(m.get("title") for m in cands if m["id"] == wrong[0][0]); return f"MISLABELED: the dialogue matches '{t_}', not '{me.get('title')}'", " | ".join(lines)
    lean = max({p_[0] for p_ in picks}, key=lambda i_: sum(1 for p_ in picks if p_[0] == i_))
    if lean == own: return f"CONSISTENT with {me.get('title')}: every sample leans to this movie, by small margins", " | ".join(lines)
    return f"inconclusive - dialogue does not clearly match the synopsis (leaning '{next(m.get('title') for m in cands if m['id'] == lean)}')", " | ".join(lines)
def verify_content(app, fid, path, parent_id, ep_ids, apps):
    """Returns (conclusion, details) - which episode(s) the dialogue in the file matches."""
    base, key, kind = apps[app]
    if kind != "series": return verify_movie(app, fid, path, parent_id, apps)
    eps = sorted(api(base, key, f"episode?seriesId={parent_id}"), key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)))
    mapped = [e for e in eps if e["id"] in set(ep_ids)]
    if not mapped: return "episode not found in the arr", ""
    idx = [i for i, e in enumerate(eps) if e["id"] in set(ep_ids)]; lo, hi = max(0, min(idx) - 4), min(len(eps), max(idx) + 5)
    cands = [e for e in eps[lo:hi] if (e.get("overview") or "").strip()]
    if len(cands) < 2: return "not enough episode descriptions on TVDB to compare against", ""
    docs = {e["id"]: (set(_toks(e.get("title", "") + " " + e.get("overview", ""))), _names(e.get("title", "") + " " + e.get("overview", ""))) for e in cands}
    import math; N = len(docs); df = {}
    for toks, names in docs.values():
        for t in toks | names: df[t] = df.get(t, 0) + 1
    idf = {t: math.log((N + 1) / (c + 0.5)) for t, c in df.items()}   # a name in every overview of the arc is worth ~0; a unique word is worth a lot
    def score(sample_toks, sample_names, d):
        hits = (sample_toks & d[0]) | (sample_names & d[1]); return sum(idf.get(t, 0) * (2 if t in d[1] else 1) for t in hits) / (len(d[0] | d[1]) ** 0.5 or 1)
    ok, pr = ffprobe(path)
    if not ok: return "ffprobe failed", str(pr)
    dur = float((pr.get("format") or {}).get("duration") or 0); rd = real_duration(path)
    if rd and abs(rd - dur) > 60: dur = rd
    subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle" and st.get("codec_name") in TEXT_SUB_CODECS and canon((st.get("tags") or {}).get("language", "und") or "und") in ("eng", "und") and not is_signs(st)]
    def _density(st):   # cues in the first 6 minutes: a signs/karaoke track has a handful, dialogue has hundreds
        try: o_ = subprocess.run(["ffmpeg", "-v", "error", "-t", "360", "-i", path, "-map", f"0:{st['index']}", "-f", "srt", "-"], capture_output=True, text=True, timeout=120).stdout; return o_.count("-->")
        except Exception: return 0
    if len(subs) > 1: subs.sort(key=lambda st: -_density(st))
    sub_stream = subs[0]["index"] if subs else None
    stem = os.path.splitext(path)[0]; sidecar = next((c for c in (stem + ".en.srt", stem + ".eng.srt", stem + ".srt") if os.path.exists(c)), None)
    a = [st for st in pr.get("streams", []) if st.get("codec_type") == "audio"]
    a_pick = next((st for st in a if canon((st.get("tags") or {}).get("language", "und") or "und") == "eng" and not re.search(r"descri|commentary", (st.get("tags") or {}).get("title") or "", re.I)), a[0] if a else None)   # English original over a dubbed default track
    alang = canon((a_pick.get("tags") or {}).get("language", "und") or "und") if a_pick else "und"; audio_stream = a_pick["index"] if a_pick else None
    lang2 = TWO_LETTER.get(alang, None) if alang not in ("und", "") else None; translate = alang not in ("eng", "und", "")
    # stage 1: the neighbouring episodes' own files (if they have text subs) are a far better reference than descriptions
    files_by_ep = {}
    try:
        for ef in api(base, key, f"episodefile?seriesId={parent_id}"):
            if ef.get("id") != fid and ef.get("path") and os.path.exists(ef["path"]): files_by_ep[ef["id"]] = ef["path"]
        ep_file = {e["id"]: files_by_ep.get(e.get("episodeFileId")) for e in cands}
    except Exception: ep_file = {}
    ref = {}
    for e in cands:
        fp = ep_file.get(e["id"])
        if fp:
            try:
                sh = _shingles(_file_sub_text(fp))
                if len(sh) > 200: ref[e["id"]] = sh
            except Exception as ex: log("verify xcheck:", repr(ex))
    if len(ref) >= 2:   # drop boilerplate shared by two or more neighbours (opening/ending lyrics, recaps, next-episode previews)
        df_ = {}
        for r_ in ref.values():
            for sh_ in r_: df_[sh_] = df_.get(sh_, 0) + 1
        boiler = {sh_ for sh_, c_ in df_.items() if c_ >= 2}; ref = {eid: r_ - boiler for eid, r_ in ref.items()}
    else: boiler = set()
    xhits = []   # (frac, label, overlap)
    lines = []; picks = []
    for frac in (0.15, 0.5, 0.85):
        start = max(0, dur * frac - 45)
        try: txt, how = _dialogue_sample(path, start, 90, sub_stream, sidecar, lang2, translate, audio_stream)
        except Exception as e: lines.append(f"{int(frac*100)}%: sample failed {e!r}"); continue
        if how == "whisper": how = f"whisper on {alang} audio" + (" translated" if translate else "")
        if ref:
            ssh_ = _shingles(txt) - boiler
            if len(ssh_) >= 20:
                ov = sorted(((len(ssh_ & r_) / len(ssh_), eid) for eid, r_ in ref.items()), reverse=True)
                if ov and ov[0][0] >= 0.25:
                    be_ = next(e for e in cands if e["id"] == ov[0][1]); xl = f"S{be_['seasonNumber']:02d}E{be_['episodeNumber']:02d}"
                    xhits.append((frac, xl, ov[0][0])); lines.append(f"{int(frac*100)}% ({how}): {ov[0][0]:.0%} of this dialogue is word-for-word in the file labelled {xl}")
        st_, sn = set(_toks(txt)), _names(txt)
        if len(st_) < 15: lines.append(f"{int(frac*100)}%: too little dialogue ({how})"); continue
        scored = sorted([(score(st_, sn, d), e) for e, d in docs.items()], key=lambda x: -x[0])
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0, None))
        be = next(e for e in cands if e["id"] == best[1]); lab = f"S{be['seasonNumber']:02d}E{be['episodeNumber']:02d}"
        conf = best[0] / (second[0] or 0.01)
        picks.append((lab, conf, be["id"])); lines.append(f"{int(frac*100)}% ({how}): best {lab} '{be.get('title','')[:28]}' score {best[0]:.2f}, next {second[0]:.2f}")
    mapped_labs = {f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}" for e in mapped}
    if xhits:   # word-for-word matches with other files decide outright
        xl = [h[1] for h in xhits]
        if len(set(xl)) >= 2: return f"MULTI-EPISODE FILE: the dialogue is word-for-word the content of the files labelled {', '.join(dict.fromkeys(xl))} (arr says {', '.join(sorted(mapped_labs))})", " | ".join(lines) + f" | compared against {len(ref)} neighbouring files' subtitles"
        if len(xhits) >= 2 or xhits[0][2] >= 0.5: return f"DUPLICATE CONTENT: this file carries the same dialogue as the file labelled {xl[0]} ({max(h[2] for h in xhits):.0%} word-for-word) - one of the two is mislabelled; arr says this one is {', '.join(sorted(mapped_labs))}", " | ".join(lines) + f" | compared against {len(ref)} neighbouring files' subtitles"
    if not picks: return "inconclusive - no usable dialogue", " | ".join(lines)
    labs = [p_[0] for p_ in picks]; strong = [p_ for p_ in picks if p_[1] >= 2.0]
    slabs = [p_[0] for p_ in strong]
    if len(set(slabs)) >= 2: concl = f"MULTI-EPISODE FILE: dialogue clearly matches {', '.join(dict.fromkeys(slabs))} at different points - the arr thinks it is {', '.join(sorted(mapped_labs))}"
    elif strong and all(l in mapped_labs for l in slabs): concl = f"CONTENT MATCHES {', '.join(sorted(mapped_labs))} (clear at {len(strong)}/3 points)"
    elif len(strong) >= 2 and not any(l in mapped_labs for l in slabs): concl = f"MISLABELED: dialogue clearly matches {slabs[0]} but the arr has it as {', '.join(sorted(mapped_labs))}"
    elif strong: concl = f"probably {slabs[0]} (1 clear point) vs arr {', '.join(sorted(mapped_labs))} - adjacent episodes of one arc share names, treat as a hint"
    else:
        lean = max(set(labs), key=labs.count)
        uniq = f"; no word-for-word overlap with the {len(ref)} neighbouring episode files (only meaningful if they share the same subtitle translation)" if ref else ""
        if lean in mapped_labs and all(l in mapped_labs for l in labs): concl = f"CONSISTENT with {lean}: every sample leans to the episode the arr says, just by small margins (neighbouring episode descriptions are very similar){uniq}"
        elif ref and not xhits: concl = f"NOT A COPY of a neighbour: no word-for-word overlap with the {len(ref)} neighbouring episode files (caveat: a different subtitle translation would not match either); descriptions lean {lean} by small margins, arr says {', '.join(sorted(mapped_labs))} - the extra length is still unexplained, spot-check where the file restarts"
        elif lean in mapped_labs: concl = f"mostly consistent with {lean} ({labs.count(lean)}/{len(labs)} samples lean that way, the other(s) elsewhere) - weak evidence either way"
        else: concl = f"inconclusive - dialogue does not clearly match any single description (leaning {lean}, arr says {', '.join(sorted(mapped_labs))})"
    return concl, " | ".join(lines)
# ---------- F20: subtitle timing (are the subtitles in step with the speech?) ----------
SUBSYNC_MIN_S = 1.0   # smaller offsets are not worth touching
def sync_windows(dur):
    """Three sample windows (start, length) at 15 / 50 / 85 % of the file: 60 s each on a normal file, shorter on tiny ones."""
    wlen = 60.0 if dur >= 600 else max(8.0, dur / 4)
    return [(max(0.0, dur * f - wlen / 2), wlen) for f in (0.15, 0.5, 0.85)]
def pick_windows(dur, cue_starts, wlen=60.0, step=15.0):
    """Three windows, one per third of the file, each where the subtitles are densest: speech-rich samples judge timing best (openings, endings and
    fight scenes have little to compare). Short clips and files without cue information keep the fixed windows."""
    if dur < 600 or not cue_starts: return sync_windows(dur)
    out = []
    for lo, hi in ((0.05 * dur, 0.35 * dur), (0.35 * dur, 0.65 * dur), (0.65 * dur, 0.95 * dur)):
        best = None; t = lo
        while t + wlen <= hi + 1e-6:
            n = sum(1 for c in cue_starts if t <= c <= t + wlen)
            if best is None or n > best[0]: best = (n, t)
            t += step
        if best and best[0] >= 4: out.append((round(best[1], 1), wlen))
    return out if len(out) >= 2 else sync_windows(dur)
_TS = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")
def _ts(t):
    m = _TS.match(t); h, mi, se, ms = m.groups(); return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000
def parse_sub_cues(text):
    """Cue (start, end) seconds from SRT, VTT or ASS text."""
    out = [(_ts(m.group(1)), _ts(m.group(2))) for m in re.finditer(r"(\d+:\d\d:\d\d[,.]\d{1,3})\s*-->\s*(\d+:\d\d:\d\d[,.]\d{1,3})", text)]
    if not out: out = [(_ts(m.group(1)), _ts(m.group(2))) for m in re.finditer(r"^Dialogue:\s*\d+,(\d+:\d\d:\d\d[.,]\d{1,3}),(\d+:\d\d:\d\d[.,]\d{1,3})", text, re.M)]
    return out
def estimate_offset(sub_starts, asr_starts, max_off=30.0):
    """One window: how late the subtitles run against the speech. -> (offset s, share of speech segments explained, matched count). Positive = subtitles late."""
    if not sub_starts or not asr_starts: return None, 0.0, 0
    cands = [s_ - a_ for a_ in asr_starts for s_ in sub_starts if abs(s_ - a_) <= max_off]
    if not cands: return None, 0.0, 0
    bins = {}
    for d_ in cands: bins[round(d_ / 0.2)] = bins.get(round(d_ / 0.2), 0) + 1
    centre = max(bins, key=bins.get) * 0.2
    near = sorted(d_ for d_ in cands if abs(d_ - centre) <= 0.45); off = near[len(near) // 2]
    matched = sum(1 for a_ in asr_starts if any(abs((s_ - a_) - off) <= 0.45 for s_ in sub_starts))
    return off, matched / len(asr_starts), matched
def decide_sync(windows):
    """windows: [(position 0..1, offset, support, matched)] -> (status, offset, note); status in: in sync | late | early | drift | unknown."""
    good = [w for w in windows if w[1] is not None and w[2] >= 0.5 and w[3] >= 3]
    if len(good) < 2: return "unknown", None, f"too little speech or subtitle text to judge ({len(good)}/{len(windows)} usable samples)"
    offs = [w[1] for w in good]; spread = max(offs) - min(offs); med = sorted(offs)[len(offs) // 2]
    if spread > 0.7: return "drift", med, f"drift: {good[0][1]:+.1f}s at {good[0][0]:.0%} -> {good[-1][1]:+.1f}s at {good[-1][0]:.0%} ({len(good)}/{len(windows)} samples)"
    if abs(med) < SUBSYNC_MIN_S: return "in sync", med, f"within {abs(med):.1f}s ({len(good)}/{len(windows)} samples agree)"
    return ("late" if med > 0 else "early"), med, f"{abs(med):.1f}s {'late' if med > 0 else 'early'} ({len(good)}/{len(windows)} samples agree)"
def asr_segments_from(raw):
    """Speech segments (start, end) from whatever the Whisper endpoint answered: whisper-asr-webservice json, subgen's srt/vtt text, or json lines."""
    txt = (raw or "").strip()
    if not txt: return []
    try:
        j = json.loads(txt)
        if isinstance(j, dict) and "segments" in j: return [(float(x.get("start") or 0), float(x.get("end") or 0)) for x in (j.get("segments") or []) if (x.get("text") or "").strip()]
        if isinstance(j, list): return [(float(x.get("start") or 0), float(x.get("end") or 0)) for x in j if isinstance(x, dict) and (x.get("text") or "").strip()]
    except ValueError: pass
    if "-->" in txt: return parse_sub_cues(txt)
    segs = []
    for line in txt.splitlines():   # json lines: one segment per line
        try: x = json.loads(line); segs.append((float(x.get("start") or 0), float(x.get("end") or 0)))
        except Exception: continue
    if segs: return segs
    raise ValueError(f"Whisper endpoint answered in an unknown format: {txt[:80]!r}")
def _asr_segments(path, start, dur, audio_stream=None, lang2=None):
    """Speech segments (start, end) relative to the window, from the Whisper endpoint's json output."""
    wav = os.path.join(CONFIG_DIR, f"sync-{os.getpid()}-{int(start)}.wav")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", path] + (["-map", f"0:{audio_stream}"] if audio_stream is not None else []) + ["-vn", "-ac", "1", "-ar", "16000", wav], capture_output=True, timeout=300, check=True)
        import uuid; bnd = uuid.uuid4().hex; body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + open(wav, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
        q = "task=transcribe&output=json&encode=false" + (f"&language={lang2}" if lang2 else ""); last = None
        for base_url in SUBGEN_URLS:
            try:
                req = urllib.request.Request(f"{base_url.rstrip('/')}/asr?{q}", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
                with urllib.request.urlopen(req, timeout=900) as r: raw = r.read().decode("utf-8", "replace")
                return asr_segments_from(raw)
            except Exception as e: last = e; log("sync whisper:", base_url, repr(e))
        raise last or RuntimeError("no whisper endpoint")
    finally:
        try: os.remove(wav)
        except OSError: pass
def find_subtitle_source(path, pr):
    """The subtitles a player would use: an external file in a preferred language first, else the first embedded text track. -> (kind, ref, lang2) or None."""
    d, base = os.path.split(path); stem = os.path.splitext(base)[0].lower(); best = None
    try: names = sorted(os.listdir(d))
    except OSError: names = []
    for n in names:
        ln = n.lower(); ext = os.path.splitext(ln)[1]
        if ext not in (".srt", ".ass", ".ssa", ".vtt") or not ln.startswith(stem): continue
        rest = ln[len(stem):-len(ext)].strip(".").split("."); tags = [t for t in rest if t not in ("hi", "sdh", "forced", "cc", "default")]
        if "forced" in rest: continue
        lang = canon(tags[0]) if tags and len(tags[0]) in (2, 3) else "und"
        score = (0 if lang in PREF_SUBS else 1 if lang == "und" else 2, len(rest))
        if score[0] < 2 and (best is None or score < best[0]): best = (score, os.path.join(d, n), lang)
    if best: return ("sidecar", best[1], TWO_LETTER.get(best[2], "en"))
    subs = [st for st in pr.get("streams", []) if st.get("codec_type") == "subtitle" and st.get("codec_name") in TEXT_SUB_CODECS and canon((st.get("tags") or {}).get("language", "und") or "und") in (PREF_SUBS | {"und"}) and not is_signs(st) and not (st.get("disposition") or {}).get("forced")]
    if subs: return ("embedded", subs[0]["index"], TWO_LETTER.get(canon((subs[0].get("tags") or {}).get("language", "und") or "und"), "en"))
    return None
def subtitle_sync_check(path, pr=None):
    """Listen to three short samples and compare the speech times with the subtitle times. -> dict(status, offset, note, source, windows, lang2)."""
    if pr is None:
        ok, pr = ffprobe(path)
        if not ok: return dict(status="unknown", note=f"ffprobe failed: {pr}", source=None)
    src = find_subtitle_source(path, pr)
    if not src: return dict(status="unknown", note="no text subtitles to check (picture subtitles cannot be timed this way)", source=None)
    kind, ref, lang2 = src
    dur = float((pr.get("format") or {}).get("duration") or 0); rd = real_duration(path) if dur else 0
    if rd and abs(rd - dur) > 60: dur = rd
    if not dur: return dict(status="unknown", note="no duration", source=src)
    a = [st for st in pr.get("streams", []) if st.get("codec_type") == "audio"]
    a_pick = next((st for st in a if canon((st.get("tags") or {}).get("language", "und") or "und") in PREF_AUDIO), a[0] if a else None)
    alang = canon((a_pick.get("tags") or {}).get("language", "und") or "und") if a_pick else "und"; audio_stream = a_pick["index"] if a_pick else None
    asr_lang = TWO_LETTER.get(alang) if alang not in ("und", "") else None
    if kind == "sidecar": all_cues = parse_sub_cues(open(ref, encoding="utf-8-sig", errors="replace").read())
    else: all_cues = parse_sub_cues(subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{ref}", "-f", "srt", "-"], capture_output=True, text=True, timeout=600).stdout)   # the whole track once; cue times are absolute
    windows = []
    for start, wlen in pick_windows(dur, [s_ for s_, e_ in all_cues]):
        subs = [s_ - start for s_, e_ in all_cues if start <= s_ <= start + wlen + 5]
        asr = [s_ for s_, e_ in _asr_segments(path, start, wlen, audio_stream, asr_lang)]
        off, support, matched = estimate_offset(subs, asr); windows.append((start / dur, off, support, matched))
    status, off, note = decide_sync(windows)
    return dict(status=status, offset=off, note=note, source=src, windows=windows, lang2=lang2)
def _subsync_job(job):
    """Background job: check one file; out of sync -> a SUBS_OUT_OF_SYNC finding (or refresh it); in sync -> clear it."""
    t0 = time.time(); conn = db()
    try:
        try: r = subtitle_sync_check(job["path"])
        except Exception as e:
            ok_, pr_ = ffprobe(job["path"]); r = dict(status="unknown", note=err_result(e), source=find_subtitle_source(job["path"], pr_) if ok_ else None)
        code = "SUBS_OUT_OF_SYNC"; fidh = hashlib.sha1(f"{job['app']}|{job['fid']}|{code}".encode()).hexdigest()[:16]; src = r.get("source"); now = time.time()
        src_txt = f"external '{os.path.basename(src[1])}'" if src and src[0] == "sidecar" else f"embedded track {src[1]}" if src else "no subtitles"
        if r["status"] in ("late", "early", "drift"):
            detail = f"{src_txt}: subtitles {r['note']} | {'fix: re-time in Bazarr' if r['status'] == 'drift' else 'fix: shift'} | lang={r.get('lang2')} | src={src[0]}:{src[1]}"
            if ignored(load_ignores(), code, job["label"], fidh): res = f"OUT OF SYNC but ignored by rule: {r['note']}"
            else:
                upsert_finding(conn, fidh, job["app"], job["fid"], code, "medium", job["label"], job["path"], detail, now)
                res = f"OUT OF SYNC: {r['note']}"
                if r["status"] == "drift" and src and src[0] == "embedded" and (job["app"], job["fid"]) in upcoming_map(conn):   # nobody can re-time a built-in track: get a separate file before they press play
                    try:
                        info = arr_file_info(arr_apps(), job["app"], job["fid"], job["path"])
                        enqueue(dict(type="subsearch", prio=1, fid=job["fid"], app=job["app"], kind=info["kind"], parent_id=info["parent_id"], ep_ids=info["ep_ids"], path=info["path"], label=job["label"], actor="built-in subtitles drift, up next in Plex"))
                        res += "; asked Bazarr for a separate subtitle file (up next in Plex)"
                    except Exception as e: log("subsync auto-search:", repr(e))
        elif r["status"] == "in sync":
            if conn.execute("SELECT 1 FROM finding WHERE id=? AND resolved_at IS NULL", (fidh,)).fetchone(): resolve_finding(conn, fidh, "subtitles in sync")
            res = f"in sync: {r['note']}"
        else: res = f"could not judge: {r['note']}"
        record_action(conn, "on", "subsync", job["app"], job["fid"], job["label"], f"subtitle timing check by {job['actor']} ({src_txt}, {time.time() - t0:.0f}s)", res[:300])
        conn.commit(); refresh_reports(conn)
    finally: conn.close()
def shift_subtitle_file(src, seconds, dst):
    """Rewrite an SRT / VTT / ASS file with every time moved by -seconds (subtitles that run late are pulled earlier)."""
    txt = open(src, encoding="utf-8-sig", errors="replace").read()
    def fmt(t, style):
        t = max(0.0, t); h = int(t // 3600); m = int(t % 3600 // 60); s_ = int(t % 60); ms = int(round((t - int(t)) * 1000))
        if ms == 1000: s_ += 1; ms = 0
        return f"{h:02d}:{m:02d}:{s_:02d}{style}{ms:03d}" if style in (",", ".") else f"{h}:{m:02d}:{s_:02d}.{ms // 10:02d}"
    def cue(m): sep = "," if "," in m.group(1) else "."; return fmt(_ts(m.group(1)) - seconds, sep) + " --> " + fmt(_ts(m.group(2)) - seconds, sep)
    out = re.sub(r"(\d+:\d\d:\d\d[,.]\d{1,3})\s*-->\s*(\d+:\d\d:\d\d[,.]\d{1,3})", cue, txt)
    if out == txt: out = re.sub(r"^(Dialogue:\s*\d+,)(\d+:\d\d:\d\d[.,]\d{1,3}),(\d+:\d\d:\d\d[.,]\d{1,3})", lambda m: m.group(1) + fmt(_ts(m.group(2)) - seconds, "ass") + "," + fmt(_ts(m.group(3)) - seconds, "ass"), txt, flags=re.M)
    open(dst, "w", encoding="utf-8").write(out)
def bazarr_sub_path(kind, id_, lang2):
    """The path Bazarr itself uses for the external subtitle of this episode/movie (its mounts may differ from ours)."""
    try:
        rows = (bazarr_call(f"episodes?episodeid[]={id_}" if kind == "episode" else f"movies?radarrid[]={id_}") or {}).get("data") or []
        for s_ in (rows[0].get("subtitles") or []) if rows else []:
            if s_.get("path") and s_.get("code2") == lang2: return s_["path"]
    except Exception as e: log("bazarr sub path:", repr(e))
    return None
def subsync_fix(conn, f, apps, actor, body):
    """Re-time the subtitles named in a SUBS_OUT_OF_SYNC finding: a constant offset is fixed here (external file rewritten, embedded track remuxed losslessly); drift is handed to Bazarr's sync."""
    from urllib.parse import urlencode
    d = (conn.execute("SELECT detail FROM finding WHERE id=?", (f["id"],)).fetchone() or [""])[0] or ""
    m = re.search(r"src=(sidecar|embedded):(.+?)(?: \||$)", d); off_m = re.search(r"(\d+(?:\.\d+)?)s (late|early)", d)
    if not m: return 400, {"error": "this finding has no subtitle source recorded - press 'Check subtitle timing' again first"}
    kind, ref = m.group(1), m.group(2); drift = "drift:" in d; path = f["path"]; lang2 = (re.search(r"lang=(\w\w)", d) or [None, "en"])[1]
    if drift or body.get("bazarr"):
        if kind != "sidecar": return 400, {"error": "the drifting subtitles are inside the video file and Bazarr can only re-time an external one - 'Find subtitles' or 'Make AI subtitles' gives you a file that fits"}
        if not bazarr_conf()[1]: return 400, {"error": "no Bazarr API key - cannot ask Bazarr to re-time"}
        try: info = arr_file_info(apps, f["app"], f["fid"], path)
        except Exception as e: return 409, {"error": str(e)}
        ids = dict(type="episode", id=info["ep_ids"][0]) if info["kind"] == "series" and info["ep_ids"] else dict(type="movie", id=info["parent_id"])
        bpath = bazarr_sub_path(ids["type"], ids["id"], lang2) or ref
        bazarr_call(f"subtitles?{urlencode(dict(action='sync', language=lang2, path=bpath, **ids))}", data={}, method="PATCH", timeout=900)
        record_action(conn, "on", "subsync-fix", f["app"], f["fid"], f["label"], f"Bazarr sync of {os.path.basename(ref)} by {actor}", "asked Bazarr to re-time it against the audio; the timing is checked again afterwards")
        enqueue(dict(type="subsync", fid=f["fid"], app=f["app"], path=path, label=f["label"], actor="re-check after Bazarr sync", prio=3)); refresh_reports(conn)
        return 200, {"ok": True, "queued": True, "note": "Bazarr is re-timing the subtitles against the audio; the timing is checked again when it is done"}
    if not off_m: return 400, {"error": "no offset recorded on this finding"}
    seconds = float(off_m.group(1)) * (1 if off_m.group(2) == "late" else -1)
    if kind == "sidecar":
        if not os.path.exists(ref): return 404, {"error": "the subtitle file is gone"}
        tmp = ref + ".retimed.tmp"; shift_subtitle_file(ref, seconds, tmp)
        dest, _ = quarantine(conn, ref, f"ui subsync_fix by {actor}: moved {-seconds:+.2f}s", "regrab", sidecars=False); os.rename(tmp, ref)
        try: st0 = os.stat(dest); os.chmod(ref, st0.st_mode & 0o777); os.chown(ref, st0.st_uid, st0.st_gid)
        except OSError: pass
        how = f"external file rewritten, every line moved {-seconds:+.2f}s; original -> {dest}"
    else:
        if not path or not path.lower().endswith(".mkv"): return 400, {"error": "embedded subtitles can only be re-timed inside .mkv files"}
        ok, pr = ffprobe(path)
        if not ok: return 500, {"error": "ffprobe failed", "detail": pr}
        try: mk = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True, timeout=120).stdout)
        except Exception as e: return 500, {"error": f"mkvmerge -J failed: {e!r}"}
        subs_ff = [s_ for s_ in pr.get("streams", []) if s_.get("codec_type") == "subtitle"]; subs_mk = [t for t in mk.get("tracks", []) if t.get("type") == "subtitles"]
        pos = next((i for i, s_ in enumerate(subs_ff) if str(s_["index"]) == str(ref)), None)
        if pos is None or pos >= len(subs_mk): return 500, {"error": "could not map the subtitle track between ffprobe and mkvmerge"}
        tid = subs_mk[pos]["id"]; tmp = path[:-4] + ".subsync.tmp.mkv"; cmd = ["mkvmerge", "-o", tmp, "--sync", f"{tid}:{int(round(-seconds * 1000))}", path]
        try:
            guard_write("mkvmerge remux"); p_ = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if p_.returncode not in (0, 1): raise RuntimeError((p_.stderr or p_.stdout)[-400:])
            ok2, pr2 = ffprobe(tmp)
            if not ok2 or len(pr2.get("streams", [])) != len(pr.get("streams", [])): raise RuntimeError("remuxed file unreadable or track count changed")
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return 500, {"error": f"remux failed, original untouched: {e}"}
        dest, _ = quarantine(conn, path, f"ui subsync_fix by {actor}: subtitle track {tid} moved {-seconds:+.2f}s", "regrab", sidecars=False); os.rename(tmp, path)
        try: st0 = os.stat(dest); os.chmod(path, st0.st_mode & 0o777); os.chown(path, st0.st_uid, st0.st_gid)
        except OSError: pass
        conn.execute("DELETE FROM probe WHERE path=?", (path,)); how = f"lossless remux, subtitle track moved {-seconds:+.2f}s; original -> {dest}"
    resolve_finding(conn, f["id"], "subtitles re-timed"); plx = plex_refresh(path)
    record_action(conn, "on", "subsync-fix", f["app"], f["fid"], f["label"], f"subtitles re-timed by {actor}", f"{how}; {plx}")
    enqueue(dict(type="subsync", fid=f["fid"], app=f["app"], path=path, label=f["label"], actor="re-check after re-timing", prio=3)); refresh_reports(conn)
    return 200, {"ok": True, "dest": dest, "note": f"subtitles moved {-seconds:+.1f}s; the timing is checked again in a minute, and the original is in the trash for {TRASH_DAYS} days"}
def recent_subtitle_paths(paths, days):
    """Of these video paths, the ones whose external text subtitle changed in the last `days` days (one folder listing per folder)."""
    cut = time.time() - days * 86400; dirs = {}; out = set()
    for path in paths:
        d = os.path.dirname(path)
        if d not in dirs:
            try: dirs[d] = {n.lower(): os.stat(os.path.join(d, n)).st_mtime for n in os.listdir(d) if os.path.splitext(n)[1].lower() in (".srt", ".ass", ".ssa", ".vtt")}
            except OSError: dirs[d] = {}
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        if any(n.startswith(stem) and mt >= cut for n, mt in dirs[d].items()): out.add(path)
    return out
def do_auto_subsync(conn, apps, items, plan=None):
    """Nightly: queue subtitle timing checks for files whose external subtitles are new, and for what people are watching. Returns the number queued."""
    mode = gate(setting("AUTO_SUBSYNC"), "AUTO_SUBSYNC")
    if mode == "off" or not SUBGEN_URLS: return 0
    mx = int(setting("AUTO_SUBSYNC_MAX")); days = int(setting("AUTO_SUBSYNC_DAYS"))
    if items is None:
        if plan is not None: plan.append(dict(label=f"up to {mx} files whose subtitles changed in the last {days} days, or are being watched", why="listen to three samples and compare with the subtitle times"))
        return 0
    recent = {l for (l,) in conn.execute("SELECT label FROM action WHERE kind='subsync' AND ts > ?", (time.time() - 30 * 86400,))}
    open_ = {(a, f) for a, f in conn.execute("SELECT app, fid FROM finding WHERE resolved_at IS NULL AND check_code='SUBS_OUT_OF_SYNC'")}
    cands = [it for it in items if it.get("path") and it["app"] in ("sonarr", "radarr") and not it.get("missing") and it["label"] not in recent and (it["app"], it["fid"]) not in open_]
    up = upcoming_map(conn); fresh = recent_subtitle_paths([it["path"] for it in cands], days)
    picks = [it for it in cands if (it["app"], it["fid"]) in up] + [it for it in cands if it["path"] in fresh and (it["app"], it["fid"]) not in up]
    n = 0
    for it in picks[:mx]:
        if plan is not None: plan.append(dict(label=it["label"], why="being watched" if (it["app"], it["fid"]) in up else f"subtitles changed in the last {days} days")); n += 1; continue
        if mode == "dryrun": record_action(conn, "dryrun", "subsync", it["app"], it["fid"], it["label"], "would check subtitle timing", "dryrun"); n += 1; continue
        enqueue(dict(type="subsync", fid=it["fid"], app=it["app"], path=it["path"], label=it["label"], actor="nightly")); n += 1
    return n
def ui_subsync(conn, body, actor):
    """{show, season?} or {movie} -> queue a subtitle timing check for every file of that title."""
    apps = arr_apps(); q = (body.get("show") or body.get("movie") or "").strip().lower(); season = body.get("season"); jobs = []
    if not q: return 400, {"error": "need show or movie"}
    if body.get("movie"):
        if "radarr" not in apps: return 400, {"error": "Radarr is not connected"}
        base, key, _ = apps["radarr"]; m = next((x for x in api(base, key, "movie") if x["title"].lower().startswith(q)), None)
        if not m or not (m.get("movieFile") or {}).get("path"): return 404, {"error": f"no Radarr movie with a file starting with {q!r}"}
        jobs.append(dict(type="subsync", app="radarr", fid=m["movieFile"]["id"], path=m["movieFile"]["path"], label=f"{m['title']} ({m.get('year')})", actor=actor, prio=3))
    else:
        if "sonarr" not in apps: return 400, {"error": "Sonarr is not connected"}
        base, key, _ = apps["sonarr"]; srs = next((x for x in api(base, key, "series") if x["title"].lower().startswith(q)), None)
        if not srs: return 404, {"error": f"no Sonarr series starting with {q!r}"}
        eps = {e["episodeFileId"]: e for e in api(base, key, f"episode?seriesId={srs['id']}") if e.get("episodeFileId")}
        for ef in api(base, key, f"episodefile?seriesId={srs['id']}"):
            e = eps.get(ef["id"])
            if not e or not ef.get("path"): continue
            if season not in (None, "", 0) and int(e["seasonNumber"]) != int(season): continue
            jobs.append(dict(type="subsync", app="sonarr", fid=ef["id"], path=ef["path"], label=f"{srs['title']} S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}", actor=actor, prio=3))
        jobs.sort(key=lambda j: j["label"])
    if not jobs: return 404, {"error": "nothing matched (no files for that selection)"}
    jobs = jobs[:40]
    for j in jobs: enqueue(j)
    record_action(conn, "on", "subsync", jobs[0]["app"], len(jobs), jobs[0]["label"].rsplit(" S", 1)[0] if body.get("show") else jobs[0]["label"], f"timing checks queued by {actor}", f"{len(jobs)} file(s) queued; about a minute each"); refresh_reports(conn)
    return 200, {"ok": True, "queued": len(jobs), "note": f"{len(jobs)} timing check(s) queued, about a minute each - files that are out of step show up as findings with a Re-time button"}
# ---------- F33: which language is spoken? (untagged audio) ----------
CODE3 = {v: k for k, v in TWO_LETTER.items()}
LANG_NAMES = {"english": "eng", "japanese": "jpn", "french": "fre", "german": "ger", "spanish": "spa", "korean": "kor", "chinese": "chi", "mandarin": "chi", "portuguese": "por", "italian": "ita", "russian": "rus", "arabic": "ara", "hindi": "hin", "dutch": "dut"}
LANG_WORDS = {"eng": "English", "jpn": "Japanese", "fre": "French", "ger": "German", "spa": "Spanish", "kor": "Korean", "chi": "Chinese", "por": "Portuguese", "ita": "Italian", "rus": "Russian", "ara": "Arabic", "hin": "Hindi", "dut": "Dutch"}
def detect_reply_to_code3(j):
    """Whisper's answer to /detect-language (or an /asr json with a language) -> 3-letter code, or None when it is not a language this app knows."""
    if not isinstance(j, dict): return None
    code = str(j.get("language_code") or j.get("language") or "").lower().split("-")[0]
    if code in CODE3: return CODE3[code]
    name = str(j.get("detected_language") or j.get("language") or "").lower().strip()
    return LANG_NAMES.get(name)
def detect_audio_language(path, audio_stream=None):
    """Listen to about 45 s from a fifth of the way in and ask Whisper which language it hears. -> (code3 or None, note)."""
    ok, pr = ffprobe(path)
    if not ok: return None, f"ffprobe failed: {pr}"
    dur = float((pr.get("format") or {}).get("duration") or 0); start = max(0.0, min(dur * 0.2, max(0.0, dur - 50))) if dur else 0.0
    wav = os.path.join(CONFIG_DIR, f"lang-{os.getpid()}-{int(start)}.wav")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", "45", "-i", path] + (["-map", f"0:{audio_stream}"] if audio_stream is not None else []) + ["-vn", "-ac", "1", "-ar", "16000", wav], capture_output=True, timeout=300, check=True)
        import uuid; bnd = uuid.uuid4().hex; body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + open(wav, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
        last = None
        for base_url in SUBGEN_URLS:
            for q in ("detect-language?encode=false", "asr?task=transcribe&output=json&encode=false"):
                try:
                    req = urllib.request.Request(f"{base_url.rstrip('/')}/{q}", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
                    with urllib.request.urlopen(req, timeout=600) as r: raw = r.read().decode("utf-8", "replace")
                    try: j = json.loads(raw)
                    except ValueError: continue   # srt text carries no language
                    code = detect_reply_to_code3(j)
                    if code: return code, f"Whisper heard {LANG_WORDS.get(code, code)}"
                    return None, f"Whisper heard a language this app does not label ({j.get('detected_language') or j.get('language_code') or j.get('language')})"
                except Exception as e: last = e
        return None, f"no Whisper endpoint answered ({last!r})"
    finally:
        try: os.remove(wav)
        except OSError: pass
def _langdetect_job(job):
    """Background job: detect the spoken language of an untagged file, write the label (the same lossless path as 'Set audio language'), say what that means."""
    t0 = time.time(); conn = db()
    try:
        f = finding_row(conn, job["finding"])
        code, note = detect_audio_language(job["path"])
        if not code or not f: record_action(conn, "on", "langdetect", job["app"], job["fid"], job["label"], f"spoken language check by {job['actor']} ({time.time() - t0:.0f}s)", f"could not label it: {note}"); conn.commit(); refresh_reports(conn); return
        st, res = ui_act(conn, {"id": job["finding"], "action": "fixtag", "lang": code}, f"{job['actor']} (Whisper heard {LANG_WORDS.get(code, code)})")
        if st != 200: record_action(conn, "on", "langdetect", job["app"], job["fid"], job["label"], f"spoken language check by {job['actor']}: {note}", f"label not written: {res.get('error', res)}"[:300]); conn.commit(); refresh_reports(conn); return
        why = "English audio needs no subtitles - this row clears on the next check" if code in PREF_AUDIO else f"{LANG_WORDS.get(code, code)} audio - subtitles are searched the usual way"
        record_action(conn, "on", "langdetect", job["app"], job["fid"], job["label"], f"spoken language check by {job['actor']} ({time.time() - t0:.0f}s)", f"{note}; audio labelled {code}; {why}")
        conn.commit(); refresh_reports(conn)
    except Exception as e: note_error("language detection job:", repr(e))
    finally: conn.close()
def do_auto_audiolang(conn, plan=None):
    """Nightly: queue spoken-language detection for Unknown-language rows (untagged audio, no subtitles). Returns the number queued."""
    mode = gate(setting("AUTO_AUDIO_LANG"), "AUTO_AUDIO_LANG")
    if mode == "off" or not SUBGEN_URLS: return 0
    mx = int(setting("AUTO_AUDIO_LANG_MAX")); tried = {l for (l,) in conn.execute("SELECT label FROM action WHERE kind='langdetect' AND ts > ?", (time.time() - 30 * 86400,))}
    rows = conn.execute("SELECT id, app, fid, label, path FROM finding WHERE resolved_at IS NULL AND check_code='UND_AUDIO_NO_SUBS' AND app IN ('sonarr','radarr','whisparr') AND detail NOT LIKE '[working:%' ORDER BY first_seen").fetchall()
    n = 0
    for fid_, app_, afid_, label_, path_ in rows:
        if n >= mx: break
        if label_ in tried or not path_ or not os.path.exists(path_): continue
        if plan is not None: plan.append(dict(label=label_, why="the audio has no language label - listen and label it")); n += 1; continue
        if mode == "dryrun": record_action(conn, "dryrun", "langdetect", app_, afid_, label_, "would detect the spoken language", "dryrun"); n += 1; continue
        enqueue(dict(type="langdetect", finding=fid_, app=app_, fid=afid_, path=path_, label=label_, actor="nightly")); n += 1
    return n
def _verify_job(job):
    t0 = time.time()
    try: concl, det = verify_content(job["app"], job["fid"], job["path"], job["parent_id"], job["ep_ids"], arr_apps())
    except Exception as e: concl, det = err_result(e), ""
    conn = db()
    try:
        row = conn.execute("SELECT detail FROM finding WHERE id=?", (job["finding"],)).fetchone()
        base_detail = re.sub(r"^(\[verified [^\]]*\]\s*(\|\|\s*)?)+", "", row[0]) if row else ""
        conn.execute("UPDATE finding SET detail = ? WHERE id=? AND resolved_at IS NULL", (f"[verified {datetime.datetime.now():%H:%M}: {concl}] || {base_detail}", job["finding"]))
        record_action(conn, "on", "verify", job["app"], job["fid"], job["label"], f"content check by {job['actor']} ({(time.time()-t0):.0f}s)", f"{concl} | {det}"[:400])
        chk = (conn.execute("SELECT check_code FROM finding WHERE id=?", (job["finding"],)).fetchone() or [""])[0]
        if chk in ("RUNTIME_LONG", "RUNTIME_SHORT") and (concl.startswith("CONTENT MATCHES") or concl.startswith("CONSISTENT with")):
            add_ignore_rule(f"{job['finding']}   # verified {chk} {job['label']} - right episode, just a different cut"); resolve_finding(conn, job["finding"])
            record_action(conn, "on", "auto-silence", job["app"], job["fid"], job["label"], f"{chk} verified as the right episode", "silenced - extended/alternate cut, nothing to fix")
        elif concl.startswith(("MISLABELED", "MULTI-EPISODE", "DUPLICATE CONTENT")):
            conn.execute("UPDATE finding SET severity='high' WHERE id=? AND resolved_at IS NULL", (job["finding"],))
        elif chk == "TITLE_MISMATCH" and concl.startswith(("NOT A COPY", "CONTENT MATCHES", "CONSISTENT", "PROBABLY CORRECT")):
            conn.execute("UPDATE finding SET severity='low' WHERE id=? AND resolved_at IS NULL", (job["finding"],))   # odd embedded title, right content
        elif chk in ("RUNTIME_LONG", "RUNTIME_SHORT", "TITLE_MISMATCH"):
            conn.execute("UPDATE finding SET severity=CASE WHEN severity='high' THEN 'medium' ELSE severity END WHERE id=? AND resolved_at IS NULL", (job["finding"],))
        conn.commit(); refresh_reports(conn)
    finally: conn.close()
FOLLOW_WAITS = (90, 240, 600)   # seconds before each look: Plex and the arrs take a minute or more to pick a file up
FOLLOW_WHAT = {"plex": ("Plex lists the file now", "Plex still does not list it"), "arr": ("the arr lists the file now", "no arr lists it yet")}
def _followup_job(job, sleep=time.sleep):
    """#41: after 'Ask Plex to rescan' / 'Ask the arr to rescan', look again until the file shows up (the row closes, with proof in Actions)
    or the looks run out (the row stays, and Actions says what is still missing). Never ends silently."""
    what = job.get("what"); path = job.get("path") or ""; yes, no = FOLLOW_WHAT.get(what, ("done", "not yet"))
    for i, wait in enumerate(job.get("waits") or FOLLOW_WAITS):
        sleep(wait)
        conn = db()
        try:
            row = conn.execute("SELECT resolved_at FROM finding WHERE id=?", (job.get("finding"),)).fetchone()
            if not row or row[0] is not None: return "already closed"
        finally: conn.close()
        try: found = (os.path.normpath(path) in plex_inventory()) if what == "plex" else bool(arr_lists_path(arr_apps(), path)) if what == "arr" else False
        except Exception as e: found = False; log("follow-up:", repr(e))
        if found:
            conn = db()
            try: resolve_finding(conn, job["finding"], yes); record_action(conn, "on", "followup", job.get("app", ""), job.get("fid", 0), job.get("label", ""), f"checked after {job.get('after', 'the fix')}", f"verified: {yes} (look {i + 1})"); refresh_reports(conn)
            finally: conn.close()
            return "verified"
    conn = db()
    try: record_action(conn, "on", "followup", job.get("app", ""), job.get("fid", 0), job.get("label", ""), f"checked after {job.get('after', 'the fix')}", f"not yet: {no} after {sum(job.get('waits') or FOLLOW_WAITS) // 60} min - the row stays; the next audit looks again"); refresh_reports(conn)
    finally: conn.close()
    return "not yet"
def sub_worker(q=None):
    q = q or SUB_Q
    while True:
        item = q.get(); job = item[2] if isinstance(item, tuple) else item; jid = job.get("_id")
        conn = db()
        try: conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (time.time(), jid)); conn.commit()
        finally: conn.close()
        try:
            if E("JOBS", "on") == "off": st = "skipped"   # staging copy: record, never run (would hit the real Bazarr/Whisper)
            else: ({"whisper": _whisper_job, "verify": _verify_job, "subsync": _subsync_job, "langdetect": _langdetect_job, "followup": _followup_job}.get(job.get("type"), _subsearch))(job); st = "done"
        except Exception as e: note_error("sub worker:", repr(e)); st = "failed"
        finally:
            conn = db()
            try: conn.execute("UPDATE jobs SET status=?, done_at=? WHERE id=?", (st, time.time(), jid)); conn.commit()
            finally: conn.close()
            q.task_done()
def trash_list():
    out = []; logmap = {}
    lp = os.path.join(TRASH_DIR, "LOG.txt")
    if os.path.exists(lp):
        for line in open(lp):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 5: logmap[parts[4]] = (parts[0], parts[1], parts[2])
    for d, _, files in os.walk(TRASH_DIR) if os.path.isdir(TRASH_DIR) else []:
        for fn in files:
            if fn == "LOG.txt": continue
            p = os.path.join(d, fn); parts = os.path.relpath(p, TRASH_DIR).split(os.sep)
            kind, day = (parts[0], parts[1]) if parts[0] in TRASH_KEEP else ("regrab", parts[0])
            ts, why, orig = logmap.get(p, ("", "", os.path.join("/data", *parts[2:]) if parts[0] in TRASH_KEEP else os.path.join("/data", *parts[1:])))
            try: size = os.path.getsize(p)
            except OSError: size = 0
            keep = TRASH_KEEP.get(kind, TRASH_DAYS)
            try: purge_on = (datetime.date.fromisoformat(day) + datetime.timedelta(days=keep)).isoformat()
            except ValueError: purge_on = "?"
            out.append(dict(dest=p, kind=kind, day=day, name=fn, size=size, original=orig, why=why, quarantined=ts, purge_on=purge_on, original_exists=os.path.exists(orig)))
    out.sort(key=lambda x: (x["kind"], x["day"], x["name"])); return out
def ui_restore(conn, body, actor):
    dest = body.get("dest") or ""
    dest = os.path.realpath(dest); root_ = os.path.realpath(TRASH_DIR)   # normalise first, then check, then use only the normalised path
    if not dest.startswith(root_ + os.sep) or not os.path.isfile(dest): return 400, {"error": "not a quarantined file"}
    entry = next((t for t in trash_list() if t["dest"] == dest), None)
    if not entry: return 404, {"error": "not in trash listing"}
    orig = entry["original"]
    if os.path.exists(orig): return 409, {"error": "a file already exists at the original path", "original": orig}
    restore_file(dest, orig)
    conn.execute("DELETE FROM probe WHERE path=?", (orig,))
    conn.execute("UPDATE regrab SET status='restored', note=?, checked_at=? WHERE trash_dest=? AND status='pending'", (f"restored via UI by {actor}", time.time(), dest))
    resc = rescan_for_path(arr_apps(), orig) if entry["kind"] == "regrab" else "duplicate restored (arr not involved)"; plx = plex_refresh(orig)
    record_action(conn, "on", "restore", entry["kind"], dest, os.path.basename(orig), f"restored by {actor}", f"{resc}; {plx}"); refresh_reports(conn)
    return 200, {"ok": True, "original": orig, "rescan": f"{resc}; {plx}"}
def ui_purge(conn, body, actor):
    kind = body.get("kind"); n = 0; freed = 0
    if kind not in TRASH_KEEP and kind != "all": return 400, {"error": "kind must be dedupe, regrab or all"}
    for t in trash_list():
        if kind != "all" and t["kind"] != kind: continue
        try: freed += t["size"]; os.remove(t["dest"]); n += 1
        except OSError as e: log("purge:", e)
    for d, dirs, files in os.walk(TRASH_DIR, topdown=False):
        if d != TRASH_DIR and not os.listdir(d):
            try: os.rmdir(d)
            except OSError: pass
    record_action(conn, "on", "purge", "trash", kind, f"{n} files", f"purge-now ({kind}) by {actor}", f"freed {freed/1e9:.1f} GB"); refresh_reports(conn)
    return 200, {"ok": True, "deleted": n, "freed_gb": round(freed / 1e9, 1)}
def reconcile_disk_findings(conn):
    n = 0
    for fid, path in conn.execute("SELECT id,path FROM finding WHERE resolved_at IS NULL AND app='disk'").fetchall():
        if path and not os.path.exists(path): conn.execute("UPDATE finding SET resolved_at=? WHERE id=?", (time.time(), fid)); n += 1
    if n: conn.commit(); log(f"reconcile: cleared {n} disk findings whose file is gone"); refresh_reports(conn)
    return n
def ui_state(conn):
    try: latest = json.load(open(os.path.join(REPORT_DIR, "latest.json")))
    except Exception: latest = {"findings": [], "actions": [], "counts": {}, "by_severity": {}, "run": {}}
    n, tot = trash_stats(); latest["trash"] = dict(files=n, gb=round(tot / 1e9, 1), keep=TRASH_KEEP, dir=TRASH_DIR)
    latest["progress"] = PROGRESS; latest["gates"] = dict(ARR_RESCAN=ARR_RESCAN, DEDUPE=DEDUPE, REMEDIATE=REMEDIATE, PAPERCLIP=PAPERCLIP, PLEX_CHECK=PLEX_CHECK, PLEX_REFRESH=PLEX_REFRESH)
    latest["ignore_rules"] = open(IGNORE_FILE).read() if os.path.exists(IGNORE_FILE) else ""
    latest["regrabs"] = [dict(ts=datetime.datetime.fromtimestamp(r[0]).isoformat(timespec="minutes"), app=r[1], label=r[2], code=r[3], status=r[4], note=r[5], actor=r[6],
                              checked=datetime.datetime.fromtimestamp(r[7]).isoformat(timespec="minutes") if r[7] else "", orig=r[8], wait_days=float(cfg("REGRAB_WAIT_DAYS", REGRAB_WAIT_DAYS)))
                         for r in conn.execute("SELECT ts,app,label,code,status,note,actor,checked_at,orig_path FROM regrab ORDER BY ts DESC LIMIT 100")]
    latest["health"] = health_info(); latest["sub_queue"] = SUB_Q.qsize()
    act = {}; recent = []
    rows_ = conn.execute("SELECT status, job, ts, done_at, started_at FROM jobs WHERE status IN ('queued','running') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id").fetchall() \
          + conn.execute("SELECT status, job, ts, done_at, started_at FROM jobs WHERE status NOT IN ('queued','running') AND done_at > ? ORDER BY done_at DESC LIMIT 12", (time.time() - 6 * 3600,)).fetchall()
    for st_, raw, ts_, done_, started_ in rows_:   # active jobs first (all of them), then the last 12 finished
        try: jb = json.loads(raw)
        except Exception: continue
        kind = jb.get("type", "subsearch")
        if st_ in ("queued", "running"):
            e_ = act.setdefault(kind, {"queued": 0, "running": 0, "running_for": 0}); e_[st_] += 1
            if st_ == "running" and started_: e_["running_for"] = max(e_.get("running_for", 0), int(time.time() - started_))
        res_ = conn.execute("SELECT result FROM action WHERE kind=? AND label=? AND ts>=? ORDER BY ts DESC LIMIT 1", (kind, jb.get("label", ""), (started_ or ts_) - 1)).fetchone() if st_ not in ("queued", "running") else None   # the outcome, so the jobs table does not say only DONE
        recent.append(dict(type=kind, status=st_, label=jb.get("label", ""), result=(res_[0] if res_ else ""), queued=datetime.datetime.fromtimestamp(ts_).isoformat(timespec="minutes"), done=datetime.datetime.fromtimestamp(done_).isoformat(timespec="minutes") if done_ else "", running_for=int(time.time() - started_) if (st_ == "running" and started_) else None))
    busy_marks(latest.get("findings") or [], [(st_, _jload(raw), started_ or ts_) for st_, raw, ts_, _d, started_ in rows_ if st_ in ("queued", "running")], PRESSING)
    latest["jobs"] = {"active": act, "recent": recent}; latest["whisper_live"] = whisper_live(); latest["whisper_engine"] = _WL.get("engine", {}); latest["tests"] = tests_status()
    latest["devices"] = {"available": {k: v[0] for k, v in DEVICE_PROFILES.items()}, "chosen": devices_chosen()}; latest["names"] = CHECK_NAMES; latest["sev_names"] = SEV_NAMES; latest["auth"] = {"enabled": bool(auth_conf(conn)), "user": (auth_conf(conn) or {}).get("user"), "viewer": ((auth_conf(conn) or {}).get("viewer") or {}).get("user"), "local_no_login": local_no_login(conn), "local_no_login_env": LOCAL_NO_LOGIN_ENV in ("0", "1")}
    latest["sandbox"] = SANDBOX
    ab_ = about_info(fetch=False, conn=conn); latest["about"] = {"version": ab_["version"], "update_available": ab_["update_available"], "latest": ab_["latest"]["tag"]}
    try:
        apps_ = arr_apps(); latest["setup"] = dict(arrs=sorted(apps_), bazarr=bool(bazarr_conf()[1]), plex=bool(plex_token()), first_run_done=bool(conn.execute("SELECT 1 FROM run WHERE finished IS NOT NULL LIMIT 1").fetchone()), degraded=degraded())
    except Exception as e: latest["setup"] = {"error": repr(e)}
    try: latest["plan"] = plan_tonight(conn)
    except Exception as e: latest["plan"] = {"error": repr(e)}
    try: latest["today"] = today_summary(conn)
    except Exception as e: latest["today"] = {"error": repr(e)}
    try: latest["space"] = space_view(conn)
    except Exception as e: latest["space"] = {"error": repr(e)}
    return latest
JOB_WORDS = {"whisper": "AI subtitles", "subsearch": "Bazarr search", "verify": "content check", "subsync": "subtitle timing check"}
def failed_jobs(conn, hours=24):
    """Background jobs that ended in an error in the last `hours` hours, with no later success or retry for the same title. -> [dict(title, why, job_id, kind, label)]"""
    out = []; seen = set(); cut = time.time() - hours * 3600
    for kind, label, result, ts_ in conn.execute("SELECT kind, label, result, ts FROM action WHERE ts > ? AND result LIKE 'ERROR%' AND kind IN ('whisper','subsearch','verify','subsync') ORDER BY ts DESC", (cut,)):
        if (kind, label) in seen: continue
        seen.add((kind, label))
        if conn.execute("SELECT 1 FROM action WHERE kind=? AND label=? AND ts > ? AND result NOT LIKE 'ERROR%'", (kind, label, ts_)).fetchone(): continue   # it worked later
        if conn.execute("SELECT 1 FROM action WHERE kind='retry' AND label=? AND ts > ?", (label, ts_)).fetchone(): continue   # already retried, wait for it
        row = conn.execute("SELECT id FROM jobs WHERE job LIKE ? AND job LIKE ? ORDER BY id DESC LIMIT 1", (f'%"type": "{kind}"%', f'%"label": {json.dumps(label)}%')).fetchone()
        err = re.sub(r"^ERROR\s*", "", str(result)).split(" | ")[0][:140]
        out.append(dict(kind=kind, label=label, job_id=row[0] if row else None, title=f"{JOB_WORDS.get(kind, kind)} for {label}", why=f"It stopped with: {err}. Retry runs it again; if it fails the same way, About shows the exact cause to report."))
    return out
def retry_job(conn, job_id, actor):
    """Queue a copy of an earlier job (the Retry button on a failed one)."""
    row = conn.execute("SELECT job FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row: return 404, {"error": "that job is not on record any more"}
    job = json.loads(row[0]); job.pop("_id", None); job["actor"] = f"retry by {actor}"; job["prio"] = 2
    enqueue(job); record_action(conn, "on", "retry", job.get("app", ""), job.get("fid") or job.get("episodeid") or job.get("radarrid") or "", job.get("label", ""), f"{JOB_WORDS.get(job.get('type'), job.get('type'))} queued again by {actor}", "queued ahead of routine jobs")
    refresh_reports(conn); return 200, {"ok": True, "queued": True, "note": f"{JOB_WORDS.get(job.get('type'), job.get('type'))} for {job.get('label', '')} is queued again; the result lands in Actions"}
def today_summary(conn, since=None):
    """What happened since the last nightly run and what needs a human. Used by the Today panel and the daily digest."""
    now = time.time(); since = since or (now - 26 * 3600)
    raw = conn.execute("SELECT kind, mode, result, label, ts FROM action WHERE ts>=?", (since,)).fetchall(); acts = [a[:4] for a in raw]
    did = {}
    for k, m, r, l in acts:
        if m != "on" or k in ("ui-error", "settings", "ignore"): continue
        key = {"dedupe": "duplicates quarantined", "subtag": "subtitle tracks labelled", "fixheader": "lying headers repaired", "remuxmkv": "MP4 files rewrapped as MKV", "followup": "fixes checked afterwards", "fixtag": "audio tags fixed", "fixsync": "audio delays removed",
               "regrab": "re-grabs started", "whisper": "AI subtitles made", "subsearch": "Bazarr searches", "watch-priority": "subtitle pushes for what people are watching", "subsync": "subtitle timings checked", "subsync-fix": "subtitles re-timed", "retry": "failed jobs retried", "langdetect": "spoken languages detected", "verify": "content checks", "auto-silence": "verified cuts silenced", "plex-refresh": "Plex folder refreshes",
               "acceptlang": "languages accepted", "search": "searches triggered", "quarantine": "files quarantined", "restore": "files restored", "purge": "trash purges",
               "fixdefaults": "default audio/subtitle flags set", "plexselect": "Plex track selections checked", "archive": "seasons archived"}.get(k)
        if key and not str(r).startswith(("ERROR", "not done", "no subtitle")): did[key] = did.get(key, 0) + 1
    outcomes = {}
    for (st_,) in conn.execute("SELECT status FROM regrab WHERE checked_at>=?", (since,)): outcomes[st_] = outcomes.get(st_, 0) + 1
    err_rows = [a for a in raw if str(a[2]).startswith("ERROR")][-5:]
    errors = [f"{k}: {l[:40]} - {str(r)[:80]}" for k, m, r, l, ts_ in err_rows]; error_at = [datetime.datetime.fromtimestamp(ts_).strftime("%a %H:%M") for *_, ts_ in err_rows]
    needs = []; up = upcoming_set(conn); seen_ids = set()
    if up:   # F2: what someone is about to watch comes first, down to medium
        for sev, code, label, detail, fid, app_, afid_ in conn.execute("SELECT severity, check_code, label, detail, id, app, fid FROM finding WHERE resolved_at IS NULL AND severity IN ('medium','high','critical') AND detail NOT LIKE '[working:%' ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END LIMIT 400"):
            if (app_, afid_) in up and len([n for n in needs if n.get("upcoming")]) < 8: needs.append(dict(kind="finding", sev=sev, check=code, name=check_name(code), title=label, why="Up next in Plex. " + plain_sentence(code, sev, label, detail), id=fid, upcoming=True)); seen_ids.add(fid)
    for sev, code, label, detail, fid in conn.execute("SELECT severity, check_code, label, detail, id FROM finding WHERE resolved_at IS NULL AND severity IN ('high','critical') AND detail NOT LIKE '[working:%' ORDER BY first_seen DESC LIMIT 20"):
        if fid not in seen_ids: needs.append(dict(kind="finding", sev=sev, check=code, name=check_name(code), title=label, why=plain_sentence(code, sev, label, detail), id=fid))
    for label, code, note in conn.execute("SELECT label, code, note FROM regrab WHERE status='pending' AND ts < ?", (now - (float(cfg("REGRAB_WAIT_DAYS", REGRAB_WAIT_DAYS)) - 1) * 86400,)):
        needs.append(dict(kind="regrab", sev="medium", check=code, title=label, why="re-grab has not produced a file; the original comes back automatically within a day"))
    uf = {}
    for (l,) in conn.execute("SELECT label FROM finding WHERE resolved_at IS NULL AND check_code IN ('MISSING_EPISODE','MISSING_MOVIE') AND severity='medium' AND first_seen < ?", (now - 14 * 86400,)):
        s_ = l.rsplit(" S", 1)[0]; uf[s_] = uf.get(s_, 0) + 1
    if uf:
        top = ", ".join(f"{s_} ({n_})" for s_, n_ in sorted(uf.items(), key=lambda kv: -kv[1])[:3])
        needs.append(dict(kind="unfound", sev="low", check="UNFOUND", title=f"{sum(uf.values())} released item(s) not found in 14+ days", why=f"your indexers have nothing: {top} - 'search all missing' on the show, or check Prowlarr"))
    t_soon = [t for t in trash_list() if t["purge_on"] <= (datetime.date.today() + datetime.timedelta(days=1)).isoformat()]
    if t_soon: needs.append(dict(kind="trash", sev="low", check="TRASH", title=f"{len(t_soon)} file(s) purge within 24 h ({sum(t['size'] for t in t_soon)/1e9:.1f} GB)", why="restore anything you still want from the Trash tab"))
    ts_ = tests_status()
    for fj in failed_jobs(conn)[:5]: needs.append(dict(kind="job", sev="medium", check="JOBFAIL", name="A job failed", title=fj["title"], why=fj["why"], job_id=fj["job_id"]))
    if not ts_["ok"]:
        fails = [(f.get("name", "") + (f" ({f['info']})" if f.get("info") else "")) if isinstance(f, dict) else str(f) for f in (ts_["selftest"].get("failed") or []) + (ts_["uibot"].get("failed") or [])]
        which = "a self-test on this server is failing" if fails else ""
        for app_, r_ in sorted(((ts_.get("demo") or {}).get("demos") or {}).items()):   # Q40 canary: say it is the public demo, not this server
            if r_.get("failed") or not r_.get("total"):
                fails.append(f"public {app_.capitalize()} demo {r_.get('passed', 0)}/{r_.get('total', 0)}: " + "; ".join(r_.get("failed") or ["did not answer"])); which = which or "the nightly check of the public demo is failing"
        bk_ = ts_.get("backup") or {}
        if bk_ and not bk_.get("ok"): fails.append(f"backup proof: {bk_.get('integrity') or 'failed'}"); which = which or "the nightly backup proof failed"
        needs.append(dict(kind="tests", sev="high" if "server" in which or "backup" in which else "medium", check="SELFTEST", title=which or "a self-test is failing",
                          why=("; ".join(fails)[:220] + ". It reruns tonight; the History tab shows every result.") if fails else "see the History tab"))
    h = health_info()
    if h["status"] not in ("ok", "running"): needs.append(dict(kind="health", sev="high", check="HEALTH", title=f"audit is {h['status']}", why=f"last run {h.get('last_run')}"))
    if h.get("degraded"): needs.append(dict(kind="health", sev="high", check="HEALTH", name="Service unreachable", title=f"{', '.join(h['degraded'])} could not be reached during the last run", why="the checks that need it were skipped, everything else ran. Check that the service is up and that its address in the container settings is right."))
    run_ = last_run_info(conn)
    try:
        sp = {k: v for k, v in conn.execute("SELECT status, sum(bytes) FROM watch WHERE status IN ('watched_cold','never_watched') GROUP BY status")}
        if sp: needs.append(dict(kind="space", sev="low", check="SPACE", title=f"{(sp.get('watched_cold') or 0)/1e9:.0f} GB watched & cold, {(sp.get('never_watched') or 0)/1e9:.0f} GB never watched", why="candidates to archive - Space tab"))
    except Exception: pass
    return dict(since=datetime.datetime.fromtimestamp(since).isoformat(timespec="minutes"), did=did, regrab_outcomes=outcomes, errors=errors, error_at=error_at, needs=needs, scores=library_scores(conn), upcoming=len(up),
                new=run_.get("new", 0), resolved=run_.get("resolved", 0), active=run_.get("active", 0), trash=run_.get("trash", {}))
def digest_text(t):
    lines = [f"Needs you: {len(t['needs'])}" if t["needs"] else "Needs you: nothing"]
    lines += [f"  - {SEV_NAMES.get(x['sev'], x['sev'])} · {x.get('name') or check_name(x['check'])}: {x['title'][:60]}" for x in t["needs"][:6]]
    if len(t["needs"]) > 6: lines.append(f"  ... and {len(t['needs'])-6} more")
    lines.append("Night did: " + (", ".join(f"{v} {k}" for k, v in sorted(t["did"].items(), key=lambda kv: -kv[1])) or "nothing to do"))
    if t["regrab_outcomes"]: lines.append("Re-grab results: " + ", ".join(f"{v} {k}" for k, v in t["regrab_outcomes"].items()))
    if t["errors"]: lines.append("Errors: " + "; ".join(t["errors"][:3]))
    if t.get("scores"): lines.append("Library health: " + " · ".join(f"{k} {v['score']:g}" for k, v in sorted(t["scores"].items())))
    lines.append(f"Findings: {t['active']} active ({t['new']} new, {t['resolved']} resolved) · trash {t['trash'].get('files', 0)} files / {t['trash'].get('gb', 0)} GB")
    return "\n".join(lines)
def ui_history(conn):
    runs = []
    for r in conn.execute("SELECT started,finished,files,probed,active,new,resolved,extra FROM run ORDER BY started DESC LIMIT 120"):
        try: ex = json.loads(r[7] or "{}")
        except Exception: ex = {}
        runs.append(dict(started=datetime.datetime.fromtimestamp(r[0]).isoformat(timespec="minutes"), duration_s=round((r[1] or r[0]) - r[0]), files=r[2], probed=r[3], active=r[4], new=r[5], resolved=r[6], **{k: ex.get(k) for k in ("by_severity", "dedupes", "regrabs", "purged", "trash_gb", "plex_refreshed")}))
    days = {}
    for ts, kind, mode, result in conn.execute("SELECT ts,kind,mode,result FROM action WHERE ts>?", (time.time() - 90 * 86400,)):
        d = datetime.date.fromtimestamp(ts).isoformat(); e = days.setdefault(d, dict(dedupe=0, regrab=0, quarantine=0, restore=0, ignore=0, fixtag=0, subsearch=0, whisper=0, verify=0, fixdefaults=0, fixheader=0, plexrefresh=0, purge=0, purge_gb=0.0))
        if mode != "on": continue
        kind = "plexrefresh" if kind == "plex-refresh" else kind
        if kind in e and not str(result or "").startswith("ERROR"): e[kind] += 1
        if kind == "purge":
            m = re.search(r"freed ([\d.]+) GB", result or ""); e["purge_gb"] += float(m.group(1)) if m else 0
    try: sh = json.loads((conn.execute("SELECT v FROM kv WHERE k='scores_history'").fetchone() or ["[]"])[0])
    except Exception: sh = []
    flakes = [dict(ts=datetime.datetime.fromtimestamp(t).isoformat(timespec="minutes"), service=s_, what=w_, error=e_) for t, s_, w_, e_ in conn.execute("SELECT ts,service,what,error FROM flake WHERE ts > ? ORDER BY ts DESC LIMIT 200", (time.time() - 30 * 86400,))]
    per = {}
    for f_ in flakes: per[f_["service"]] = per.get(f_["service"], 0) + 1
    return {"runs": runs[::-1], "days": dict(sorted(days.items())), "tests": tests_status(), "scores_history": sh, "flakes": flakes, "flaky_services": per}
def ui_settings_get():
    items = []
    for name, (kind, default, help_) in SETTABLE.items():
        src = "per-run override" if name in OVR else "saved in UI" if name in SETTINGS else "container env" if E(name) is not None else "default"
        val = setting(name); envv = E(name)
        if kind == "secret": val = ("••••" + val[-4:]) if val else ""; envv = "(set)" if envv else None
        items.append(dict(name=name, kind=kind, default=default, env=envv, saved=bool(SETTINGS.get(name)) if kind == "secret" else SETTINGS.get(name), value=val, source=src, help=help_))
    warn = "REMEDIATE=on will be downgraded to dryrun: REMEDIATE_CONFIRM is not set in the container env" if setting("REMEDIATE") == "on" and REMEDIATE_CONFIRM != "I_UNDERSTAND_FILES_WILL_BE_DELETED" else ""
    return {"items": items, "warning": warn, "run_in_progress": RUN_LOCK.locked()}
def ui_settings_post(conn, body, actor):
    changes = body.get("set") or {}; reset = body.get("reset") or []
    if reset == "all": reset = list(SETTINGS)
    bad = [k for k in list(changes) + list(reset) if k not in SETTABLE]
    if bad: return 400, {"error": f"not settable: {bad}"}
    new = dict(SETTINGS)
    try:
        for k, v in changes.items():
            if SETTABLE[k][0] == "secret" and str(v).startswith("••••"): continue   # the masked value came back untouched
            v = validate_setting(k, v)
            if v == (E(k) if E(k) is not None else SETTABLE[k][1]): new.pop(k, None)   # same as env/default -> no override needed
            else: new[k] = v
    except ValueError as e: return 400, {"error": str(e)}
    for k in reset: new.pop(k, None)
    conn.execute("INSERT OR REPLACE INTO kv VALUES('settings',?)", (json.dumps(new),)); conn.commit(); load_settings(conn)
    mask = lambda k, v: ("••••" + str(v)[-4:] if v else v) if SETTABLE[k][0] == "secret" else v   # secrets never leave the database in clear
    diff = {k: mask(k, new.get(k, "(env/default)")) for k in set(changes) | set(reset)}
    record_action(conn, "on", "settings", "ui", "kv", f"{len(diff)} setting(s)", f"changed by {actor}", json.dumps(diff)[:300]); refresh_reports(conn)
    return 200, {"ok": True, "saved": {k: mask(k, v) for k, v in new.items()}, "applies": "next run (and immediately for trash retention / Plex / notifications)"}
START_TS = time.time()
SANDBOX = E("SANDBOX", "0") == "1"   # U10 playground: fake services, fake files, anyone may click; connections, hooks, passwords and updates stay locked
SANDBOX_NOTE = {"error": "this is the playground: its connections are fixed and it resets on the hour - install Auditarr to point it at your own apps"}
_RATE = {}   # sandbox: per-address write budget
def sandbox_rate_ok(ip, limit=120):
    now = time.time(); hits = [t for t in _RATE.get(ip, []) if now - t < 60]; hits.append(now); _RATE[ip] = hits; return len(hits) <= limit
APP_VERSION = E("APP_VERSION", "dev"); APP_REVISION = E("APP_REVISION", ""); APP_REPO = "chrizantoz/auditarr"
LATEST = {"ts": 0.0, "tag": "", "url": "", "name": "", "error": "", "releases": []}
def latest_release(fetch=True, refresh=False):
    """Newest GitHub releases (cached 6 h; pre-releases count, there may be nothing else yet). fetch=False never touches the network (page loads); UPDATE_CHECK=0 disables the check entirely."""
    if E("UPDATE_CHECK", "1") != "1": return LATEST
    if not fetch or (not refresh and LATEST["ts"] and time.time() - LATEST["ts"] < 6 * 3600): return LATEST
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{APP_REPO}/releases?per_page=6", headers={"User-Agent": "auditarr", "Accept": "application/vnd.github+json"})
        rel = [r for r in json.load(urllib.request.urlopen(req, timeout=8)) if not r.get("draft")]
        rels = [dict(tag=r.get("tag_name") or "", name=r.get("name") or r.get("tag_name") or "", url=r.get("html_url") or "", published=(r.get("published_at") or "")[:10], prerelease=bool(r.get("prerelease")), summary=((r.get("body") or "").strip().splitlines() or [""])[0][:140]) for r in rel]
        top = rels[0] if rels else {}; LATEST.update(ts=time.time(), tag=top.get("tag", ""), url=top.get("url", ""), name=top.get("name", ""), error="", releases=rels)
    except Exception as e: LATEST.update(ts=time.time(), error=str(e)[:120])
    return LATEST
def _vtuple(v):
    n = re.findall(r"\d+", v or ""); return tuple(int(x) for x in n[:3]) if n else None
def about_info(fetch=False, refresh=False, conn=None):
    """Settings → About: what is running, where, for how long, library totals, health messages, links, and whether a newer release exists."""
    import platform, shutil
    lr = latest_release(fetch, refresh); cur, new = _vtuple(APP_VERSION), _vtuple(lr["tag"])
    c_ = conn or db(); stats = {}
    try:
        ri = last_run_info(c_); stats = dict(files=ri.get("files", 0), open_findings=c_.execute("SELECT count(*) FROM finding WHERE resolved_at IS NULL").fetchone()[0], runs=c_.execute("SELECT count(*) FROM run").fetchone()[0],
                                             actions=c_.execute("SELECT count(*) FROM action").fetchone()[0], last_run=ri.get("finished", ""))
    except Exception: pass
    finally:
        if conn is None: c_.close()
    h = health_info(); msgs = []
    if cur and new and new > cur: msgs.append(dict(level="warn", text=f"A new version is available: {lr['tag']} (you run {APP_VERSION})", link=lr["url"]))
    for d in h.get("degraded") or []: msgs.append(dict(level="error", text=f"{d} could not be reached during the last run", link=""))
    if h.get("status") == "stale": msgs.append(dict(level="warn", text="The last audit is older than expected - the nightly run may not be happening", link=""))
    if not (h.get("tests") or {}).get("ok", True): msgs.append(dict(level="warn", text="The built-in self-test did not pass last time (History tab)", link=""))
    dm = ((h.get("tests") or {}).get("demo") or {}).get("demos") or {}
    if dm:
        bad = {k: v for k, v in dm.items() if v.get("failed") or not v.get("total")}
        if bad: msgs.append(dict(level="warn", text="The public demo did not pass last night's check: " + "; ".join(f"{k} {v.get('passed', 0)}/{v.get('total', 0)} ({', '.join(v.get('failed') or [])[:160]})" for k, v in bad.items()) + " - visitors may be seeing a broken page (DNS, tunnel, proxy or the container)."))
        else: msgs.append(dict(level="ok", text="Public demos checked through their public addresses at " + str(h["tests"]["demo"].get("ts", ""))[:16].replace("T", " ") + ": " + ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in dm.items()) + "."))
    if (h.get("tests") or {}).get("uibot_drift"): msgs.append(dict(level="warn", text=f"The nightly page check used a UI bot from another build ({h['tests']['uibot_drift']}) - pull both images together so the check matches this version", link=""))
    errs = errors_recent()
    if errs: msgs.append(dict(level="warn", text=f"{max(h.get('errors_24h', 0), len(errs))} error(s) in the last 24 hours - see Recent errors below", link=""))
    elif h.get("errors_24h"): msgs.append(dict(level="warn", text=f"{h['errors_24h']} failed action(s) in the last 24 hours - the Actions tab lists them; from this version on the exact cause is kept too", link=""))
    else: msgs.append(dict(level="ok", text="No errors in the last 24 hours", link=""))
    for t in ("ffmpeg", "ffprobe", "mkvpropedit"):
        if not shutil.which(t): msgs.append(dict(level="error", text=f"{t} is missing from the container - checks that need it are skipped", link=""))
    links = dict(forum="https://forums.unraid.net/topic/201247-support-auditarr-nightly-quality-control-for-your-sonarr-radarr-bazarr-plex-library/", demo="https://auditarr-demo.chrizantoz.com", docs=f"https://github.com/{APP_REPO}/blob/main/docs/USER-GUIDE.md", discussions=f"https://github.com/{APP_REPO}/discussions", issues=f"https://github.com/{APP_REPO}/issues",
                 source=f"https://github.com/{APP_REPO}", sponsors="https://github.com/sponsors/chrizantoz", kofi="https://ko-fi.com/chrizantoz")
    return dict(version=APP_VERSION, stats=stats, health=msgs, errors=errs, links=links, releases=lr.get("releases", []), revision=(APP_REVISION or "")[:12], dev_build=cur is None, docker=os.path.exists("/.dockerenv"), python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                db_mb=round((os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0) / 1e6, 1), config_dir=CONFIG_DIR, media_dir="/data" if os.path.isdir("/data") else "", timezone=E("TZ", "UTC"),
                uptime_s=int(time.time() - START_TS), tools={t: bool(shutil.which(t)) for t in ("ffmpeg", "ffprobe", "mkvpropedit", "mkvmerge")},
                latest=dict(tag=lr["tag"], url=lr["url"], name=lr["name"], checked=datetime.datetime.fromtimestamp(lr["ts"]).isoformat(timespec="minutes") if lr["ts"] else "", error=lr["error"]),
                update_available=bool(cur and new and new > cur), update_hook=bool(E("UPDATE_HOOK_URL")), check_enabled=E("UPDATE_CHECK", "1") == "1", license="GPL-3.0", repo=f"https://github.com/{APP_REPO}")
def ui_update(conn, actor):
    """POST /api/update: ask the host to update this container through a webhook (Watchtower's HTTP API or anything that pulls and recreates). The app cannot replace itself from inside."""
    url = E("UPDATE_HOOK_URL")
    if not url: return 400, {"error": "no update hook configured - update from the host (Unraid Docker tab, docker compose pull, or the deploy script); see the About tab"}
    if READ_ONLY: return 409, {"error": "read-only copy"}
    hdr = {"User-Agent": "auditarr"}; tok = E("UPDATE_HOOK_TOKEN")
    if tok: hdr["Authorization"] = "Bearer " + tok
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST", headers=hdr), timeout=30) as r: code_ = r.status
    except urllib.error.HTTPError as e: code_ = e.code
    except Exception as e: record_action(conn, "on", "update", "ui", "", "Auditarr", f"update requested by {actor}", f"hook failed: {e!r}"[:160]); return 502, {"error": f"the update hook did not answer: {e!r}"[:160]}
    record_action(conn, "on", "update", "ui", "", "Auditarr", f"update requested by {actor}", f"hook answered HTTP {code_}")
    return 200, {"ok": True, "note": "Update requested. The host pulls the new image and restarts Auditarr; this page comes back by itself in a minute or two."}
def run_selftest():
    """Run tests/selftest.py against ourselves (reversible API + detector sampling); results land in reports/selftest.json."""
    try:
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "selftest.py")], capture_output=True, text=True, timeout=1800,
                       env={**os.environ, "SELFTEST_BASE": f"http://127.0.0.1:{HTTP_PORT}", "CONFIG_DIR": CONFIG_DIR})
        r = tests_status(); log(f"selftest: {r['selftest'].get('passed')}/{r['selftest'].get('total')} passed" + (f" - FAILED: {r['selftest'].get('failed')}" if r['selftest'].get('failed') else ""))
        if r["selftest"].get("failed"): ntfy("media-audit self-test FAILED", "\n".join(f"- {f['name']}: {f['info']}" for f in r["selftest"]["failed"][:8]), "high", "x")
    except Exception as e: log("selftest:", repr(e))
def tests_status():
    out = {}
    for name in ("selftest", "uibot"):
        try: out[name] = json.load(open(os.path.join(REPORT_DIR, name + ".json")))
        except Exception: out[name] = {}
    hist = []
    try: hist = [json.loads(l) for l in open(os.path.join(REPORT_DIR, "selftest-history.jsonl")).read().splitlines()[-30:] if l.strip()]
    except Exception: pass
    out["history"] = hist; out["ok"] = all(not v.get("failed") and v.get("total") for v in (out["selftest"], out["uibot"]) if v) or (not out["selftest"] and not out["uibot"])
    br, pr_ = str((out["uibot"] or {}).get("revision") or ""), str((out["uibot"] or {}).get("page_revision") or APP_REVISION or "")   # Q27: the bot and the page it tested must be the same build
    out["uibot_drift"] = f"bot build {br[:7]} checked page build {pr_[:7]}" if br and pr_ and br[:7] != pr_[:7] else ""
    try: out["demo"] = json.load(open(os.path.join(REPORT_DIR, "demo-canary.json")))   # Q40: the public demos, checked nightly through their public addresses
    except Exception: out["demo"] = {}
    if any(v.get("failed") or not v.get("total") for v in (out["demo"].get("demos") or {}).values()): out["ok"] = False
    try:
        c_ = sqlite3.connect(DB_PATH, timeout=10); r_ = c_.execute("SELECT v FROM kv WHERE k='backup_last'").fetchone(); c_.close(); out["backup"] = json.loads(r_[0]) if r_ else {}
        if out["backup"] and not out["backup"].get("ok"): out["ok"] = False
    except Exception: out["backup"] = {}
    return out
_WL = {"ts": 0, "data": []}; GPU_PROGRESS = {}   # container -> {"line": ..., "ts": ...} posted by remote Whisper hosts (/api/whisper_progress)
def _parse_progress(c, lines):
    ip = next((i for i in range(len(lines) - 1, -1, -1) if re.search(r"\]\s+\d+% \|", lines[i])), None)
    ifin = next((i for i in range(len(lines) - 1, -1, -1) if "WORKER FINISH" in lines[i] or "Model unloaded" in lines[i]), None)
    if ip is not None and (ifin is None or ip > ifin):
        m = re.search(r"\[ (.+?)\s*\]\s+(\d+)% \|\s*(\d+)/(\d+) s \[(\d+:\d+)<(\d+:\d+)[^\]]*\].*?Jobs: (\d+) processing, (\d+) queued", lines[ip])
        if m: return dict(container=c, title=m.group(1).strip(), pct=int(m.group(2)), done_s=int(m.group(3)), total_s=int(m.group(4)), elapsed=m.group(5), eta=m.group(6), processing=int(m.group(7)), queued=int(m.group(8)))
        return dict(container=c, title="transcribing", pct=None)
    m2 = re.search(r"Jobs: (\d+) processing, (\d+) queued", lines[-1] if lines else "")
    if m2 and int(m2.group(2)): return dict(container=c, title="(starting next)", pct=None, processing=int(m2.group(1)), queued=int(m2.group(2)))
    return None
def whisper_live():
    """What the Whisper containers are doing right now, parsed from their last log lines (via the socket proxy). Cached 5 s."""
    if time.time() - _WL["ts"] < 5: return _WL["data"]
    out = []
    for c in WHISPER_CONTAINERS:
        try:
            raw = urllib.request.urlopen(f"{DOCKER_API}/containers/{c}/logs?stdout=1&stderr=1&tail=60", timeout=4).read()
            txt = re.sub(rb"[\x00-\x02]\x00\x00\x00.{4}", b"", raw, flags=re.S).decode("utf-8", "replace")   # strip docker stream frame headers
            r_ = _parse_progress(c, [l for l in txt.splitlines() if l.strip()])
            if r_: out.append(r_)
        except Exception as e: out.append(dict(container=c, error=str(e)[:80]))
    for c, rec in list(GPU_PROGRESS.items()):   # remote hosts (the desktop GPU) push their last log lines; stale after 90 s
        if time.time() - rec["ts"] > 90: continue
        r_ = _parse_progress(c, rec["lines"])
        if r_: out.append(r_)
    for r_ in out: r_["engine"] = ENGINE_NAMES.get(r_.get("container"), r_.get("container"))
    gpu_ok = False
    if WHISPER_GPU_URL:
        try: urllib.request.urlopen(f"{WHISPER_GPU_URL.rstrip('/')}/status", timeout=2).read(); gpu_ok = True
        except Exception: gpu_ok = False
    _WL["engine"] = dict(gpu_online=gpu_ok, gpu_name=WHISPER_GPU_NAME, cpu_name=WHISPER_CPU_NAME, active=WHISPER_GPU_NAME if gpu_ok else WHISPER_CPU_NAME,
                         note="desktop GPU online - new AI-subtitle jobs go there" if gpu_ok else "desktop offline - jobs fall back to shinra's CPU (about 5x slower)")
    _WL.update(ts=time.time(), data=out); return out
def health_info():
    """ok | running | stale | failed. Stale = last finished run older than STALE_HOURS (fresh containers get STALE_HOURS of grace)."""
    last = None
    try:
        c = sqlite3.connect(DB_PATH, timeout=10); row = c.execute("SELECT max(finished) FROM run").fetchone(); c.close(); last = row[0] if row else None
    except Exception: pass
    age_h = (time.time() - last) / 3600 if last else None
    if RUN_LOCK.locked(): st = "running"
    elif PROGRESS.get("phase") == "failed": st = "failed"
    elif age_h is None: st = "ok" if (time.time() - START_TS) / 3600 < STALE_HOURS else "stale"
    else: st = "stale" if age_h > STALE_HOURS else "ok"
    ts_ = tests_status(); err24 = 0
    try:
        c = sqlite3.connect(DB_PATH, timeout=10); err24 = c.execute("SELECT count(*) FROM action WHERE result LIKE 'ERROR%' AND ts > ?", (time.time() - 86400,)).fetchone()[0]; c.close()
    except Exception: pass
    return dict(status=st, errors_24h=err24 + len([e for e in ERRORS if e["ts"] > time.time() - 86400]), read_only=READ_ONLY, write_attempts=len(WRITE_LEDGER), write_log=[w for _, w in WRITE_LEDGER[-10:]], degraded=degraded(), busy=INFLIGHT["n"] + (1 if RUN_LOCK.locked() else 0), deps={k: dict(ok=v["ok"], error=v["error"]) for k, v in DEPS.items()},
                last_run=datetime.datetime.fromtimestamp(last).isoformat(timespec="seconds") if last else None, age_hours=round(age_h, 1) if age_h is not None else None,
                stale_after_hours=STALE_HOURS, phase=PROGRESS.get("phase"), sub_queue=SUB_Q.qsize(),
                tests=dict(ok=ts_["ok"], uibot_drift=ts_.get("uibot_drift", ""), selftest=f"{ts_['selftest'].get('passed', '?')}/{ts_['selftest'].get('total', '?')} @ {str(ts_['selftest'].get('ts', ''))[:16]}" if ts_["selftest"] else "never", uibot=f"{ts_['uibot'].get('passed', '?')}/{ts_['uibot'].get('total', '?')} @ {str(ts_['uibot'].get('ts', ''))[:16]}" if ts_["uibot"] else "never"))

# ---------- daemon ----------
LIVE_HTML = r"""<!doctype html><meta charset=utf-8><title>media-audit</title>
<style>body{font:14px system-ui;margin:24px;background:#111;color:#ddd}a{color:#9cf}.bar{background:#333;height:18px;border-radius:9px;overflow:hidden;margin:8px 0}.bar div{background:#4a8;height:100%;transition:width .5s}
pre{background:#1a1a1a;padding:10px;border-radius:6px;white-space:pre-wrap}.critical{color:#f66}.high{color:#fa5}.medium{color:#fd6}.low{color:#9c9}.dryrun{color:#9cf}.on{color:#fa5}</style>
<h1>media-audit</h1><p id=st>loading...</p><div class=bar><div id=b style="width:0%"></div></div><p id=det></p>
<h3>New findings this run</h3><pre id=rec></pre><h3>Actions this run</h3><pre id=act></pre>
<p><a href=latest.html>Full report (latest run)</a> - <a href=latest.json>json</a> - <a href=latest.md>markdown</a> - <a href=history/>history</a> - <a href="#" onclick="fetch('/run');this.textContent='run requested';return false">run now</a></p>
<script>
const esc=s=>s.replace(/</g,'&lt;');
async function tick(){try{const r=await fetch('progress.json?'+Date.now());const p=await r.json();
document.getElementById('st').textContent=`phase: ${p.phase}  (started ${p.started||'-'}, updated ${p.updated||'-'})`;
const pct=p.total?Math.round(100*p.done/p.total):(p.phase==='done'?100:0);document.getElementById('b').style.width=pct+'%';
document.getElementById('det').textContent=(p.total?`${p.done} / ${p.total}  (${pct}%) - `:'')+(p.detail||'');
document.getElementById('rec').innerHTML=(p.recent||[]).map(l=>{const m=l.match(/^\[(\w+)\]/);return `<span class="${m?m[1]:''}">${esc(l)}</span>`}).join('\n')||'(none yet)';
document.getElementById('act').innerHTML=(p.actions||[]).map(l=>{const m=l.match(/^\[(\w+)\]/);return `<span class="${m?m[1]:''}">${esc(l)}</span>`}).join('\n')||'(none)';
}catch(e){document.getElementById('st').textContent='no run yet - progress.json missing';}}
tick();setInterval(tick,3000);</script>"""
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=REPORT_DIR, **k)
    def _json(self, code, obj):
        body = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _actor(self):
        """Writes are allowed from the LAN / tailnet / the docker host itself, or through NPM when Authelia has set Remote-User."""
        ip = self.client_address[0]; user = self.headers.get("Remote-User") or self.headers.get("X-Remote-User")
        if SANDBOX: return f"visitor:{(self.headers.get('X-Forwarded-For') or ip).split(',')[0].strip()}"   # the playground: anyone may click; its locks and write budget do the guarding
        if ip.startswith(("192.168.", "10.", "100.", "127.")) or ip == "::1" or ip == GATEWAY_IP: return user or f"lan:{ip}"
        if user: return f"{user}@{ip}"
        return None
    def _auth(self):
        """(allowed, user, role). Open page when no password is set (LAN model); otherwise a session cookie, the container itself, or (if TRUST_PROXY_AUTH) a proxy header."""
        conf = auth_conf()
        if not conf: return True, None, "admin"
        ip = self.client_address[0]
        u = session_user(conf, self.headers.get("Cookie"))
        if u: return True, u[0], u[1]
        if ip in ("127.0.0.1", "::1"): return True, "local", "admin"
        if direct_local(ip, self.headers) and local_no_login(): return True, "lan", "admin"
        if TRUST_PROXY_AUTH and (self.headers.get("Remote-User") or self.headers.get("X-Remote-User")): return True, self.headers.get("Remote-User") or self.headers.get("X-Remote-User"), "admin"
        return False, None, None
    def _login_page(self, bad=False):
        body = LOGIN_HTML.replace("__ERR__", '<p class="err">Wrong user or password.</p>' if bad else "").encode()
        self.send_response(401 if bad else 200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"): self.path = "/ui.html"
        if p == "/health":
            h = health_info(); return self._json(200 if h["status"] in ("ok", "running") else 503, h)
        if p == "/login": return self._login_page("bad" in self.path)
        ok_, _u, role_ = self._auth()
        if not ok_: return self._json(401, {"error": "sign in first"}) if p.startswith("/api/") or p.endswith((".json", ".md")) else self._login_page()
        if p == "/api/state":
            conn = db()
            try: j_ = ui_state(conn); j_.setdefault("auth", {})["role"] = role_; j_["auth"]["via"] = _u if _u in ("lan", "local") else ""; return self._json(200, j_)
            finally: conn.close()
        if p == "/api/history":
            conn = db()
            try: return self._json(200, ui_history(conn))
            finally: conn.close()
        if p == "/api/settings": return self._json(200, ui_settings_get())
        if p == "/api/setup":
            c_ = db()
            try: return self._json(200, ui_setup(c_))
            finally: c_.close()
        if p == "/api/hooks":
            c_ = db()
            try: return self._json(200, {"hooks": hook_status(c_)})
            finally: c_.close()
        if p == "/api/plan":
            c_ = db()
            try: return self._json(200, plan_tonight(c_))
            finally: c_.close()
        if p == "/api/discover": return self._json(403, SANDBOX_NOTE) if SANDBOX else self._json(200, discover_services())
        if p == "/api/frame":   # one frame from the lower part of the picture, at the moment the painted text was seen (F32)
            m_ = re.search(r"[?&]id=([0-9a-f]{8,20})", self.path); c_ = db()
            try: row = c_.execute("SELECT path, detail FROM finding WHERE id=?", (m_.group(1) if m_ else "",)).fetchone()
            finally: c_.close()
            if not row or not row[0] or not os.path.exists(row[0]): return self._json(404, {"error": "no file for that row"})
            t_ = re.search(r"seen at (\d+)s", row[1] or ""); at = t_.group(1) if t_ else "60"
            jpg = subprocess.run(["ffmpeg", "-v", "error", "-ss", at, "-i", row[0], "-frames:v", "1", "-vf", "crop=iw:ih*0.34:0:ih*0.66,scale=min(960\\,iw):-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"], capture_output=True, timeout=60).stdout
            if not jpg: return self._json(500, {"error": "could not read a frame from the file"})
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(jpg))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(jpg); return
        if p == "/api/errors":
            m_ = re.search(r"[?&]hours=(\d+)", self.path); return self._json(200, dict(errors=errors_recent(hours=int(m_.group(1)) if m_ else 24)))
        if p == "/api/about":
            c_ = db()
            try: return self._json(200, about_info(fetch="refresh=1" in self.path, refresh="refresh=1" in self.path, conn=c_))
            finally: c_.close()
        if p == "/api/selftest":
            threading.Thread(target=run_selftest, daemon=True).start(); return self._json(202, {"ok": True, "note": "self-test started; results in the header chip / History tab in ~1-2 min"})
        if p == "/api/trash": return self._json(200, {"items": trash_list(), "keep": TRASH_KEEP})
        if p == "/run": return self._run()
        return super().do_GET()
    def _run(self):
        from urllib.parse import urlparse, parse_qs
        q = {k.upper(): v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        bad = [k for k in q if k not in OVERRIDABLE]
        if q and not ALLOW_RUN_OVERRIDES: return self._json(403, {"error": "overrides disabled"})
        if bad: return self._json(400, {"error": f"not overridable: {bad}", "allowed": sorted(OVERRIDABLE)})
        if RUN_LOCK.locked(): return self._json(409, {"error": "run already in progress"})
        if q and not self._actor(): return self._json(403, {"error": "not allowed from here"})
        OVR.clear(); OVR.update(q); log("run requested via /run with overrides", q, "by", self._actor())
        threading.Thread(target=guarded_run, daemon=True).start(); return self._json(202, {"ok": True, "overrides": q})
    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/webhook/"):
            from urllib.parse import urlparse, parse_qs
            tok = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]; conn = db()
            try: ok_tok = tok and tok == hook_token(conn)
            finally: conn.close()
            if not ok_tok: return self._json(403, {"error": "bad or missing token - connect the hook from the Setup page"})
            try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except Exception: return self._json(400, {"error": "bad json"})
            app = p.split("/")[2] if len(p.split("/")) > 2 else "sonarr"; ev = body.get("eventType") or ""
            if ev == "Test": return self._json(200, {"ok": True, "note": "Auditarr hears you"})
            if ev not in ("Download", "Upgrade"): return self._json(200, {"ok": True, "ignored": ev})
            pid = (body.get("series") or body.get("movie") or {}).get("id"); path = (body.get("episodeFile") or body.get("movieFile") or {}).get("path")
            if not pid: return self._json(400, {"error": "no series/movie id in the payload"})
            threading.Thread(target=audit_one, args=(app, pid, path, ev.lower()), daemon=True).start()
            return self._json(202, {"ok": True, "note": "checking the new file in a few seconds"})
        if p == "/login":
            from urllib.parse import parse_qs
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")); conf = auth_conf()
            user = (form.get("user") or [""])[0].strip(); pw = (form.get("password") or [""])[0]
            # the role written into the cookie comes from the username only; the password check is just the gate (CodeQL #9)
            role_ = account_role(conf, user)
            if role_ and check_password(conf, user, pw) == role_:
                secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
                self.send_response(303); self.send_header("Set-Cookie", f"auditarr_s={session_cookie(conf, role=role_)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}"); self.send_header("Location", "/"); self.end_headers(); return
            time.sleep(1.5); log(f"login failed for {user!r} from {self.client_address[0]}"); return self._login_page(bad=True)
        ok_, auth_user, role_ = self._auth()
        if not ok_: return self._json(401, {"error": "sign in first"})
        if role_ == "viewer" and p != "/api/logout": return self._json(403, {"error": "this account can look but not change anything"})
        if p == "/api/logout":
            self.send_response(200); self.send_header("Set-Cookie", "auditarr_s=; Path=/; Max-Age=0"); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b'{"ok": true}'); return
        actor = (auth_user if auth_user not in (None, "local") else None) or self._actor()
        if not actor: return self._json(403, {"error": "writes allowed only from the LAN/tailnet or through the Authelia-protected hostname"})
        if SANDBOX and not sandbox_rate_ok(self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()): return self._json(429, {"error": "slow down - the playground allows 120 changes a minute per visitor"})
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception: return self._json(400, {"error": "bad json"})
        if p == "/run" or p == "/api/run": return self._run()
        conn = db()
        try:
            INFLIGHT["n"] += 1
            if p == "/api/ignore": code, obj = ui_ignore(conn, body, actor)
            elif p == "/api/act": code, obj = ui_act(conn, body, actor)
            elif p == "/api/act_bulk":
                ids = body.get("ids") or []; ok = 0; errs = []
                if len(ids) > 500: code, obj = 400, {"error": "max 500 per call"}
                else:
                    rescanned = set()
                    for i in ids:
                        fr = finding_row(conn, i) or {}
                        if body.get("action") == "quarantine" and is_gap(fr): errs.append({"id": i, "error": "refused: inside a folder an arr lists - rescan in the arr, then decide one file at a time"}); continue
                        if body.get("action") == "arrrescan":
                            hit = (re.search(r"\((/.*?)\) but ", fr.get("detail") or "") or [None, fr.get("path")])[1]
                            if hit in rescanned: ok += 1; continue
                            rescanned.add(hit)
                        c, o = ui_act(conn, {"id": i, "action": body.get("action"), "lang": body.get("lang")}, actor)
                        if c == 200 and o.get("already_gone"): gone = locals().get("gone", 0) + 1
                        elif c == 200: ok += 1
                        else: errs.append({"id": i, "error": o.get("error")})
                    code, obj = 200, {"ok": True, "done": ok, "already_gone": locals().get("gone", 0), "failed": errs[:20], "failed_count": len(errs)}
            elif p == "/api/restore": code, obj = ui_restore(conn, body, actor)
            elif p == "/api/purge": code, obj = ui_purge(conn, body, actor)
            elif p == "/api/settings":
                locked = [k for k in list((body.get("set") or {})) + ([] if (body.get("reset") or []) == "all" else list(body.get("reset") or [])) if k in SETTABLE and SETTABLE[k][0] in ("url", "secret")]
                code, obj = (403, SANDBOX_NOTE) if SANDBOX and (locked or body.get("reset") == "all") else ui_settings_post(conn, body, actor)
            elif p == "/api/whisper": code, obj = ui_whisper(conn, body, actor)
            elif p == "/api/subsync": code, obj = ui_subsync(conn, body, actor)
            elif p == "/api/retry_job": code, obj = retry_job(conn, int(body.get("id") or 0), actor)
            elif p == "/api/setpassword" and SANDBOX: code, obj = 403, SANDBOX_NOTE
            elif p == "/api/watch_priority":   # run the watch-priority pass now (tests, curl); the daemon does it every 5 minutes on its own
                s_, w_ = watch_priority_pass(conn); code, obj = 200, {"ok": True, "searches": s_, "ai_subtitles": w_, "note": f"{s_} Bazarr search(es) and {w_} AI-subtitle job(s) queued for what people are watching"}
            elif p == "/api/search_show": code, obj = ui_search_show(conn, body, actor)
            elif p == "/api/hook_setup": code, obj = (403, SANDBOX_NOTE) if SANDBOX else ensure_hook(conn, str(body.get("app") or ""))
            elif p == "/api/update": code, obj = (403, SANDBOX_NOTE) if SANDBOX else ui_update(conn, actor)
            elif p == "/api/local_login":
                on_ = bool(body.get("on"))
                if LOCAL_NO_LOGIN_ENV in ("0", "1"): code, obj = 409, {"error": f"LOCAL_NO_LOGIN={LOCAL_NO_LOGIN_ENV} is set on the container; change it there"}
                else:
                    conn.execute("INSERT OR REPLACE INTO kv VALUES('local_no_login',?)", ("1" if on_ else "0",)); conn.commit()
                    record_action(conn, "on", "settings", "ui", "auth", "local_login", f"set by {actor}", "no sign-in on the home network" if on_ else "sign-in everywhere")
                    code, obj = 200, {"ok": True, "note": "On your own network the page opens without a sign-in; through your domain it still asks." if on_ else "The page asks for a sign-in everywhere, your own network too."}
            elif p == "/api/setpassword":
                conf = auth_conf(conn)
                if conf and not (auth_user and auth_user not in ("local", "lan")) and check_password(conf, str(body.get("current_user") or conf["user"]), str(body.get("current") or "")) != "admin": code, obj = 403, {"error": "the current password is needed to change it"}
                else:
                    role_set = "viewer" if body.get("role") == "viewer" else "admin"
                    try:
                        save_account(conn, str(body.get("user") or ("viewer" if role_set == "viewer" else "admin")), str(body.get("password") or ""), role=role_set)
                        record_action(conn, "on", "settings", "ui", "auth", "login", f"{role_set} password set by {actor}", "page requires sign-in" if role_set == "admin" else ("viewer account set" if body.get("password") else "viewer account removed"))
                        code, obj = 200, {"ok": True, "note": "From now on the page asks for a sign-in. You are signed in on this browser after the next page load." if role_set == "admin" else ("Viewer account ready: that login sees everything and can change nothing." if body.get("password") else "Viewer account removed.")}
                    except ValueError as e: code, obj = 400, {"error": str(e)}
            elif p == "/api/test_connection":
                from urllib.parse import urlparse as _up
                known = {_up(u).netloc for u in [conn_value(f"{k}_URL") or "" for k in ("SONARR", "RADARR", "WHISPARR", "BAZARR")] + [PLEX_URL or ""] if u}
                k_ = str(body.get("kind") or ""); key_ = body.get("key") or ""
                if SANDBOX and _up(str(body.get("url") or "")).netloc not in known: code, obj = 403, SANDBOX_NOTE   # the playground tests only its own fake hosts
                else:
                    if str(key_).startswith("••••") or not key_: key_ = conn_value(f"{k_.upper()}_APIKEY" if k_ != "plex" else "PLEX_TOKEN") or ((arr_apps().get(k_) or (None, None))[1] if k_ in ARR_KINDS else bazarr_conf()[1] if k_ == "bazarr" else plex_token())
                    code, obj = 200, test_connection(k_, body.get("url") or "", key_)
            elif p == "/api/archive": code, obj = ui_archive(conn, body, actor)
            elif p == "/api/whisper_progress":
                GPU_PROGRESS[str(body.get("container") or "remote")[:40]] = {"lines": [str(l)[:400] for l in (body.get("lines") or [])][-10:], "ts": time.time()}; _WL["ts"] = 0; code, obj = 200, {"ok": True}
            else: code, obj = 404, {"error": "unknown endpoint"}
        except urllib.error.HTTPError as e:
            code, obj = 502, {"error": f"the arr answered HTTP {e.code} for {e.url.split('/api/v3/')[-1][:60]} - the file/episode may have changed since the last audit; run the audit again"}
            try: record_action(conn, "on", "ui-error", "api", p, body.get("id") or body.get("dest") or "", f"{body.get('action') or p} by {actor}", obj["error"])
            except Exception: pass
        except Exception as e:
            import traceback; traceback.print_exc(); code, obj = 500, {"error": repr(e)}
        finally: conn.close(); INFLIGHT["n"] = max(0, INFLIGHT["n"] - 1)
        return self._json(code, obj)
    def log_message(self, *a): pass
LAST_RUN = None; RUN_LOCK = threading.Lock(); SCHEDULED_RUN = {"flag": False}
def _gateway_ip():
    """The container's default gateway = the docker host; host-local requests (docker-proxy) arrive from it."""
    try:
        for line in open("/proc/net/route").readlines()[1:]:
            f = line.split()
            if f[1] == "00000000": return ".".join(str(int(f[2][i:i+2], 16)) for i in (6, 4, 2, 0))
    except Exception: pass
    return ""
GATEWAY_IP = _gateway_ip()
def guarded_run():
    global LAST_RUN
    if not RUN_LOCK.acquire(blocking=False): log("run already in progress"); return
    try: run_once(); LAST_RUN = datetime.datetime.now().isoformat(timespec="seconds")
    except Exception as e:
        note_error("run failed:", repr(e)); progress("failed", repr(e))
    finally: RUN_LOCK.release()
def daemon():
    os.makedirs(REPORT_DIR, exist_ok=True); open(os.path.join(REPORT_DIR, "live.html"), "w").write(LIVE_HTML)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
    if os.path.exists(src):
        import shutil; shutil.copy(src, os.path.join(REPORT_DIR, "ui.html"))
    try:
        c0 = db(); load_settings(c0); reconcile_disk_findings(c0); c0.close()
    except Exception as e: log("startup:", repr(e))
    if SETTINGS: log("UI-saved settings:", SETTINGS)
    if E("AUTH_RESET") == "1" and E("AUTH_USER") and E("AUTH_PASSWORD"):
        c1 = db(); save_account(c1, E("AUTH_USER"), E("AUTH_PASSWORD")); c1.close(); log("AUTH_RESET: password replaced from env - remove AUTH_RESET now")
    try: requeue_jobs()
    except Exception as e: log("requeue:", repr(e))
    for q_ in (SUB_Q, VERIFY_Q, WHISPER_Q, FOLLOW_Q): threading.Thread(target=sub_worker, args=(q_,), daemon=True).start()
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever(), daemon=True).start()
    log(f"http on :{HTTP_PORT}; daily run at {RUN_AT} ({E('TZ','UTC')}); GET /run to start one now")
    if E("RUN_ON_START", "1") == "1": threading.Thread(target=guarded_run, daemon=True).start()
    def _update_checker():
        while True:
            try: latest_release(True)
            except Exception: pass
            time.sleep(86400)
    threading.Thread(target=_update_checker, daemon=True).start()
    def _watch_loop():   # F31: subtitles first for what people are watching
        time.sleep(90)
        while True:
            try:
                c_ = db()
                try: watch_priority_pass(c_)
                finally: c_.close()
            except Exception as e: note_error("watch priority:", repr(e))
            time.sleep(300)
    threading.Thread(target=_watch_loop, daemon=True).start()
    hh, mm = [int(x) for x in RUN_AT.split(":")]
    while True:
        now = datetime.datetime.now(); nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now: nxt += datetime.timedelta(days=1)
        time.sleep((nxt - now).total_seconds()); SCHEDULED_RUN["flag"] = True; guarded_run()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--daemon", action="store_true"); a = ap.parse_args()
    daemon() if a.daemon else run_once()
