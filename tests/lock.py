"""The lock shared with Dedupearr: while one app changes a file, the other leaves that file alone.
One small marker per file in LOCK_DIR, named after the file's inode, holding {"app", "what", "path", "since"} as JSON.
The marker format is a contract between the two apps: Dedupearr's tests/fixes.py checks the same shape.
Run: python tests/lock.py   Exit 1 on failure."""
import json, os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-lock-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "reports"); os.makedirs(os.environ["REPORT_DIR"])
os.environ["TRASH_DIR"] = os.path.join(TMP, "media", ".media-audit-trash"); os.makedirs(os.environ["TRASH_DIR"]); os.environ.pop("LOCK_DIR", None)
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:240]}]"))
def theirs(path, app="Dedupearr", what="moving it to the holding bin", age=0):   # what Dedupearr writes while it works on a file
    os.makedirs(audit.LOCK_DIR, exist_ok=True); m = os.path.join(audit.LOCK_DIR, f"{os.stat(path).st_ino}.lock")
    json.dump(dict(app=app, what=what, path=path, since=time.time() - age), open(m, "w")); os.utime(m, (time.time() - age, time.time() - age)); return m
def busy(fn):
    try: fn(); return ""
    except audit.FileBusy as e: return str(e)

lib = os.path.join(TMP, "media", "tv"); os.makedirs(lib); f1 = os.path.join(lib, "Show - S01E01.mkv"); open(f1, "wb").write(b"x" * 100)
check("the lock folder sits beside the trash folder unless LOCK_DIR says otherwise", audit.LOCK_DIR == os.path.join(TMP, "media", ".fix-locks"), audit.LOCK_DIR)

# --- taking and dropping
tok = audit.lock_take(f1, "rewrapping the file"); info = json.load(open(tok))
check("taking the lock writes one marker named after the file's inode", os.path.basename(tok) == f"{os.stat(f1).st_ino}.lock" and os.listdir(audit.LOCK_DIR) == [os.path.basename(tok)])
check("the marker says who, what, which file and since when", info["app"] == "Auditarr" and info["what"] == "rewrapping the file" and info["path"] == f1 and abs(info["since"] - time.time()) < 5, info)
check("this app's own marker never reads as someone else's", audit.lock_holder(f1) == "")
tok2 = audit.lock_take(f1, "moving it to the trash"); audit.lock_drop(tok2)
check("a step inside a fix takes the lock again and dropping it keeps the outer lock", tok2 == tok and os.path.exists(tok))
audit.lock_drop(tok); check("dropping the lock removes the marker", not os.path.exists(tok) and not audit._LOCKS)
ln = os.path.join(lib, "second name.mkv"); os.link(f1, ln)
check("two names of one file share one lock", audit._lock_marker(ln) == audit._lock_marker(f1))

# --- the other app holds it
m = theirs(f1)
said = audit.lock_holder(f1); check("a file Dedupearr is working on is named in plain words", said.startswith("Dedupearr is working on this file right now (moving it to the holding bin)") and "nothing was changed" in said, said)
check("taking the lock is refused while Dedupearr holds it", busy(lambda: audit.lock_take(f1, "x")).startswith("Dedupearr is working on this file") and os.path.exists(m) and not audit._LOCKS)
check("the second name of that file is refused as well", busy(lambda: audit.lock_take(ln, "x")) != "")
calls = []; audit._quarantine = lambda *a: calls.append(a) or ("dest", ["x"])
check("moving a file to the trash is refused while Dedupearr holds it, and nothing moves", busy(lambda: audit.quarantine(None, f1, "test")) != "" and not calls and os.path.exists(f1))
conn = audit.db(); audit.finding_row = lambda c, fid: {"id": fid, "path": f1, "app": "sonarr", "fid": "1", "label": "Show S01E01", "check": "MP4_IN_MKV"}
audit._ui_act = lambda c, b, a: calls.append(b) or (200, {"ok": True})
code, obj = audit.ui_act(conn, {"id": "f1", "action": "remuxmkv"}, "test")
check("a press on the page is refused with the reason, the fix never starts", code == 409 and "Dedupearr is working on this file" in obj["error"] and not calls and not audit.PRESSING, (code, obj))
code, obj = audit.ui_act(conn, {"id": "f1", "action": "ignore"}, "test")
check("a press that changes no file is not held up", code == 200 and len(calls) == 1, (code, obj))
os.remove(m); calls.clear(); seen = []
audit._ui_act = lambda c, b, a: seen.append(json.load(open(audit._lock_marker(f1)))["what"]) or audit.quarantine(c, f1, "inside") and (200, {"ok": True})
code, obj = audit.ui_act(conn, {"id": "f1", "action": "remuxmkv"}, "test")
check("a press holds the lock for the whole fix, its trash step included, and frees it at the end", code == 200 and seen == ["rewrapping the file"] and len(calls) == 1 and os.listdir(audit.LOCK_DIR) == [], (code, seen, os.listdir(audit.LOCK_DIR)))
def boom(c, b, a): raise RuntimeError("mkvmerge fell over")
audit._ui_act = boom
try: audit.ui_act(conn, {"id": "f1", "action": "remuxmkv"}, "test")
except RuntimeError: pass
check("a fix that stops with an error still frees the lock", os.listdir(audit.LOCK_DIR) == [] and not audit._LOCKS)

# --- markers that mean nothing any more
m = theirs(f1, age=7 * 3600)
check("a marker older than six hours is ignored", audit.lock_holder(f1) == "")
tok = audit.lock_take(f1, "x"); check("... and the lock can be taken over it", tok and json.load(open(tok))["app"] == "Auditarr"); audit.lock_drop(tok)
m = theirs(f1, app="Auditarr"); tok = audit.lock_take(f1, "x")
check("a marker this app left behind before a restart is taken over", tok and not audit.lock_holder(f1)); audit.lock_drop(tok)
m = theirs(f1); open(m, "w").write("")
check("a marker still being written counts as held", audit.lock_holder(f1).startswith("Another app is working on this file") and busy(lambda: audit.lock_take(f1, "x")) != "")
os.remove(m); a = theirs(f1, app="Auditarr"); f2 = os.path.join(lib, "other.mkv"); open(f2, "wb").write(b"y"); b = theirs(f2); f3 = os.path.join(lib, "old.mkv"); open(f3, "wb").write(b"z"); c3 = theirs(f3, age=7 * 3600)
n = audit.lock_sweep(); check("start-up clears this app's old markers and stale ones, never a fresh one of Dedupearr", n == 2 and not os.path.exists(a) and not os.path.exists(c3) and os.path.exists(b), (n, os.listdir(audit.LOCK_DIR)))
os.remove(b)

# --- no lock folder, no lock: the fix runs as before
real = audit.LOCK_DIR; audit.LOCK_DIR = os.path.join(TMP, "nowhere", "deeper", ".fix-locks")
check("with no place for the lock folder the fix runs unlocked and nothing is created", audit.lock_take(f1, "x") is None and audit.lock_holder(f1) == "" and not os.path.exists(os.path.join(TMP, "nowhere")))
audit.LOCK_DIR = real; audit.READ_ONLY = True
check("read-only mode writes no marker", audit.lock_take(f1, "x") is None and os.listdir(audit.LOCK_DIR) == []); audit.READ_ONLY = False
check("a file that is gone takes no lock", audit.lock_take(os.path.join(lib, "gone.mkv"), "x") is None)
src = open(os.path.join(ROOT, "audit.py")).read()
check("the nightly fixes skip a file Dedupearr holds", src.count("if lock_holder(") + src.count("or lock_holder(") >= 4)
check("both in-place edits (default tracks, subtitle labels) run under the lock", src.count('tok = lock_take(path, "setting the default tracks")') == 1 and src.count('tok = lock_take(path, "labelling the subtitles")') == 1)

print(f"\n{sum(results)} of {len(results)} checks passed"); sys.exit(0 if all(results) else 1)
