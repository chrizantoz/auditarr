// Real-browser UI test (Playwright + Chromium). Serves ui.html with the fixture JSON, opens it at phone and desktop width,
// and checks what jsdom cannot see: layout (no horizontal scroll, controls inside the viewport), plain-language rules in the
// rendered default view, help and confirm interactions. Screenshots land in uibot/screens/.
// Usage: node browser_bot.js <ui.html> <fixtures dir>      Exit 1 on any failure.
const fs = require("fs"), path = require("path"), http = require("http");
const { chromium } = require("playwright");
const [,, uiPath, dataDir] = process.argv;
const LIVE = /^https?:\/\//.test(dataDir || "");   // second arg may be a running Auditarr instead of a fixtures folder: then real buttons hit the real API
const html = LIVE ? "" : fs.readFileSync(uiPath, "utf8");
const fixtures = {}; if (!LIVE) for (const n of ["state", "history", "settings", "trash"]) fixtures[n] = fs.readFileSync(path.join(dataDir, n + ".json"), "utf8");
const server = http.createServer((req, res) => {
  const u = req.url.split("?")[0];
  if (req.method === "POST") { res.setHeader("Content-Type", "application/json"); return res.end(JSON.stringify({ ok: true, silenced: 1, note: "test", done: 1, failed: [], failed_count: 0, applies: "x", queued: 1, dest: "/data/media/.trash/x.mkv" })); }
  for (const n of Object.keys(fixtures)) if (u === "/api/" + n) { res.setHeader("Content-Type", "application/json"); return res.end(fixtures[n]); }
  if (u === "/" || u === "/index.html") { res.setHeader("Content-Type", "text/html"); return res.end(html); }
  res.statusCode = 404; res.end("{}");
});
const results = []; const check = (name, cond, info = "") => results.push([!!cond, name, String(info).slice(0, 160)]);
const rawCode = /\b[A-Z]{2,}_[A-Z_]{2,}\b/;
(async () => {
  let base = dataDir; if (!LIVE) { await new Promise(r => server.listen(0, "127.0.0.1", r)); base = `http://127.0.0.1:${server.address().port}/`; }
  const browser = await chromium.launch(); fs.mkdirSync(path.join(__dirname, "screens"), { recursive: true });
  for (const [label, width, height] of [["phone", 400, 860], ["desktop", 1280, 900]]) {
    const page = await browser.newPage({ viewport: { width, height } }); const jsErrors = [];
    page.on("pageerror", e => jsErrors.push(String(e))); page.on("console", m => { if (m.type() === "error") jsErrors.push(m.text()); });
    await page.goto(base); await page.waitForSelector("#findings details.grp-d", { timeout: 15000 });
    await page.evaluate(() => { const d = document.querySelector("#findings details.grp-d"); if (d) d.open = true; });   // groups start collapsed; open the first one like a person would
    await page.waitForTimeout(50);
    const m = await page.evaluate(() => {
      const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const nav = [...document.querySelectorAll("nav button")].filter(vis);
      return {
        scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
        navInside: nav.every(b => b.getBoundingClientRect().right <= window.innerWidth + 1), navCount: nav.length,
        rows: document.querySelectorAll("#findings tr[id^=r-]").length, plain: document.querySelectorAll("#findings tr[id^=r-] .plain").length,
        rawInSummaries: [...document.querySelectorAll("#findings .grp-head")].filter(s => /\b[A-Z]{2,}_[A-Z_]{2,}\b/.test(s.textContent)).length,
        simple: document.body.classList.contains("simple"), historyHidden: !vis(document.querySelector("nav button[data-tab=history]")),
        firstBtn: (() => { const b = document.querySelector("#findings details.grp-d[open] tr[id^=r-] button"); if (!b) return null; const r = b.getBoundingClientRect(); return { inside: r.right <= window.innerWidth + 1 && r.width > 0, text: b.textContent.trim() }; })(),
        fontOk: parseFloat(getComputedStyle(document.body).fontSize) >= 13,
      };
    });
    check(`${label}: no horizontal page scroll`, m.scrollW <= m.innerW + 1, `scrollWidth ${m.scrollW} vs ${m.innerW}`);
    check(`${label}: every visible nav button fits the screen`, m.navInside && m.navCount >= 5, `${m.navCount} buttons`);
    check(`${label}: every row leads with a plain sentence`, m.rows > 0 && m.plain === m.rows, `${m.plain}/${m.rows}`);
    check(`${label}: no raw check codes in group headers`, m.rawInSummaries === 0, m.rawInSummaries + " headers with codes");
    check(`${label}: starts in simple view with History hidden`, m.simple && m.historyHidden);
    check(`${label}: first row button is on screen and a verb`, m.firstBtn && m.firstBtn.inside && !/^(re-grab|fix tag|subsearch)$/i.test(m.firstBtn.text), JSON.stringify(m.firstBtn));
    check(`${label}: body text at least 13px`, m.fontOk);
    // help opens and closes
    await page.click("button[data-help=start]"); await page.waitForTimeout(100);
    check(`${label}: help panel opens`, await page.isVisible("#helpbox") && /How this page works/.test(await page.textContent("#helpbox")));
    await page.click("#help-close"); await page.waitForTimeout(50); check(`${label}: help panel closes`, !(await page.isVisible("#helpbox")));
    // a row button asks for confirmation and 'no' puts it back
    const btn = page.locator("#findings details.grp-d[open] tr[id^=r-] button[data-ign]").first();
    check(`${label}: an open group shows row buttons`, (await btn.count()) > 0);
    if (await btn.count()) { await btn.click(); await page.waitForTimeout(80); check(`${label}: button asks before acting`, await page.isVisible(".confirm")); await page.locator(".confirm button").last().click(); await page.waitForTimeout(80); check(`${label}: 'no' restores the button`, !(await page.isVisible(".confirm"))); }
    // advanced toggle reveals the technical tabs
    await page.click("#adv-toggle"); await page.waitForTimeout(80);
    check(`${label}: Advanced reveals History tab`, await page.isVisible("nav button[data-tab=history]")); await page.click("#adv-toggle");
    if (LIVE && label === "desktop") {   // the real thing: move a duplicate to the trash through the page, then Undo it from the toast
      const low = page.locator(".sev[value=low]"); if (!(await low.isChecked())) await low.click();   // a tiny duplicate is FYI, hidden by default
      await page.waitForTimeout(100); await page.evaluate(() => document.querySelectorAll("#findings details.grp-d").forEach(d => d.open = true));
      const q = page.locator("#findings tr[id^=r-] button[data-act=quarantine]").first();
      check("live: a duplicate row offers Move to trash", (await q.count()) > 0);
      if (await q.count()) {
        await q.click(); await page.waitForTimeout(80); await page.locator(".confirm button.primary").first().click();
        await page.waitForFunction(() => /Undo/.test(document.querySelector("#toast").textContent), null, { timeout: 20000 }).catch(() => {});
        check("live: Move to trash done, toast offers Undo", /done/.test(await page.textContent("#toast")) && (await page.locator("#toast button").count()) > 0, await page.textContent("#toast"));
        await page.locator("#toast button").first().click();
        await page.waitForFunction(() => /put back/.test(document.querySelector("#toast").textContent), null, { timeout: 20000 }).catch(() => {});
        check("live: Undo put the file back", /put back/.test(await page.textContent("#toast")), await page.textContent("#toast"));
      }
    }
    // accessibility: axe-core, fail on serious/critical
    const axePath = path.join(__dirname, "node_modules", "axe-core", "axe.min.js");
    if (fs.existsSync(axePath)) {
      await page.addScriptTag({ path: axePath });
      const ax = await page.evaluate(async () => { const r = await window.axe.run(document, { runOnly: ["wcag2a", "wcag2aa"] }); return r.violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length, help: v.help, where: v.nodes.slice(0, 4).map(n => n.target.join(" ") + (n.any && n.any[0] && n.any[0].data ? ` (${n.any[0].data.fgColor} on ${n.any[0].data.bgColor} = ${n.any[0].data.contrastRatio})` : "")) })); });
      const bad = ax.filter(v => v.impact === "serious" || v.impact === "critical");
      check(`${label}: no serious/critical accessibility violations`, bad.length === 0, bad.map(v => `${v.id} x${v.n}`).join(", "));
      if (ax.length) console.log(`  (${label} axe: ${ax.map(v => `${v.id}/${v.impact} x${v.n}: ${v.where.join(" | ")}`).join("; ")})`);
    } else console.log("  (axe-core not installed, accessibility scan skipped)");
    // monkey: seeded random clicking; every write must have been preceded by a confirm box, and nothing may throw
    if (!LIVE && label === "desktop") {
      let seed = 42; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
      await page.evaluate(() => { window.__confirms = 0; window.__posts = 0; new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1 && (n.classList.contains("confirm") || (n.querySelector && n.querySelector(".confirm")))) window.__confirms++; }).observe(document.body, { childList: true, subtree: true });
        const of = window.fetch; window.fetch = (u, o) => { if (o && o.method === "POST" && !/api\/(run|settings|selftest)/.test(String(u))) window.__posts++; return of(u, o); }; });
      const before = jsErrors.length;
      for (let i = 0; i < 60; i++) {
        const btns = await page.$$("main button:not(#runnow):not([data-purge]), nav button[data-tab]");
        if (!btns.length) break; const b = btns[Math.floor(rnd() * btns.length)];
        try { if (await b.isVisible()) await b.click({ timeout: 1000 }); } catch (e) {} await page.waitForTimeout(30);
      }
      const m2 = await page.evaluate(() => ({ posts: window.__posts, confirms: window.__confirms }));
      check(`monkey: 60 random clicks without JavaScript errors`, jsErrors.length === before, jsErrors.slice(before, before + 2).join(" | "));
      check(`monkey: every write was preceded by a confirm box`, m2.posts <= m2.confirms, JSON.stringify(m2));
      await page.goto(base); await page.waitForSelector("#findings details.grp-d", { timeout: 15000 });   // fresh page for the screenshot
    }
    await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(150);   // baselines are taken from the top of the page
    await page.screenshot({ path: path.join(__dirname, "screens", `ui-${label}${LIVE ? "-live" : ""}.png`), fullPage: false });
    await page.emulateMedia({ colorScheme: "dark" }); await page.goto(base); await page.waitForSelector("#findings details.grp-d", { timeout: 15000 });   // dark variant for the banner / README
    await page.evaluate(() => { const d = document.querySelector("#findings details.grp-d"); if (d) d.open = true; window.scrollTo(0, 0); }); await page.waitForTimeout(150);
    await page.screenshot({ path: path.join(__dirname, "screens", `ui-${label}${LIVE ? "-live" : ""}-dark.png`), fullPage: false }); await page.emulateMedia({ colorScheme: "light" });
    check(`${label}: no JavaScript errors`, jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
    await page.close();
  }
  await browser.close(); if (!LIVE) server.close();
  let bad = 0; for (const [ok, name, info] of results) { console.log((ok ? "PASS " : "FAIL ") + name + (info && !ok ? `  [${info}]` : "")); if (!ok) bad++; }
  console.log(`\n${bad} failure(s) of ${results.length}`); process.exit(bad ? 1 : 0);
})().catch(e => { console.error("BROWSER BOT CRASH", e); if (!LIVE) server.close(); process.exit(2); });
