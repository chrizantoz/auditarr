#!/bin/sh
# Fetches the live API JSON from the audit container and runs the headless UI bot against the deployed ui.html.
# Env: AUDIT_URL (default http://media-audit:3091), UI_HTML (default /app/ui.html), OUT (default /out/uibot.json)
set -u
AUDIT_URL="${AUDIT_URL:-http://media-audit:3091}"; UI_HTML="${UI_HTML:-/app/ui.html}"; OUT="${OUT:-/out/uibot.json}"
mkdir -p /tmp/data
for e in state history settings trash; do curl -sf "$AUDIT_URL/api/$e" -o /tmp/data/$e.json || { echo "{\"ts\":\"$(date -Iseconds)\",\"passed\":0,\"total\":0,\"failed\":[\"could not fetch api/$e\"]}" > "$OUT"; exit 1; }; done
node /bot/ui_bot.js "$UI_HTML" /tmp/data > /tmp/bot.log 2>&1; rc=$?
PASSED=$(grep -c "^PASS " /tmp/bot.log); TOTAL=$(grep -cE "^(PASS|FAIL) " /tmp/bot.log)
node -e '
const fs=require("fs"); const log=fs.readFileSync("/tmp/bot.log","utf8").split("\n");
const failed=log.filter(l=>l.startsWith("FAIL ")).map(l=>l.slice(5).split("  [")[0]);
fs.writeFileSync(process.argv[1], JSON.stringify({ts:new Date().toISOString().slice(0,19), passed:+process.argv[2], total:+process.argv[3], failed}, null, 1));
' "$OUT" "$PASSED" "$TOTAL"
echo "uibot: $PASSED/$TOTAL passed"; grep "^FAIL" /tmp/bot.log; cat /tmp/bot.log | tail -1
exit $rc
