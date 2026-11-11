"""F32 subtitles painted into the picture: the lettering score on synthetic clips (text / nothing / a corner logo), the verdict, the cache and the budget.
Run: python tests/hardsub.py   Exit 1 on failure (skips the clip checks when ffmpeg cannot draw text here)."""
import os, sys, tempfile, subprocess, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-hardsub-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
D = tempfile.mkdtemp(prefix="hs-")
font = subprocess.run(["fc-match", "-f", "%{file}", "sans"], capture_output=True, text=True).stdout.strip() if shutil.which("fc-match") else ""
def clip(name, vf=None):
    p = os.path.join(D, name); cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=30"] + (["-vf", vf] if vf else []) + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", p]
    r = subprocess.run(cmd, capture_output=True, text=True); return p if r.returncode == 0 else None
text = clip("text.mp4", f"drawtext=fontfile={font}:text='Je ne sais pas ce que tu veux dire':fontcolor=white:fontsize=26:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-50:enable='between(t,4,12)+between(t,16,27)'") if font else None
plain = clip("plain.mp4"); logo = clip("logo.mp4", "drawbox=x=560:y=300:w=60:h=40:color=white@0.9:t=fill,drawbox=x=560:y=300:w=60:h=40:color=black:t=3")
check("ffmpeg made the plain and logo clips", plain and logo)
if text:
    b, w, h = audit.frame_band(text, 8); sc, where = audit.text_score(b, w, h); check("lettering in the band scores high", sc > 0.06, sc)
    b, w, h = audit.frame_band(text, 2); sc2, _ = audit.text_score(b, w, h); check("the same clip without text at that moment scores low", sc2 < 0.02, sc2)
    audit.HARDSUB_BUDGET["left"] = 5; audit.HARDSUB_CACHE.clear()
    v = audit.hardsub_verdict(text, 30); check("verdict: burned-in subtitles", v[0] == "burned" and v[1] > 0.06, v)
    check("the verdict is cached per file", audit.hardsub_verdict(text, 30) == v and audit.HARDSUB_BUDGET["left"] == 4)
else: print("SKIP text clip (no font for drawtext here)")
audit.HARDSUB_BUDGET["left"] = 5
check("verdict: a plain clip is clean", audit.hardsub_verdict(plain, 30)[0] == "clean", audit.hardsub_verdict(plain, 30))
check("verdict: a corner logo is not mistaken for subtitles", audit.hardsub_verdict(logo, 30)[0] == "clean", audit.hardsub_verdict(logo, 30))
audit.HARDSUB_BUDGET["left"] = 0; audit.HARDSUB_CACHE.clear()
check("no budget left: unknown, nothing inspected", audit.hardsub_verdict(plain, 30)[0] == "unknown")
conn = audit.db(); audit.HARDSUB_CACHE["/x.mkv"] = (1.0, "burned", 0.1, 12.0); audit.flush_hardsub(conn); audit.HARDSUB_CACHE.clear(); audit.load_hardsub(conn)
check("the cache survives in the database", audit.HARDSUB_CACHE.get("/x.mkv", (0, ""))[1] == "burned")
s_ = audit.plain_sentence("NO_SUBS", "low", "Show S01E01", "audio=['jpn'] embedded_subs=[] external=[] | subtitles burned into the picture (language unknown; seen at 40s)", "sonarr")
check("plain sentence explains painted subtitles and the frame button", "painted into the picture" in s_ and "Show me a frame" in s_, s_)
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
