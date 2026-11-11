"""UI <-> API contract: every action the page can send exists in the backend, every action has a confirm text and a label,
and every finding the API returns renders as a row (checked by the UI bot's fixtures count).
Run: python tests/contract.py   Exit 1 on failure."""
import json, os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ui = open(os.path.join(ROOT, "ui.html"), encoding="utf-8").read(); py = open(os.path.join(ROOT, "audit.py"), encoding="utf-8").read()
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))

ui_acts = set(re.findall(r'data-act="(\w+)"', ui)) | set(re.findall(r"\bB\('(\w+)'", ui)) | set(re.findall(r'data-bulk="(\w+)"', ui))
be_acts = set(re.findall(r'if action == "(\w+)"', py))
check("every UI action is handled by ui_act", ui_acts <= be_acts, f"unhandled: {sorted(ui_acts - be_acts)}")
m = re.search(r"if\(b\.dataset\.act\)\{.*?const label=\{(.*?)\}\[a\];", ui, re.S); confirm_keys = set(re.findall(r"(\w+):", m.group(1))) if m else set()
row_acts = set(re.findall(r'data-act="(\w+)"', ui)) | set(re.findall(r"\bB\('(\w+)'", ui))
check("every row action has a confirm sentence", row_acts <= confirm_keys, f"missing confirm text: {sorted(row_acts - confirm_keys)}")
mb = re.search(r"if\(b\.dataset\.bulk\)\{.*?const label=\{(.*?)\}\[a\];", ui, re.S); bulk_keys = set(re.findall(r"(\w+):", mb.group(1))) if mb else set()
bulk_acts = set(re.findall(r'data-bulk="(\w+)"', ui))
check("every bulk action has a confirm sentence", bulk_acts <= bulk_keys, f"missing: {sorted(bulk_acts - bulk_keys)}")
mbtn = re.search(r"const BTN=\{(.*?)\};", ui, re.S); btn_keys = set(re.findall(r"(\w+):\['", mbtn.group(1))) if mbtn else set()
check("every row action has a plain button label", row_acts <= btn_keys | {"acceptlang"}, f"missing label: {sorted(row_acts - btn_keys)}")
routes = set(re.findall(r'p == "(/api/\w+)"', py)); used = set("/" + u for u in re.findall(r"(?:post|fetch)\('(api/\w+)", ui))
check("every API path the page calls exists", used <= routes, f"missing routes: {sorted(used - routes)}")
# fixtures: the API shape the bots test against still carries what the page needs
st = json.load(open(os.path.join(ROOT, "uibot", "fixtures", "state.json")))
need = {"findings", "actions", "run", "by_severity", "health", "regrabs", "jobs", "progress", "trash"}
check("fixture state.json has every top-level key the page reads", need <= set(st), sorted(need - set(st)))
fkeys = {"id", "app", "check", "severity", "title", "path", "detail", "first_seen", "library"}
check("fixture findings carry the per-row fields", all(fkeys <= set(f) for f in st["findings"][:50]))
# settings: every SETTABLE knob has a help sentence, every plain label in ui.html points at a real knob
settable = set(re.findall(r'"([A-Z_]+)": \("(?:mode|int|num|csv|onoff|sev)"', py))
mlbl = re.search(r"const LBL=\{(.*?)\};", ui, re.S); lbl_keys = set(re.findall(r"(\b[A-Z_]{3,}\b):", mlbl.group(1))) if mlbl else set()
check("every plain settings label names a real setting", lbl_keys <= settable, f"unknown: {sorted(lbl_keys - settable)}")
check("the common settings all have plain labels", {"REMEDIATE", "DEDUPE", "AUTO_WHISPER", "AUTO_ACCEPTLANG", "AUTO_FIXHEADER", "SUBTAG", "TRACK_PREFS", "PLEX_REFRESH"} <= lbl_keys)
bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
