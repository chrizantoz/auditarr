"""F20 subtitle timing: cue parsing, offset estimation, the verdict, the file shift, and which subtitle file is checked.
Run: python tests/subsync.py   Exit 1 on failure."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-subsync-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))

srt = "1\n00:00:01,500 --> 00:00:03,000\nHello\n\n2\n00:01:02,250 --> 00:01:04,000\nThere\n"
vtt = "WEBVTT\n\n00:00:01.500 --> 00:00:03.000\nHello\n"
ass = "[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.50,0:00:03.00,Default,Hello\nDialogue: 0,0:01:02.25,0:01:04.00,Default,There\n"
check("parses SRT", audit.parse_sub_cues(srt) == [(1.5, 3.0), (62.25, 64.0)], audit.parse_sub_cues(srt))
check("parses VTT", audit.parse_sub_cues(vtt) == [(1.5, 3.0)])
check("parses ASS", audit.parse_sub_cues(ass) == [(1.5, 3.0), (62.25, 64.0)], audit.parse_sub_cues(ass))
check("sample windows: three 60 s windows on a 40-minute file", audit.sync_windows(2400) == [(330.0, 60.0), (1170.0, 60.0), (2010.0, 60.0)], audit.sync_windows(2400))
check("sample windows: a 30 s test clip gets 8 s windows (mirrored by e2e_stack.py)", audit.sync_windows(30) == [(0.5, 8.0), (11.0, 8.0), (21.5, 8.0)], audit.sync_windows(30))

dense = [600 + i * 3.0 for i in range(40)] + [1500 + i * 2.5 for i in range(30)] + [2100 + i * 4.0 for i in range(20)]   # talk at 10, 25 and 35 minutes; silence elsewhere
pw = audit.pick_windows(2400, dense); check("windows go where the subtitles are dense, one per third", len(pw) == 3 and 540 <= pw[0][0] <= 600 and 1440 <= pw[1][0] <= 1500 and 2040 <= pw[2][0] <= 2100, pw)
check("short clips keep the fixed windows", audit.pick_windows(30, [1.0, 2.0, 3.0, 4.0, 5.0]) == audit.sync_windows(30))
check("no cue information keeps the fixed windows", audit.pick_windows(2400, []) == audit.sync_windows(2400))
speech = [0.4, 1.9, 4.1, 5.0, 7.3, 8.1, 10.6, 12.2, 13.0, 15.5, 17.1, 19.4, 20.2, 22.8, 24.1, 26.6, 27.9, 29.5]
late = [t + 2.4 for t in speech]; off, sup, n = audit.estimate_offset(late, speech)
check("subtitles 2.4 s late are measured as +2.4", off is not None and abs(off - 2.4) < 0.15 and sup == 1.0, (off, sup, n))
early = [t - 1.5 for t in speech]; off, sup, n = audit.estimate_offset(early, speech)
check("subtitles 1.5 s early are measured as -1.5", off is not None and abs(off + 1.5) < 0.15, (off, sup))
noisy = [t + 2.4 for t in speech[::2]] + [100.0, 140.0]; off, sup, n = audit.estimate_offset(noisy, speech)
check("half the lines missing still gives the right offset with lower support", off is not None and abs(off - 2.4) < 0.15 and 0.4 < sup < 0.7, (off, sup))
check("no overlap at all gives no answer", audit.estimate_offset([500.0, 600.0], speech)[0] is None)
check("empty inputs give no answer", audit.estimate_offset([], speech) == (None, 0.0, 0))

W = lambda offs, sup=1.0, n=8: [(p, o, sup, n) for p, o in zip((0.15, 0.5, 0.85), offs)]
check("verdict: in sync", audit.decide_sync(W([0.1, -0.2, 0.3]))[0] == "in sync")
st, off, note = audit.decide_sync(W([2.4, 2.5, 2.3])); check("verdict: late, with a plain note", st == "late" and abs(off - 2.4) < 0.2 and "2.4s late" in note, (st, off, note))
check("verdict: early", audit.decide_sync(W([-1.6, -1.5, -1.7]))[0] == "early")
st, off, note = audit.decide_sync(W([0.3, 1.4, 2.9])); check("verdict: drift when the offset grows through the file", st == "drift" and "drift:" in note and "85%" in note, (st, note))
check("verdict: unknown with one usable sample", audit.decide_sync([(0.15, 2.0, 1.0, 8), (0.5, None, 0, 0), (0.85, None, 0, 0)])[0] == "unknown")
check("verdict: unknown when matches are thin", audit.decide_sync(W([2.0, 2.0, 2.0], sup=0.3, n=2))[0] == "unknown")
# 2026-11-13: one noisy sample made a third of the open timing rows 'drift'; Bazarr's re-time then changed nothing, pressed 5 times
L = lambda offs, more: audit.decide_sync_looked(W(offs), more)
asked = []
st, off, note, ws = L([0.2, 3.1, 0.2], lambda near: asked.extend(near) or [(near[0] - 0.04, 0.3, 1.0, 8), (near[0] + 0.04, 0.2, 1.0, 8)])
check("second look: one odd sample, the two extra samples agree with the rest -> in sync, the odd one ignored", st == "in sync" and "odd sample ignored" in note and asked == [0.5], (st, note, asked))
st, off, note, ws = L([0.2, 3.1, 0.2], lambda near: [(near[0] - 0.04, 3.0, 1.0, 8), (near[0] + 0.04, 3.2, 1.0, 8)])
check("second look: the extra samples hear the same jump -> drift stays, confirmed", st == "drift" and "confirmed by a second look" in note, (st, note))
st, off, note, ws = L([0.2, 3.1, 0.2], lambda near: [(near[0], None, 0, 0)])
check("second look: the extra samples hear too little -> unsure, never 'drift'", st == "unknown" and note.startswith("unsure:"), (st, note))
st, off, note, ws = L([2.4, 2.5, 6.0], lambda near: [(0.8, 2.4, 1.0, 8), (0.9, 2.6, 1.0, 8)])
check("second look: a stray last sample on a late file -> late, and the shift fixes it", st == "late" and abs(off - 2.5) < 0.2, (st, off, note))
called = []
st, off, note, ws = L([0.3, 1.4, 2.9], lambda near: called.append(near) or [])
check("a steady slide through the file is real drift and needs no second look", st == "drift" and not called, (st, called))
st, off, note, ws = audit.decide_sync_looked([(0.15, None, 0, 0), (0.5, 0.4, 1.0, 8), (0.85, 3.2, 1.0, 8)], lambda near: called.append(sorted(near)) or [(0.15, 0.4, 1.0, 8), (0.67, 0.5, 1.0, 8)])
check("two disagreeing samples: listen where the missing one was and halfway, the majority decides", st == "in sync" and called and called[-1][0] == 0.15 and abs(called[-1][1] - 0.675) < 0.01, (st, note, called))
check("an in-sync verdict never takes a second look", audit.decide_sync_looked(W([0.1, 0.2, 0.0]), lambda near: 1 / 0)[0] == "in sync")

d = tempfile.mkdtemp(); src = os.path.join(d, "a.en.srt"); open(src, "w").write(srt); dst = os.path.join(d, "a.out.srt")
audit.shift_subtitle_file(src, 1.25, dst); cues = audit.parse_sub_cues(open(dst).read())
check("shifting an SRT by 1.25 s pulls every cue earlier", cues == [(0.25, 1.75), (61.0, 62.75)], cues)
audit.shift_subtitle_file(src, 5.0, dst); check("a shift past zero clamps at zero", audit.parse_sub_cues(open(dst).read())[0] == (0.0, 0.0))
asrc = os.path.join(d, "a.ass"); open(asrc, "w").write(ass); adst = os.path.join(d, "a.out.ass"); audit.shift_subtitle_file(asrc, -0.5, adst)
check("shifting an ASS file keeps its format and moves the lines", "Dialogue: 0,0:00:02.00,0:00:03.50,Default,Hello" in open(adst).read(), open(adst).read()[:200])
vsrc = os.path.join(d, "a.vtt"); open(vsrc, "w").write(vtt); vdst = os.path.join(d, "a.out.vtt"); audit.shift_subtitle_file(vsrc, 0.5, vdst)
check("shifting a VTT keeps the dot separator", "00:00:01.000 --> 00:00:02.500" in open(vdst).read(), open(vdst).read())

vid = os.path.join(d, "Show - S01E01 - Pilot.mkv"); open(vid, "wb").write(b"")
for n in ("Show - S01E01 - Pilot.fr.srt", "Show - S01E01 - Pilot.en.forced.srt", "Show - S01E01 - Pilot.en.srt", "Show - S01E02 - Other.en.srt"): open(os.path.join(d, n), "w").write(srt)
src_ = audit.find_subtitle_source(vid, {"streams": []})
check("picks the English external file, not the forced one or the other episode", src_ and src_[0] == "sidecar" and src_[1].endswith("Pilot.en.srt") and src_[2] == "en", src_)
os.remove(os.path.join(d, "Show - S01E01 - Pilot.en.srt"))
pr = {"streams": [{"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}, {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}, "disposition": {}}]}
src_ = audit.find_subtitle_source(vid, pr); check("falls back to the embedded text track, skipping picture subtitles", src_ == ("embedded", 3, "en"), src_)
check("nothing usable -> None", audit.find_subtitle_source(vid, {"streams": pr["streams"][:1]}) is None)
s_late = audit.plain_sentence("SUBS_OUT_OF_SYNC", "medium", "Show S01E01", "external 'x.en.srt': subtitles 2.4s late (3/3 samples agree) | fix: shift | lang=en | src=sidecar:/x.en.srt", "sonarr")
check("plain sentence names the delay in seconds", "about 2 seconds late" in s_late and "Re-time" in s_late, s_late)
j_ = '{"text":"a b","segments":[{"start":1.0,"end":2.0,"text":"a"},{"start":3.5,"end":4.0,"text":"b"},{"start":5.0,"end":6.0,"text":"  "}]}'
check("endpoint answer: json with segments", audit.asr_segments_from(j_) == [(1.0, 2.0), (3.5, 4.0)])
check("endpoint answer: srt text (subgen style)", audit.asr_segments_from("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n2\n00:00:03,500 --> 00:00:04,000\nyo\n") == [(1.0, 2.0), (3.5, 4.0)])
check("endpoint answer: vtt text", audit.asr_segments_from("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n") == [(1.0, 2.0)])
check("endpoint answer: json lines", audit.asr_segments_from('{"start":1,"end":2,"text":"a"}\n{"start":3,"end":4,"text":"b"}') == [(1.0, 2.0), (3.0, 4.0)])
check("endpoint answer: empty means no speech", audit.asr_segments_from("") == [])
try: audit.asr_segments_from("la la la"); check("endpoint answer: plain words are rejected loudly", False)
except ValueError: check("endpoint answer: plain words are rejected loudly", True)
bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
