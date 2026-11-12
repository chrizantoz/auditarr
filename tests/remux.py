"""Rewrap the file (MP4_IN_MKV): a real MP4 saved with a .mkv name is rewrapped as Matroska by mkvmerge, nothing re-encoded.
The new file is built beside the old one and checked before the original goes to the trash; a failure leaves the original untouched.
Needs ffmpeg and mkvmerge. Run: python tests/remux.py   Exit 1 on failure."""
import os, sys, shutil, subprocess, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-remux-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "reports"); os.makedirs(os.environ["REPORT_DIR"])
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:240]}]"))
if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")): print("SKIP: needs ffmpeg and mkvmerge"); sys.exit(0)

lib = os.path.join(TMP, "media", "Show (2021)"); os.makedirs(lib); trash = os.path.join(TMP, "trash")
def moved_to_trash(conn, path, why, kind="regrab", sidecars=True):   # the real quarantine maps paths under /data; here the trash is a temp folder
    dest = os.path.join(trash, os.path.basename(path)); os.makedirs(trash, exist_ok=True); os.rename(path, dest); return dest, [os.path.basename(path)]
audit.quarantine = moved_to_trash; audit.plex_refresh = lambda p: "plex skipped"
srt = os.path.join(TMP, "s.srt"); open(srt, "w").write("1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n")
mp4 = os.path.join(lib, "Show (2021) - S01E01 - Pilot [WEBDL-1080p].mkv")
r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-i", srt,
                    "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "mov_text", "-metadata:s:a:0", "language=eng",
                    "-metadata:s:s:0", "language=eng", "-f", "mp4", mp4], capture_output=True, text=True)
check("test file: an MP4 with a .mkv name was made", r.returncode == 0 and open(mp4, "rb").read(12)[4:8] == b"ftyp", r.stderr[-200:])
conn = audit.db()
check("the file's first bytes say it is not Matroska, even with no format in the probe", not audit.is_matroska({"path": mp4, "probe": {"streams": []}}))
src = open(os.path.join(ROOT, "audit.py")).read(); check("the audit raises MP4_IN_MKV for a .mkv that is not Matroska", 'F("MP4_IN_MKV"' in src and "not is_matroska(it)" in src)

f = {"id": "f1", "path": mp4, "app": "disk", "fid": "1", "label": "Show S01E01", "check": "MP4_IN_MKV"}
ino0 = os.stat(mp4).st_ino; os.chmod(mp4, 0o640)
code, obj = audit.remux_to_mkv(conn, f, {}, "test")
ok, pr = audit.ffprobe(mp4) if os.path.exists(mp4) else (False, {})
kinds = {k: sum(1 for s in pr.get("streams", []) if s.get("codec_type") == k) for k in ("video", "audio", "subtitle")} if ok else {}
check("Rewrap the file: done, the file at the same path is now Matroska", code == 200 and open(mp4, "rb").read(4) == audit.MKV_MAGIC and "matroska" in str(pr.get("format", {}).get("format_name")), (code, obj))
check("every track came along: 1 video, 1 audio, the subtitle as a text track", kinds == {"video": 1, "audio": 1, "subtitle": 1}, kinds)
check("nothing was re-encoded (the video is still the same H.264 stream)", ok and pr["streams"][0].get("codec_name") == "h264")
check("the length is the same", ok and abs(float(pr["format"]["duration"]) - 8.0) < 1.0, pr.get("format", {}).get("duration"))
check("the original waits in the trash, and the new file keeps its permissions", os.path.exists(os.path.join(trash, os.path.basename(mp4))) and os.stat(mp4).st_ino != ino0 and (os.stat(mp4).st_mode & 0o777) == 0o640)
check("no temporary file is left beside it", not [n for n in os.listdir(lib) if ".tmp." in n], os.listdir(lib))
check("the action log names what happened", any(a[0] == "remuxmkv" and "original ->" in (a[1] or "") for a in conn.execute("SELECT kind, result FROM action")))
check("the note says it is now a real Matroska file and where the original went", "trash" in obj.get("note", ""), obj)

code, obj = audit.remux_to_mkv(conn, f, {}, "test")
check("pressing it again on a real Matroska file only clears the finding", code == 200 and obj.get("already_gone"), (code, obj))

bad = os.path.join(lib, "Show (2021) - S01E02 - Cut short.mkv")   # index first (faststart), then cut the media: the header still says 8 s, the rewrap comes out short
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(trash, os.path.basename(mp4)), "-map", "0:v", "-map", "0:a", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", bad], check=True)
with open(bad, "r+b") as fh: fh.truncate(os.path.getsize(bad) // 3)
before = open(bad, "rb").read()
code, obj = audit.remux_to_mkv(conn, dict(f, path=bad, fid="2"), {}, "test")
check("a rewrap that does not match the original (shorter, or failed) is thrown away and the original stays exactly as it was", code == 500 and "original untouched" in obj.get("error", "") and open(bad, "rb").read() == before, (code, obj))
junk = os.path.join(lib, "Show (2021) - S01E03 - Junk.mkv"); open(junk, "wb").write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4000); j0 = open(junk, "rb").read()
code, obj = audit.remux_to_mkv(conn, dict(f, path=junk, fid="3"), {}, "test")
check("an unreadable file is refused before anything is written", code == 500 and open(junk, "rb").read() == j0, (code, obj))
check("and no temporary file is left behind", not [n for n in os.listdir(lib) if ".tmp." in n], os.listdir(lib))
check("the failure is in the action log", any(a[0] == "remuxmkv" and (a[1] or "").startswith("ERROR") for a in conn.execute("SELECT kind, result FROM action")))
code, obj = audit.remux_to_mkv(conn, dict(f, path=os.path.join(lib, "gone.mkv")), {}, "test"); check("a file that is gone is refused", code == 404)
code, obj = audit.remux_to_mkv(conn, dict(f, path=os.path.join(lib, "x.mp4")), {}, "test"); check("a file without a .mkv name is refused", code == 400)
conn.close(); bad_n = results.count(False); print(f"\n{len(results) - bad_n} passed, {bad_n} failed"); sys.exit(1 if bad_n else 0)
