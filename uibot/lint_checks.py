"""Q28: bot checks must follow the data, not a fixed count. A check like `.length > 5` passes on a messy library and fails on a tidy one.
Flags `check(` lines in the bots that compare a length/size/count with a literal of 3 or more, unless the line says why with `// fixed:`.
Run: python3 uibot/lint_checks.py   Exit 1 on findings."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); bad = []
for name in ("ui_bot.js", "browser_bot.js"):
    for i, line in enumerate(open(os.path.join(HERE, name), encoding="utf-8"), 1):
        if "check(" not in line or "// fixed:" in line: continue
        for m in re.finditer(r"(\w+(?:\.(?:length|size|count))?)\s*(>=|>|===|==)\s*(\d+)\b(?!\s*\))", line):
            if int(m.group(3)) >= 3 and not re.search(r"(ms|Date\.now|t0|width|height|status|code|\d{3,})", m.group(1)): bad.append(f"{name}:{i}: {m.group(0)}  ->  derive the number from the data, or explain with // fixed: why it is constant")
print("\n".join(bad) if bad else "bot checks: no fixed counts"); sys.exit(1 if bad else 0)
