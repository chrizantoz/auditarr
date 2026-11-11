"""Every button on the page must be pressed by a bot at least once (the Save-connections bug lived in a button no test ever touched).
Collects button ids from ui.html and the data-* hooks the click handler dispatches on (if(b.dataset.X) / b.dataset.X&&...), then checks the bots
(ui_bot.js, browser_bot.js) reference each. Fails on any that is not pressed and not excused in SKIP with a reason.
Run: python3 uibot/lint_buttons.py   Exit 1 on findings."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
ui = open(os.path.join(ROOT, "ui.html"), encoding="utf-8").read()
bots = "\n".join(open(os.path.join(HERE, n), encoding="utf-8").read() for n in ("ui_bot.js", "browser_bot.js"))
SKIP = {   # pressed only against real services or a real host; each has a reason and a home elsewhere
    "update-now": "asks the host to swap the container (deploy drill covers the hook)",
    "pw-save": "sign-in covered by tests/authz.py", "vw-save": "viewer account covered by tests/authz.py", "vw-remove": "viewer account covered by tests/authz.py", "logout": "covered by tests/authz.py",
    "w-go-movie": "same handler as w-go, movie branch", "s-go-movie": "same handler as s-go, movie branch",
    "upd-chip": "a tab button (data-tab=about); tab switching is pressed throughout the suite",
}
ids = sorted(i for i in set(re.findall(r"<button[^>]*\bid=\"([^\"$]+)\"", ui)) if "${" not in i)
hooks = sorted(set(re.findall(r"\bif\(b\.dataset\.([a-z]+)\)", ui)) | set(re.findall(r"\bif\(b\.dataset\.([a-z]+)&&", ui)))
missing = []
for i in ids:
    if i in SKIP: continue
    if not re.search(r"[#'\"]" + re.escape(i) + r"\b", bots): missing.append(f"button #{i}")
for h in hooks:
    if not re.search(r"data-" + h + r"\b|dataset\." + h + r"\b|\[" + h + r"\]", bots): missing.append(f"buttons with data-{h}")
if missing:
    print("buttons no bot ever presses (add a press to a bot, or a reason to SKIP):"); [print("  " + m) for m in missing]; sys.exit(1)
print(f"every button is pressed by a bot: {len([i for i in ids if i not in SKIP])} ids, {len(hooks)} action hooks ({len(SKIP)} excused with a reason)")
