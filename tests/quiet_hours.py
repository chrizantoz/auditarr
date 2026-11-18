"""F21 quiet hours for heavy jobs: AI subtitles and the speech checks wait inside QUIET_HOURS and while Plex is playing something
(QUIET_WHEN_STREAMING); a press on the page, a retry and subtitles for what someone is watching never wait; a held job runs by itself
once the reason is gone, and jobs that may run are not stuck behind it.
Run: python tests/quiet_hours.py   Exit 1 on failure."""
import datetime, os, queue, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-quiet-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
conn = audit.db(); audit.load_settings(conn)
def ui_set(**kv): return audit.ui_settings_post(conn, {"set": {k: str(v) for k, v in kv.items()}}, "test")
at = lambda h, m: datetime.datetime(2026, 11, 18, h, m)

# ---- the window
check("a window inside one day", audit.in_window("18:00-23:00", at(19, 30)) and not audit.in_window("18:00-23:00", at(23, 0)) and not audit.in_window("18:00-23:00", at(17, 59)))
check("a window across midnight", audit.in_window("22:00-06:30", at(23, 15)) and audit.in_window("22:00-06:30", at(3, 0)) and not audit.in_window("22:00-06:30", at(7, 0)))
check("an empty or broken window is never quiet", not audit.in_window("", at(12, 0)) and not audit.in_window("noon", at(12, 0)))
code, obj = ui_set(QUIET_HOURS="18:00 - 23:00"); check("the page saves a window as typed with spaces", code == 200 and audit.setting("QUIET_HOURS") == "18:00-23:00", (code, obj))
code, obj = ui_set(QUIET_HOURS="evenings"); check("a window that is not start-end is refused with a sentence that shows how", code == 400 and "18:00-23:00" in str(obj), (code, obj))

# ---- which jobs wait
audit.plex_streams = lambda max_age=60: STREAMS[0]; STREAMS = [0]
ui_set(QUIET_HOURS="", QUIET_WHEN_STREAMING="on")
night = dict(type="whisper", label="X S01E01", actor="nightly")
check("nothing playing, no window: heavy work runs", audit.job_waits(night) == "")
STREAMS[0] = 2; check("Plex is playing: nightly AI subtitles wait and say why", "watching on Plex (2 streams)" in audit.job_waits(night), audit.job_waits(night))
check("…a timing check and a language check wait too", all(audit.job_waits(dict(type=t, label="x")) for t in ("subsync", "langdetect", "verify")))
check("…a Bazarr search is light and never waits", audit.job_waits(dict(type="subsearch", label="x")) == "")
check("…a press on the page never waits", audit.job_waits(dict(night, pressed=True)) == "")
check("…subtitles for what someone is watching never wait", audit.job_waits(dict(night, prio=1)) == "")
check("…a retry never waits", audit.job_waits(dict(night, prio=2)) == "")
ui_set(QUIET_WHEN_STREAMING="off"); check("QUIET_WHEN_STREAMING=off: streams do not hold anything", audit.job_waits(night) == "")
STREAMS[0] = 0; ui_set(QUIET_HOURS="00:00-23:59"); check("inside quiet hours heavy work waits and says until when", "quiet hours (00:00 to 23:59)" in audit.job_waits(night), audit.job_waits(night))

# ---- a press is recognised: jobs queued while a page request is answered are stamped
real_lane = audit._lane; q = queue.PriorityQueue(); audit._lane = lambda job: q
audit._REQ.press = True; audit.enqueue(dict(type="subsync", fid=1, app="sonarr", path="", label="pressed one")); audit._REQ.press = False
audit.enqueue(dict(type="subsync", fid=2, app="sonarr", path="", label="nightly one", actor="nightly"))
got = {}
while not q.empty(): it = q.get(); got[it[2]["label"]] = it[2]
check("a job queued from a page request is marked as pressed", got.get("pressed one", {}).get("pressed") is True, got.get("pressed one"))
check("a job queued by the night is not", not got.get("nightly one", {}).get("pressed"), got.get("nightly one"))

# ---- the worker holds a job and runs it by itself once the reason is gone; a pressed job is not stuck behind it
ran = []; audit._verify_job = lambda job: ran.append((job["label"], time.time()))
audit.QUIET_RECHECK_S = 0.3; ui_set(QUIET_HOURS=""); STREAMS[0] = 1; ui_set(QUIET_WHEN_STREAMING="on")
wq = queue.PriorityQueue(); threading.Thread(target=audit.sub_worker, args=(wq,), daemon=True).start()
wq.put((5, 1, dict(type="verify", label="held", _id=901))); time.sleep(0.5)
wq.put((5, 2, dict(type="verify", label="pressed", pressed=True, _id=902))); time.sleep(1.0)
check("while Plex plays, the nightly check waits", "held" not in [r[0] for r in ran], ran)
check("…the pressed check behind it ran anyway", "pressed" in [r[0] for r in ran], ran)
st = audit.ui_state(conn) if hasattr(audit, "ui_state") else {}
check("…and the page is told how many wait and why", (st.get("jobs") or {}).get("held") == 1 and "watching on Plex" in (st.get("jobs") or {}).get("quiet", ""), (st.get("jobs") or {}).get("quiet"))
STREAMS[0] = 0; time.sleep(1.2)
check("when the stream ends the held check runs by itself", "held" in [r[0] for r in ran], ran)
check("…and nothing is left waiting", not audit.HELD, audit.HELD)
audit._lane = real_lane
src = open(os.path.join(ROOT, "audit.py")).read()
check("the nightly time-bar repairs (a whole-file remux) wait for the next night in quiet hours", 'AUTO_FIXHEADER") != "off" and quiet_reason()' in src)
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
