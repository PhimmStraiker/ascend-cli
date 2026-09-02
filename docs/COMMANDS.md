# Command reference

The full, always-current reference is generated from the CLI itself:

- **[COMMAND_MAP.md](COMMAND_MAP.md)** — every command and flag, with values and defaults.
- **[command-map.html](command-map.html)** — the same, as a browsable page.
- **[architecture.html](architecture.html)** — interactive map: the flows and the commands for each.

Or ask the tool directly:

```bash
ascend --help                 # tiered: START HERE, EVERYDAY, MORE — in the order you use them
ascend <command> --help       # flags + examples for one command
ascend target add --help      # the fastest way in
```

The everyday surface is **`target`** — `add`, `list`, `show`, `check`, `rm` — with `app`,
`adapter` and `keys` as the machinery underneath it. Nothing was removed or renamed when
`target` was added; every command that worked before still works.

Start here: **[BUILD_ADAPTER.md](BUILD_ADAPTER.md)** (connect to a target) and
**[APP_TYPES.md](APP_TYPES.md)** (bridge/api/gcp/bedrock). To drive the CLI from an agent or a
script, read **[AGENTS.md](AGENTS.md)** — the JSON contract and the stable exit codes.
