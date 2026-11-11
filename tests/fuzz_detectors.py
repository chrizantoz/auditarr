"""Fuzz the detectors (Q12): thousands of random but plausible ffprobe results and arr records go through audit.check()
and audit.plain_sentence(). Invariants: no exception, only known check codes, severities from the fixed set, a sentence for every
finding, sentences free of raw codes. Deterministic seed so a failure is reproducible; pass a different seed to explore.
Run: python tests/fuzz_detectors.py [iterations=3000] [seed=7]"""
import os, random, re, sys, tempfile, traceback
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp()); os.environ.setdefault("REPORT_DIR", os.environ["CONFIG_DIR"])
sys.path.insert(0, ROOT)
import audit  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000; SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 7; rnd = random.Random(SEED)
LANGS = ["eng", "jpn", "kor", "fre", "ger", "spa", "fil", "chi", "und", "", None, "en", "jp", "zxx", "mis", "xx-weird", "English"]
VCODECS = ["h264", "hevc", "av1", "vp9", "mpeg2video", "mpeg4", None]; PIX = ["yuv420p", "yuv420p10le", "yuv444p", "", None]
ACODECS = ["aac", "ac3", "eac3", "truehd", "dts", "flac", "opus", "mp3", None]; FIELDS = ["progressive", "tt", "bb", "tb", "unknown", "", None]
SUBS = ["subrip", "ass", "hdmv_pgs_subtitle", "mov_text", "dvd_subtitle", None]
def maybe(v, p=0.8): return v if rnd.random() < p else None
def stream_video():
    st = {"codec_type": "video", "codec_name": rnd.choice(VCODECS), "width": rnd.choice([0, 640, 1280, 1920, 3840, None]), "height": rnd.choice([0, 480, 720, 1080, 2160, None]),
          "pix_fmt": rnd.choice(PIX), "field_order": rnd.choice(FIELDS), "start_time": rnd.choice(["0.000000", "0.042000", "3.5", "-1.2", None, "junk"])}
    if rnd.random() < 0.15: st["side_data_list"] = [{"side_data_type": "DOVI configuration record", "dv_profile": rnd.choice([5, 7, 8, None])}]
    if rnd.random() < 0.1: st["disposition"] = {"attached_pic": 1}
    return st
def stream_audio():
    st = {"codec_type": "audio", "codec_name": rnd.choice(ACODECS), "start_time": rnd.choice(["0.000000", "2.1", None, "x"]), "profile": rnd.choice(["DTS-HD MA", "LC", None, ""])}
    tags = {}
    l = rnd.choice(LANGS)
    if l is not None and rnd.random() < 0.85: tags["language"] = l
    if rnd.random() < 0.3: tags["title"] = rnd.choice(["Commentary", "Descriptive", "Japanese", "5.1", "", None])
    if tags: st["tags"] = tags
    return st
def stream_sub():
    st = {"codec_type": "subtitle", "codec_name": rnd.choice(SUBS)}; l = rnd.choice(LANGS)
    if l is not None and rnd.random() < 0.8: st["tags"] = {"language": l, **({"title": rnd.choice(["Signs & Songs", "Forced", "SDH", "Full", ""])} if rnd.random() < 0.5 else {})}
    if rnd.random() < 0.2: st["disposition"] = {"forced": rnd.choice([0, 1]), "default": rnd.choice([0, 1])}
    return st
def probe():
    if rnd.random() < 0.05: return False, {"error": rnd.choice(["ffprobe timeout", "Invalid data found", ""])}
    streams = [stream_video() for _ in range(rnd.choice([0, 1, 1, 1, 2]))] + [stream_audio() for _ in range(rnd.choice([0, 1, 1, 2, 3]))] + [stream_sub() for _ in range(rnd.choice([0, 0, 1, 2, 5]))]
    rnd.shuffle(streams)
    fmt = {"format_name": rnd.choice(["matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "avi", None]), "duration": rnd.choice(["1320.5", "60", "2.5", "9000", "", "N/A", None, "abc"]), "size": str(rnd.choice([0, 50_000_000, 1_500_000_000])),
           "tags": {k: v for k, v in (("title", rnd.choice(["Episode 3", "Completely Other Title Here", "", None, "x" * 300])),) if v is not None}}
    return True, {"streams": streams, "format": fmt}
def item(i):
    kind = rnd.choice(["series", "series", "movie"]); app = rnd.choice(["sonarr", "radarr", "whisparr"]) if kind == "series" else "radarr"
    ok, pr = probe(); exp = rnd.choice([0, 22 * 60, 45 * 60, 95 * 60, 1, None])
    ext = rnd.choice([".mkv", ".mp4", ".avi", ".ts", ""]); path = rnd.choice([f"/data/media/tv/Show {i} (2020)/Season 01/Show {i} - S01E0{rnd.randint(1,9)}{ext}", f"/data/media/anime/Show {i}/ep{ext}", f"/data/media/movies/Movie {i} (1999)/m{ext}", None, ""])
    it = dict(app=app, kind=kind, fid=i, parent_id=i % 50, label=f"Show {i} S01E01" if kind == "series" else f"Movie {i} (1999)", path=path, size=rnd.choice([0, 1000, 5_000_000_000]),
              orig=rnd.choice(["eng", "jpn", "kor", "", None]), intent=audit.intent_from_tags(rnd.choice([[], ["audit-ignore"], ["audit-lang-jpn"], ["audit-nosubs"], ["audit-lang-"]])),
              title=rnd.choice([f"Show {i}", "", "A"]), season=rnd.choice([0, 1, 12, None]), ep_titles=rnd.choice([[], ["Pilot"], ["Some Other Episode Name"], [None]]), quality=rnd.choice([{}, {"name": "Bluray-1080p", "resolution": 1080}, {"name": None, "resolution": None}]),
              mi={} if rnd.random() < 0.7 else {"audioStreamCount": rnd.choice([0, 1, None]), "audioLanguages": rnd.choice(["eng", "jpn/eng", "", None]), "runTime": rnd.choice(["0:42:00", "", None, "abc"]), "resolution": rnd.choice(["1920x1080", "", None])},
              expected=exp, expected_lo=exp, date_added=None, ep_ids=[i] if kind == "series" else [], n_eps=rnd.choice([0, 1, 2]), series_type=rnd.choice(["standard", "anime", None]), edge=rnd.choice([True, False]),
              slug="s", monitored=True, probe=pr if ok else pr, probe_ok=ok, disk_size=rnd.choice([0, 100, 2_000_000_000, None]), mtime=0.0)
    if rnd.random() < 0.05: it["missing"] = True
    return it

bad = 0; codes_seen = set(); findings = 0
known = set(audit.CHECK_NAMES); sevs = set(audit.SEV_RANK); raw = re.compile(r"\b[A-Z]{2,}_[A-Z_]{2,}\b")
for i in range(N):
    it = item(i)
    try:
        res = audit.check(it, set(), set())
        for code, sev, detail in res:
            findings += 1; codes_seen.add(code)
            if code not in known: print(f"FAIL unknown code {code} (item {i})"); bad += 1
            if sev not in sevs: print(f"FAIL bad severity {sev} for {code} (item {i})"); bad += 1
            if not isinstance(detail, str): print(f"FAIL non-string detail for {code} (item {i})"); bad += 1
            s = audit.plain_sentence(code, sev, it["label"], detail, it["app"])
            if not s or len(s) < 10: print(f"FAIL empty sentence for {code} (item {i}): {s!r}"); bad += 1
            if raw.search(s): print(f"FAIL raw code in sentence for {code} (item {i}): {s[:80]}"); bad += 1
    except Exception:
        bad += 1; tb = traceback.format_exc().strip().splitlines(); print(f"FAIL exception on item {i}: {tb[-1]}  at {' | '.join(l.strip() for l in tb[-3:-1])}  item={ {k: it[k] for k in ('kind','path','orig','series_type','expected')} }")
        if bad > 10: break
print(f"\n{N} fuzzed items, {findings} findings, {len(codes_seen)} distinct codes, {bad} failure(s)  (seed {SEED})")
sys.exit(1 if bad else 0)
