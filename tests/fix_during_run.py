"""A fix applied while an audit is running must not be undone when the audit writes its findings back (seen on production 2026-11-11:
Set audio language mid-run, the run re-opened the finding at its end, the self-test then flagged a detector disagreement).
Run: python tests/fix_during_run.py   Exit 1 on failure."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-race-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
conn = audit.db(); started = time.time() - 300   # the run began five minutes ago
args = ("abc123", "sonarr", 11, "UND_AUDIO_NO_SUBS", "low", "Show S01E01", "/data/tv/x.mkv", "audio language untagged")
check("first sighting: the finding is created", audit.upsert_finding(conn, *args, started) == "new" and conn.execute("SELECT resolved_at FROM finding WHERE id='abc123'").fetchone()[0] is None)
check("seen again by a later run: kept, not duplicated", audit.upsert_finding(conn, *args, started + 10) == "kept" and conn.execute("SELECT count(*) FROM finding WHERE id='abc123'").fetchone()[0] == 1)
audit.resolve_finding(conn, "abc123")   # someone presses Set audio language while the run is still going (now > started)
check("the run writing back afterwards leaves the fix alone", audit.upsert_finding(conn, *args, started) == "skipped" and conn.execute("SELECT resolved_at FROM finding WHERE id='abc123'").fetchone()[0] is not None)
check("a later run that still sees the problem re-opens it", audit.upsert_finding(conn, *args, time.time() + 5) == "new" and conn.execute("SELECT resolved_at FROM finding WHERE id='abc123'").fetchone()[0] is None)
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
