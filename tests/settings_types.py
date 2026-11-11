"""Q29 settings type safety. The UI stores every setting as text. Two guards:
1. static: every numeric setting read with setting("X") in audit.py is wrapped in int()/float() (or read through its typed global).
2. dynamic: save every setting through the same path the UI uses, then run the planners, nightly selectors, Today and health code - no text-versus-number crash may occur.
Run: python tests/settings_types.py   Exit 1 on failure."""
import os, re, sys, tempfile, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-settings-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:220]}]"))

src = open(os.path.join(ROOT, "audit.py"), encoding="utf-8").read()
numeric = {n for n, (kind, _, _) in audit.SETTABLE.items() if kind in ("int", "num")}
bad = []
for m in re.finditer(r'setting\("([A-Z_0-9]+)"\)', src):
    name = m.group(1)
    if name not in numeric: continue
    before = src[max(0, m.start() - 12):m.start()]
    if not re.search(r"(int|float|str)\(\s*$", before): bad.append((src.count("\n", 0, m.start()) + 1, name))
check("every numeric setting read in audit.py is wrapped in int()/float()", not bad, bad[:6])
modes = {n for n, (kind, _, _) in audit.SETTABLE.items() if kind in ("mode", "onoff")}
cmp_bad = [(src.count("\n", 0, m.start()) + 1, m.group(0)) for m in re.finditer(r'setting\("([A-Z_0-9]+)"\)\s*(>=|<=|<|>|[-+*/]\s*\d)', src) if m.group(1) in modes | numeric]
check("no setting value is compared or calculated with as raw text", not cmp_bad, cmp_bad[:6])

conn = audit.db()
saved = {}
for name, (kind, default, _) in audit.SETTABLE.items():
    v = {"mode": "on", "onoff": "on", "int": "7" if default != "0" else "3", "num": "2.5", "sev": "medium", "csv": default or "eng"}.get(kind, default)
    if name == "DEVICES": v = default
    saved[name] = audit.validate_setting(name, v) if kind not in ("url", "secret") else default
conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)", (json.dumps(saved),)); conn.commit()   # exactly what Settings → Save stores
audit.load_settings(conn)
check("all settings loaded back as text", all(isinstance(audit.setting(n), str) for n in audit.SETTABLE), [n for n in audit.SETTABLE if not isinstance(audit.setting(n), str)][:4])
typed = {audit._NAME_MAP.get(n, n): getattr(audit, audit._NAME_MAP.get(n, n), None) for n, (k, _, _) in audit.SETTABLE.items() if k in ("int", "num")}
check("typed globals are numbers after a UI save", all(isinstance(v, (int, float)) for v in typed.values()), {k: type(v).__name__ for k, v in typed.items() if not isinstance(v, (int, float))})
def runs(name, fn):
    try: fn(); check(f"{name} runs with every setting saved as text", True)
    except TypeError as e: check(f"{name} runs with every setting saved as text", False, repr(e))
    except Exception as e: check(f"{name} runs with every setting saved as text (other errors are not type errors)", "not supported between" not in repr(e) and "unsupported operand" not in repr(e), repr(e))
runs("plan_tonight", lambda: audit.plan_tonight(conn))
runs("today_summary", lambda: audit.today_summary(conn))
runs("health_info", lambda: audit.health_info())
runs("about_info", lambda: audit.about_info(conn=conn))
runs("watch_priority_pass", lambda: audit.watch_priority_pass(conn, targets=[]))
runs("do_auto_subsync (plan)", lambda: audit.do_auto_subsync(conn, {}, None, plan=[]))
runs("do_auto_whisper (plan)", lambda: audit.do_auto_whisper(conn, {}, plan=[]))
runs("refresh_reports", lambda: audit.refresh_reports(conn))
conn.close(); bad_n = results.count(False); print(f"\n{len(results) - bad_n} passed, {bad_n} failed"); sys.exit(1 if bad_n else 0)
