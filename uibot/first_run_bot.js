// First-run walkthrough in a real browser (Playwright + Chromium), against a BLANK Auditarr and a fake arr stack:
// the Setup tab opens by itself, the person types addresses and keys, presses Test, presses Save connections, reloads,
// sees the values kept and the apps connected, presses Find my apps, then starts the first audit and sees findings.
// This is the path the first outside user took when Save connections was broken. Usage: node first_run_bot.js <auditarr url> <mock url>
const { chromium } = require("playwright");
const [,, BASE, MURL] = process.argv;
const results = []; const check = (name, cond, info = "") => { results.push([!!cond, name, String(info).slice(0, 180)]); console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : `  [${String(info).slice(0, 180)}]`)); };
(async () => {
  const browser = await chromium.launch(); const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }); const jsErrors = [];
  page.on("pageerror", e => jsErrors.push(String(e))); page.on("console", m => { if (m.type() === "error") jsErrors.push(m.text()); });
  try {
    await page.goto(BASE, { waitUntil: "load" }); await page.waitForSelector("#setup .card[data-svc]", { timeout: 20000 });
    check("first run: the Setup tab opens by itself on a blank install", !(await page.getAttribute("#tab-setup", "class") || "").includes("hidden"));
    for (const svc of ["sonarr", "radarr", "bazarr"]) {
      const card = page.locator(`#setup .card[data-svc=${svc}]`);
      await card.locator("[data-f=url]").fill(`${MURL}/${svc}`); await card.locator("[data-f=key]").fill("test");
      await card.locator("button[data-test]").click();
      await page.waitForFunction(s => /connected|not reachable|failed|answer|refused/i.test(document.querySelector(`#setup .card[data-svc=${s}] .st`).textContent), svc, { timeout: 30000 });
      check(`first run: Test on ${svc} says connected`, /connected/i.test(await card.locator(".st").textContent()), await card.locator(".st").textContent());
    }
    await page.click("#save-setup");
    await page.waitForFunction(() => /saved|failed/i.test(document.querySelector("#toast").textContent), null, { timeout: 15000 });
    check("first run: Save connections confirms what it saved", /connections saved.*sonarr/i.test(await page.textContent("#toast")), await page.textContent("#toast"));
    await page.reload({ waitUntil: "load" }); await page.waitForSelector("#setup .card[data-svc]", { timeout: 20000 }); await page.waitForTimeout(1500);
    const urls = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#setup .card[data-svc]")].map(c => [c.dataset.svc, c.querySelector("[data-f=url]").value])));
    check("first run: after a reload the addresses are still there", urls.sonarr === `${MURL}/sonarr` && urls.radarr === `${MURL}/radarr` && urls.bazarr === `${MURL}/bazarr`, JSON.stringify(urls));
    const keyHint = await page.getAttribute("#setup .card[data-svc=sonarr] [data-f=key]", "placeholder");
    check("first run: the key field says it is already set instead of showing the key", /already set/i.test(keyHint || ""), keyHint);
    const chips = await page.evaluate(() => [...document.querySelectorAll("#setup .card[data-svc] .st")].map(e => e.textContent.trim()));
    check("first run: the cards show connected after the reload", chips.filter(t => /connected/i.test(t)).length >= 3, chips.join(" | "));
    await page.click("#discover"); await page.waitForFunction(() => document.querySelectorAll("#discover-out button[data-use]").length > 0 || /nothing|none/i.test(document.querySelector("#discover-out").textContent), null, { timeout: 60000 });
    check("first run: Find my apps lists apps with a Use this button", (await page.locator("#discover-out button[data-use]").count()) > 0, (await page.textContent("#discover-out")).slice(0, 120));
    const runBtn = page.locator("#runnow2"); check("first run: the first-audit button is enabled once the arrs are connected", (await runBtn.count()) > 0 && !(await runBtn.isDisabled()));
    await runBtn.click(); await page.waitForFunction(() => /started/i.test(document.querySelector("#toast").textContent), null, { timeout: 15000 });
    await page.click("nav button[data-tab=findings]");
    try { await page.waitForFunction(() => document.querySelectorAll("#findings tr[id^=r-]").length > 0, null, { timeout: 180000 }); }
    catch (e) { const st = await page.evaluate(async () => { try { const j = await (await fetch("api/state?" + Date.now())).json(); return JSON.stringify({ run: j.run, progress: j.progress, health: j.health && j.health.status, findings: (j.findings || []).length }); } catch (x) { return "state fetch failed: " + x; } }); const pg = await page.evaluate(() => ({ tab: [...document.querySelectorAll("nav button")].find(b => b.classList.contains("on"))?.textContent, findingsText: (document.querySelector("#findings") || {}).innerText?.slice(0, 160), groups: document.querySelectorAll("#findings details.grp-d").length, sev: [...document.querySelectorAll(".sev")].map(c => c.value + ":" + c.checked).join(","), toast: (document.querySelector("#toast") || {}).textContent, pending: !!document.querySelector("#apply-refresh:not([hidden])") }));
      check("first run: findings appeared within 3 minutes of pressing Run first audit", false, st.slice(0, 170) + " | page: " + JSON.stringify(pg).slice(0, 300)); }
    check("first run: the first audit produced findings on the page", (await page.locator("#findings tr[id^=r-]").count()) > 0);
    check("first run: every row leads with a plain sentence", await page.evaluate(() => [...document.querySelectorAll("#findings tr[id^=r-]")].every(r => (r.querySelector(".plain") || {}).textContent)), "");
  } catch (e) { check("first run: walkthrough ran without a stuck step", false, String(e).split("\n")[0]); }
  check("first run: no JavaScript errors", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
  await browser.close(); const bad = results.filter(r => !r[0]).length; console.log(`\n${bad} failure(s) of ${results.length} (first-run walkthrough)`); process.exit(bad ? 1 : 0);
})().catch(e => { console.error("FIRST RUN BOT CRASH", e); process.exit(2); });
