"""Q25 no swallowed errors: a failure inside a job keeps its traceback (errors.log + About → Recent errors + health count).
Run: python tests/errors_log.py   Exit 1 on failure."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-errlog-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
LOG = os.path.join(audit.REPORT_DIR, "errors.log")
conn = audit.db()
check("a clean start has no errors", audit.errors_recent() == [] and not os.path.exists(LOG))
check("About says so in a positive line", any(m["level"] == "ok" and "No errors" in m["text"] for m in audit.about_info(conn=conn)["health"]))

def broken_job():
    try: return 1 >= "x"   # the exact class of bug that hid for a day: text compared with a number
    except Exception as e: return audit.err_result(e)
res = broken_job()
check("err_result returns the usual ERROR text for the action log", res.startswith("ERROR TypeError"), res)
errs = audit.errors_recent()
check("…and keeps the traceback with the function name and line", len(errs) == 1 and errs[0]["where"] == "broken_job" and "errors_log.py" in errs[0]["trace"] and "1 >= \"x\"" in errs[0]["trace"], errs and errs[0]["trace"][-200:])
check("errors.log on disk holds the same block", os.path.exists(LOG) and "broken_job" in open(LOG).read() and "Traceback" in open(LOG).read())

try: raise RuntimeError("worker fell over")
except Exception as e: audit.note_error("sub worker:", repr(e))
check("note_error inside an except block records the trace too", any("worker fell over" in x["text"] and "RuntimeError" in x["trace"] for x in audit.errors_recent()))
audit.note_error("just a note, no exception")
check("note_error outside an except block records text only", any(x["text"] == "just a note, no exception" and x["trace"] == "" for x in audit.errors_recent()))

# record_action with an ERROR result inside an except block is captured without changing the call sites
try: raise ValueError("bazarr said no")
except Exception as e: audit.record_action(conn, "on", "whisper", "sonarr", 1, "Show S01E01", "x", f"ERROR {e!r}")
check("an ERROR action result inside an except keeps its traceback", any("bazarr said no" in x["text"] and "ValueError" in x["trace"] for x in audit.errors_recent()))
h = audit.health_info(); check("health counts errors in the last 24 h", h["errors_24h"] >= 2, h["errors_24h"])
msgs = audit.about_info(conn=conn)["health"]; check("About warns and lists them", any("error(s) in the last 24 hours" in m["text"] for m in msgs) and len(audit.about_info(conn=conn)["errors"]) >= 3)

# survives a restart: forget memory, read the file back
audit.ERRORS.clear(); back = audit.errors_recent()
check("after a restart the file still tells the story", len(back) >= 3 and all("ts" in x and x["when"] for x in back) and any(x["where"] == "broken_job" for x in back), [x["where"] for x in back])
check("old entries fall out of the window", audit.errors_recent(hours=0) == [])
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
