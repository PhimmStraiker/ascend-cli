# Skills: the reasoning layer over a deterministic CLI

`ascend` below means `python3 shells/cli/ascend.py`.

## The model

Three layers sit over one core, with no duplicated logic:

- **Deterministic core (the CLI).** Every verb takes `--json` and emits a stable
  envelope: `{"ok": true, "data": {...}}` or `{"ok": false, "error": {...}}` on
  stdout, prose on stderr. Counting, grouping, provenance splits, lifecycle
  transitions, adapter classification, and the CI gate all live here. All are
  tested and repeatable.
- **Skills (the reasoning layer).** `SKILL.md` workflows that orchestrate CLI
  verbs. They choose, sequence, and interpret. They do not reimplement CLI logic.
  Determinism stays in the CLI; a skill supplies judgment only where a CLI
  classifier reports low confidence or refuses to answer.
- **The agent (the driver).** Any host executes the skills against the CLI:
  Claude Code, Cursor, Codex, or a plain terminal. For shell-less hosts (claude.ai
  web, Cowork, locked-down runtimes) the **thin MCP shim** (`shells/mcp/`) execs
  the same `ascend <verb> --json` 1:1; shell-having hosts call the CLI directly
  and skip the MCP token overhead. Eleven tools are exposed, and
  `ascend_target_add` / `ascend_target_check` lead the list: an agent reads it
  top-down, and without them it could run and read an assessment but not create
  the thing being assessed, so it hit a wall at the first step.

`agent/` is the **judgment layer**: prompt material only. Nothing in it is
imported, run, or affects a CLI number. It records the calls the CLI refuses to
guess, for a human or agent to apply with the evidence in front of them, chiefly
false-positive triage (`agent/TRIAGE.md`).

## How the skill set grows

The skill set expands as the platform grows. As the Straiker platform APIs add capabilities, the
CLI gains the commands to use them, and new agent skills are added on top to
orchestrate those commands. The reasoning layer expands with the API; the
deterministic core stays the same contract (`--json` in, `{ok,data}` out), so new
skills compose with the existing ones without changing anything below them.

## Skill ↔ CLI mapping

| Skill | Drives | Judgment it adds |
|---|---|---|
| **onboard-target** | `target add <thing>`, then composes the skills below | end-to-end sequencing; a live-probe gate before launch |
| **build-adapter** | `adapter build --har\|--url\|--api\|--curl\|--spec` → `adapter validate` (also `adapter list\|show\|configs\|layers`) | resolves only the low-confidence layers the classifier flags |
| **run-assessment** | `target check` → `assess run` / `assess watch` / `assess results` | control choice, monitoring, lifecycle-correct execution |
| **triage-findings** | `results <export.csv> --values` (`--json`) | FP triage + auth-gating severity recalc via `agent/TRIAGE.md` |

## The everyday noun is `target`

A target used to be four things held in your head at once — an adapter config, an application
record, a stored bridge key, and a purpose string — and keeping them in sync was a manual step
between every other step. `target` is one noun over all four:

```
ascend target add <thing>      # build + validate the adapter, register the app, store the key
ascend target list             # adapter, registered, serving
ascend target show <t>         # everything bound to one target, in one place
ascend target check <t>        # re-prove it against the live endpoint, and time it
ascend target rm <t>           # delete the application and drop its key
```

`target add` detects what `<thing>` is — a URL, a request copied out of devtools, an exported
browser session, or a saved config — so a skill does not have to pick between five
mutually-exclusive source flags before it knows the answer. It stops once the target is registered
and proven; `--run` continues into an assessment.

Nothing was removed or renamed underneath. `app`, `adapter` and `keys` are the same commands they
were and remain the right surface for a step done by hand: `adapter build` when a layer needs
resolving, `app create` for a specific platform type, `keys` when a key has to move.

The inputs to **build-adapter** are a HAR export, a curl/proxied send, a spec, or
a live URL — the same set `target add` detects. All enter through `ascend adapter build` (or
`ascend discover` for the classification alone); nothing ships until `ascend adapter validate` is
green (replay of the captured turn and a fresh probe, compared to the observed answer).
`ascend target check` is that same gate, re-runnable later against a registered target, and it is
what separates a pass from a false pass on the next run.

## run-assessment and the bridge lifecycle

A **bridge-type** app runs its adapter on your side; the CLI relays probes for it.
The bridge lifecycle is now automatic. Do not add a manual start step:

- `ascend assess run` on a bridge-type app **auto-starts the bridge** (the CLI's
  built-in relay) before probes are scheduled, and the bridge **self-stops** when
  the assessment reaches a terminal state.
- While an assessment is **paused** the bridge stays alive; it self-stops only when the run
  reaches a terminal state. Idle cleanup is opt-in via `--idle-timeout` (off by default).
- `ascend assess resume` **re-ensures** a bridge. Use it after a Console-side
  resume, since the SaaS cannot start a process on your machine.
- `ascend bridge sync` reconciles bridges to assessment state (start for
  running/paused apps, stop for terminal). Use it as the manual fallback when state
  changed in the Console.
- `ascend bridge start` still exists for advanced use (remote, continuous, or
  pre-start), but is not a step in the normal flow.

A bridge is **per-app**: one relay is shared across that app's assessments with no
cross-assessment contamination. The v2 pull protocol (`transport/`) carries only
an opaque `request_id`/`msg_id` that the bridge echoes back, and the platform
attributes each probe to its assessment. False-pass safety is preserved: a bridge
never self-stops when it cannot verify state.

## Repo layout

| Folder | Holds |
|---|---|
| `control/` | v3 API client (fixed create → pause → resume → results lifecycle) |
| `runtime/` | bridge engine, adapters, layer discovery, supervisor, cred/session handling |
| `shells/` | `cli/` (deterministic CLI) and `mcp/` (thin 1:1 shim for shell-less hosts) |
| `reporting/` | deterministic analysis (counting, provenance, turns) + the CI gate |
| `agent/` | judgment prompts (`TRIAGE.md`, `README.md`), prompt material only |
| `skills/` | reasoning workflows (`SKILL.md` per skill) |
| `transport/` | v2 pull protocol (lease/term, bridge clients) |
| `docs/` | reference docs (surface, glossary, lifecycle, capability matrix) |
| `demo/` | recorded walkthroughs and demo targets |
| `configs/` | example and engagement adapter configs |
| `templates/` | scaffolds (e.g. the MCP shim template) |
| `scripts/` | build and codegen (binary build, command-map generation) |
| `tests/` | the test suite that guards the deterministic core |
