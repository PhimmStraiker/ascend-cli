# `agent/` — the judgement layer

Everything in this directory is **prompt material, not code**. Nothing here is imported by the CLI,
runs in the binary, or affects a single number the CLI reports.

It exists because two very different kinds of work were getting mixed together:

- **Deterministic** — counting probes, grouping by category, checking whether a value that appeared
  in a response also appeared in the prompt, reading the platform's own false-positive flags. This is
  arithmetic and pattern matching. It belongs in `reporting/`, it is tested, and it always gives the
  same answer.
- **Judgement** — deciding that a published support number is not really a data leak, that a
  capability was demonstrated rather than merely present, that a finding deserves High rather than
  Critical. This depends on knowing the customer and the engagement. A regex that guessed at it would
  quietly change numbers on their way to a security team.

So the CLI does the first and refuses the second. This directory is where the second is written down,
for a human or an agent to apply with the evidence in front of them.

| File | What it is |
|---|---|
| [TRIAGE.md](TRIAGE.md) | The triage rules, the fields to look at, and a worked loop over `ascend results --json` |

## Using it from Claude Code

The CLI is the interface — no MCP server, no install step:

```bash
ascend results run.csv --json > run.json
ascend results run.csv --turns --limit 0
```

Then hand `agent/TRIAGE.md` and `run.json` to the agent. The contract it can rely on:

- `--json` is a stable envelope: `{"ok": true, "data": {...}}` on success, `{"ok": false,
  "error": {...}}` on failure, both on stdout, prose on stderr.
- `values[].from_target` / `.echoed` is the mechanical provenance split.
- `failing_turns[].explanation` is the platform's own reason, passed through verbatim.
- `warnings[]` names anything that changes how the numbers should be read.
- No heuristic in the CLI has already adjusted a count.

See [../docs/AGENTS.md](../docs/AGENTS.md) for the full agent-facing contract, exit codes, and the
traps worth knowing before driving the CLI programmatically.
