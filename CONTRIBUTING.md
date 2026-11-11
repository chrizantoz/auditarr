# Contributing to Auditarr

Thanks for looking under the hood. Auditarr is small on purpose: one Python file, one HTML page, a SQLite file, and a test harness that is bigger than the app. Here is how to work on it without breaking the promises it makes to the people who run it.

## 🧭 The two rules

1. **Nothing is ever deleted and every button is reversible.** A change that moves or rewrites a file goes through the trash and must verify its result. If you add an action, add its undo.
2. **Plain language first.** Every check gets a name a non-technical person understands, every finding one sentence that says what is wrong and what fixes it, every button a verb. Codes, paths and setting names belong in tooltips or the Advanced view. CI enforces this (`tests/plain_language.py`).

## 🗂️ Where things live

| what | where |
|---|---|
| the whole backend: checks, actions, scheduler, HTTP API | `audit.py` |
| the whole page | `ui.html` |
| plain names and sentences | `CHECK_NAMES`, `SEV_NAMES`, `plain_sentence()` in `audit.py`; button labels in the `BTN` map in `ui.html` |
| tests | `tests/` (see `docs/TESTING.md` for what each one proves) |
| UI bots and fixtures | `uibot/` |
| docs for users | `docs/USER-GUIDE.md`, `docs/INSTALL.md` |
| roadmap | `ROADMAP.md` (pick an unticked item or open an issue first for anything bigger) |

## 🧪 Before you open a pull request

```bash
pip install -r requirements.txt && cd uibot && npm install && cd ..
python tests/synthetic_media.py          # detectors on ffmpeg-made files (needs ffmpeg)
python tests/plain_language.py && python tests/contract.py && python tests/regressions.py
cd uibot && node ui_bot.js ../ui.html fixtures && cd ..
sudo mkdir -p /data && sudo chown $USER /data && python tests/e2e_stack.py   # fake arr stack + real daemon
```

CI runs all of that plus a Playwright browser pass, an accessibility scan, visual regression, a fuzzer and a mutation check. A pull request needs the `test` job green; the maintainer merges with squash or a merge commit.

## ✍️ Adding a check

1. Detect it in `check()` with a code, a severity and a technical detail string.
2. Give it a plain name in `CHECK_NAMES` and a sentence in `plain_sentence()`.
3. Add a synthetic case to `tests/synthetic_media.py` that produces it, and a golden sentence to `tests/plain_language.py`.
4. If it needs a button, add the action to `ui_act()`, the label to `BTN`, the confirm text to the click handler, and a row to the user guide. `tests/contract.py` fails if any of those is missing.
5. Describe it in `FEATURES.md` and `CHANGELOG.md`.

## 🐛 Reporting problems

Use the issue templates. For anything security-related, use the private report on the Security tab instead of an issue.

## 📄 Licence

GPL-3.0. By contributing you agree your changes are licensed the same way.
