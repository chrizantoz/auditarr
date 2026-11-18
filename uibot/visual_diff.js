// Visual regression: compare the browser bot's screenshots (uibot/screens/) with the committed baselines (uibot/baseline/).
// Q35: every picture in shots.js is compared (the Findings view plus every tab, phone and desktop). Fails when more than 1 % of pixels changed,
// when the bot did not take a picture it should have, or when a picture has no baseline (VISUAL_NEW_OK=1 lets a brand-new picture pass once).
// Refresh baselines deliberately: copy CI's screenshots (the ui-screenshots artifact, not local ones: fonts differ) into baseline/ and commit.
const fs = require("fs"), path = require("path");
const { PNG } = require("pngjs"); const pixelmatch = (m => m.default || m)(require("pixelmatch"));   // v6 is ESM-only
const { NAMES } = require("./shots");
const here = __dirname, base = process.env.VISUAL_BASELINE || path.join(here, "baseline"), out = path.join(here, "screens"); let bad = 0, seen = 0;   // VISUAL_BASELINE: compare with another folder (local proofs; CI uses the committed one)
for (const name of NAMES) {
  const a = path.join(base, name), b = path.join(out, name);
  if (!fs.existsSync(b)) { console.log(`FAIL ${name}: the browser bot took no such screenshot`); bad++; continue; }
  if (!fs.existsSync(a)) { const ok = process.env.VISUAL_NEW_OK === "1"; console.log(`${ok ? "SKIP" : "FAIL"} ${name}: no baseline yet (copy CI's ${name} from the ui-screenshots artifact into uibot/baseline/ and commit)`); if (!ok) bad++; continue; }
  const A = PNG.sync.read(fs.readFileSync(a)), B = PNG.sync.read(fs.readFileSync(b)); seen++;
  if (A.width !== B.width || A.height !== B.height) { console.log(`FAIL ${name}: size ${A.width}x${A.height} vs ${B.width}x${B.height}`); bad++; continue; }
  const diff = new PNG({ width: A.width, height: A.height });
  const n = pixelmatch(A.data, B.data, diff.data, A.width, A.height, { threshold: 0.15 }); const pct = 100 * n / (A.width * A.height);
  fs.writeFileSync(path.join(out, "diff-" + name), PNG.sync.write(diff));
  console.log(`${pct > 1 ? "FAIL" : "PASS"} ${name}: ${pct.toFixed(2)} % of pixels changed`); if (pct > 1) bad++;
}
console.log(`\n${bad} failure(s) of ${NAMES.length} pictures (${seen} compared)`); process.exit(bad ? 1 : 0);
