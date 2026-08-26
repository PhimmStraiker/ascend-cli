#!/bin/bash
# fleet-walkthrough.command — a REAL, recorded walkthrough of the Ascend CLI fleet capabilities.
#
# Nothing here is synthetic: it runs live commands against your real Straiker tenant, and
# screen-records the actual Terminal window while it does. It is paced for a human watcher —
# each step announces what it is about to prove, runs it, then pauses so you can read the output.
#
#   1. double-click this file (or: bash demo/fleet-walkthrough.command)
#   2. the FIRST time, macOS asks Terminal for Screen Recording permission:
#        System Settings > Privacy & Security > Screen Recording > enable Terminal, then re-run
#   3. the .mov lands in demo/ and the tenant is cleaned up at the end
#
# Env:
#   STRAIKER_PAT   required (or it is read from ../.env)
#   NO_RECORD=1    run the walkthrough without screen-recording
#   PACE=1.5       speed multiplier for the pauses (default 1.0; use 2 to slow it down)
set -u

cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
PACE="${PACE:-1.0}"
OUT="$REPO/demo/fleet-walkthrough-$(date +%Y%m%d-%H%M%S).mov"

# ---- credentials ------------------------------------------------------------------------
if [ -z "${STRAIKER_PAT:-}" ]; then
  for env_file in "$REPO/../.env" "$REPO/.env"; do
    if [ -f "$env_file" ]; then
      # shellcheck disable=SC1090
      STRAIKER_PAT="$(grep -m1 '^export STRAIKER_PLATFORM_API_KEY=' "$env_file" 2>/dev/null \
        | sed 's/^export STRAIKER_PLATFORM_API_KEY=//' | tr -d '"'"'"' ')"
      [ -n "$STRAIKER_PAT" ] && break
    fi
  done
fi
if [ -z "${STRAIKER_PAT:-}" ]; then
  echo "STRAIKER_PAT is not set and no .env was found — export it and re-run." >&2
  exit 1
fi
export STRAIKER_PAT

# ---- presentation helpers ---------------------------------------------------------------
BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[38;5;44m'; PINK=$'\033[38;5;204m'; OFF=$'\033[0m'
pause() { sleep "$(python3 -c "print($1 * $PACE)")"; }
say()   { printf '\n%s# %s%s\n' "$CYAN" "$1" "$OFF"; pause 1.6; }
note()  { printf '%s  %s%s\n' "$DIM" "$1" "$OFF"; pause 1.2; }
run()   {                      # echo the command as if typed, then run it
  printf '\n%s$ %s%s\n' "$PINK" "$1" "$OFF"
  pause 0.9
  eval "$1"
  pause "${2:-2.5}"
}
title() { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; pause 1.2; }

cleanup() {
  printf '\n%s# cleanup — leaving your tenant exactly as we found it%s\n' "$CYAN" "$OFF"
  ./ascend relay stop --all >/dev/null 2>&1
  for n in "Demo Fleet A" "Demo Fleet B" "Demo Fleet C"; do
    ./ascend app delete "$n" >/dev/null 2>&1
  done
  ./ascend keys prune >/dev/null 2>&1
  rm -f configs/demo-fleet-a.json configs/demo-fleet-b.json configs/demo-fleet-c.json
  pkill -f 'scripts/test_fixtures.py' >/dev/null 2>&1
  [ -n "${REC_PID:-}" ] && kill -INT "$REC_PID" 2>/dev/null && wait "$REC_PID" 2>/dev/null
  printf '  done.\n'
  [ -f "$OUT" ] && printf '%s  recording: %s%s\n' "$BOLD" "$OUT" "$OFF"
}
trap cleanup EXIT INT TERM

# ---- local targets so the walkthrough is deterministic ----------------------------------
python3 scripts/test_fixtures.py >/dev/null 2>&1 &
sleep 2

# ---- start recording --------------------------------------------------------------------
if [ -z "${NO_RECORD:-}" ]; then
  echo "recording to: $OUT"
  echo "(first run: grant Terminal 'Screen Recording' in System Settings, then re-run)"
  screencapture -v -D 1 "$OUT" & REC_PID=$!
  sleep 3
fi

clear
title "Ascend CLI — one tenant, many agents, many relays"
note "Everything below is live: a real Straiker tenant, real assessments, real relays."
pause 2

# =========================================================================================
say "1. Which tenant am I locked to?  (the CLI refuses to mix customers)"
run "./ascend tenant show" 3.5
note "Only a SHA-256 fingerprint is stored — never the raw tenant id, never the PAT."
note "A PAT from a different tenant is refused outright, not warned about."

say "2. Preflight: key, scopes, bridge reachability, optional deps."
run "./ascend doctor" 3.5

# =========================================================================================
say "3. Map three agents. No browser, no hand-written schema — and each config is VALIDATED"
say "   against the live target before it is written."
for pair in "a:8790" "b:8810" "c:8796"; do
  n="demo-fleet-${pair%%:*}"; p="${pair##*:}"
  run "./ascend map --api http://127.0.0.1:$p/chat --out $n.json" 2.0
done
note "map also takes --url (real browser), --curl, --spec, --har — and --bearer/--login-url for auth."

# =========================================================================================
say "4. Register all three with Ascend. Each mints its own tc- key, stored locally,"
say "   bound to its config — so you never paste a key again."
# NOTE: macOS ships bash 3.2 — no ${x^^} uppercase expansion. Keep this portable.
for pair in "a:A" "b:B" "c:C"; do
  x="${pair%%:*}"; X="${pair##*:}"
  run "./ascend app create --type bridge --name 'Demo Fleet $X' --controls sys_prompt_leak --config demo-fleet-$x" 2.0
done

say "5. The local key store — one tc- key per app, masked."
run "./ascend keys list" 4.0

# =========================================================================================
say "6. Bring up the FLEET: one detached relay per app. No keys on the command line"
say "   (argv is world-readable via ps) — they go in each child's environment."
run "./ascend relay start --app 'Demo Fleet A' --app 'Demo Fleet B' --app 'Demo Fleet C' --qpm-total 60" 4.0
note "These survive this terminal closing. --qpm-total splits the rate so a shared host isn't hammered."
sleep 6

say "7. Who is serving what?"
run "./ascend relay ls" 5.0
note "* = actively consuming probes. It also flags the inverse: an ACTIVE assessment with NO relay."

# =========================================================================================
say "8. Start all three assessments in one command (the control set is validated once)."
run "./ascend assess run --app 'Demo Fleet A' --app 'Demo Fleet B' --app 'Demo Fleet C' --name 'walkthrough wave'" 4.0

say "9. Watch every live run in one table — including whether a relay is answering it."
note "(30 seconds of live polling; a run nobody answers would show RELAY NONE)"
( ./ascend assess watch --all --interval 5 & WATCH=$!; sleep 30; kill $WATCH 2>/dev/null ) 2>/dev/null
pause 2

say "10. Probes answered, per relay."
run "./ascend relay ls --no-check" 5.0

# =========================================================================================
say "11. The failure mode that matters: kill a relay and the run does NOT fail —"
say "    unanswered probes score as no findings, i.e. a FALSE PASS. So we surface it."
run "./ascend assess run --app 'Demo Fleet A' --name 'orphan demo' --no-wait" 2.5
RPID=$(./ascend relay ls --no-check --json 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin)['relays']
    print(next((r['pid'] for r in rows if (r.get('app_name') or '').endswith('A')), ''))
except Exception: print('')
")
if [ -n "$RPID" ]; then
  run "kill -9 $RPID   # simulate a relay crash" 3.0
fi
run "./ascend relay ls" 6.0
note "That NO-RELAY block is the guard: a clean score from an unserved run is worthless."

# =========================================================================================
say "12. The whole app/assessment picture from the CLI."
run "./ascend app list --with-runs | head -12" 5.0

say "13. Stop the fleet."
run "./ascend relay stop --all" 3.5
note "An in-flight long-poll isn't interruptible, so it reports draining honestly."

title "map -> register -> fleet -> run -> watch.  One tenant. One binary."
pause 4
