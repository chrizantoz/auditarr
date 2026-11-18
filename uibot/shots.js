// Q35: the screenshots the browser bot takes and visual_diff.js compares with uibot/baseline/. One list, so a tab cannot drop out of the comparison quietly.
const TABS = [["findings", false], ["trash", false], ["actions", false], ["space", false], ["settings", false], ["setup", false], ["about", false], ["history", true], ["live", true]];   // [tab, needs the Advanced view]
const WIDTHS = [["phone", 400, 860], ["desktop", 1280, 900]];
const NAMES = [...WIDTHS.map(([l]) => `ui-${l}.png`), ...TABS.flatMap(([t]) => WIDTHS.map(([l]) => `tab-${t}-${l}.png`))];
module.exports = { TABS, WIDTHS, NAMES };
