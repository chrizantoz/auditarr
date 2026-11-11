"""Plain-language guard: the sentences and names people see must stay human.
1. Golden tests: known detail strings produce the pinned sentence (a changed template is a deliberate change, update the table).
2. Lint: every check name, every sentence for every check, and every button label in ui.html is code-free, jargon-free, short, and a sentence.
Run: python tests/plain_language.py   Exit 1 on failure."""
import os, re, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp()); os.environ.setdefault("REPORT_DIR", os.environ["CONFIG_DIR"])
sys.path.insert(0, ROOT)
import audit  # noqa: E402

results = []
def check(name, cond, info=""): results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:220]}]"))

GOLDEN = [
 ("WRONG_LANGUAGE", "high", "Show S01E02", "audio=['fre'] original=eng wanted=['eng']", "sonarr",
  "The audio is French but this should be English. A re-download usually fixes it."),
 ("WRONG_LANGUAGE", "low", "The Alibi S01E01", "audio=['fil'] original=eng - but 100% of this season's 14 files are ['fil'], so probably intended / metadata wrong. Tag the series audit-lang-fil to accept, or fix its language in the arr", "sonarr",
  "The whole season is in Filipino, so that is probably the show's real language, not a broken file."),
 ("WRONG_LANGUAGE", "low", "X S01E01", "[two releases agree: the re-grabbed replacement has the same WRONG_LANGUAGE - probably not a defect; re-grab hidden, ignore it if you agree] || audio=['fre'] original=eng", "sonarr",
  "Two different downloads look the same here, so this is how the show is, not a broken file. Safe to ignore."),
 ("DUB_ONLY", "medium", "Anime S01E01", "anime with audio=['eng'] and no jpn track - dubbed-only release; re-grab fetches a Japanese-audio or dual-audio release", "sonarr",
  "This anime only has English audio, no Japanese track. A re-download fetches a dual-audio release."),
 ("NO_SUBS", "medium", "Anime S01E03", "audio=['jpn'] embedded_subs=[] external=[] | Bazarr still searching", "sonarr",
  "No subtitles yet. Bazarr is still searching; if it finds nothing for a few days, AI subtitles are made automatically."),
 ("UND_AUDIO_NO_SUBS", "low", "Jersey Shore S01E01", "audio language untagged, no subtitles anywhere | Bazarr searching", "sonarr",
  "The audio has no language label and there are no subtitles, so players cannot tell what it is. Usually just a missing label."),
 ("MISSING_EPISODE", "medium", "Show S01E04", "released 2026-10-12 (30 days ago), monitored, no file | not in queue - press 'search' to look again", "sonarr",
  "Released 30 days ago and not downloaded yet. Nothing found on your indexers so far. 'Search now' tries again."),
 ("NO_AUDIO", "critical", "Beta Movie (2021)", "no audio stream", "radarr", "There is no audio track in this file. It needs re-downloading."),
 ("SAMPLE_FILE", "high", "Show S01E01", "40 MB, 0.3 min where 25 min expected - sample/stub?", "sonarr", "This is a tiny sample, not the real episode. It needs re-downloading."),
 ("RUNTIME_SHORT", "high", "Show S01E01", "file 12 min vs expected 25 min (1 ep) - truncated or wrong content?", "sonarr",
  "The file is 12 minutes but the episode should be about 25. Probably cut off."),
 ("RUNTIME_LONG", "low", "Show S01E01", "file 48 min vs expected 25 min - season premiere/finale, extended episodes are normal there", "sonarr",
  "The file is 48 minutes where about 25 were expected. Premieres and finales are often extended, so this is probably fine."),
 ("TITLE_MISMATCH", "low", "Show S01E02", "embedded title 'Other Episode' does not match 'Show' / Second", "sonarr",
  "The title written inside the file names a different episode. Often harmless (fansub naming), 'Check the episode' settles it."),
 ("DUPLICATE_VIDEO", "medium", "x.mkv", "900 MB in sonarr root /data/tv - not managed by any arr (same episode as a managed file)", "disk",
  "There are two files for this episode in the folder. Only one is in use; the other is wasting space."),
 ("ORPHAN_FILE", "medium", "x.mkv", "900 MB in sonarr root /data/tv - not managed by any arr", "disk",
  "A video file nobody manages: neither Sonarr nor Radarr knows about it, and Plex may not either."),
 ("MISSING_FILE", "high", "Show S01E01", "path not found on disk (arr still lists it)", "sonarr", "Sonarr still lists this file but it is not on disk. A rescan will fix the record."),
 ("BAD_DURATION_HEADER", "medium", "Show S01E01", "container header says 48 min but the streams end at 24 min (expected 24) - the header is wrong", "sonarr",
  "The file claims the wrong length, so players show a wrong time bar and may stop early. 'Fix the time bar' repairs it without re-downloading."),
 ("AUDIO_OFFSET", "low", "Show S01E01", "audio delay 2.10s matches the end of the intro segment at 2.10s - intentional: the source's audio starts after a cold open", "sonarr",
  "The audio starts a moment after the video. That is intentional here (the video is cut at the same spot), nothing to do."),
 ("PLEX_MISSING", "medium", "Show S01E01", "file exists on disk, Plex has not indexed it", "sonarr", "The file is on disk but Plex has not picked it up. 'Refresh in Plex' makes it rescan the folder."),
 ("TITLE_MISMATCH", "low", "Show S01E02", "[verified 14:24: CONTENT MATCHES S01E02 (3/3 samples)] || embedded title 'x'", "sonarr", "Checked: the dialogue matches this episode. Safe to ignore."),
 ("AV1", "low", "Show S01E01", "AV1 video - PS5 / older Shield / older TVs need a transcode | affects: PlayStation 5", "sonarr", "AV1 video: your PlayStation 5 cannot play it directly, so Plex transcodes for it."),
 ("SUBS_OUT_OF_SYNC", "medium", "Show S01E01", "external 'x.en.srt': subtitles 2.4s late (3/3 samples agree) | fix: shift | lang=en | src=sidecar:/x.en.srt", "sonarr",
  "The subtitles run about 2 seconds late through the whole file. 'Re-time the subtitles' shifts them into place, no re-download."),
 ("SUBS_OUT_OF_SYNC", "medium", "Show S01E01", "external 'x.en.srt': subtitles drift: +0.3s at 15% -> +2.9s at 85% (3/3 samples) | fix: re-time in Bazarr | lang=en | src=sidecar:/x.en.srt", "sonarr",
  "The subtitles start close but slide further out as the file goes on. 'Re-time the subtitles' has Bazarr fit them to the audio again."),
 ("NO_SUBS", "medium", "Anime S01E03", "[working: AI subtitles queued 14:02] || audio=['jpn'] embedded_subs=[] external=[]", "sonarr", "AI subtitles queued 14:02. This row clears by itself when it is done; nothing to do."),
]
for code, sev, label, detail, app, want in GOLDEN:
    got = audit.plain_sentence(code, sev, label, detail, app); check(f"golden {code}/{sev}", got == want, f"got: {got}")

RAW = re.compile(r"\b[A-Z]{2,}_[A-Z_]{2,}\b")
JARGON = re.compile(r"\b(arr|arrs|regrab|re-grab|remux|ffprobe|ffmpeg|mkvpropedit|mkv|subsearch|fixtag|subtag|acceptlang|plexrefresh|probe|API|JSON|env|stdout)\b", re.I)
def lint(where, text, max_words=36):
    bad = []
    if RAW.search(text): bad.append("raw code")
    if JARGON.search(text): bad.append("jargon: " + JARGON.search(text).group(0))
    if len(text.split()) > max_words: bad.append(f"{len(text.split())} words")
    if not text.rstrip().endswith((".", "!", "?")): bad.append("no full stop")
    check(f"lint {where}", not bad, f"{bad}: {text[:120]}")

# every check: a representative detail per code (the templates must never fall through to the raw detail)
SAMPLE_DETAIL = {"WRONG_LANGUAGE": "audio=['fre'] original=eng wanted=['eng']", "DUB_NOT_WANTED": "tagged audit-lang-fil but audio=['eng']", "DUB_ONLY": "anime with audio=['eng'] and no jpn track",
 "NO_SUBS": "audio=['jpn'] embedded_subs=[] external=[] | not in Bazarr wanted list", "UND_AUDIO_NO_SUBS": "audio language untagged, no subtitles anywhere",
 "MISSING_EPISODE": "released 2026-10-12 (30 days ago), monitored, no file | queued 40%", "MISSING_MOVIE": "released 2026-10-12 (30 days ago), monitored, no file", "MISSING_SCENE": "released 2026-10-12 (30 days ago), monitored, no file",
 "MISSING_FILE": "path not found on disk", "ZERO_SIZE": "0-byte file", "CORRUPT_HEADER": "ffprobe: x", "NO_AUDIO": "no audio stream", "NO_VIDEO": "no video stream",
 "RUNTIME_SHORT": "file 12 min vs expected 25 min (1 ep)", "RUNTIME_LONG": "file 48 min vs expected 25 min (1 ep) - extended cut", "RUNTIME_DISAGREE": "ffprobe 20 min vs arr mediaInfo 40 min",
 "SAMPLE_FILE": "40 MB", "TITLE_MISMATCH": "embedded title 'x' does not match 'y'", "DUPLICATE_VIDEO": "900 MB", "ORPHAN_FILE": "900 MB", "BAD_DURATION_HEADER": "container header says 48 min",
 "AUDIO_OFFSET": "audio track starts 2.10s after/before video with no matching cut", "PLEX_MISSING": "x", "RES_MISLABELED": "1280x720 but tagged Bluray-1080p", "HI10P": "10-bit H.264 (yuv420p10le)",
 "AV1": "AV1 video", "SUBS_OUT_OF_SYNC": "external 'x.en.srt': subtitles 2.4s late (3/3 samples agree) | fix: shift | lang=en | src=sidecar:/x.en.srt", "INTERLACED": "interlaced video (tt)", "TRUEHD_ONLY": "only lossless audio (TrueHD)", "DV_PROFILE5": "Dolby Vision profile 5", "DV_PROFILE7": "Dolby Vision profile 7 (dual layer, compat id 6)"}
for code in audit.CHECK_NAMES:
    if code in ("SPACE", "UNFOUND", "TRASH", "SELFTEST", "HEALTH", "JOBFAIL"): continue
    lint(f"name {code}", audit.CHECK_NAMES[code] + ".", max_words=8)
    sev = "low" if code in ("RUNTIME_LONG", "AUDIO_OFFSET") else "medium"
    txt = audit.plain_sentence(code, sev, "Show S01E01", SAMPLE_DETAIL.get(code, "x"), "sonarr")
    check(f"sentence exists for {code}", txt and txt != SAMPLE_DETAIL.get(code, "x") and len(txt) > 20, txt)
    lint(f"sentence {code}", txt)
for sev, name in audit.SEV_NAMES.items(): lint(f"severity {sev}", name + ".", max_words=3)

# button labels in ui.html (the visible word, not the tooltip)
ui = open(os.path.join(ROOT, "ui.html"), encoding="utf-8").read()
m = re.search(r"const BTN=\{(.*?)\};", ui, re.S); labels = re.findall(r"\w+:\['([^']+)'", m.group(1)) if m else []
check("BTN label table found in ui.html", len(labels) >= 12, len(labels))
for l in labels: lint(f"button '{l}'", l + ".", max_words=5)
for key, title in re.findall(r"(\w+):\['[^']+','([^']*)'", m.group(1) if m else ""):
    check(f"button {key} has a tooltip explaining it", len(title) > 15)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)
