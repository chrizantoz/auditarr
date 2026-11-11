"""Detector regression tests on synthetic media. Generates tiny files with ffmpeg, each with one known defect,
runs audit.check() / audit.orphan_scan() on them and asserts the exact finding codes. No arr, Bazarr or Plex needed.
Run: python tests/synthetic_media.py   (needs ffmpeg + ffprobe; mkvtoolnix optional)   Exit code 1 on any failure."""
import json, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-synth-")
os.environ.setdefault("CONFIG_DIR", os.path.join(TMP, "config")); os.environ.setdefault("REPORT_DIR", os.path.join(TMP, "reports"))
os.makedirs(os.environ["CONFIG_DIR"], exist_ok=True); os.makedirs(os.environ["REPORT_DIR"], exist_ok=True)
sys.path.insert(0, ROOT)
import audit  # noqa: E402

MEDIA = os.path.join(TMP, "data", "tv", "Alpha Show (2020)", "Season 01"); os.makedirs(MEDIA, exist_ok=True)
results = []
def check(name, cond, info=""): results.append((name, bool(cond), str(info)[:200])); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))

from synthlib import have_encoder, make as _make   # noqa: E402
def make(name, **kw): return _make(os.path.join(MEDIA, name), **kw)

def item(path, expected=30, orig="eng", series_type="standard", title="Alpha Show", ep_titles=("Beta Gamma Delta",), disk_size=None, kind="series", app="sonarr"):
    ok, pr = audit.ffprobe(path); st = os.stat(path)
    return dict(app=app, kind=kind, fid=1, parent_id=1, label=f"{title} S01E01", path=path, size=st.st_size, orig=orig, intent=audit.intent_from_tags([]), title=title, season=1,
                ep_titles=list(ep_titles), quality={}, mi={}, expected=expected, expected_lo=expected, date_added=None, ep_ids=[1], n_eps=1, series_type=series_type, edge=False,
                slug="alpha-show", monitored=True, probe=pr, probe_ok=ok, disk_size=disk_size if disk_size is not None else st.st_size, mtime=st.st_mtime)

def codes(it): return {c for c, _, _ in audit.check(it, set(), set())}
def sev(it, code): return next((s for c, s, _ in audit.check(it, set(), set()) if c == code), None)

# ---- cases ----
ok_path = make("alpha.show.s01e01.ok.mkv")
check("clean file -> no findings", codes(item(ok_path)) == set(), codes(item(ok_path)))

p = make("alpha.show.s01e01.noaudio.mkv", audio=False)
check("no audio stream -> NO_AUDIO critical", sev(item(p), "NO_AUDIO") == "critical", codes(item(p)))

p = make("alpha.show.s01e01.french.mkv", alang="fre")
check("French-only audio on an English show -> WRONG_LANGUAGE high", sev(item(p), "WRONG_LANGUAGE") == "high", codes(item(p)))

p = make("alpha.show.s01e01.icelandic.mkv", alang="isl")
c = codes(item(p)); check("Icelandic-tagged audio (nobody dubs into it) -> WRONG_LANGUAGE low (mis-tag)", sev(item(p), "WRONG_LANGUAGE") == "low", c)

p = make("alpha.show.s01e01.und.mkv", alang=None)
check("untagged audio, no subs -> UND_AUDIO_NO_SUBS low", sev(item(p), "UND_AUDIO_NO_SUBS") == "low", codes(item(p)))

p = make("alpha.show.s01e01.jpn.mkv", alang="jpn")
it = item(p, orig="jpn", series_type="anime"); check("anime, Japanese audio, no subs, Bazarr not hunting -> NO_SUBS high (not DUB_ONLY)", sev(it, "NO_SUBS") == "high" and "DUB_ONLY" not in codes(it), codes(it))

p = make("alpha.show.s01e01.dub.mkv", alang="eng")
it = item(p, orig="jpn", series_type="anime"); check("anime with only an English track -> DUB_ONLY medium", sev(it, "DUB_ONLY") == "medium", codes(it))

p = make("alpha.show.s01e01.sample.mkv", seconds=20)
it = item(p, expected=1500); check("20 s file where 25 min expected -> SAMPLE_FILE high", sev(it, "SAMPLE_FILE") == "high", codes(it))

p = make("alpha.show.s01e01.short.mkv", seconds=200)
it = item(p, expected=1500, disk_size=int(300e6)); check("200 s of a 25 min episode (big file) -> RUNTIME_SHORT high", sev(it, "RUNTIME_SHORT") == "high", codes(it))

p = make("alpha.show.s01e01.long.mkv", seconds=620)
it = item(p, expected=60, disk_size=int(300e6)); check("10 min file where 1 min expected -> RUNTIME_LONG medium", sev(it, "RUNTIME_LONG") == "medium", codes(it))
it["edge"] = True; check("same on a season premiere/finale -> RUNTIME_LONG low", sev(it, "RUNTIME_LONG") == "low", codes(it))

p = make("alpha.show.s01e01.title.mkv", title="Completely Unrelated Episode Name")
check("embedded title from another episode -> TITLE_MISMATCH low", sev(item(p), "TITLE_MISMATCH") == "low", codes(item(p)))
p = make("alpha.show.s01e01.goodtitle.mkv", title="Alpha Show - Beta Gamma Delta")
check("embedded title that matches -> no TITLE_MISMATCH", "TITLE_MISMATCH" not in codes(item(p)), codes(item(p)))

p = make("alpha.show.s01e01.hi10p.mkv", vopts=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le"])
check("10-bit H.264 -> HI10P medium", sev(item(p), "HI10P") == "medium", codes(item(p)))

p = make("alpha.show.s01e01.offset.mkv", aoffset=3.0)
c = codes(item(p)); check("audio starting 3 s late -> AUDIO_OFFSET low", sev(item(p), "AUDIO_OFFSET") == "low", c)

if have_encoder("libsvtav1") or have_encoder("libaom-av1"):
    enc = "libsvtav1" if have_encoder("libsvtav1") else "libaom-av1"
    p = make("alpha.show.s01e01.av1.mkv", seconds=5, vopts=["-c:v", enc, "-pix_fmt", "yuv420p"] + (["-cpu-used", "8"] if enc == "libaom-av1" else ["-preset", "12"]))
    check("AV1 video -> AV1 low", sev(item(p), "AV1") == "low", codes(item(p)))
else: print("SKIP AV1 (no AV1 encoder in this ffmpeg)")

p = make("alpha.show.s01e01.truehd.mkv", seconds=5, aopts=["-c:a", "truehd", "-strict", "-2"]) if have_encoder("truehd") else None
if p: check("TrueHD-only audio -> TRUEHD_ONLY low", sev(item(p), "TRUEHD_ONLY") == "low", codes(item(p)))
else: print("SKIP TRUEHD_ONLY (no truehd encoder)")

# a stale arr record: the path is gone
it = item(ok_path); it["path"] = os.path.join(MEDIA, "gone.mkv"); it["missing"] = True
check("arr lists a file that is not on disk -> MISSING_FILE high + rescan", sev(it, "MISSING_FILE") == "high" and it.get("needs_rescan"), codes(it))

# arr says there are no audio streams and ffprobe could not run (mediaInfo path)
it = item(ok_path); it["probe"] = None; it["probe_ok"] = False; it["mi"] = {"audioStreamCount": 0, "audioLanguages": ""}
check("arr mediaInfo with 0 audio streams -> NO_AUDIO critical", sev(it, "NO_AUDIO") == "critical", codes(it))

# audit-ignore tag silences everything
it = item(make("alpha.show.s01e01.ignored.mkv", audio=False)); it["intent"] = audit.intent_from_tags(["audit-ignore"])
check("audit-ignore tag -> no findings at all", codes(it) == set(), codes(it))

# ---- orphan / duplicate scan on a folder ----
managed = ok_path; twin = os.path.join(MEDIA, "Alpha.Show.S01E01.other-group.mkv"); shutil.copy(managed, twin)
orphan = os.path.join(MEDIA, "Some.Random.Movie.2019.mkv"); shutil.copy(managed, orphan)
items = [item(managed)]
found = {(code, os.path.basename(path)): sev_ for code, sev_, path, _ in audit.orphan_scan(items, {"sonarr": [os.path.dirname(os.path.dirname(MEDIA))]})}
check("second file for the same episode -> DUPLICATE_VIDEO", any(k == ("DUPLICATE_VIDEO", os.path.basename(twin)) for k in found), found)
check("unknown video in the root -> ORPHAN_FILE", any(k == ("ORPHAN_FILE", os.path.basename(orphan)) for k in found), found)
check("the managed file itself is not an orphan", not any(k[1] == os.path.basename(managed) for k in found), found)
check("tiny files are low severity", all(v == "low" for v in found.values()), found)

# ---- read-only mode: every write path refuses and is counted ----
audit.READ_ONLY = True; audit.WRITE_LEDGER.clear(); refused = 0
for fn in (lambda: audit.guard_write("test"), lambda: audit.quarantine(None, ok_path, "test"), lambda: audit.restore_file(ok_path, ok_path + ".x"),
           lambda: audit.api("http://127.0.0.1:1", "k", "command", "POST", {"name": "x"}), lambda: audit.bazarr_call("system/settings", {"a": 1})):
    try: fn()
    except audit.ReadOnly: refused += 1
    except Exception as e: print("  (unexpected non-ReadOnly error:", repr(e)[:80], ")")
check("READ_ONLY refuses trash moves, restores, arr POSTs and Bazarr POSTs", refused == 5 and len(audit.WRITE_LEDGER) == 5, (refused, len(audit.WRITE_LEDGER)))
try: audit.api("http://127.0.0.1:1", "k", "series")   # a GET must still try (and fail to connect, which is fine here)
except audit.ReadOnly: check("READ_ONLY still allows GET", False, "GET refused")
except Exception: check("READ_ONLY still allows GET", True)
audit.READ_ONLY = False

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed  ({TMP})")
json.dump([dict(name=n, ok=o, info=i) for n, o, i in results], open(os.path.join(TMP, "synthetic.json"), "w"), indent=1)
shutil.rmtree(TMP, ignore_errors=True) if not failed else None
sys.exit(1 if failed else 0)
