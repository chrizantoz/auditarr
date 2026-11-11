// Q34: fuzz the page's data. Loads ui.html once in jsdom, then feeds it hundreds of mangled copies of the real state
// (missing fields, nulls, empty lists, wrong types, huge strings, odd unicode) and re-renders every tab. No JavaScript error allowed.
// Usage: node fuzz_ui.js <ui.html> <fixtures dir> [iterations=400] [seed=7]      Exit 1 when any mangled state throws.
const fs = require("fs"), path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");
const [,, uiPath, dataDir, nArg, seedArg] = process.argv; const N = +(nArg || 400); let seed = +(seedArg || 7);
const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff; const pick = a => a[Math.floor(rnd() * a.length)];
const html = fs.readFileSync(uiPath, "utf8");
const data = {}; for (const n of ["state", "history", "settings", "trash"]) data[n] = JSON.parse(fs.readFileSync(path.join(dataDir, n + ".json"), "utf8"));
let current = { ...data }; const errors = [];
const vc = new VirtualConsole(); vc.on("jsdomError", e => errors.push(String(e.message || e))); vc.on("error", m => errors.push(String(m)));
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "http://auditarr.test/", virtualConsole: vc, beforeParse(w) {
  w.fetch = async (url, opts) => {
    const u = String(url); const body = (o) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => JSON.parse(JSON.stringify(o)), text: async () => JSON.stringify(o) });
    if (opts && opts.method === "POST") return body({ ok: true, note: "test", done: 1, failed: [], failed_count: 0, saved: {}, queued: 1 });
    for (const n of ["state", "history", "settings", "trash"]) if (u.includes("api/" + n)) return body(current[n]);
    if (u.includes("api/setup")) return body(current.setup || { services: [], first_run_done: true, runs: 3, complete: true, roots: [] });
    if (u.includes("api/about")) return body(current.about || { version: "dev", stats: {}, health: [], links: {}, releases: [], tools: {}, errors: [] });
    return { ok: false, status: 404, headers: { get: () => "" }, json: async () => ({}) };
  };
  w.setInterval = () => 0; w.scrollTo = () => {}; w.HTMLElement.prototype.scrollIntoView = function () {};
} });
const w = dom.window, d = w.document, $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const BIG = "x".repeat(5000), ODD = "🧪 ünïcödé ‮ reversed <b>&amp;</b> \u0000";
function mangle(obj, log, depth = 0) {   // one random mutation somewhere in a deep copy
  const o = JSON.parse(JSON.stringify(obj)); let node = o, trail = [];
  for (let i = 0; i < 1 + Math.floor(rnd() * 4); i++) {
    const keys = node && typeof node === "object" ? Object.keys(node) : [];
    if (!keys.length) break;
    const k = pick(keys); if (node[k] && typeof node[k] === "object" && rnd() < 0.75) { trail.push(k); node = node[k]; } else { trail.push(k); break; }
  }
  const parent = trail.length > 1 ? trail.slice(0, -1).reduce((a, k) => a[k], o) : o; const key = trail[trail.length - 1];
  if (key === undefined) return o;
  const op = pick(["delete", "null", "empty-string", "empty-list", "empty-object", "number", "string", "huge", "odd", "negative", "bool", "truncate"]);
  const v = parent[key];
  if (op === "delete") delete parent[key]; else if (op === "null") parent[key] = null; else if (op === "empty-string") parent[key] = ""; else if (op === "empty-list") parent[key] = [];
  else if (op === "empty-object") parent[key] = {}; else if (op === "number") parent[key] = 42; else if (op === "string") parent[key] = "nope"; else if (op === "huge") parent[key] = BIG;
  else if (op === "odd") parent[key] = ODD; else if (op === "negative") parent[key] = -1; else if (op === "bool") parent[key] = false;
  else if (op === "truncate" && Array.isArray(v)) parent[key] = v.slice(0, 1);
  log.push(`${trail.join(".")} <- ${op}`); return o;
}
process.on("unhandledRejection", e => errors.push("unhandled: " + String(e && e.stack ? e.stack.split("\n")[0] : e)));   // a page promise that rejects must count as a failure, not kill the fuzzer
process.on("uncaughtException", e => errors.push("uncaught: " + String(e && e.stack ? e.stack.split("\n")[0] : e)));
(async () => {
  await sleep(400); const baseErrors = errors.length;
  const tabs = $$("nav button[data-tab]").map(b => b.dataset.tab); const failures = [];
  for (let i = 0; i < N; i++) {
    const log = []; const mutated = { ...data };
    const target = pick(["state", "state", "state", "state", "history", "settings", "trash", "setup", "about"]);
    const basis = target === "setup" ? { services: ["sonarr", "radarr", "bazarr", "plex"].map(k => ({ kind: k, ok: true, url: "http://x", has_key: true, source: "env", optional: false, version: "1" })), first_run_done: true, runs: 3, complete: true, roots: [{ path: "/data", visible: true }] }
      : target === "about" ? { version: "v1", revision: "abc", stats: { files: 1 }, health: [{ level: "ok", text: "fine", link: "" }], links: { demo: "http://x" }, releases: [{ tag: "v1", name: "v1", url: "", published: "2026-01-01", prerelease: false, summary: "" }], tools: { ffmpeg: true }, errors: [], uptime_s: 5 } : data[target];
    let m = basis; for (let k = 0; k < 1 + Math.floor(rnd() * 3); k++) m = mangle(m, log);
    mutated[target] = m; current = mutated;
    const before = errors.length;
    try {
      await w.eval("load()"); await sleep(20);
      for (const t of tabs) { const b = $(`nav button[data-tab=${t}]`); if (b) { b.click(); await sleep(t === "about" || t === "setup" || t === "trash" ? 40 : 10); } }
    } catch (e) { errors.push(String(e)); }
    if (errors.length > before) failures.push({ target, log, err: errors.slice(before).join(" | ").slice(0, 200) });
    current = { ...data };
  }
  await w.eval("load()"); await sleep(30);
  for (const f of failures.slice(0, 12)) console.log(`FAIL ${f.target}: ${f.log.join("; ")}  ->  ${f.err}`);
  console.log(`\n${failures.length} of ${N} mangled states broke the page (${baseErrors} errors before fuzzing)`); process.exit(failures.length ? 1 : 0);
})();
