# 📘 Auditarr user guide

A plain-language tour: what the app does, what you see on the page, and what every button does. No coding needed. If you only read one section, read **Your daily routine**.

## 🔍 What Auditarr does

Every night it opens every video file your Sonarr, Radarr and Whisparr manage and checks what is really inside: audio language, subtitles, runtime, picture format, whether Plex can see it, whether the file even exists. Anything that looks wrong becomes a **finding**. Many findings it fixes on its own with safe, reversible steps. The rest wait for you on the Findings tab with one-click buttons.

Nothing is ever deleted. Any file Auditarr moves goes to a **trash folder** first and can be restored with one click for days or weeks.

## ☕ Your daily routine

1. 📱 You get one push notification in the morning: **"Needs you: …"** and **"Night did: …"**. If it says *Needs you: nothing*, you are done.
2. 👀 If something needs you, open the page. The **Today** panel at the top of Findings repeats the push with buttons.
3. 🔘 Click the button the row suggests, or **Ignore** if you do not care. That is all.

## 🚦 Reading a finding

Each row is one file (or one missing episode) with a coloured chip:

| chip | meaning | what to do |
|---|---|---|
| 🔴 **Won't play** (critical) | the file cannot play at all: no audio, no video, unreadable | Download a better copy |
| 🟠 **Wrong** (high) | wrong content or wrong language, you would notice when pressing play | read the sentence, usually Download a better copy or "xxx is correct" |
| 🟡 **Waiting** (medium) | a real gap that is being worked on: no subtitles yet, episode not downloaded yet | normally nothing, it clears by itself |
| 🟢 **FYI** (low) | information, taste, or compatibility notes | Ignore the whole group if you do not care |

Each row has one plain sentence that says what is wrong and what fixes it. A small **details** link underneath opens the technical text and the file path. A sentence starting with "Checked:" is the result of an automatic content check.

Click a title to open its row. The **Plex ▶** link opens the item in Plex so you can test it in a few seconds. The **Sonarr/Radarr** link opens the show in its arr.

## 🔗 The Setup tab (first time only)

Three steps. **1 Connect your apps**: press **Find my apps** and Auditarr looks for Sonarr, Radarr, Whisparr, Bazarr and Plex on your server's Docker network and LAN; press **Use this** next to what it found. Or paste each app's address and API key (Sonarr and Radarr: Settings → General; Plex: the X-Plex-Token from any item's XML) and press **Test**; green means Auditarr can talk to it. Keys are kept in Auditarr's own database and shown masked afterwards. **2 Media folders**: the folders your apps use must be visible inside the Auditarr container at the same path; the page tells you which are and which are not. **3 Instant checks on import** (optional): press **Connect** per app and Sonarr/Radarr will tell Auditarr about every import, so a bad download is flagged within a minute instead of at night. **4 Your players**: tick what you watch on; compatibility notes then say which player needs a transcode, or vanish. **5 Protect the page**: set a user and password and the page asks for a sign-in from then on (a **Sign out** button appears in the top bar). **5 First audit**: one button; the page follows it phase by phase. After that the audit runs nightly on its own and you live on the Findings tab.

## 🗂️ The tabs

- 🩺 **Findings**: everything open, grouped by show or by kind of problem. The Today panel (Needs you · Done overnight · Tonight's plan) and the AI-subtitles tool live here.
- 🗑️ **Trash**: files Auditarr moved aside, with a **restore** button each and the date they purge for good.
- 📜 **Actions**: a log of everything done, by Auditarr or by you, plus the **Background jobs** list (what is being searched, transcribed or verified right now) and the **re-grab tracker** (did the replacement arrive, and was it better).
- 💽 **Space**: seasons everyone has finished watching and nobody has touched in months, with their size, and seasons nobody has ever watched. **archive** unmonitors them in the arr and moves the files to the trash with a 30-day grace period.
- 📈 **History**: one line per nightly run, the self-test results, library health over time, and **Flaky connections**: services that failed once and recovered. A few is normal; the same one every night is worth a look.
- ⚙️ **Settings**: switches for what Auditarr may do by itself, in plain words (Off / Only report / On). Change, **Save changes**, done. Advanced view shows every knob with its environment name.
- 📡 **Live**: the running audit's progress and the last log lines.
- ℹ️ **About**: version and build, newest release, what it runs in, tools found, database size, uptime, and **Check for updates**. An **Update now** button appears when the host offers an update hook; otherwise it tells you how to update where you installed it.

## 🏷️ The chips in the header

- 💚 **health**: green when the last run finished recently and worked.
- 🧪 **tests**: green when the built-in self-test and the browser bot passed last night.
- ⏳ **working …**: how many background jobs are running and for how long.
- 🤖 **AI subtitles now**: which episode is being transcribed, percent done and the estimated time left.
- 🖥️ **AI engine**: which machine is doing the transcribing. "desktop GPU" is fast (a minute an episode), "server CPU" is the fallback (about twelve minutes an episode).

## 🔎 Filters above the list

**group by** check or show · **library** Anime / TV / Movies / XXX · **check** one kind of finding · **app** Sonarr, Radarr, Whisparr, disk · **search** title, detail or path. Groups you open stay open while the page refreshes.

## 🔘 Buttons on a row

| button | what it does | undo |
|---|---|---|
| 🙈 **Ignore** | hides this one finding for good | delete its line in `ignore.txt` |
| 🔁 **Download a better copy** | moves the file to the trash and tells the arr to download a replacement. Tracked: if nothing arrives in 4 days the original comes back by itself | **Undo** in the toast, or Trash → Put back |
| 🗑️ **Move to trash** | same, for files no arr manages (duplicates, unknown files, samples) | Undo, or Trash → Put back |
| 🏷️ **Set audio language** | the audio really is English (or whatever) but the file says "unknown": writes the correct label. No re-download | the old label is in the Actions log |
| 💬 **Label subtitle tracks** | reads an unlabelled subtitle track, works out its language and labels it | same |
| ✅ **xxx is correct** | tells Auditarr this show is supposed to be in that language (a Filipino drama, a Korean show). Clears every language finding for the show and stops future ones | remove the `audit-lang-xxx` tag on the show in Sonarr/Radarr |
| 🔎 **Find subtitles** | asks Bazarr to search for subtitles right now | — |
| 🤖 **Make AI subtitles** | Whisper listens to the audio and writes English subtitles. About a minute on the GPU; progress shows in the header | delete the `.en.srt` next to the file |
| ⏱️ **Fix the time bar** | the file claims the wrong length, so players show a wrong time bar. Rewrites the container losslessly | Undo, or Trash → Put back |
| 🎚️ **Fix audio delay** | audio starts late: removes the delay losslessly. Hidden when the delay is intentional | Undo, or Trash → Put back |
| ⏳ *(any row showing* **working**) | a job is running for that file (AI subtitles, a content check). It leaves Needs you at once, its buttons are hidden, and it clears by itself when the job lands. If the job gives up, the row comes back. | — |
| 🧐 **Check the episode** / **Check the movie** | compares the dialogue with the neighbouring episodes and the TVDB descriptions (for a movie: with its synopsis) and says whether this really is what it claims to be. The result replaces the sentence on the row | **Check again** runs it again |
| 🔍 **Search now** | for a missing episode or movie: asks the arr to search right now | — |
| 📺 **Refresh in Plex** | asks Plex to rescan that folder | — |
| 📦 **Archive** (Space tab) | unmonitor the season and move it to the trash with a 30-day grace period | Trash → Put back within 30 days |

Every button asks **"sure?"** inline before it does anything. Hover a button to see the technical name it had before (re-grab, fix tag, ...).

## 🎛️ Simple and Advanced view

The page starts in **Simple** view: plain names, one sentence per finding, the technical tabs (History, Live) hidden, and only the settings most people touch. The **Advanced** button in the top bar shows everything: check codes, file paths, every setting with its environment name, and the "Tonight's plan" gate list. The choice is remembered per browser.

## ❓ Help inside the page

Every section has a **?** button that explains it in a few sentences and links here.

## 🧰 Buttons on a group header

- **Ignore "…" everywhere ×N**: silence that kind of finding for every title. One line in `ignore.txt`, easy to undo. Use it for FYI groups you do not care about.
- **Ignore "…" here ×N** (grouped by show): silence that kind of finding for this show only.
- **Search all missing ×N** (grouped by show): one search command to the arr for every missing episode or movie of that title.
- The other group buttons are the row buttons applied to every matching file in the group (Download a better copy ×12, Find subtitles ×5, ...).

## 🛠️ The Tools box

- **AI subs for a show / season / movie**: type the title as it appears in Sonarr or Radarr, optionally a season, press **queue**. Every episode goes through Whisper, one after another. Watch progress in the header or on the Actions tab.
- **Run now**: start a full audit immediately instead of waiting for tonight.
- **run API self-test now**: checks that every button and API still works. Takes about a minute.

## 🩺 What the common checks mean

| check | plain words |
|---|---|
| WRONG_LANGUAGE | the audio is not in the language the arr expects. Low when the whole season agrees (the show is simply in that language) |
| DUB_ONLY | an anime with only the English dub, no Japanese track |
| NO_SUBS | no subtitles anywhere, and Bazarr is still looking |
| UND_AUDIO_NO_SUBS | the audio has no language label and there are no subtitles, so nothing can tell what it is |
| MISSING_EPISODE / MOVIE / SCENE | released a while ago, monitored, but no file yet. The detail shows what the arr has tried |
| RUNTIME_SHORT / RUNTIME_LONG | much shorter or longer than TVDB says. Short usually means a truncated file, long is usually an extended cut or ads |
| TITLE_MISMATCH | the title embedded in the file is a different episode's name |
| DUPLICATE_VIDEO | two files for the same episode in one folder |
| ORPHAN_FILE | a video on disk that no arr knows about |
| BAD_DURATION_HEADER | the file claims a wrong length, players show the wrong time bar |
| AUDIO_OFFSET | the audio starts late; often intentional (a cold open), then fix sync is hidden |
| PLEX_MISSING | the file exists but Plex does not list it |
| HI10P / AV1 / INTERLACED / TRUEHD_ONLY / DV_PROFILE5 | compatibility notes: 10-bit H.264 (anime standard), AV1 video, interlaced video, lossless-only audio, Dolby Vision without a fallback. Some players need a transcode |

## 🌙 What runs by itself every night

- Re-checks every file that changed and every open finding.
- Repairs lying headers, labels unlabelled subtitle tracks, sets the default audio and subtitle tracks (anime: Japanese audio with English subs; everything else: English audio) in the files and in Plex.
- Runs the content check on new runtime and title findings and silences the ones that turn out fine.
- When Bazarr has found no subtitles for 3 days, queues AI subtitles (up to 10 a night).
- When a whole season is consistently in one language, accepts that language for the show.
- Re-grabs files that are truly broken, if **REMEDIATE** is on: only high findings of the kinds listed in `REMEDIATE_CHECKS`, only after the finding has survived two nights, at most 5 a night, and each item only once. The file goes to the trash first.
- Learns from re-grabs: if the replacement shows the same finding, two releases agree and the finding drops to low.
- Reconciles with Plex, checks that tracked re-grabs arrived, backs up its own database, runs its self-test, sends the digest.

## 💬 Frequently asked

**I ignored something and it is back.** Ignore hides the exact finding. If the file was replaced, the new file is a new finding. Use **ignore CHECK everywhere** or the per-show ignore if you never want to see that kind.

**How do I know AI subtitles are working?** The header chip shows the current episode and percent. The Actions tab shows the queue under Background jobs and the result ("subtitle landed after 1.8 min") when it finishes.

**A re-grab came back the same.** The tracker says "same defect". Two releases agree, so the finding drops to low and the re-grab button disappears. Ignore it, or block that release group in the arr.

**How long before AI subtitles are made?** By default a file must sit without subtitles for 3 days (Settings → "days a no-subtitles finding must stay open"). Set it to 1 for the night after. For the same day, turn on the import hook (Setup step 3) and set "after a new download … this many minutes" to, say, 120: Bazarr gets two hours, then Whisper runs.

**Can my family open the page without breaking anything?** Yes. Once sign-in is on, Setup → Protect the page has a **read-only account**: give them that user and password. They see everything, every button is hidden, and even a hand-written request to change something is refused by the server.

**The phone push: can it take me straight to the problem?** Tap it. It opens the page on the Findings tab (or the Trash tab when files were moved). If you reach the page through a public address, set `PUBLIC_URL` to it so the link works away from home.

**Is anything deleted?** No. Moved files sit in the trash for 3 days (duplicates), 14 days (re-grabs) or 30 days (archive) and purge after that. The Trash tab shows the countdown.

## FAQ

**Something failed. Where do I see why?** About tab → Recent errors. Each entry shows when and exactly where in the code it happened, with the full trace, so you can paste it into a bug report. When nothing has gone wrong, About shows a green "No errors in the last 24 hours" and Today shows ✓ no errors.

**A job failed. What now?** It shows up under Needs you on the Findings tab as "A job failed" with the reason and a Retry button. Retry queues it again ahead of routine work. If it fails the same way twice, open About → Recent errors and paste that entry into a bug report.

**Which audio and subtitles does it pick?** By default every title plays in its original language with subtitles in your language: Japanese anime in Japanese, Korean drama in Korean, English titles in English. If your household prefers dubs, set PREFER_DUBS to on and titles play dubbed in your language whenever the file has that track, falling back to the original when it does not. Your language comes from PREFERRED_AUDIO and PREFERRED_SUBS (first entry counts). A tag on a show or movie in Sonarr or Radarr, such as audit-lang-fre, overrides the rule for that title. This is applied inside MKV files (default track flags) and to your own account's selection in Plex; other Plex accounts follow their own Plex language preference.

**A row says "Unknown language, no subtitles" but the file plays fine in English.** The audio track has no language label, so the app cannot know it is English. Press **Detect the language**: Whisper listens to a short sample and the audio is labelled with the language it hears. English audio needs no subtitles, so the row clears on the next check. If it hears Japanese or another language, the file gets subtitles the usual way. The nightly run does this by itself for up to ten files (setting AUTO_AUDIO_LANG). Some releases have subtitles painted into the picture. The app looks for that lettering in a few frames of the lower band; when it finds it, the row says "painted into the picture" at low severity and offers **Show me a frame** so you can see which language it is. Such text is not a subtitle track: no player can switch it, and AI subtitles will show on top of it.

**Can it tell when subtitles are out of sync?** Yes. If the drifting subtitles are a track built into the video file, Bazarr cannot re-time them, so the row offers **Find subtitles** (a separate file that fits) and **Make AI subtitles** instead; for something up next in Plex the night asks Bazarr on its own. Press **Check subtitle timing** in the Tools box for a show or a movie, or wait for the nightly check, which looks at files whose subtitles changed in the last week and at what people are watching. The app listens to three short samples with Whisper and compares the speech with the subtitle times. If the subtitles run a second or more early or late, the file shows up as "Subtitles out of sync" with a **Re-time the subtitles** button. A constant offset is fixed on the spot: an external subtitle file is rewritten, an embedded track is remuxed losslessly, and the original goes to the trash. Subtitles that drift further out as the file goes on are handed to Bazarr, which re-times them against the audio. Either way the timing is checked again afterwards.

**Which files get subtitles first?** Whatever someone is watching right now in Plex, and whatever is next up on their Home. Every 5 minutes the app checks, and for those shows (the next six episodes and the start of the following season) and movies it asks Bazarr to search straight away. If Bazarr still has nothing 20 minutes later, AI subtitles are made. Both jump ahead of every other job. Rows show ▶ watching now or 📺 next up. Settings: WATCH_PRIORITY (on, dryrun, off) and WATCH_PRIORITY_MINUTES.

**Why is a button showing a spinner, and what is the moving bar at the top?** The page is waiting on the app. The spinner sits in the button you pressed until the app answers; the thin bar under the top edge runs while any request is in flight. A pulsing chip means work is going on in the background: a running audit, a row with a job queued, or a title someone is watching.
