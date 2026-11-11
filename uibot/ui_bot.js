// Headless UI test: loads the real ui.html in jsdom, serves the real API JSON captured from the live container,
// then clicks through every tab and control and asserts the DOM looks right. Usage: node ui_bot.js <ui.html> <dir with state.json history.json settings.json trash.json>
const fs = require("fs"), path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");
const [,, uiPath, dataDir] = process.argv;
const html = fs.readFileSync(uiPath, "utf8");
const data = {}; for (const n of ["state", "history", "settings", "trash"]) data[n] = fs.readFileSync(path.join(dataDir, n + ".json"), "utf8");
const errors = []; const vc = new VirtualConsole(); vc.on("jsdomError", e => errors.push(String(e.message || e))); vc.on("error", m => errors.push(String(m)));
const posts = [];
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "http://auditarr.test/", virtualConsole: vc, beforeParse(w) {
  w.fetch = async (url, opts) => {
    const u = String(url); const body = (s) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => JSON.parse(s), text: async () => s });
    if (opts && opts.method === "POST") { posts.push({ url: u, body: opts.body }); return body(JSON.stringify({ ok: true, silenced: 1, note: "test", done: 1, failed: [], failed_count: 0, saved: {}, applies: "x", queued: 1, dest: "/data/media/.trash/x.mkv" })); }
    if (u.includes("api/state")) return body(data.state); if (u.includes("api/history")) return body(data.history);
    if (u.includes("api/settings")) return body(data.settings); if (u.includes("api/trash")) return body(data.trash);
    if (u.includes("api/setup")) return body(JSON.stringify({ services: ["sonarr", "radarr", "whisparr", "bazarr", "plex"].map(k => ({ kind: k, ok: false, url: "", has_key: false, source: "not configured", optional: k === "whisparr", error: "not configured" })), first_run_done: false, runs: 0, complete: false, media_visible: null, roots: [], hint: "mount the media" }));
    if (u.includes("api/run")) return body("{}"); return { ok: false, status: 404, headers: { get: () => "" }, json: async () => ({}) };
  };
  w.setInterval = () => 0; // no background polling in the test
} });
const w = dom.window, d = w.document, $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const results = []; const check = (name, cond, info = "") => { results.push([cond, name, info]); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EDGE = fs.existsSync(path.join(dataDir, "edge.json")) ? JSON.parse(fs.readFileSync(path.join(dataDir, "edge.json"), "utf8")).case : null;
async function edgeChecks(S) {
  const txt = sel => ($(sel) || { textContent: "" }).textContent;
  if (EDGE === "empty") {
    check("empty: Today says nothing needs you", /nothing needs you/.test(txt("#today-sum")), txt("#today-sum"));
    check("empty: findings area shows a friendly empty note", /nothing matches|nothing/i.test(txt("#findings")) && $$("#findings tr[id^=r-]").length === 0, txt("#findings").slice(0, 60));
    for (const t of ["trash", "actions", "space", "settings"]) { $(`nav button[data-tab=${t}]`).click(); await sleep(150); }
    check("empty: trash/actions/space show empty notes, not errors", /empty/.test(txt("#trash")) && /no actions|no background jobs|no re-grabs/.test(txt("#tab-actions")) && /nothing here/.test(txt("#space")), txt("#space").slice(0, 60));
  }
  if (EDGE === "fresh") {
    check("fresh: banner says nothing is connected", /not connected to anything yet/.test(txt("#setup-banner")), txt("#setup-banner").slice(0, 80));
    check("fresh: the Setup tab opened by itself", !$("#tab-setup").classList.contains("hidden"));
    await sleep(200); check("fresh: setup shows the three steps and a card per app", /Connect your apps/.test(txt("#setup")) && $$("#setup .card").length >= 4 && $$("#setup button[data-test]").length >= 4, $$("#setup .card").length + " cards");
    check("fresh: first-audit button is disabled until an arr is connected", !!$("#runnow2") && $("#runnow2").disabled);
    $("#setup button[data-test]").click(); await sleep(100); check("fresh: Test posts to /api/test_connection", posts.some(p => p.url.includes("api/test_connection")));
  }
  if (EDGE === "huge") {
    const t0 = Date.now(); const renderCount = $$("#findings tr[id^=r-]").length;
    check("huge: 2000-finding show renders", renderCount > 0 && /Giant Show/.test(txt("#findings")), renderCount + " rows");
    $("#f-group").value = "library"; $("#f-group").dispatchEvent(new w.Event("change")); await sleep(50);
    check("huge: library grouping caps a giant group at 300 rows with a hint", /showing 300 of/.test(txt("#findings")), txt("#findings").slice(0, 80));
    check("huge: re-render under 4 s", Date.now() - t0 < 4000, (Date.now() - t0) + " ms");
  }
  if (EDGE === "down") {
    check("down: header chip names the unreachable services", /bazarr, plex, whisparr unreachable/.test(txt("#s-health")), txt("#s-health"));
    check("down: Today lists the outage first", /could not be reached/.test(txt("#today-body")));
    check("down: AI engine chip shows the CPU fallback", /AI engine: server CPU/.test(txt("#s-health")));
    $("nav button[data-tab=actions]").click(); await sleep(100); check("down: jobs panel explains the missing live info", /no live info/.test(txt("#jobs")), txt("#jobs").slice(0, 80));
  }
  if (EDGE === "errors") {
    check("errors: Today shows 'unavailable' instead of crashing", /unavailable/.test(txt("#today-sum")), txt("#today-sum"));
    check("errors: health chip shows stale with the age", /stale/.test(txt("#s-health")) && /41 h/.test(txt("#s-health")), txt("#s-health"));
    check("errors: failing tests are visible in the header", /tests 2 failing/.test(txt("#s-health")), txt("#s-health"));
    $("nav button[data-tab=space]").click(); await sleep(100); check("errors: Space shows the error text", /Tautulli unreachable/.test(txt("#space")));
    $("nav button[data-tab=history]").click(); await sleep(200); check("errors: History names the failing checks", /settings save/.test(txt("#history")) && /tab trash visible/.test(txt("#history")));
  }
  check(`${EDGE}: no JavaScript errors`, errors.length === 0, errors.slice(0, 3).join(" | "));
  let bad = 0; for (const [ok, name, info] of results) { console.log((ok ? "PASS " : "FAIL ") + name + (info ? `  [${info}]` : "")); if (!ok) bad++; }
  console.log(`\n${bad} failure(s) of ${results.length} (edge case: ${EDGE})`); process.exit(bad ? 1 : 0);
}
(async () => {
  await sleep(400);
  const S = JSON.parse(data.state), F = S.findings;
  if (EDGE) return edgeChecks(S);
  check("header shows last run + files", /last run/.test($("#s-run").textContent) && /files/.test($("#s-run").textContent), $("#s-run").textContent.slice(0, 90));
  check("header shows per-app counts", /sonarr \d+/.test($("#s-run").textContent) && /whisparr \d+/.test($("#s-run").textContent));
  check("health chip rendered", /healthy|running|stale|failed/.test($("#s-health").textContent), $("#s-health").textContent);
  check("severity chips", $$("#s-sev .chip").length === 4);
  check("Today panel rendered", /need you|nothing needs you/.test($("#today-sum").textContent) && /Needs you/.test($("#today-body").textContent), $("#today-sum").textContent.slice(0, 90));
  const libs = [...new Set(F.map(f => f.library))].sort();
  check("library dropdown lists every library", $$("#f-lib option").length === libs.length + 1, $$("#f-lib option").map(o => o.textContent).join(" | "));
  check("check dropdown populated", $$("#f-check option").length > 5);
  const groups0 = $$("#findings details.grp-d").length; check("findings grouped by show", groups0 > 10, groups0 + " groups");
  check("rows have Plex + arr links", $$("#findings a[title='open in Plex']").length > 0 && $$("#findings a[title^='open in']").length > 0, $$("#findings a[title='open in Plex']").length + " plex links");
  // severity filter: 'low' is unchecked by default -> enabling it adds rows
  const rows0 = $$("#findings tr[id^=r-]").length; $$(".sev").find(c => c.value === "low").click(); $$(".sev").find(c => c.value === "low").dispatchEvent(new w.Event("change")); await sleep(50);
  const rows1 = $$("#findings tr[id^=r-]").length; check("enabling low severity shows more rows", rows1 > rows0, `${rows0} -> ${rows1}`);
  // library filter
  const sel = $("#f-lib"); sel.value = libs[0]; sel.dispatchEvent(new w.Event("change")); await sleep(50);
  check("library filter narrows the count line", new RegExp(libs[0]).test($("#f-count").textContent) && $$("#findings tr[id^=r-]").length <= rows1, $("#f-count").textContent.slice(0, 80));
  sel.value = ""; sel.dispatchEvent(new w.Event("change")); await sleep(50);
  // group by library
  $("#f-group").value = "library"; $("#f-group").dispatchEvent(new w.Event("change")); await sleep(50);
  check("group by library -> one section per library", $$("#findings details.grp-d").length === libs.length, $$("#findings details.grp-d").length + " vs " + libs.length);
  $("#f-group").value = "check"; $("#f-group").dispatchEvent(new w.Event("change")); await sleep(50);
  check("group by check works", $$("#findings details.grp-d").length === new Set(F.map(f => f.check)).size);
  $("#f-group").value = "show"; $("#f-group").dispatchEvent(new w.Event("change")); await sleep(50);
  // check-specific buttons
  const has = code => F.some(f => f.check === code);
  const btn = (sel) => $$(sel).length;
  if (has("MISSING_EPISODE")) check("missing episodes get a search button, no re-grab", btn("button[data-act=search]") > 0 && !F.filter(f => f.check === "MISSING_EPISODE").some(f => $(`#r-${f.id} button[data-act=regrab]`)));
  if (has("AUDIO_OFFSET")) check("intentional audio offsets hide fix sync", !F.filter(f => f.check === "AUDIO_OFFSET" && /intentional/.test(f.detail)).some(f => $(`#r-${f.id} button[data-act=fixsync]`)));
  if (has("DUB_ONLY")) check("dub-only rows get re-grab", F.filter(f => f.check === "DUB_ONLY").some(f => $(`#r-${f.id} button[data-act=regrab]`)));
  if (has("NO_SUBS")) check("no-subs rows get find subs", F.filter(f => f.check === "NO_SUBS" && f.app === "sonarr").some(f => $(`#r-${f.id} button[data-act=subsearch]`)));
  if (has("HI10P")) check("HI10P movies get re-grab, anime does not", F.filter(f => f.check === "HI10P").every(f => !!$(`#r-${f.id} button[data-act=regrab]`) === (f.library !== "Anime")));
  check("group headers offer bulk buttons", btn("button[data-bulk]") > 0, btn("button[data-bulk]") + " bulk buttons");
  check("fix-tag box pre-filled with a language", $$("input[id^=lang-]").every(i => /^[a-z]{3}$/.test(i.value)), $$("input[id^=lang-]").length + " boxes");
  // ignore -> inline confirm -> no
  const ign = $("button[data-ign]"); ign.click(); await sleep(30);
  check("ignore shows inline confirm", !!$(".confirm")); $(".confirm button:last-child").click(); await sleep(30); check("confirm 'no' restores the button", !$(".confirm") && !!$("button[data-ign]"));
  // ignore -> yes -> POST sent
  $("button[data-ign]").click(); await sleep(30); $(".confirm button.primary").click(); await sleep(100);
  check("ignore 'yes' posts to /api/ignore", posts.some(p => p.url.includes("api/ignore")), posts.map(p => p.url).join(","));
  // a group the user opened must survive a background refresh
  const closed = $$("#findings details.grp-d").find(d => !d.open); if (closed) { closed.open = true; const k = closed.dataset.k; await w.load(); await sleep(100); check("open group survives a background refresh", !!$$("#findings details.grp-d").find(d => d.dataset.k === k && d.open), k); }
  // tabs
  for (const t of ["trash", "actions", "history", "settings", "live"]) { $(`nav button[data-tab=${t}]`).click(); await sleep(150); check(`tab ${t} visible`, !$(`#tab-${t}`).classList.contains("hidden")); }
  check("trash tab lists items or empty note", $("#trash").textContent.length > 10);
  check("actions tab shows the re-grab tracker", /pending|imported|same_defect|restored|no re-grabs/.test($("#regrabs").textContent));
  check("history tab draws the chart + runs table", !!$("#history svg") && $$("#history table tr").length > 2, $$("#history table tr").length + " rows");
  const SET = JSON.parse(data.settings);
  check("settings tab renders one control per setting", $$("#settings [data-set]").length === SET.items.length, `${$$("#settings [data-set]").length}/${SET.items.length}`);
  $$("#settings [data-set]")[0].value = "dryrun"; $("#save-settings").click(); await sleep(100);
  check("settings save posts to /api/settings", posts.some(p => p.url.includes("api/settings")));
  check("live tab shows phase", /phase/.test($("#l-st").textContent));
  $("nav button[data-tab=space]").click(); await sleep(100); check("space tab lists cold + never-watched with archive buttons", $$("#space table").length >= 1 && $$("#space button[data-archive]").length >= 1 && /Watched/.test($("#space").textContent) && /Never watched/.test($("#space").textContent), $$("#space table").length + " tables, " + $$("#space button[data-archive]").length + " archive buttons");
  $("nav button[data-tab=findings]").click(); await sleep(50);
  // whisper tool
  $("#w-show").value = "Kakegurui"; $("#w-season").value = "1"; $("#w-go").click(); await sleep(30); $(".confirm button.primary").click(); await sleep(100);
  check("whisper tool posts show+season", posts.some(p => p.url.includes("api/whisper") && /Kakegurui/.test(p.body)));
  // ---- usability (Layer 1): plain words first, codes only as tooltips, verbs on buttons, help and undo
  const rowsAll = $$("#findings tr[id^=r-]"); check("every row leads with a plain-language sentence", rowsAll.length > 0 && rowsAll.every(r => r.querySelector(".plain") && r.querySelector(".plain").textContent.trim().length > 15), rowsAll.length + " rows");
  const rawCode = /\b[A-Z]{2,}_[A-Z_]{2,}\b/; check("group headers show no raw check codes", $$("#findings .grp-head").every(sm => !rawCode.test(sm.textContent)), ($$("#findings .grp-head").find(sm => rawCode.test(sm.textContent)) || {}).textContent);
  check("row problem column shows names, codes only in tooltips", rowsAll.slice(0, 200).every(r => !rawCode.test(r.children[1].textContent) && rawCode.test(r.children[1].title)), (rowsAll.find(r => rawCode.test(r.children[1].textContent)) || { children: [0, { textContent: "" }] }).children[1].textContent);
  const jargon = /^(re-grab|regrab|fix tag|fixtag|subsearch|plexrefresh|fixsync|fixheader|subtag|acceptlang|verify content|quarantine)$/i;
  check("buttons use plain verbs, not arr jargon", !$$("#findings button").some(x => jargon.test(x.textContent.trim())), ($$("#findings button").find(x => jargon.test(x.textContent.trim())) || {}).textContent);
  check("severity chips use plain words", $$("#s-sev .chip").every(c => /won't play|wrong|waiting|FYI/i.test(c.textContent)), $$("#s-sev .chip").map(c => c.textContent).join(","));
  if (has("NO_SUBS")) check("no-subs rows offer AI subtitles", F.filter(f => f.check === "NO_SUBS" && f.app === "sonarr").some(f => $(`#r-${f.id} button[data-act=whisper]`)));
  check("page starts in simple view (technical tabs hidden)", d.body.classList.contains("simple") && $("nav button[data-tab=history]").classList.contains("adv-only"));
  $("#adv-toggle").click(); await sleep(30); check("Advanced toggle switches the view", !d.body.classList.contains("simple") && /Simple/.test($("#adv-toggle").textContent)); $("#adv-toggle").click(); await sleep(30);
  $("button[data-help=start]").click(); await sleep(30); check("help button opens the help panel", !$("#helpbox").classList.contains("hidden") && /How this page works/.test($("#helpbox").textContent));
  $("#help-close").click(); await sleep(30); check("help panel closes", $("#helpbox").classList.contains("hidden"));
  const rg = $("#findings button[data-act=regrab]"); if (rg) { rg.click(); await sleep(30); $(".confirm button.primary").click(); await sleep(150); check("file moves offer Undo in the toast", /Undo/.test($("#toast").textContent) && !!$("#toast button"), $("#toast").textContent.slice(0, 80)); }
  check("no JavaScript errors during the session", errors.length === 0, errors.slice(0, 3).join(" | "));
  let bad = 0; for (const [ok, name, info] of results) { console.log((ok ? "PASS " : "FAIL ") + name + (info ? `  [${info}]` : "")); if (!ok) bad++; }
  console.log(`\n${bad} failure(s) of ${results.length}`); process.exit(bad ? 1 : 0);
})().catch(e => { console.error("BOT CRASH", e); process.exit(2); });
