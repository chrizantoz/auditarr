"""Build the static demo: the real ui.html with the sanitized fixtures bundled as its API, a demo banner, and simulated actions.
Output: site/index.html (self-contained). Served by GitHub Pages from CI, or published anywhere as a single file.
Usage: python uibot/make_demo.py [out file]"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); FX = os.path.join(HERE, "fixtures")
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "site", "index.html"); os.makedirs(os.path.dirname(out), exist_ok=True)
ui = open(os.path.join(ROOT, "ui.html"), encoding="utf-8").read()
data = {n: json.load(open(os.path.join(FX, n + ".json"))) for n in ("state", "history", "settings", "trash")}
st = data["state"]
st["setup"] = {"arrs": ["sonarr", "radarr", "whisparr"], "bazarr": True, "plex": True, "first_run_done": True, "degraded": []}
data["setup"] = {"services": [dict(kind=k, ok=True, version=v, name=k.capitalize(), url=f"http://demo.local:{p}", has_key=True, source="saved in the setup page", optional=k == "whisparr",
                                    roots=[{"path": r, "visible": True} for r in rs]) for k, v, p, rs in (("sonarr", "4.0.10", 8989, ["/data/media/tv", "/data/media/anime"]), ("radarr", "5.14.0", 7878, ["/data/media/movies"]),
                                    ("whisparr", "2.0.0", 6969, ["/data/media/xxx"]), ("bazarr", "1.5.1", 6767, []), ("plex", "1.41.3", 32400, []))],
                 "first_run_done": True, "runs": len(data["history"].get("runs", [])) or 12, "complete": True, "media_visible": True,
                 "roots": [{"path": p, "visible": True} for p in ("/data/media/tv", "/data/media/anime", "/data/media/movies", "/data/media/xxx")], "data_dir": "/data", "hint": "Demo: every folder is visible."}
data["hooks"] = {"hooks": [dict(app=a, connected=True, url_ok=True) for a in ("sonarr", "radarr", "whisparr")]}
data["plan"] = st.get("plan") or {}
shim = """<script>
window.DEMO=%s;
(function(){const D=window.DEMO;const json=o=>({ok:true,status:200,headers:{get:()=>'application/json'},json:async()=>JSON.parse(JSON.stringify(o)),text:async()=>JSON.stringify(o)});
 window.fetch=async function(url,opts){const u=String(url);
  if(opts&&opts.method==='POST'){await new Promise(r=>setTimeout(r,350));return json({ok:true,note:'demo - nothing was changed',silenced:1,done:(JSON.parse(opts.body||'{}').ids||[1]).length,already_gone:0,failed:[],failed_count:0,saved:{},applies:'demo',queued:true,dest:'/data/media/.media-audit-trash/demo.mkv',rescan:'demo',searched:{sonarr:{items:3}},what:'created',url:'http://demo/webhook'})}
  for(const k of ['state','history','settings','trash','setup','hooks','plan'])if(u.includes('api/'+k))return json(D[k]);
  if(u.includes('api/run')||u.includes('api/selftest'))return json({ok:true,note:'demo - the audit is simulated'});
  return {ok:false,status:404,headers:{get:()=>''},json:async()=>({})}};
 document.addEventListener('DOMContentLoaded',()=>{const b=document.createElement('div');b.id='demo-banner';b.innerHTML='🧪 <b>Demo</b> with sample data from a real library (titles replaced). Buttons show what they would do; nothing is changed. <a href="https://github.com/chrizantoz/auditarr" target="_blank" rel="noopener">Auditarr on GitHub</a>';document.body.prepend(b)});
})();
</script>
<style>#demo-banner{background:var(--acc);color:var(--acc-ink);padding:8px 16px;font-size:13px;text-align:center}#demo-banner a{color:inherit;font-weight:600}</style>
"""
html = ui.replace("<script>\nconst $=s=>document.querySelector(s)", shim % json.dumps(data) + "<script>\nconst $=s=>document.querySelector(s)", 1)
assert "window.DEMO" in html, "could not find the main script to prepend the demo shim"
html = html.replace("<title>Auditarr</title>", "<title>Auditarr demo</title>", 1)
open(out, "w", encoding="utf-8").write(html); print(f"demo written to {out} ({os.path.getsize(out)/1e6:.1f} MB, {len(st.get('findings', []))} findings)")
