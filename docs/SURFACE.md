# Product surface: one core, three shells

**One core, exposed three ways, with zero logic duplication.**
Deterministic behaviour → CLI. Reasoning-heavy workflow → a Skill orchestrates the CLI (never
reimplements it). Hosts that cannot run a shell → a curated MCP shim.

```
              ┌──────────────────────── core (this repo) ────────────────────────┐
              │  runtime (lease · dispatch · adapters) · control (v3 API client)  │
              │  discovery (capture · classify · compose · validate) · reporting  │
              └───────────────▲───────────────▲───────────────▲──────────────────┘
                              │               │               │
                   CLI (deterministic)   Skills (reasoning)   MCP (curated tools)
                   scriptable, --help    SKILL.md workflows   shell-less hosts
```

## The shape: one noun in front, the machinery behind it

A target used to be four things you had to hold in your head and keep in sync — an adapter
config, an application record, a stored bridge key, and a purpose string. `target` is the one
noun for all of them, and it is what the root help leads with:

- **`target`**: `add` (detect a URL / cURL / HAR / saved config → adapt → prove → register) ·
  `list` · `show` · `check` (re-prove against the live endpoint) · `rm`
- **`assess`**: `run` · `watch` · `pause` · `resume` · `list` — the relay is started and stopped
  for you
- **`results`** / **`reports`** / **`export`** / **`ci`**: read findings, and gate on them
- **`doctor`**: preflight — key and scopes, API and lease-service reachability, dependencies

Underneath, unchanged and fully supported: **`app`**, **`adapter`**, **`keys`**, plus
`bridge`, `chat`, `controls`, `onboard`, `policy`, `status`, `tenant`, `export`. Nothing was
removed or renamed when `target` was added, so existing scripts keep working; `ascend <command>
--help` documents each one.

The root help is tiered (START HERE / EVERYDAY / MORE) rather than alphabetical, because the
question a new operator has is "what do I run first", not "what exists".

## CLI: the primary surface

Every command takes `--json`. Machine output goes to stdout, human prose to stderr, so
redirecting one never corrupts the other. Exit codes are stable and documented in
[`AGENTS.md`](AGENTS.md) — that file is the contract for driving this tool from an agent or a
script.

## Skills: reasoning workflows (Claude Code plugin)

Four skills, each driving one phase by calling the same commands you would:

- **build-adapter**: capture → resolve the ambiguous layers with judgment → `adapter validate`
  → iterate. Carries a per-target-pattern catalog and a symptom-ordered troubleshooting
  playbook, because most failures here present as a different failure than they are.
- **onboard-target**: evidence → adapter → registered target → first run.
- **run-assessment**: choose controls and strategies → run → monitor → summarize.
- **triage-findings**: false-positive triage, severity recalculation, finding write-up.

Determinism lives in the CLI; the skills supply judgment only where the answer is genuinely
ambiguous.

## MCP: curated, deliberately not a mirror

`shells/mcp/` execs the same CLI verbs with `--json`. It exposes a **curated** subset — leading
with `target_add` and `target_check`, so an agent can go from a bare URL to a result without
leaving MCP — and it is intentionally **not** a 1:1 mirror of every command: an MCP server
injects every tool schema into the model's context on every conversation, while a CLI costs only
the `--help` an agent actually reads. For agents that have a shell (Claude Code, Codex, Cursor),
call `ascend <verb> --json` directly. MCP is for hosts that cannot.

## Distribution (all from one repo)

CLI → pip / standalone binary (primary) · Skills → Claude Code plugin · MCP → thin shim that
execs the CLI, for shell-less hosts.
