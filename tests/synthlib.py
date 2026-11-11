"""Shared helpers for the synthetic-media tests: tiny ffmpeg-generated episodes with one known defect each."""
import os, subprocess

def have_encoder(name):
    try: return name in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
    except Exception: return False

def make(path, seconds=30, audio=True, alang="eng", vopts=None, aopts=None, title=None, aoffset=0.0, extra=None):
    """One synthetic episode at `path`: colour bars + a sine tone. Default: 64x64 h264 ultrafast, aac, language tag on the audio."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}"]
    if audio: cmd += (["-itsoffset", str(aoffset)] if aoffset else []) + ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-map", "0:v"] + (["-map", "1:a"] if audio else [])
    cmd += vopts or ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio: cmd += (aopts or ["-c:a", "aac", "-b:a", "32k"]) + (["-metadata:s:a:0", f"language={alang}"] if alang else [])
    if title: cmd += ["-metadata", f"title={title}"]
    cmd += extra or []
    cmd += [path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0: raise RuntimeError(f"ffmpeg failed for {os.path.basename(path)}: {p.stderr[-300:]}")
    return path
