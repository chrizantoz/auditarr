"""Mutation check (Q10): prove the synthetic-media suite has teeth. Copies the repo to a temp dir, breaks one detector on purpose,
runs tests/synthetic_media.py there and demands a FAILURE. If a mutant survives, the suite is decorative for that check.
Run: python tests/mutation_check.py [all | index]   (CI rotates one mutant per day; 'all' runs every mutant, ~1 min each)"""
import datetime, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUTANTS = [   # (name, exact text in audit.py, replacement that silently breaks the detector)
    ("NO_AUDIO never fires", 'if not a: F("NO_AUDIO", "critical", "no audio stream")', 'if False: F("NO_AUDIO", "critical", "no audio stream")'),
    ("HI10P ignores 10-bit", 'if codec == "h264" and "10" in pix: F("HI10P"', 'if codec == "h264" and "99" in pix: F("HI10P"'),
    ("WRONG_LANGUAGE always low", 'F("WRONG_LANGUAGE", "high", f"audio={sorted(real)} original={it[\'orig\'] or \'?\'} wanted={sorted(acceptable)}"', 'F("WRONG_LANGUAGE", "low", f"audio={sorted(real)} original={it[\'orig\'] or \'?\'} wanted={sorted(acceptable)}"'),
    ("SAMPLE_FILE threshold broken", 'if exp > 600 and (dur < 180 or (it.get("disk_size") or it["size"]) < 120e6):', 'if exp > 600 and (dur < 1 or (it.get("disk_size") or it["size"]) < 1):'),
    ("DUB_ONLY disabled", 'if real and not (real & accept_oa):', 'if False and real and not (real & accept_oa):'),
    ("AUDIO_OFFSET threshold too high", "if off > 1.5:", "if off > 1500:"),
    ("duplicate detection off", 'code, sev = "DUPLICATE_VIDEO", "medium"', 'code, sev = "ORPHAN_FILE", "medium"'),
    ("read-only guard disabled", "    if not READ_ONLY: return\n    WRITE_LEDGER.append", "    if True: return\n    WRITE_LEDGER.append"),
]
arg = sys.argv[1] if len(sys.argv) > 1 else str(datetime.date.today().timetuple().tm_yday % len(MUTANTS))
todo = MUTANTS if arg == "all" else [] if arg == "anchors" else [MUTANTS[int(arg) % len(MUTANTS)]]
src = open(os.path.join(ROOT, "audit.py")).read(); bad = 0
if arg == "anchors":   # every run: each mutant must still find its line (2026-11-12: Dedupearr lost one to a refactor; the one-a-day rotation would not have said so for weeks)
    miss = [n for n, old, _ in MUTANTS if old not in src]
    print(f"{len(MUTANTS) - len(miss)} of {len(MUTANTS)} mutant anchors found" + (": missing " + ", ".join(miss) if miss else "")); sys.exit(1 if miss else 0)
for name, old, new in todo:
    if old not in src: print(f"FAIL mutant '{name}': anchor text not found in audit.py (update the mutant)"); bad += 1; continue
    tmp = tempfile.mkdtemp(prefix="auditarr-mut-")
    for f in ("audit.py",): shutil.copy(os.path.join(ROOT, f), tmp)
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(tmp, "tests"), ignore=shutil.ignore_patterns("__pycache__", "homelab"))
    open(os.path.join(tmp, "audit.py"), "w").write(src.replace(old, new, 1))
    p = subprocess.run([sys.executable, os.path.join(tmp, "tests", "synthetic_media.py")], capture_output=True, text=True, timeout=1200, env={**os.environ, "CONFIG_DIR": os.path.join(tmp, "cfg"), "REPORT_DIR": os.path.join(tmp, "cfg")})
    caught = p.returncode != 0
    print(f"{'PASS' if caught else 'FAIL'} mutant '{name}': {'caught by the suite' if caught else 'SURVIVED - the suite did not notice'}  ({[l for l in p.stdout.splitlines() if l.startswith('FAIL')][:2]})")
    bad += 0 if caught else 1; shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(todo) - bad} of {len(todo)} mutants caught"); sys.exit(1 if bad else 0)
