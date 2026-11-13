"""Who may close a row (lesson of #46, 2026-11-13: every audit closed every 'Subtitles out of sync' row because the audit never makes one itself).
1. Every kind of row Auditarr makes has a maker in ROW_MAKER (or is a Today-page notice in TODAY_ONLY), so the blanket clean-ups know what they may close.
2. Every place in audit.py that closes rows is known here, with the reason it may. A new one fails until someone looks at it.
3. The blanket clean-ups ask sweep_may_close(); a fix or a row's own check closes only the row it handled.
Run: python tests/row_owners.py   Exit 1 on failure."""
import os, re, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="auditarr-owners-"); os.environ["CONFIG_DIR"] = TMP; os.environ["REPORT_DIR"] = os.path.join(TMP, "reports"); os.makedirs(os.environ["REPORT_DIR"])
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:300]}]"))
src = open(os.path.join(ROOT, "audit.py")).read(); L = src.split("\n")

# 1. every kind of row has a maker
named = set(audit.CHECK_NAMES)
made = set(re.findall(r'\bF\("([A-Z][A-Z0-9_]+)"', src)) | set(re.findall(r'\bupsert\([^,]+,\s*[^,]+,\s*"([A-Z][A-Z0-9_]+)"', src)) | set(re.findall(r'\bcode(?:, sev)? = "([A-Z][A-Z0-9_]+)"', src)) | set(audit.JOB_MADE)
known = set(audit.ROW_MAKER) | audit.TODAY_ONLY
check("every kind of row Auditarr makes has a maker (ROW_MAKER) or is a Today notice", made <= known, sorted(made - known))
check("every named kind is listed exactly once: a row kind or a Today notice", named <= known and not (set(audit.ROW_MAKER) & audit.TODAY_ONLY), sorted(named - known))
ci = src.index("def check(it, bz_eps"); ce = src.index("\ndef check_", ci) if "\ndef check_" in src[ci:] else len(src)
in_check = set(re.findall(r'\bF\("([A-Z][A-Z0-9_]+)"', src[ci:src.index("\ndef ", ci + 10)]))
check("the per-file checks' kinds are marked 'check' (an import re-check may close them)", all(audit.ROW_MAKER.get(c) == "check" for c in in_check), {c: audit.ROW_MAKER.get(c) for c in in_check if audit.ROW_MAKER.get(c) != "check"})
check("kinds only a whole audit makes are marked 'audit' (an import re-check never sees them)", all(audit.ROW_MAKER[c] == "audit" for c in ("PLEX_MISSING", "MISSING_EPISODE", "MISSING_MOVIE", "MISSING_SCENE", "ORPHAN_FILE", "DUPLICATE_VIDEO")))

# 2. every place that closes rows is known
def encl(i):
    for k in range(i, -1, -1):
        m = re.match(r"\s*def (\w+)\(", L[k])
        if m: return m.group(1)
CLOSERS = {   # function -> why it may close rows
    "resolve_finding": "the helper itself",
    "_ui_act": "a button on that one row (quarantine, re-grab, fix the time bar, fix the audio delay, label the audio ...): it closes the row it fixed",
    "ui_ignore": "the person ignores that row, or that kind of row for a show",
    "accept_language": "the person accepts a show's language: closes that show's language rows",
    "remux_to_mkv": "Rewrap the file closes its own row",
    "subsync_fix": "Re-time the subtitles closes its own row",
    "do_dedupe": "the night moves a duplicate to the trash and closes that row",
    "do_subtag": "the night labels untagged subtitles and closes that file's no-subtitles rows",
    "_t": "fix the time bar (night) closes the row of the file it fixed",
    "whisper_rows": "AI subtitles landed: closes that file's no-subtitles rows",
    "_subsearch": "Bazarr found subtitles: closes that row",
    "_subsync_job": "the timing check found the subtitles in sync: closes its own row",
    "_verify_job": "the content check proved the file right: closes its own row",
    "_followup_job": "the follow-up saw Plex or the arr list the file: closes its own row",
    "arr_has_now": "SWEEP: the end-of-audit clean-up",
    "audit_one": "SWEEP: the import re-check",
    "reconcile_disk_findings": "SWEEP: unknown files that are gone",
}
sites = {}
for i, l in enumerate(L):
    if "UPDATE finding SET resolved_at" in l or re.search(r"\bresolve_finding\(c\w*,", l) or l.lstrip().startswith("def resolve_finding("):
        sites.setdefault(encl(i), []).append(i + 1)
check("every place that closes rows is known, with its reason (a new one: say who may close what, then add it here)", set(sites) <= set(CLOSERS), {k: v for k, v in sites.items() if k not in CLOSERS})
sweeps = [k for k, why in CLOSERS.items() if why.startswith("SWEEP")]
for k in sweeps:
    lines = sites.get(k, [])
    check(f"the blanket clean-up in {k}() asks sweep_may_close before it closes anything", lines and all(any("sweep_may_close(" in L[n - 1 - d] for d in range(0, 3)) for n in lines), [(n, L[n - 1].strip()[:90]) for n in lines])

# 3. the rule itself
d = tempfile.mkdtemp(); p = os.path.join(d, "a.mkv"); open(p, "wb").write(b"x")
check("the end-of-audit clean-up may close a per-file row and a whole-audit row", audit.sweep_may_close("audit", "NO_SUBS", p, p) and audit.sweep_may_close("audit", "PLEX_MISSING", p, p))
check("the import re-check may close a per-file row, never a whole-audit row", audit.sweep_may_close("import", "WRONG_LANGUAGE", p, p) and not audit.sweep_may_close("import", "PLEX_MISSING", p, p) and not audit.sweep_may_close("import", "MISSING_EPISODE", p, p))
check("no clean-up closes a timing row on the same, present file", not any(audit.sweep_may_close(s_, "SUBS_OUT_OF_SYNC", p, p) for s_ in ("audit", "import", "gone")))
check("any clean-up may close a timing row once its file is replaced or gone", audit.sweep_may_close("audit", "SUBS_OUT_OF_SYNC", p, p + ".new") and audit.sweep_may_close("gone", "SUBS_OUT_OF_SYNC", p + ".x", p + ".x"))
check("an unknown kind is never closed by a clean-up", not any(audit.sweep_may_close(s_, "SOMETHING_NEW", p, p) for s_ in ("audit", "import", "gone")))
bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
