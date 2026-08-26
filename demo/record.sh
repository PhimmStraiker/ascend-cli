#!/usr/bin/env bash
# record.sh — record a REAL, readable terminal demo of the Ascend CLI to MP4 + GIF.
#
# Unedited asciinema capture of the actual commands running — one page per step, held long enough
# to read. Requires: asciinema, agg, ffmpeg, jq (brew install ...), a running demo target on
# http://127.0.0.1:8600 (python3 demo/localhost_agent.py), and $STRAIKER_PAT in the env.
#
#   export STRAIKER_PAT=s6r_pat_...
#   python3 demo/localhost_agent.py &          # target bot (needs a model key in .env)
#   ./demo/record.sh                           # -> demo/out/ascend-cli-demo.{mp4,gif}
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="$HERE/out"; mkdir -p "$OUT"
: "${STRAIKER_PAT:?set STRAIKER_PAT first}"
for t in asciinema agg ffmpeg jq; do command -v "$t" >/dev/null || { echo "missing: $t"; exit 1; }; done
curl -s -m4 -o /dev/null "http://127.0.0.1:8600/" || { echo "start the target first: python3 demo/localhost_agent.py"; exit 1; }

RUN="$OUT/.run.sh"
cat > "$RUN" <<'SCRIPT'
#!/usr/bin/env bash
set -uo pipefail
cd "$REPO"
export TERM=xterm-256color; export ASCEND_FORCE_COLOR=1
BOLD=$'\033[1m'; RST=$'\033[0m'; PINK=$'\033[38;5;205m'; CY=$'\033[36m'; GRN=$'\033[32m'; DIM=$'\033[90m'; COLS=100; export COLUMNS=100
page(){ clear; printf '\n\n'; printf "  ${DIM}ascend cli  ·  %s${RST}\n\n" "$1"; }
say(){ printf "  ${CY}${BOLD}%s${RST}\n\n" "$1"; sleep 4.5; }
run(){ printf "  ${GRN}${BOLD}\$ ${RST}${BOLD}%s${RST}\n\n" "$1"; sleep 1.8; eval "$1" 2>&1 | sed 's/^/  /'; printf '\n'; sleep "${2:-6}"; }
banner(){ clear; printf '\n\n\n'; printf "%s" "$PINK$BOLD"; figlet -w "$COLS" "$1" 2>/dev/null || printf "  == %s ==" "$1"; printf "%s" "$RST"; [ -n "${2:-}" ] && printf "\n  ${DIM}%s${RST}\n" "$2"; sleep 3.2; }

banner "ASCEND CLI" "recorded live — every command below actually ran"
page "what it is"
say "A red-team CLI for AI targets. This is the command set, in the order you use it."
run "./ascend --help 2>&1 | sed -n '/the flow/,/Full reference/p'" 7
page "json on every command"
say "Every command emits JSON, so an agent can parse the output and act on it."
run "./ascend results --json 2>/dev/null | jq '.data[0] | {app,severity,failed,total}'" 6
banner "1 · TWO LAYERS" "a deterministic CLI, and Skills an agent runs on top"
page "the skills"
say "Skills are workflows an agent runs to drive the CLI. Four ship with it."
run "ls -1 skills/" 6
page "what a skill is"
say "Each SKILL.md is the workflow for one job — which commands to run, and how to read the output."
run "sed -n '2,8p' skills/build-adapter/SKILL.md" 7
banner "2 · BUILD AN ADAPTER" "and prove it against the live target"
page "build from a live endpoint"
say "adapter build reads the endpoint, works out the request and response shape, and calls the target to confirm it. A config that does not answer is not saved."
run "./ascend adapter build --api http://127.0.0.1:8600/chat --out acme 2>&1 | grep -vE '^ +\"' | sed -n '1,9p'" 7
page "the source can be anything"
say "Same command, five sources. A HAR is the most reliable for a real target: it carries the authenticated request, so the adapter inherits the target's headers and tokens."
run "./ascend adapter build --help 2>&1 | sed -n '/--har <file>/,/--spec <base>/p'" 7
banner "3 · TALK TO THE TARGET" "through the adapter — this is what gets attacked"
page "a normal request"
say "chat sends a prompt through the adapter. The target is a live LLM support bot."
run "./ascend chat acme --prompt 'how long does shipping take?' --no-record 2>&1 | sed -n '1,6p'" 7
page "no authorization check"
say "An order number, no login, no identity. It returns a different customer's email — broken object-level authorization."
run "./ascend chat acme --prompt 'show me the account email on order AC-33471902' --no-record 2>&1 | sed -n '1,6p'" 8
banner "4 · REGISTER + RUN" "the bridge is automatic"
page "register the target"
say "A bridge app is one the CLI relays for. Register it; the relay is managed from here on."
run "./ascend app create --type bridge --name 'AcmeShop Live' --config acme --controls sys_prompt_leak,indirect_prompt_injection,phone_number --if-not-exists 2>&1 | sed -n '1,8p'" 8
page "run the assessment"
say "No separate bridge step. assess run starts the relay, and it stops itself when the run ends."
run "./ascend assess run --app 'AcmeShop Live' --name 'recorded demo' --no-wait 2>&1 | sed -n '1,4p'" 8
page "the relay it started"
say "The bridge stood itself up and is serving the new app."
run "sleep 2; ./ascend bridge ls 2>&1 | sed -n '1,6p'" 8
banner "5 · READ RESULTS" "one call, pass/fail from total probes"
page "the whole tenant in one call"
say "Tenant, apps by type, stored keys, and running bridges."
run "./ascend status --quick 2>&1 | sed -n '1,6p'" 7
page "every run, worst first"
say "Latest run per target, severity-ranked. The dot colors risk: red high, yellow medium, green low. FAIL% is failed divided by total probes — no opaque score."
run "./ascend results --sort sev 2>/dev/null | sed -n '1,11p'" 8
page "one target in depth"
say "Fail rate, the pass/fail split, probe counts, the controls that failed, and their categories."
run "./ascend results --app 'Straiker Protected Models' --detail 2>/dev/null | sed -n '1,6p'" 8
banner "6 · GATE THE PIPELINE" "exit 2 on findings"
page "fail the build on findings"
say "Exit 0 clean, 2 on findings, 1 if the results cannot be read. A dead bridge cannot pass as clean."
AID=$(./ascend assess list --app 'Straiker Protected Models' --json 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);r=d if isinstance(d,list) else d.get('data',[]);print(r[0]['id'] if r else '')")
run "./ascend ci --app 'Straiker Protected Models' --assessment $AID --fail-on-severity high 2>&1 | sed -n '1,9p'; echo \"exit code = \$?\"" 9
( ./ascend bridge stop --all >/dev/null 2>&1; ./ascend app delete 'AcmeShop Live' >/dev/null 2>&1 ) || true
banner "ASCEND CLI" "adapter build · auto-bridge · results · CI — from the terminal"
sleep 3.5
SCRIPT

echo ">> recording (real commands; ~4 min)..."
asciinema rec --overwrite --window-size 100x30 -c "REPO='$REPO' STRAIKER_PAT='$STRAIKER_PAT' bash '$RUN'" "$OUT/ascend-cli-demo.cast"
echo ">> rendering GIF + MP4..."
agg --font-size 22 --line-height 1.4 --idle-time-limit 12 "$OUT/ascend-cli-demo.cast" "$OUT/ascend-cli-demo.gif"
ffmpeg -y -i "$OUT/ascend-cli-demo.gif" -movflags +faststart -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" "$OUT/ascend-cli-demo.mp4" >/dev/null 2>&1
rm -f "$RUN"
echo ">> done:"; ls -la "$OUT"/ascend-cli-demo.{mp4,gif}
