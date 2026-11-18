// Q38: error-path personas in a real browser (Playwright + Chromium), against a running Auditarr and the fake arr stack (tests/mock_stack.py).
// Things go wrong on purpose and each press must end in a plain sentence on the page - never silence, never a code dump:
//   1. Find subtitles while Bazarr is switched off          2. Make AI subtitles while Bazarr has no speech-to-text provider
//   3. Run now pressed twice
// Usage: node error_paths_bot.js <auditarr url> <mock url> <title to use, e.g. S01E03>      Exit 1 on any failure.
const { chromium } = require("playwright");
const [,, BASE, MURL, TITLE] = process.argv;
const results = []; const check = (name, cond, info = "") => { results.push([!!cond, name]); console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : `  [${String(info).slice(0, 220)}]`)); };
const dump = /Traceback|Errno|Error\(|URLError|gaierror|HTTPConnection|<urlopen|\{"|\b[A-Z]{2,}_[A-Z_]{2,}\b|^\s*\d{3}\s*$/;   // what a code dump looks like
const plain = t => !!t && t.trim().length > 12 && !dump.test(t);
const world = async w => { const r = await fetch(MURL + "/__world", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(w) }); return r.ok; };
(async () => {
  const browser = await chromium.launch(); const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }); const jsErrors = [];
  page.on("pageerror", e => jsErrors.push(String(e)));
  const toastAfter = async (prev, ms = 15000) => { await page.waitForFunction(pv => { const t = document.querySelector("#toast"); return t && !t.classList.contains("hidden") && t.textContent.trim() !== "" && t.textContent !== pv; }, prev, { timeout: ms }).catch(() => {}); return (await page.textContent("#toast")).trim(); };
  const showAll = async () => { await page.fill("#f-q", TITLE); await page.evaluate(() => document.querySelectorAll(".sev").forEach(c => { if (!c.checked) c.click(); })); await page.waitForTimeout(300); await page.evaluate(() => document.querySelectorAll("#findings details.grp-d").forEach(d => d.open = true)); };
  const press = async sel => {   // one press the way a person does it: the button, then 'yes' when the page asks
    const prev = await page.textContent("#toast"); const b = page.locator(sel).first(); if (!(await b.count())) return null;
    await b.click(); const yes = page.locator(".confirm button.primary:not([disabled])").first();
    if (await yes.count()) await yes.click();
    return toastAfter(prev);
  };
  const needsLine = async (re, ms) => {   // the page refreshes by itself; wait for the Needs-you line about this job and return its text
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const t = await page.evaluate(src => { const r = new RegExp(src, "i"); const li = [...document.querySelectorAll("#today-body li")].find(l => r.test(l.textContent)); return li ? li.textContent : ""; }, re.source);
      if (t) return t; await page.waitForTimeout(1000);
    }
    return "";
  };
  try {
    await page.goto(BASE); await page.waitForSelector("#findings details.grp-d", { timeout: 20000 });
    // 1. Find subtitles with Bazarr switched off
    check("errors: the fake Bazarr can be switched off", await world({ down: ["bazarr"], no_whisper: false }));
    await showAll();
    let t = await press("#findings tr[id^=r-] button[data-act=subsearch]");
    check("errors: a row offers Find subtitles", t !== null);
    if (t !== null) {
      check("errors: Find subtitles with Bazarr off answers at once in a plain sentence", plain(t), t);
      const line = await needsLine(/Bazarr search for/, 90000);
      check("errors: …and the page then says by itself, in plain words, that Bazarr could not be reached", /could not be reached/i.test(line) && plain(line), line || "nothing appeared under Needs you within 90 s");
      check("errors: …and offers to try again", !!line && (await page.locator("#today-body li", { hasText: "Bazarr search for" }).locator("button[data-retry]").count()) > 0);
    }
    // 2. Make AI subtitles while Bazarr has no speech-to-text provider
    await world({ down: [], no_whisper: true });
    await page.waitForFunction(() => document.querySelector("#findings button[data-act=whisper]"), null, { timeout: 60000 }).catch(() => {}); await showAll();
    t = await press("#findings tr[id^=r-] button[data-act=whisper]");
    check("errors: a row offers Make AI subtitles", t !== null);
    if (t !== null) {
      check("errors: Make AI subtitles answers at once in a plain sentence", plain(t), t);
      const line = await needsLine(/AI subtitles for/, 120000);
      check("errors: …and the page then says by itself that Bazarr has no speech-to-text provider, in plain words", /speech-to-text/i.test(line) && plain(line), line || "nothing appeared under Needs you within 120 s");
    }
    await world({ no_whisper: false });
    // 3. Run now, twice
    let prev = await page.textContent("#toast"); await page.click("#runnow"); t = await toastAfter(prev);
    check("errors: Run now says the audit started", /started/i.test(t), t);
    prev = await page.textContent("#toast"); await page.click("#runnow"); t = await toastAfter(prev, 8000);
    check("errors: pressing Run now again explains an audit is already running, in a full sentence", /already running/i.test(t) && /\.$/.test(t) && plain(t), t);
    await page.waitForFunction(async () => { const j = await (await fetch("api/state?" + Date.now())).json(); return j.progress && ["done", "idle", "failed"].includes(j.progress.phase); }, null, { timeout: 180000, polling: 2000 }).catch(() => {});
  } catch (e) { check("errors: the personas ran without a stuck step", false, String(e).split("\n")[0]); }
  finally { await world({ down: [], no_whisper: false }).catch(() => {}); }
  check("errors: no JavaScript errors", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
  await browser.close(); const bad = results.filter(r => !r[0]).length; console.log(`\n${bad} failure(s) of ${results.length} (error-path personas)`); process.exit(bad ? 1 : 0);
})().catch(e => { console.error("ERROR PATHS BOT CRASH", e); process.exit(2); });
