#!/bin/bash
# record-walkthrough.command — a REAL macOS Terminal recording of Ascend Bridge v2.
#
# Double-click it (or `bash demo/record-walkthrough.command`). It opens in a real Terminal.app
# window, spins up a local echo agent (nothing external is touched), paces through the bridge's
# capabilities, and screen-records the whole thing to a .mov via macOS `screencapture -v`.
#
# The FIRST time, macOS will ask Terminal for Screen Recording permission — grant it in
# System Settings > Privacy & Security > Screen Recording, then run again.
set -u
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
OUT="$REPO/demo/walkthrough-$(date +%Y%m%d-%H%M%S).mov"
ECHO_PORT=8790

# ---- a local echo agent so the walkthrough is deterministic + offline --------------------
python3 - "$ECHO_PORT" >/dev/null 2>&1 <<'PY' &
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b=json.dumps({"ok":True}).encode(); self.send_response(200)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0)); d=json.loads(self.rfile.read(n) or b"{}")
        msg=d.get("message") or d.get("prompt") or ""
        b=json.dumps({"response":f"DemoBot: you said {msg}"}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1",int(sys.argv[1])),H).serve_forever()
PY
ECHO_PID=$!
trap 'kill $ECHO_PID $REC_PID 2>/dev/null' EXIT
sleep 1

# ---- start the screen recording ----------------------------------------------------------
echo "Recording to: $OUT"
echo "(first run: grant Terminal 'Screen Recording' in System Settings, then re-run)"
screencapture -v -D 1 "$OUT" & REC_PID=$!
sleep 2

# ---- paced walkthrough -------------------------------------------------------------------
type_run() {                    # simulate typing, then run
  printf '\n\033[1;32m$\033[0m '
  for ((i=0; i<${#1}; i++)); do printf '%s' "${1:$i:1}"; sleep 0.012; done
  printf '\n'; sleep 0.4; eval "$1"; sleep "${2:-2}"
}
note() { printf '\n\033[1;36m# %s\033[0m\n' "$1"; sleep 1; }

clear
printf '\033[1mAscend Bridge v2 — capabilities walkthrough\033[0m\n'
sleep 1.5

note "1) Everything wired up? 14 adapters, provider presets, all offline-checkable."
type_run "./ascend doctor" 4
type_run "./ascend adapter list" 3

note "2) Validated provider presets — copy, set an env var, go."
type_run "./ascend adapter configs | grep example- | head -20" 3

note "3) Map a target — derive AND validate a config, no browser, no hand-written schema."
type_run "./ascend map --api http://127.0.0.1:${ECHO_PORT}/chat --out configs/demo.json" 5

note "4) Auth-first — the same map, but supplying a bearer token (baked into the config)."
type_run "./ascend map --api http://127.0.0.1:${ECHO_PORT}/chat --bearer DEMO-TOKEN --out configs/demo-auth.json" 5

note "5) Talk to it — a live conversation, recorded."
type_run "./ascend chat demo --prompt 'what can you help me with?'" 4

note "6) Replay any session as a findings table."
type_run "./ascend results captures/\$(ls -t captures 2>/dev/null | head -1)" 3

printf '\n\033[1mmap → auth → chat → assess. One binary, ~95%% of agents.\033[0m\n'
sleep 3

# ---- stop the recording ------------------------------------------------------------------
kill -INT $REC_PID 2>/dev/null
wait $REC_PID 2>/dev/null
printf '\n\033[1;32mSaved:\033[0m %s\n' "$OUT"
sleep 2
