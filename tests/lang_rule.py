"""The default-track rule (wanted_langs): which audio and subtitle language the night picks for a title, under both choices of PREFER_DUBS,
with the user's own languages and the per-title tag. Run: python tests/lang_rule.py   Exit 1 on failure."""
import os, sys, json, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="auditarr-lang-"); os.environ["REPORT_DIR"] = os.environ["CONFIG_DIR"]
sys.path.insert(0, ROOT)
import audit  # noqa: E402
results = []
def check(name, cond, info=""): results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:200]}]"))
conn = audit.db()
def ui_set(**kv): conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)", (json.dumps(kv),)); conn.commit(); audit.load_settings(conn)
def item(orig, kind="series", anime=False, tag=None, path="/data/tv/Show (2020)/Season 01/Show - S01E01.mkv"):
    return dict(orig=orig, series_type="anime" if anime else "standard", app="sonarr" if kind == "series" else "radarr", path=path, intent={"lang": tag})

ui_set()   # defaults: original language, English subtitles
check("default: Korean drama plays in Korean with English subtitles", audit.wanted_langs(item("kor")) == (["kor"], "eng"), audit.wanted_langs(item("kor")))
check("default: Japanese anime plays in Japanese", audit.wanted_langs(item("jpn", anime=True)) == (["jpn"], "eng"))
check("default: Chinese donghua plays in Chinese, Japanese dub second", audit.wanted_langs(item("chi", anime=True)) == (["chi", "jpn"], "eng"), audit.wanted_langs(item("chi", anime=True)))
check("default: an English-original title plays in English", audit.wanted_langs(item("eng")) == (["eng"], "eng"))
check("default: a title with no known original language plays in English", audit.wanted_langs(item("")) == (["eng"], "eng"))
check("a tag on the title wins", audit.wanted_langs(item("kor", tag="fre")) == (["fre"], "eng"))

ui_set(PREFER_DUBS="on")
check("dubs: Korean drama plays in English when the file has it, Korean otherwise", audit.wanted_langs(item("kor")) == (["eng", "kor"], "eng"), audit.wanted_langs(item("kor")))
check("dubs: anime plays in English, then its original, then Japanese", audit.wanted_langs(item("chi", anime=True)) == (["eng", "chi", "jpn"], "eng"), audit.wanted_langs(item("chi", anime=True)))
check("dubs: the tag still wins", audit.wanted_langs(item("kor", tag="jpn")) == (["jpn"], "eng"))

ui_set(PREFERRED_AUDIO="fre,eng", PREFERRED_SUBS="fre")   # a French household, original-language mode
check("French user: Korean drama in Korean with French subtitles", audit.wanted_langs(item("kor")) == (["kor"], "fre"), audit.wanted_langs(item("kor")))
check("French user: a French-original title plays in French", audit.wanted_langs(item("fre")) == (["fre"], "fre"))
check("French user: an English-original title plays in English (the original), French subtitles", audit.wanted_langs(item("eng")) == (["eng"], "fre"), audit.wanted_langs(item("eng")))
ui_set(PREFERRED_AUDIO="fre,eng", PREFERRED_SUBS="fre", PREFER_DUBS="on")
check("French user who prefers dubs: French first, the original second", audit.wanted_langs(item("eng")) == (["fre", "eng"], "fre"), audit.wanted_langs(item("eng")))
conn.close(); bad = results.count(False); print(f"\n{len(results) - bad} passed, {bad} failed"); sys.exit(1 if bad else 0)
