# Product surface — one core, three shells (holistic model)

**Principle: one core, exposed three ways, zero logic duplication.**
Deterministic behaviour → CLI. Agent-invokable → MCP mirrors the CLI 1:1. Reasoning-heavy
workflow → a Skill orchestrates CLI/MCP (never reimplements them).

```
              ┌──────────────────────── core (this repo) ────────────────────────┐
              │  transport (v2 lease/term/browser) · runtime (compose adapters)   │
              │  control (v3 api, fixed lifecycle) · discovery (layer classifiers)│
              └───────────────▲───────────────▲───────────────▲──────────────────┘
                              │               │               │
                   CLI (deterministic)   MCP (typed tools)   Skills (reasoning)
                   scriptable, --help    for any agent host  SKILL.md workflows
```

## CLI — deterministic substrate (`ascend <group> <verb>`, full --help everywhere)
- **app**: create --type bridge|api|gcp|bedrock (default bridge; returns the bridge key) · list · get · bind · delete
- **assess**: run · status · pause · resume · results · list   (lifecycle: created→pause→resume)
- **controls**: list (filter deprecated/agentic/category; warn on zero-probe)
- **runtime**: start   (v2 pull-mode; --capture, --qpm, --max-workers)
- **discover**: har|url|capture → per-layer classification → draft config (+confidence+evidence)
- **adapter**: validate(hard gate) · list · show · layers(introspect capability matrix)
- **ci**: nonzero on new findings / severity breach; baseline diff
- **doctor**: preflight (key scopes, reachability, egress/proxy, tmux/deps, control sanity)

## MCP — a THIN passthrough, not a parallel surface (optional / deferred)
Decision: CLI + skills are the primary surfaces. MCP is **not** hand-written logic — it is a
thin wrapper that execs the same CLI verbs with `--json` (or imports the same core). It earns
its place ONLY for hosts without a shell (Cowork, claude.ai web, locked-down enterprise) or for
remote/hosted org-wide deployment. For shell-having agents (Claude Code, Codex, Cursor) the
agent just calls `ascend <verb> --json` directly — MCP would only add tool-definition token
overhead (4–32x vs a CLI call). So: every CLI command emits `--json`; MCP is an auto-generated
1:1 shim added later if a shell-less host needs it, never a v1 priority and never a second impl.

## Skills — reasoning workflows (Claude Code plugin)
- **build-adapter** — drive `discover` → resolve low-confidence layers with judgment → `adapter
  validate` → iterate. Determinism in the CLI; reasoning only on the ambiguous residue.
- **onboard-target** — map → build-adapter → app create → adapter validate → first run.
- **run-assessment** — choose controls/strategies → run → monitor → summarize.
- **triage-findings** — FP triage, auth-gating severity recalc, STAR framing.
- **report** — brand-consistent readout (reuses existing ascend-report).

## Distribution (all from one repo)
CLI → pip/standalone binary (primary) · Skills → Claude Code plugin marketplace · MCP → optional thin shim (exec CLI --json) for shell-less hosts.
