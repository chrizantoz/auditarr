# 🔍 Auditarr — what it does

Auditarr is a nightly quality-control bot for a **Sonarr / Radarr / Whisparr / Sportarr / Bazarr / Plex** library. Each night it opens every file your arr apps manage, checks what is *actually* inside against what the arr and Plex *believe*, fixes the safe problems on its own, and puts everything else in front of you with one-click buttons. Anything it moves goes to a Trash folder with a countdown first — nothing is deleted outright, and every move can be undone.

### 📖 A few words you'll see

- **A "finding"** is one problem Auditarr spotted on one file (wrong audio language, a broken header, a missing episode…). Findings are grouped so you can deal with a whole show at once.
- 🚦 **Severity** — how much it matters: 🔴 *critical* / 🟠 *high* (worth a look) down to 🟡 *medium* and 🟢 *low* (usually fine, shown only if you ask).
- 🗑️ **Quarantine / re-grab** — move a bad file to Trash and ask the arr to download a fresh copy.
- 🧰 **Remux** — rebuild a file *without* re-encoding, so quality is untouched; used for lossless repairs.
- 🗣️ **Original-language audio** — the language a title was made in (Japanese for anime, Korean for K-drama), as opposed to an English dub.

## 🩺 What it checks (each becomes a "finding")

The left column is the internal code (handy for searching); the middle column is what it actually means.

| Check | In plain terms | Severity |
|---|---|---|
| MISSING_FILE | the arr lists a file that isn't on disk | 🟠 high (🟢 low if it's one of Auditarr's own pending re-downloads) |
| ZERO_SIZE / CORRUPT_HEADER / NO_AUDIO / NO_VIDEO | the file is empty or won't play | 🔴 critical / 🟠 high |
| WRONG_LANGUAGE | the audio is neither the title's original language nor one you prefer | 🟠 high; 🟢 low when a whole season/series is like that, or it's an obvious mis-tag |
| DUB_ONLY | an anime / foreign title whose file has **no** original-language audio track (dub only) | 🟡 medium |
| DUB_NOT_WANTED | you tagged the title `audit-lang-xxx` but the file lacks that audio | 🟡 medium |
| NO_SUBS | no audio you understand **and** no subtitles you understand | 🟡 medium if Bazarr is still searching, otherwise 🟠 high |
| UND_AUDIO_NO_SUBS | the audio language isn't tagged and there are no subtitles | 🟢 low |
| RUNTIME_SHORT / RUNTIME_LONG | the file is much shorter/longer than the episode should be (handles multi-episode files, season quirks, premieres/finales) | 🟠 high / 🟡 medium |
| BAD_DURATION_HEADER | the file claims one length but the video actually ends at another (Plex shows the wrong runtime) | 🟡 medium |
| RUNTIME_DISAGREE | the file and the arr disagree on length (a stale scan) → triggers an arr rescan | 🟡 medium |
| RES_MISLABELED | the picture is smaller than the quality tag claims (e.g. "1080p" that isn't) | 🟡 medium |
| AUDIO_OFFSET | audio starts more than 1.5 s out of sync (recognises intentional intro delays) | 🟢 low |
| TITLE_MISMATCH | the title embedded in the file looks like a different episode | 🟢 low |
| DV_PROFILE5 / DV_PROFILE7 / HI10P / AV1 / INTERLACED / TRUEHD_ONLY | formats some players can't handle | 🟠 high → 🟢 low |
| PLEX_MISSING | the arr has the file but Plex never added it to the library | 🟡 medium |
| ORPHAN_FILE / DUPLICATE_VIDEO | a video on disk no arr knows about / an unmanaged twin of a managed episode | 🟡 medium |
| MISSING_EPISODE / MISSING_MOVIE / MISSING_SCENE | monitored and released, but still no file — shown with the arr's own evidence (queue, last grab/fail, last search, blocklist) | 🟡 medium / 🟢 low |

🏷️ **Tell it how you want a title handled** with arr tags: `audit-ignore`, `audit-lang-xxx`, `audit-nosubs`, `audit-noremediate`. For one-offs, use `ignore.txt` or the ignore buttons in the UI.

## 📺 Knows your players

Tell it which players you own (PS5, Shield, LG TV, Apple TV, Google TV, Fire TV, Roku, Samsung, Plex Web, desktop, phones). Compatibility notes about 10-bit video, AV1, Dolby Vision, lossless audio and interlacing then name the player that would need a transcode, and are dropped when none of yours cares.

## 🙂 Built for people, not just operators

- Plain names and a one-sentence explanation on every finding; buttons are verbs. Codes, file paths and setting names stay out of the way (in **Advanced** view or tooltips).
- Undo after any file move, a "?" help button on every section, and Settings shown as plain switches.
- 👁️ A **read-only login** for the rest of the household: they see everything, the buttons are hidden, and the server refuses any change from that account.
- **Tonight's plan** shows exactly what the next nightly run will do *before* it does it.

## 🔘 What the buttons do (one click, all reversible)

- 🔁 **Re-grab** — move the bad file to Trash and have the arr rescan and search for a better copy; a tracker watches the replacement and reports back (imported / same defect again / restored after N days / got the identical release).
- 🗑️ **Quarantine / Restore / Purge** — Trash organised by type (duplicates 3 days, re-downloads 14, archives 30); Plex and the arr are told about every move.
- 🏷️ **Fix tag** — set the audio-language tag inside an MKV (never overwrites a tag that already looks right). **Accept language for show** — records `audit-lang-xxx` in the arr and clears that show's language findings.
- 💬 **Tag subs** — work out the language of untagged embedded subtitle tracks from their text and label them; sparse tracks get the "forced" flag.
- 🔎 **Find subs** — ask Bazarr to search right now. **AI subtitles** — generate subtitles for a whole show/season/movie through Bazarr's Whisper provider (replaces a mismatched provider subtitle).
- ⏱️ **Fix header** — a lossless remux that repairs a file lying about its length (fixes over-long subtitle cues, keeps styling, drops only tracks it can't repair).
- 🎚️ **Fix sync** — a lossless remux that removes a built-in audio delay; it refuses when the delay is an intentional intro offset.
- 🧐 **Verify content** — samples the dialogue at 15/50/85 % (from embedded/sidecar subs, otherwise Whisper on the original-language audio) and compares it word-for-word with neighbouring episodes (openings/recaps ignored) and TVDB descriptions. Verdicts: CONTENT MATCHES, CONSISTENT, DUPLICATE CONTENT, MULTI-EPISODE, MISLABELED, or inconclusive. Movies are checked against their own synopsis and two dozen decoy synopses from the same library.
- 🔍 **Search** — kick off the arr's search for a missing episode/movie/scene. **Plex refresh** — rescan one folder.
- 📦 **Archive** — unmonitor a season/movie you've watched and haven't touched in a while, and move its files to Trash (30-day countdown).
- 🔍 **Search all missing** — for one show/movie, send the arr a search for every released-but-missing item at once.

## 🌙 What it does on its own every night

- 🧹 Duplicates quarantined, untagged subtitle tracks labelled, lying headers repaired, stale arr media info rescanned, Plex folders refreshed.
- 🧐 New runtime/title findings get a content check; verified-correct extended cuts silence themselves; confirmed mislabels are raised to high.
- ⚡ With the import hook on, a new download without subtitles can get AI subtitles the same day: Bazarr gets a head start of N minutes, then Whisper runs (off by default).
- 🗣️ **Original-language audio by default:** every title is set to play in its original language with English subtitles (Korean drama in Korean, anime in Japanese, Chinese donghua in Chinese), and English-made titles in English — written both into the MKVs and into Plex's remembered per-episode choice.
- 📈 **Watch stats:** Tautulli plays + Plex watched counts + file sizes feed the **Space** tab, listing watched-and-cold and never-watched seasons/movies.
- 💾 A consistent SQLite backup is written to `/config/backups`.
- 🔁 **Auto re-download of serious problems** exists but is off by default and gated: `REMEDIATE=off|dryrun|on` plus an explicit confirm variable.
- 🤖 **AI subtitles when Bazarr gives up** (`AUTO_WHISPER`): no-subtitle findings older than 3 days get a Whisper job (up to 10 a night, one attempt per title per week).
- 🏷️ **Accept whole-season languages** (`AUTO_ACCEPTLANG`): when every file in a season is in one language the arr didn't expect, the title is tagged `audit-lang-xxx` automatically.
- 🧠 **Learns from re-downloads:** if a fresh copy shows the same problem, two releases now "agree," the finding drops to low and the re-grab button hides; the auto-re-download never grabs the same item twice.
- 📭 **Still-missing titles** that nothing has found in 14+ days are listed by show in the nightly digest.

## 📊 What it shows you

- **Today** panel: what needs a human, what the night did, and any errors. The scheduled run sends the same summary as one phone push (ntfy); tapping the push opens the page on the right tab.
- **Findings** grouped however you like (by show → check, check → show, or library → show); each group shows its library (Anime / TV / Movies / XXX) and how recently it was watched, with direct links into Plex and the arr on every row.
- **Trash**, **Actions** (background jobs, re-grab tracker, full action log), **Space**, **History** (findings per run, actions per day, nightly self-test results), **Settings** (every option, saved, with the precedence above), and **Live** (run progress).
- Optional: a read-only daily hand-off to a Paperclip agent to review the high findings.

## 🧪 It tests itself

- The API and detectors run a self-test inside the container after every run (`GET /api/selftest` any time).
- A headless browser bot (jsdom) clicks through the whole UI nightly.
- `/health` returns 503 if the last run is stale or failed; the test result shows as a chip in the header.
- **Staged updates:** a new image is first run against a *copy* of the database with all write switches off, and is only promoted if the self-test passes and the finding count doesn't swing.

## 🔌 What it talks to

Sonarr / Radarr / Whisparr (v3 API), Bazarr (search, Whisper provider, subsync), Plex (libraries, inventory, partial scans, stream selection, deep links), Tautulli (plays / last played), ntfy (notifications), Paperclip (optional agent hand-off), a read-only Docker socket proxy (for live Whisper progress), and subgen (Whisper) behind an nginx failover router (GPU first, CPU fallback).
