# Ascend CLI — thin MCP shim

This is an **optional** surface. The primary surfaces are the deterministic CLI
(`ascend <group> <verb> --json`) and the reasoning Skills that drive it
(`skills/*/SKILL.md`). See `docs/SURFACE.md` for the "one core, three shells"
model.

## What this is

`server.py` is a **thin 1:1 passthrough**. Every MCP tool shells out to the exact
same CLI verb with `--json` and returns the parsed JSON. It holds **no** logic of
its own — no API calls, no adapter code, no lifecycle handling. All of that lives
in the core (`control/`, `runtime/`) and is exposed through the CLI. The shim only
removes one blocker: hosts that cannot run a shell.

## When to use MCP vs the CLI

| Your agent host | Use |
|---|---|
| Has a shell (Claude Code, Codex, Cursor, a terminal, CI) | **The CLI directly.** `python3 shells/cli/ascend.py <verb> --json`. Skip MCP entirely. |
| Has **no** shell (claude.ai web, Cowork, locked-down enterprise runtime) | **This MCP shim.** It is the only way to reach the core without a shell. |
| Remote / hosted org-wide deployment behind one endpoint | This shim, run as a shared server. |

Why the CLI is preferred when a shell exists: MCP tool definitions sit in the
model's context on every turn (schemas for all nine tools), which costs materially
more tokens than a single `ascend ... --json` call. A thin shim is also a second
place that can drift from the CLI, so it is deliberately generated-style and kept
dumb.

## Tools (1:1 with CLI verbs)

| MCP tool | CLI verb |
|---|---|
| `ascend_app_list` | `app list` |
| `ascend_app_create_bridge` | `app create --type bridge` |
| `ascend_controls_list` | `controls list` |
| `ascend_controls_validate` | `controls validate` |
| `ascend_assess_run` | `assess run` |
| `ascend_assess_status` | `assess status` |
| `ascend_assess_results` | `assess results` |
| `ascend_adapter_list` | `adapter list` |
| `ascend_doctor` | `doctor` |

The runtime/relay verb (`runtime start`) is intentionally **not** exposed over
MCP — it is a long-lived foreground process (leases probes and relays them to the
target until stopped), which does not fit a request/response tool call. Run it
from a shell or a supervisor.

## Auth

Auth flows through the CLI exactly as on the command line: set `$STRAIKER_PAT` in
the server's environment (and `$STRAIKER_BRIDGE_API_KEY` if you later add a live
probe). Each tool also accepts optional `token` / `base` / `bridge_base` arguments
that map to the CLI global flags, for hosts that inject per-call credentials.

## Wiring it into a host

`server.py` speaks newline-delimited JSON-RPC 2.0 over stdio (MCP protocol
revision `2024-11-05`): `initialize`, `tools/list`, `tools/call`, `ping`,
`shutdown`. No `mcp` package is required — stdlib only.

Example stdio server entry:

```json
{
  "mcpServers": {
    "ascend-bridge": {
      "command": "python3",
      "args": ["shells/mcp/server.py"],
      "env": { "STRAIKER_PAT": "s6r_pat_..." }
    }
  }
}
```

## Testing without a live client

The tool catalog is plain data, so you can exercise it with no MCP client:

```bash
python3 shells/mcp/server.py --manifest                 # print tools/list JSON
python3 shells/mcp/server.py --call ascend_adapter_list '{}'
python3 shells/mcp/server.py --call ascend_doctor '{}'
```

In Python, `build_argv(name, args)` (pure, deterministic argv mapping),
`run_tool(name, args)` (subprocess + parse), `list_tools()` and
`handle_request(req)` are all importable and unit-testable directly.
