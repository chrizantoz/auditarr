# Changelog

All dates 2026. Images: `ghcr.io/chrizantoz/auditarr` (app) and `ghcr.io/chrizantoz/auditarr-uibot` (headless UI test).

## Unreleased
- Nightly UI bot: the button checks look only at rows the page actually draws under the current filters (two false alarms on production when every no-subtitles row was FYI and hidden).
- New addresses: the public demo is https://auditarr-demo.chrizantoz.com and the owner's instance https://auditarr.chrizantoz.com; the old demo.chrizantoz.com and media-audit.chrizantoz.com keep working.
- Subtitles out of sync, when the drifting subtitles are a track inside the video file: the row now says that Bazarr cannot re-time a built-in track and offers Find subtitles and Make AI subtitles instead of a Re-time button that could only fail (seen on Chucky S01E02/E04, 2026-11-11). Background jobs now show their outcome next to DONE, in words ("out of sync and drifting: 9.2 s late 32% of the way in, 0.3 s late at 88%; needs re-timing, not a shift"), so a finished timing check is not mistaken for a fix.
- Fixed: after saying yes to "Make AI subtitles for a whole show", the box stayed on "working…" with its buttons greyed out until the page was reloaded; the button now comes back as soon as the job is queued. Found by the browser bot on the public demo. The bot also ignores resources the network blocks outside the app (a proxy's analytics beacon) and names the step it got stuck on.
- The page no longer breaks on a half or odd API answer (found by the new data fuzzer, Q34): every answer is shaped before it is drawn.
- Subtitles painted into the picture (F32): files reported without subtitles get eight frames of their lower band inspected for burned-in lettering; when found, the row says so at low severity instead of "no subtitles" and offers Show me a frame so you can tell the language.
- Public playground at https://auditarr-demo.chrizantoz.com: try every button on fake files without installing anything; it resets on the hour.
- Fixed: a fix applied while an audit was running (for example Set audio language pressed mid-run) was undone when the audit finished and wrote back what it had seen earlier; the self-test then reported a detector disagreement. The write-back now leaves anything resolved during the run alone, and the self-test skips a file that changed after it was flagged.
- Playground (U10): `sandbox/` runs the real app next to a fake Sonarr, Radarr, Bazarr, Plex and Whisper on synthetic files with real defects, every button working, reset on the hour. `SANDBOX=1` locks connections, Find my apps, webhook setup, the update hook and passwords, tests only the fake hosts, and limits each visitor to 120 changes a minute; the page shows a playground banner.
- The night's audio choice is now a setting (U27): original language with subtitles in your language (default), or dubbed in your language when the file has it. Subtitles follow your preferred subtitle language instead of always English.
- Fixed: on a fresh install the page never redrew by itself after the first audit (the yellow setup banner was mistaken for an open confirm box, so new results stayed pending until a reload). Found by the new first-run walkthrough.
- Which language is spoken (F33): files whose audio has no language label get a short Whisper listen and the label it hears. English means no subtitles are needed and the row clears; other languages get subtitles as usual. Nightly, and a Detect the language button on Unknown language rows.
- AI subtitles via Bazarr: the job now waits for a new Bazarr history entry instead of comparing clocks, so Bazarr and Auditarr may run in different time zones (found by the fake Bazarr answering in real shapes).
- Testing (Q24, second half): the fake stack gained a Plex (sections, library, On Deck, sessions, refresh) and real-shaped Bazarr answers; the stack test now covers not-in-Plex, next-up, watch priority and a landed AI subtitle end to end.
- Testing (Q33): a real browser now walks through a blank first install in CI: Setup, Test, Save connections, reload, Find my apps, first audit.
- Testing (Q32): every button on the page is now pressed by a bot in CI, and a lint fails the build when a new button is added without a press.
- Fixed: the Save connections button on the Setup page did nothing (an error was thrown before the request was sent, with no message). Reported by the first outside user on the Unraid forum. The page now also shows a message whenever something goes wrong instead of failing silently, and the fresh-install bot presses Save connections in CI.
- The nightly page check records which build of the bot tested which build of the page, and About warns when they differ (Q27).
- Failed jobs ask for a click (Q31): a background job that stopped with an error now appears under Needs you with a Retry button and the reason it stopped.
- CI runs every Monday on its own and can push a warning when that run fails (Q30).
- Subtitle timing: the three samples are now taken where the subtitles are densest, so openings, endings and quiet scenes no longer leave the check unable to judge.
- Testing (Q28): bot checks may no longer hard-code counts, and a tidy library is now one of the fixture sets the page must pass.
- Settings type safety (Q29): a test saves every setting as text the way the UI does and runs the planners and summaries against it, and lints the code for raw numeric reads.
- Real answers in the fake stack (Q24, first half): sanitised recordings of what real Sonarr, Radarr, Whisparr, Bazarr and Plex answer now live in tests/shapes, and the fake arr services carry every field the real ones send. A test guards both fidelity and privacy of the recordings.
- Staging soak before promotion (Q26): a new image is promoted only after a read-only run on a copy of the real database passes the self-test, the UI bot, an empty error log, zero write attempts, and a findings comparison with production.
- Subtitle timing (F20): the app can now tell when subtitles run early, late or drift. It listens to three short samples with Whisper and compares them with the subtitle times. Out-of-step files become findings with a Re-time button that fixes a constant offset on the spot (external file rewritten, embedded track remuxed losslessly) or asks Bazarr to re-time drifting subtitles. Checked nightly for recently changed subtitles and for what people are watching; on demand per show or movie from the Tools box.
- No swallowed errors (Q25): any failure inside a job or hook now keeps its full traceback in errors.log and on the About tab under Recent errors, ready to paste into a bug report. About shows a green "No errors in the last 24 hours" line when all is well, Today says ✓ no errors, and the health check counts errors.
- Subtitles first for what people are watching (F31): every 5 minutes, whatever is playing in Plex or next up on a Home gets its missing subtitles pushed to the front: Bazarr search now, AI subtitles if Bazarr still has nothing 20 minutes later. Settings WATCH_PRIORITY and WATCH_PRIORITY_MINUTES. Rows show ▶ watching now.
- The page shows when it is busy (U26): a moving bar while a request is in flight, a spinner in the pressed button, pulsing chips for running work.
- History: the actions-per-day table and the 90-day summary now also count AI subtitles made, dialogue checks, default-track fixes, time-bar repairs and Plex refreshes. Failed attempts are not counted.
- Sign-in: the role written into the session cookie now comes from the account name alone; the password check is only the gate. Clears a scanner warning that mistook the role for the password. The read-only account can no longer share a name with the main account.

## v0.9.1 — 2026-11-11

### Added
- 👁️ **Read-only login** (F9): Setup → Protect the page → read-only account. A viewer sees the whole page without buttons, and the server answers 403 to any change from that session; the role travels in the signed session cookie.
- 🎬 **Content check for movies** (S1): the dialogue is compared with the movie's synopsis against two dozen decoy synopses from the same Radarr library (matches / mislabeled / inconclusive). Nightly auto-verify and Tonight's plan include movies.
- 📲 **Pushes open the page** (S3): every ntfy notification carries a link to the Findings tab (Trash when files were moved); the page reads `#findings`, `#trash` and the other tab names from the address. `PUBLIC_URL` sets the address used in links.
- 🔍 **Find my apps** on the Setup page (U14): looks for Sonarr, Radarr, Whisparr, Bazarr and Plex on the server and fills the address; says when a key is already mounted.
- 🧭 Connection errors now say what happened and what to do (U15): wrong key and where to copy it, nothing listening and the Docker 'localhost' trap, name not found, timed out, URL base, certificate.
- 🧩 Auditarr is in Unraid **Community Applications** (auto-approved 2026-11-11): Apps tab → search Auditarr → Install.
- 🧘 The page no longer pulls the list out from under you: background refreshes update the header chips only; the lists re-render when something changed and you have been idle for a few seconds, otherwise a small ↻ refresh button appears. Focus, scroll position and open groups survive a refresh. (Owner: "it keeps refreshing and I lose my place.")
- ℹ️ **About** tab: version, build, newest release, runtime, tools, database size, uptime; **Check for updates**, an "update available" chip in the header, and **Update now** through an optional host hook (`UPDATE_HOOK_URL`). The image now carries its version (`APP_VERSION`) and build (`APP_REVISION`).
- 🔄 The instant check after an import now also clears that file's earlier findings that are gone (a replaced or re-tagged file no longer waits for the night to drop off the list).
- 🧯 Two host drills in `docs/auditarr-deploy.sh`: `restore` boots last night's backup in a throwaway container and checks it, `drill 4` puts Sonarr behind a 200-second latency proxy to prove the audit gives up on a slow service in time.
- 🧪 The fake Sonarr/Radarr/Bazarr used by the tests is published as its own small project, [arr-mock](https://github.com/chrizantoz/arr-mock), chaos mode included.
- 🧑‍💻 Persona tasks in the live browser test: a pretend first-time user searches for a missing episode, makes AI subtitles for a whole show and ignores a finding, using only the page.
- 🔁 Flaky connections are forgiven once and remembered: a read that fails and works on retry no longer fails the run, and the History tab lists which service wobbles and how often (30 days).
- ⏱️ Performance budget in CI: a 10,000-file / 5,000-finding library must build its report and page state within fixed time and size limits.
- 🌪️ Chaos test in CI: the fake stack throws 500s, slow answers and a vanishing Radarr at the audit; it must finish degraded, keep that app's findings, write nothing, and recover on the next run.
- 📲 The morning push lists problems by their plain names ("Won't play · No audio: Beta Movie") instead of codes, and is now pinned by a golden test.
- ⏳ Rows with a running job (AI subtitles) show **working**, leave Needs you immediately and resolve by themselves when the subtitles land; if the job gives up the row returns. (Owner: "if I trigger Make AI subtitles it should leave the row and Needs you.")
- ⚡ **Same-day AI subtitles** (F14): with the import hook connected, a new download that has no subtitles gets Whisper subtitles `AUTO_WHISPER_IMPORT_MINUTES` after it lands if Bazarr has not found any by then (default 0 = off; the nightly `AUTO_WHISPER_DAYS` rule stays).
- Tests: viewer role in `authz.py` and the end-to-end stack (53 checks), regression cases for the movie check and the cookie-before-localhost order.

## 2026-11-11 — v2.8 "for humans"

### Added
- End-to-end test in CI: a fake Sonarr/Radarr/Bazarr (`tests/mock_stack.py`) + synthetic media + the real daemon; actions move real files, Undo restores them, arr commands are asserted, the built-in self-test runs inside it, and the Playwright bot clicks the real page (trash → Undo).
- Plain-language guard (`tests/plain_language.py`: 19 golden sentences + jargon/length lint on every name, sentence and button) and a UI↔API contract test (`tests/contract.py`).
- Unreachable services are tracked per run (health `degraded`, Today panel, header chip); weekly fault drill (`auditarr-deploy.sh drill`, Sundays) proves the audit finishes with Bazarr, Plex and Whisparr down and writes nothing.
- Orphan files are re-checked live against the arr before being reported (imports that land mid-run are not orphans).
- Phone layout (tables stack under 760 px) verified by the browser test at 400 px.
- Test fixtures are now generated from a running instance and sanitized (`uibot/make_fixtures.py`); four edge worlds (empty library, 2000-finding show, every service down, API errors) run through the UI bot in CI (`uibot/edge_fixtures.py`).
- Browser run adds an axe-core accessibility scan (fails on serious/critical) and a 60-click seeded monkey (no JavaScript errors, every write preceded by a confirm box); controls have accessible names.
- The weekly fault drill rotates: all three services, then Bazarr, Plex, Whisparr alone.
- Device profiles (F4): tick the players you own on the Setup page; 10-bit, AV1, Dolby Vision, lossless-audio and interlaced notes then name the affected player ("your PlayStation 5 cannot play it directly") and disappear when none of your players cares.
- More safety tests: authorization rules tested directly (open mode vs sign-in mode, proxy header only with TRUST_PROXY_AUTH, tampered and expired cookies), a Trivy scan of the built images in CI (fails on fixable critical/high CVEs), and an upgrade test that boots the daemon on the previous release's database (`tests/db-samples/`).
- CodeQL: path handling in restore normalised before the trash-root check, ignore rules parsed without a regex, CI token read-only by default; two cookie-signature alerts dismissed as false positives with reasoning.
- Watch-ahead priority (F2): Plex On Deck decides which files are 'next up'; they get a 📺 badge, their groups sort first, the Today panel lists their problems first (down to medium), and the nightly content checks and AI subtitles start with them.
- Library health (F3): one score per library (100 minus weighted open findings per 100 files), shown as chips on Today, in the morning digest, and as a trend chart in History.
- Tests with teeth: a detector fuzzer (4,500 random probe results per run, found two crashes on odd arr metadata, both fixed), a regression corpus with one case per real incident, and a mutation check that breaks a detector on purpose and demands the suite notice. MP4 rewrites now verify the swapped file against a fingerprint of the verified copy and roll back on mismatch; the bulk fix-tag button is MKV-only.
- Security: Dependabot alerts and weekly update PRs, secret scanning with push protection, CodeQL default setup, SECURITY.md with private vulnerability reporting.
- Built-in sign-in (F13): Setup step 4 sets a user and password (PBKDF2-hashed in the database, or `AUTH_USER`/`AUTH_PASSWORD` env); login page, signed session cookie, Sign out; `/health` and the webhook stay open; `TRUST_PROXY_AUTH=1` honours a proxy's Remote-User. Until a password is set the page keeps the LAN-only model.
- Going public: GPL-3.0 license (owner's choice), home-specific files moved out of the tree (owner scripts under `tests/homelab/`, personal template and notes removed), neutral defaults (no built-in host IP, generic transcriber names, Paperclip off), generic deploy script reading a site env file.
- Import hook (F1): one click on the Setup page registers an 'Auditarr' webhook in Sonarr/Radarr (on import and upgrade, token-protected URL); the new file is listed, probed and checked within seconds, the finding appears immediately, and a push goes out if it is serious. The nightly run stays the safety net.
- Visual identity: teal accent, magnifier-check logo and favicon, light theme (system preference or the ◐ toggle), consistent chips/buttons; `docs/icon.png`.
- First-timer install docs (`docs/INSTALL.md`), `docs/docker-compose.yml`, generic `docs/unraid-template-public.xml`.
- Setup tab (Layer 2): connection cards with Test buttons for Sonarr, Radarr, Whisparr, Bazarr and Plex, saved connections (masked secrets) that override the env and mounted files, media-folder visibility check, guided first audit with plain-language phases; banner on Findings until connected and audited. README quick start is now one mount + the wizard.
- Group headers moved out of the collapsible summary (accessibility: no buttons nested in a control); a 5th edge world "fresh install" runs the wizard through the UI bot.
- Plain-language UI: every check and severity has a name ("Wrong language", "Won't play"), every finding leads with one sentence that says what is wrong and what fixes it, buttons are verbs ("Download a better copy", "Make AI subtitles"), old names stay as tooltips. Undo in the toast after any file move. Simple view by default (technical tabs, codes and paths behind **Advanced**), "?" help on every section, Settings as grouped plain switches (Off / Only report / On).
- Per-row **Make AI subtitles** button; **Tonight's plan** in the Today panel (`/api/plan`): what the next run will re-download, transcribe, accept or repair, from the same selection code the night uses.
- Backup proof: the nightly SQLite backup is reopened and integrity-checked; result in History and in the health status.
- Testing foundation: `READ_ONLY=1` mode with a write ledger (staging runs read-only and fails on any write attempt), per-check staging comparison (fails on new medium+ noise), pyflakes in CI, 26 synthetic-media detector tests in CI (ffmpeg-generated files with known defects), 47 UI-bot checks including plain-language rules.
- Season-consistent DUB_NOT_WANTED is FYI (a show tagged for one language with a season in another).

## 2026-11-11 — v2.7

### Added
- Nightly AI-subtitle fallback (`AUTO_WHISPER`): Bazarr-failed NO_SUBS / UND_AUDIO_NO_SUBS findings older than `AUTO_WHISPER_DAYS` get a Whisper job, `AUTO_WHISPER_MAX` per night.
- Nightly accept-language (`AUTO_ACCEPTLANG`): whole seasons consistently in one unexpected language are tagged `audit-lang-xxx` in the arr.
- "Search all missing" per show/movie group (`/api/search_show`) and an UNFOUND line in Today/digest for items nothing has found in 14+ days.
- Re-grab learning: same-defect replacements downgrade the finding to low, hide re-grab, and block auto-remediation for that item; auto-remediation also tries each item at most once per 30 days.
- Staging runs with `JOBS=off` (never runs the copied job queue against the real Bazarr/Whisper).

### Changed
- REMEDIATE is on in production (high WRONG_LANGUAGE / RUNTIME_SHORT only, after two runs and 20 h, max 5 per night, trash first).

### Fixed
- Self-test "regrab refused for a missing item" matched MISSING_FILE and failed staging falsely.
- Whisper job completion detection read Bazarr's prose timestamp; now uses `parsed_timestamp`.

## 2026-11-10 — v2.3 → v2.6 (one long day)

### Added
- Plex integration: folder refresh after every file action, PLEX_MISSING reconciliation, per-row Plex deep links, Plex remembered track selection.
- Tracked re-grabs with verified outcomes (imported / same defect / restored after `REGRAB_WAIT_DAYS` / identical release re-downloaded) and automatic restore from the trash.
- Bazarr: one-click search, Whisper (AI) subtitles via a paced, persisted job queue, "AI subtitles for a whole show/season/movie" tool; Bazarr tuned (subsync on, extra provider, minimum score).
- Settings tab (persisted overrides), History tab (runs, actions per day, self-tests), Today panel + daily digest push, Space tab (watched-and-cold / never-watched with Archive), library categories (Anime / TV / Movies / XXX), per-app scanned counts, background-job visibility with live Whisper progress.
- New checks: DUB_ONLY, MISSING_EPISODE / MISSING_MOVIE / MISSING_SCENE (with the arr's search evidence), BAD_DURATION_HEADER, PLEX_MISSING.
- New actions: tag subs, accept language for show, fix header, fix sync (with intentional-delay guard), verify content (neighbour-file cross-check + description scoring, boilerplate-aware, original-language audio), search, plex refresh, archive.
- Night automation: auto-verify new runtime/title findings, auto fix headers, auto-silence verified cuts, default audio/subtitle flags (original language + English subs), nightly SQLite backup.
- Self-tests: in-container API/detector suite after every run, nightly headless UI bot, `/health` 503 on stale/failed runs.
- Packaging: GitHub repository, CI (compile + UI bot on fixtures → GHCR images), staged updates on the host (new image tested against a database copy before promotion, rollback kept).
- GPU speech-to-text: nginx failover router sends Whisper jobs to a desktop RTX GPU (large-v3) and falls back to the server CPU.

### Fixed
- Per-finding ignore rules never matched (comment parsing).
- "New findings this run" accumulated across runs.
- Open groups collapsed and scroll jumped on background refresh.
- Re-grab tracker misread same-named replacements as restores; identical re-downloads now reported.
- DUPLICATE_VIDEO race after a re-grab (replacement flagged as a duplicate of the stale arr record); quarantine refuses the last copy.
- Content check false positives from shared opening/ending/recap text and from dubbed default audio tracks.
- Fix header: handles over-long subtitle cues (start or duration) and mistimed tracks; repairs ASS tracks natively.
- Bazarr container OOM under the new load (memory cap raised).
- Nightly run slowdown from end-of-stream probes (budgeted, cached).

### Changed
- Season premieres/finales: RUNTIME_LONG is low. Mis-tagged release languages (non-dub languages) are low with a fix-tag path.
- AUDIO_OFFSET recognises intentional intro delays (video cut at the same timestamp) and hides fix-sync for them.
- Whisper renamed "AI subtitles" in the UI.
- Code is bind-mount-free: the container runs the published image; edits go through the repository.

## 2026-11-09 — v2.0 → v2.2
- First release: nightly audit of Sonarr/Radarr/Whisparr files (ffprobe-based checks, orphan scan), reports (HTML/JSON/MD), live progress page, ntfy, optional Paperclip hand-off, gated remediation and dedupe with a trash folder, Unraid template, Uptime Kuma monitor, Homepage card, Authelia-protected public hostname.
- UI "Auditarr": grouped filterable findings, inline confirm actions (ignore, quarantine, re-grab, fix tag), bulk per show, Trash and Actions tabs.
