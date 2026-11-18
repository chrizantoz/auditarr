// Q36: sign-in and the read-only account in a real browser (Playwright + Chromium), against a running Auditarr with NO password yet.
// The page is opened twice: through 127.0.0.1 (the container itself, always let in) to turn sign-in on and add a viewer from the Setup tab,
// and through a home-network address (sign-in required) to do what a person does: wrong password, right password, reload, sign out,
// then sign in as the viewer, who sees no action buttons and whose every change the server refuses in a plain sentence.
// Usage: node signin_bot.js <http://127.0.0.1:port> <http://<lan address>:port>      Exit 1 on any failure.
const { chromium } = require("playwright");
const [,, LOCAL, LAN] = process.argv;
const results = []; const check = (name, cond, info = "") => { results.push([!!cond, name]); console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : `  [${String(info).slice(0, 200)}]`)); };
const ADMIN = ["owner", "owner-pass-1"], VIEWER = ["family", "family-pass-1"];
const rawCode = /\b[A-Z]{2,}_[A-Z_]{2,}\b|Traceback|Error\(|Errno|\{"error"/;   // a code dump instead of a sentence
(async () => {
  const browser = await chromium.launch(); const jsErrors = [];
  const fresh = async () => { const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } }); const p = await ctx.newPage(); p.on("pageerror", e => jsErrors.push(String(e))); return [ctx, p]; };
  const toastAfter = async (p, prev, re) => { await p.waitForFunction(([pv, r]) => { const t = document.querySelector("#toast").textContent; return t !== pv && new RegExp(r, "i").test(t); }, [prev, re.source], { timeout: 15000 }).catch(() => {}); return p.textContent("#toast"); };
  const login = async (p, user, pw) => { await p.fill("input[name=user]", user); await p.fill("input[name=password]", pw); await Promise.all([p.waitForLoadState("load"), p.click("form button")]); };
  try {
    // 1. the owner turns sign-in on from the Setup tab (opened on the server itself)
    let [ctx, p] = await fresh();
    await p.goto(LOCAL + "/#setup"); await p.waitForSelector("#pw-save", { timeout: 20000 });
    check("signin: Setup offers 'Turn sign-in on' while the page is open to everyone", /Turn sign-in on/.test(await p.textContent("#pw-save")));
    await p.fill("#pw-user", ADMIN[0]); await p.fill("#pw-new", ADMIN[1]); let prev = await p.textContent("#toast"); await p.click("#pw-save");
    let t = await toastAfter(p, prev, /sign-in|failed/); check("signin: turning it on says so in a plain sentence", /asks for a sign-in/i.test(t), t);
    await p.waitForSelector("#vw-save", { state: "attached", timeout: 15000 });
    await p.evaluate(() => { const d = document.querySelector("#vw-save").closest("details"); if (d) d.open = true; });
    await p.fill("#vw-user", VIEWER[0]); await p.fill("#vw-new", VIEWER[1]);
    check("signin: the read-only account form asks for the main password right there (not in another box)", await p.isVisible("#vw-current"));   // found by this bot 2026-11-18: the form read the password from the closed 'change the password' box
    if (await p.isVisible("#vw-current")) await p.fill("#vw-current", ADMIN[1]); prev = await p.textContent("#toast"); await p.click("#vw-save");
    t = await toastAfter(p, prev, /viewer|failed/); check("signin: adding the read-only account says what it can do", /sees everything and can change nothing/i.test(t), t);
    await ctx.close();
    // 2. someone at home opens the page through its address and port
    [ctx, p] = await fresh();
    await p.goto(LAN + "/"); check("signin: the page asks for a sign-in", (await p.title()).includes("sign in") && (await p.locator("input[name=password]").count()) === 1, await p.title());
    await login(p, ADMIN[0], "not-the-password");
    check("signin: a wrong password says so and asks again", /Wrong user or password/.test(await p.textContent("body")) && (await p.locator("input[name=password]").count()) === 1);
    await login(p, ADMIN[0], ADMIN[1]); await p.waitForURL(u => !/\/login/.test(u.pathname), { timeout: 15000 }).catch(() => {}); await p.waitForSelector("#findings details.grp-d, #findings p", { timeout: 20000 }).catch(() => {});
    check("signin: the right password opens the page with its findings", (await p.locator("#findings tr[id^=r-], #findings details.grp-d").count()) > 0 && !(await p.title()).includes("sign in"), await p.title());
    await p.reload({ waitUntil: "load" }); await p.waitForSelector("#findings details.grp-d", { timeout: 20000 }).catch(() => {});
    check("signin: a reload keeps you signed in", !(await p.title()).includes("sign in") && (await p.locator("#findings details.grp-d").count()) > 0);
    check("signin: Sign out is offered", await p.isVisible("#logout"));
    await p.click("#logout"); await p.waitForSelector("input[name=password]", { timeout: 15000 }).catch(() => {});
    await p.goto(LAN + "/"); check("signin: after Sign out the page asks again", await p.waitForSelector("input[name=password]", { timeout: 10000 }).then(() => true, () => false), p.url());
    // 3. the read-only account
    await login(p, VIEWER[0], VIEWER[1]); await p.waitForURL(u => !/\/login/.test(u.pathname), { timeout: 15000 }).catch(() => {}); await p.waitForSelector("#findings details.grp-d", { timeout: 20000 }).catch(() => {});
    await p.evaluate(() => document.querySelectorAll("#findings details.grp-d").forEach(d => d.open = true)); await p.waitForTimeout(100);
    check("viewer: the page opens and says it is read-only", await p.isVisible("#viewer-badge"), await p.evaluate(() => document.body.className));
    const visibleActs = await p.evaluate(() => [...document.querySelectorAll("main button, #tab-findings button, #runnow, #runnow2")].filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !b.dataset.tab && !b.dataset.help && !b.dataset.jump && b.id !== "adv-toggle" && b.id !== "theme-toggle" && b.id !== "logout" && !b.closest("summary"); }).map(b => b.id || b.textContent.trim().slice(0, 30)));
    check("viewer: no action button is visible", visibleActs.length === 0, visibleActs.slice(0, 6).join(", "));
    for (const tab of ["setup", "settings"]) {
      await p.click(`nav button[data-tab=${tab}]`); await p.waitForTimeout(400);
      const editable = await p.evaluate(t => [...document.querySelectorAll(`#tab-${t} input, #tab-${t} select`)].filter(i => { const r = i.getBoundingClientRect(); return r.width > 0 && getComputedStyle(i).pointerEvents !== "none" && i.id !== "f-q"; }).length, tab);
      check(`viewer: nothing on ${tab} can be changed`, editable === 0, editable + " editable fields");
    }
    const tries = await p.evaluate(async () => { const out = []; for (const [u, b] of [["api/ignore", { id: "x" }], ["api/run", {}], ["api/settings", { set: { DEDUPE: "on" } }], ["api/setpassword", { role: "admin", user: "evil", password: "evilevil1" }], ["api/act", { id: "x", action: "quarantine" }]]) { const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }); out.push([u, r.status, (await r.json().catch(() => ({}))).error || ""]); } return out; });
    check("viewer: every change is refused by the server", tries.every(([, s]) => s === 403), JSON.stringify(tries));
    check("viewer: the refusal is a plain sentence", tries.every(([, , e]) => /can look but not change anything/.test(e) && !rawCode.test(e)), JSON.stringify(tries[0]));
    await ctx.close();
  } catch (e) { check("signin: the walkthrough ran without a stuck step", false, String(e).split("\n")[0]); }
  check("signin: no JavaScript errors", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
  await browser.close(); const bad = results.filter(r => !r[0]).length; console.log(`\n${bad} failure(s) of ${results.length} (sign-in walkthrough)`); process.exit(bad ? 1 : 0);
})().catch(e => { console.error("SIGNIN BOT CRASH", e); process.exit(2); });
