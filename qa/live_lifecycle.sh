#!/usr/bin/env bash
# live_lifecycle.sh — the ship gate for any bridge/CLI change.
#
# Everything here runs against the REAL platform, a REAL registered app and a REAL agent. Nothing is
# mocked. This exists because bridge bugs have shipped twice on green unit tests whose status values
# were invented: the platform emits statuses those tests never saw (a run against a slow target was
# observed going running -> paused and staying there), so unit green does not mean it works.
#
#   export STRAIKER_PAT=s6r_pat_...            # tenant PAT
#   python3 demo/localhost_agent.py &          # the target (add --slow-secs 120 for a slow agent)
#   qa/live_lifecycle.sh <aapp_id>             # an existing bridge-type app with a stored key
#
# Exit 0 only if every invariant below holds.
set -uo pipefail
cd "$(dirname "$0")/.."
ASCEND="python3 shells/cli/ascend.py"
APP="${1:-}"
AGENT="${ASCEND_QA_AGENT:-http://127.0.0.1:8600/chat}"
FAILED=0

say()  { printf '\n=== %s ===\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED+1)); }

[ -n "${STRAIKER_PAT:-}" ] || { echo "STRAIKER_PAT is not set"; exit 2; }
[ -n "$APP" ] || { echo "usage: qa/live_lifecycle.sh <aapp_id>"; exit 2; }

relay_field() {  # relay_field <app_id> <dotted.field>
  python3 - "$APP" "$1" <<'PY'
import sys, json
sys.path.insert(0, "runtime")
import supervisor as S
rec = S.read_status(sys.argv[1]) or {}
cur = rec
for part in sys.argv[2].split("."):
    cur = (cur or {}).get(part) if isinstance(cur, dict) else None
print("" if cur is None else cur)
PY
}

say "0. target is reachable"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST "$AGENT" \
        -H 'Content-Type: application/json' -d '{"message":"qa ping"}')
[ "$code" = "200" ] && pass "agent answered 200" || fail "agent returned $code at $AGENT"

say "1. control plane reachable"
$ASCEND --json doctor >/dev/null 2>&1 && pass "doctor ok" || fail "doctor failed"

say "2. app create --type bridge with NO --controls"
# v3 rejects control_type 'all' AND rejects omitting it; the CLI must resolve the catalog itself.
NAME="zz-qa-$(date +%H%M%S)"
OUT=$($ASCEND --json app create --type bridge --name "$NAME" 2>&1)
if echo "$OUT" | grep -q '"id"'; then
  pass "created a bridge app with no --controls"
  NEW=$(echo "$OUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
  [ -n "$NEW" ] && $ASCEND app delete "$NEW" >/dev/null 2>&1 && pass "cleaned up $NEW"
else
  fail "bridge create without --controls: $(echo "$OUT" | head -2)"
fi

say "3. no duplicate relay: a manually started bridge is visible to the auto-lifecycle"
$ASCEND bridge stop --app "$APP" >/dev/null 2>&1
$ASCEND runtime start --app "$APP" --config "${ASCEND_QA_CONFIG:-acme}" >/dev/null 2>&1 &
MANUAL=$!
for _ in $(seq 1 30); do [ "$(relay_field state)" = "serving" ] && break; sleep 1; done
if [ "$(relay_field state)" = "serving" ]; then
  pass "manual relay registered under the APP id (not the config name)"
else
  fail "manual relay never registered under $APP — is_serving() would be blind to it"
fi

say "4. an unbound relay NEVER self-stops (standalone stays persistent)"
sleep 35   # past a reconcile beat
[ "$(relay_field state)" = "serving" ] \
  && pass "unbound relay still serving after a reconcile beat" \
  || fail "unbound relay self-stopped — a standalone bridge must stay up"
kill "$MANUAL" 2>/dev/null; $ASCEND bridge stop --app "$APP" >/dev/null 2>&1

say "5. full run: bridge auto-starts, binds, answers, then is released"
RUN="qa-lifecycle-$(date +%H%M%S)"
$ASCEND assess run --app "$APP" --name "$RUN" --interval 10 --timeout 900 >/tmp/qa_run.txt 2>&1
ANSWERED=$(relay_field stats.answered); BOUND=$(relay_field assessment_id)
[ -n "$BOUND" ] && pass "relay was bound to its assessment ($BOUND)" \
                || fail "relay was never bound to an assessment id"
if [ "${ANSWERED:-0}" -gt 0 ] 2>/dev/null; then
  pass "probes were answered ($ANSWERED) — no false pass"
else
  fail "answered=0: every probe failed (check the adapter timeout vs target latency)"
fi
if $ASCEND bridge ls 2>/dev/null | grep -q "^ \*serving .*$APP"; then
  fail "relay still serving after the run finished — parent did not release it"
else
  pass "relay released by the parent after the run"
fi

say "RESULT"
[ "$FAILED" -eq 0 ] && { echo "  all invariants held"; exit 0; }
echo "  $FAILED invariant(s) FAILED"; exit 1
