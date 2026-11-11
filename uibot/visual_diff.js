// Visual regression: compare the browser bot's screenshots (uibot/screens/ui-*.png) with the committed baselines (uibot/baseline/).
// Fails when more than 1 % of pixels changed. No baseline yet -> prints how to create it and passes.
// Refresh baselines deliberately: copy screens/ui-phone.png and ui-desktop.png into baseline/ and commit (CI screenshots, not local ones).
const fs = require("fs"), path = require("path");
const { PNG } = require("pngjs"); const pixelmatch = (m => m.default || m)(require("pixelmatch"));   // v6 is ESM-only
const here = __dirname, base = path.join(here, "baseline"), out = path.join(here, "screens"); let bad = 0, seen = 0;
for (const name of ["ui-phone.png", "ui-desktop.png"]) {
  const a = path.join(base, name), b = path.join(out, name);
  if (!fs.existsSync(b)) { console.log(`SKIP ${name}: no screenshot`); continue; }
  if (!fs.existsSync(a)) { console.log(`SKIP ${name}: no baseline yet (copy ${path.relative(process.cwd(), b)} to uibot/baseline/ and commit)`); continue; }
  const A = PNG.sync.read(fs.readFileSync(a)), B = PNG.sync.read(fs.readFileSync(b)); seen++;
  if (A.width !== B.width || A.height !== B.height) { console.log(`FAIL ${name}: size ${A.width}x${A.height} vs ${B.width}x${B.height}`); bad++; continue; }
  const diff = new PNG({ width: A.width, height: A.height });
  const n = pixelmatch(A.data, B.data, diff.data, A.width, A.height, { threshold: 0.15 }); const pct = 100 * n / (A.width * A.height);
  fs.writeFileSync(path.join(out, "diff-" + name), PNG.sync.write(diff));
  console.log(`${pct > 1 ? "FAIL" : "PASS"} ${name}: ${pct.toFixed(2)} % of pixels changed`); if (pct > 1) bad++;
}
console.log(`\n${bad} failure(s) of ${seen} compared`); process.exit(bad ? 1 : 0);
