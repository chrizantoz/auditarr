// Q39: a page left open for a long time. Loads the real page from a running Auditarr into jsdom with its real timers and real API, keeps it
// open across the audit that may be running and `runs` more audits started from outside the page (the way the nightly run starts while
// nobody is looking), and never touches it. Passes when every finished audit shows up on the page by itself (no reload, no click),
// nothing is left waiting for a click, and memory and page size stay flat. Run with node --expose-gc so memory can be measured fairly.
// Usage: node --expose-gc soak_bot.js <auditarr url> [out.json] [runs=2] [max minutes=40]      Exit 1 on any failure.
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const [,, BASE0, OUT, RUNS = "2", MAXMIN = "40"] = process.argv;
const BASE = BASE0.replace(/\/$/, "") + "/";
const results = []; const check = (name, cond, info = "") => { results.push([!!cond, name, String(info).slice(0, 200)]); console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : `  [${String(info).slice(0, 200)}]`)); };
const sleep = ms => new Promise(r => setTimeout(r, ms)); const t0 = Date.now(); const deadline = t0 + Number(MAXMIN) * 60000;
const get = async p => (await fetch(BASE + p + (p.includes("?") ? "&" : "?") + "t=" + Date.now())).json();
const health = () => get("health").catch(() => ({}));
const finished = h => ["done", "idle", "failed", ""].includes(String(h.phase || ""));
const samples = [];
(async () => {
  for (let i = 0; i < 36; i++) { try { if ((await fetch(BASE + "health")).status) break; } catch (e) {} await sleep(5000); }   // the staging copy may still be starting
  const html = process.env.UI_HTML ? fs.readFileSync(process.env.UI_HTML, "utf8") : await (await fetch(BASE)).text(); const errors = [];   // UI_HTML: try a page from disk against a running app
  const vc = new VirtualConsole(); vc.on("jsdomError", e => errors.push(String(e.message || e)));
  const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: BASE, virtualConsole: vc, beforeParse(w) {
    w.fetch = (u, o) => fetch(new URL(String(u), BASE).href, o);   // the page talks to the real app
    w.HTMLElement.prototype.scrollIntoView = function () {}; w.scrollTo = () => {};
  } });
  const w = dom.window, $ = s => w.document.querySelector(s);
  const pageRun = () => { try { return String(w.eval("S && S.health && S.health.last_run") || ""); } catch (e) { return ""; } };
  const sample = label => { if (global.gc) { global.gc(); global.gc(); } const m = { label, min: +((Date.now() - t0) / 60000).toFixed(1), heap_mb: +(process.memoryUsage().heapUsed / 1e6).toFixed(1), nodes: w.document.getElementsByTagName("*").length, findings: (() => { try { return w.eval("(S.findings||[]).length"); } catch (e) { return -1; } })() }; samples.push(m); console.log("sample", JSON.stringify(m)); return m; };
  const waitFor = async (cond, ms) => { const end = Math.min(deadline, Date.now() + ms); while (Date.now() < end) { if (await cond()) return true; await sleep(2000); } return false; };
  await waitFor(async () => pageRun() !== "", 60000);
  check("soak: the page opened on the real app", pageRun() !== "" && !!$("#findings"), pageRun());
  let h = await health();
  if (!finished(h)) {   // the audit the staging copy starts on its own: keep the page open through it
    await waitFor(async () => finished(h = await health()), deadline - Date.now());
    const seen = await waitFor(async () => pageRun() === String(h.last_run || ""), 90000);
    check("soak: the audit that was running when the page opened shows up on the page by itself", seen, `page ${pageRun()} vs app ${h.last_run}`);
  }
  await sleep(15000); const base = sample("settled");
  for (let i = 1; i <= Number(RUNS); i++) {
    const before = String((await health()).last_run || "");
    const r = await fetch(BASE + "api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });   // started from outside the page, like the nightly run
    check(`soak: audit ${i} started from outside the page`, r.status === 202 || r.status === 409, r.status);
    const done = await waitFor(async () => { h = await health(); return finished(h) && String(h.last_run || "") !== before; }, 30 * 60000);
    check(`soak: audit ${i} finished`, done, JSON.stringify({ phase: h.phase, last_run: h.last_run }));
    const seen = await waitFor(async () => pageRun() === String(h.last_run || ""), 150000);   // the page checks quietly about once a minute while nothing runs
    check(`soak: audit ${i} shows up on the page by itself, with nobody touching it`, seen, `page still on ${pageRun()} while the app finished ${h.last_run}`);
    await sleep(25000);   // the 'nobody is touching it' window the page waits for before it redraws
    let pending = true; try { pending = !!w.eval("PENDING") || !$("#apply-refresh").classList.contains("hidden"); } catch (e) {}
    check(`soak: after audit ${i} nothing is left waiting for a click`, !pending);
    sample(`after audit ${i}`);
  }
  const last = samples[samples.length - 1];
  const heapOk = last.heap_mb <= base.heap_mb * 1.5 + 25, nodesOk = last.nodes <= base.nodes * 1.5 + 2000;   // flat: a refresh redraws, it must not pile up
  check("soak: memory stays flat while the page is open", heapOk, `${base.heap_mb} MB -> ${last.heap_mb} MB`);
  check("soak: the page does not grow (elements)", nodesOk, `${base.nodes} -> ${last.nodes} elements`);
  check("soak: no JavaScript errors while open", errors.length === 0, errors.slice(0, 2).join(" | "));
  w.close();
  const failed = results.filter(r => !r[0]).map(r => r[1] + (r[2] ? ` (${r[2]})` : ""));
  if (OUT) fs.writeFileSync(OUT, JSON.stringify({ ts: new Date().toISOString().slice(0, 19), passed: results.length - failed.length, total: results.length, failed, minutes: +((Date.now() - t0) / 60000).toFixed(1), samples, revision: process.env.BOT_REVISION || "" }, null, 1));
  console.log(`\n${failed.length} failure(s) of ${results.length} (long-open page, ${((Date.now() - t0) / 60000).toFixed(1)} min)`); process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("SOAK BOT CRASH", e); if (OUT) try { fs.writeFileSync(OUT, JSON.stringify({ ts: new Date().toISOString().slice(0, 19), passed: 0, total: 1, failed: ["soak bot crashed: " + String(e).slice(0, 160)] })); } catch (x) {} process.exit(2); });
