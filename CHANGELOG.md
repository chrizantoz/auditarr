# Changelog

All dates 2026. Images: `ghcr.io/chrizantoz/auditarr` (app) and `ghcr.io/chrizantoz/auditarr-uibot` (headless UI test).

## 2026-11-11 — v2.9

### Added
- 👁️ **Read-only login** (F9): Setup → Protect the page → read-only account. A viewer sees the whole page without buttons, and the server answers 403 to any change from that session; the role travels in the signed session cookie.
- 🎬 **Content check for movies** (S1): the dialogue is compared with the movie's synopsis against two dozen decoy synopses from the same Radarr library (matches / mislabeled / inconclusive). Nightly auto-verify and Tonight's plan include movies.
- 📲 **Pushes open the page** (S3): every ntfy notification carries a link to the Findings tab (Trash when files were moved); the page reads `#findings`, `#trash` and the other tab names from the address. `PUBLIC_URL` sets the address used in links.
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
