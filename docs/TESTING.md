# How Auditarr tests itself

A one-person project that moves other people's files has to be paranoid. This is the harness, in the order a change passes through it. Copy whatever is useful; everything here is plain Python, Node and shell.

## 1. On every push (GitHub Actions, `.github/workflows/ci.yml`)

| step | what it proves | file |
|---|---|---|
| compile + pyflakes | no syntax errors, no undefined names | `audit.py`, `tests/*.py` |
| synthetic media | 26 ffmpeg-generated files, each with one known defect (French audio, no audio, 10-bit, wrong length, duplicate, orphan…), must produce exactly the expected finding and severity | `tests/synthetic_media.py` |
| slow-service drill (host, weekly rotation) | Sonarr behind a 200 s latency proxy: the staged read-only run must finish within 25 minutes, report Sonarr as degraded and write nothing, proving the client timeouts work | `docs/auditarr-deploy.sh drill 4` |
| restore drill (host, weekly) | the newest nightly state backup is integrity-checked and booted in a throwaway container; the page must come up with its open findings and run history; a backup older than 48 h fails | `docs/auditarr-deploy.sh restore` |
| persona tasks (live browser) | three tasks a non-technical person would try, done only through the page against the live fake stack and judged by outcome: find out why an episode is missing, make AI subtitles for a whole show, ignore a finding; plus trash → Undo | `uibot/browser_bot.js` (LIVE mode, run by `tests/e2e_stack.py`) |
| performance budget | 10,000 files, 5,000 open findings, 2,000 actions seeded; report build, page state, Today, scores, plan and history must each finish within a fixed budget, the state payload under 12 MB, peak memory under 900 MB | `tests/perf_budget.py` |
| fake arr stack | `tests/mock_stack.py` is also published on its own as [arr-mock](https://github.com/chrizantoz/arr-mock) for other arr companion apps; keep the two in sync when the mock changes | `tests/mock_stack.py` |
| chaos in the fake stack | random 500s, slow answers and Radarr dropping connections mid-run: the audit ends degraded (not crashed), keeps the vanished app's findings open, makes no writes, and the next run is clean again | `tests/e2e_chaos.py` |
| golden digest | the morning push pinned word for word from a fixed world, delivered to a mock ntfy with title, priority and a link into the page; a 'working' row must stay out of Needs you | `tests/golden_digest.py` (`--update` re-pins) |
| plain-language guard | 19 pinned sentences; every check name, sentence and button label is code-free, jargon-free, short, and ends in a full stop | `tests/plain_language.py` |
| contract | every action the page can send exists in the backend, has a confirm sentence and a label; every API path the page calls exists | `tests/contract.py` |
| jsdom bot | 46 checks against sanitized fixtures from a real library, plus five edge worlds (empty, 2000-finding show, every service down, API errors, fresh install) | `uibot/ui_bot.js`, `uibot/fixtures/`, `uibot/edge_fixtures.py` |
| real browser (Playwright) | phone and desktop: no horizontal scroll, controls on screen, plain words only, help opens, confirm before every write, accessibility scan (axe, serious/critical fail), 60-click seeded monkey, screenshots | `uibot/browser_bot.js` |
| visual regression | screenshots vs committed baselines, >1 % pixel change fails | `uibot/visual_diff.js`, `uibot/baseline/` |
| end to end | a fake Sonarr/Radarr/Bazarr + synthetic media under `/data` + the real daemon: findings appear, Move to trash moves the file, Undo restores it, re-download sends the arr its commands, the import webhook flags a bad file within seconds, sign-in gates the page, secrets never leak, the built-in self-test passes, and the browser bot clicks the real page | `tests/e2e_stack.py`, `tests/mock_stack.py` |

Only after all of that does CI publish the image.

## 2. Before production (on the Docker host, `docs/auditarr-deploy.sh update`)

1. Pull `:latest`; stop if the digest is unchanged.
2. Start the candidate as a **staging** container on another port with a copy of the real database and `READ_ONLY=1`: every write to an arr, Bazarr, Plex or a media file is refused and counted (`guard_write` in `audit.py`), independent of the per-feature gates. Staging also runs with every nightly automation gate off and `JOBS=off`.
3. Let it run a full audit and its self-test.
4. Judge: self-test green, run finished, total findings within 20 % of production, no check more than doubled (medium and above), zero write attempts.
5. **Promote** only then: wait until production is idle (`/health` reports `busy: 0`), recreate the container, remember the previous image for `rollback`.

## 3. Every night, in production

- `/api/selftest` after each run: API and detector checks with reversible actions.
- The jsdom bot against the live page (the `auditarr-uibot` image; it fetches the page from the running app unless one is mounted at `/app/ui.html`), result shown in the header chip and the History tab.
- Backup proof: the nightly SQLite backup is reopened and integrity-checked.
- Every Sunday a **fault drill**: the production image is staged with Bazarr, Plex or Whisparr pointed at a dead port (rotating); the run must finish, report the outage as degraded, and write nothing.

## 4. Rules that made the difference

- **Tests must bite.** When CI's accessibility scan rejected a new theme twice, that was the harness doing its job. Assertions that cannot fail are decoration.
- **Prove writes, do not list them.** A read-only mode that counts attempts beats a checklist of feature gates: the next feature cannot forget to be on the list.
- **Compare per check, fail on increases.** Totals hide a detector that silently vanished or doubled; drops are usually fixes landing, so they are notes, not failures.
- **Real files, fake services.** ffmpeg can make a 30-second file with any defect you like in a second; a fake arr is a 150-line HTTP server. Together they let the whole pipeline run on a CI runner.
- **Sanitize fixtures, then use them everywhere.** The same scrubbed snapshot feeds the bots, the edge worlds and the public demo.

## Running it locally

```bash
python tests/synthetic_media.py            # needs ffmpeg
python tests/plain_language.py && python tests/contract.py
cd uibot && npm install jsdom@24.1.3 && node ui_bot.js ../ui.html fixtures && cd ..
cd uibot && npm install playwright@1.49.1 axe-core@4.10.2 && npx playwright install chromium-headless-shell && node browser_bot.js ../ui.html fixtures && cd ..
sudo mkdir -p /data && sudo chown $USER /data && python tests/e2e_stack.py   # fake stack + real daemon + browser bot
```
