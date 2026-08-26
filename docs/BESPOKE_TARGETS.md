# Bespoke targets — the local-shim escape hatch

The bridge natively covers HTTP request/response, streaming (SSE/WebSocket), multi-step session
APIs, browser widgets, the model providers, and the vendor platforms — the ~95% you actually
meet. For the rare target that doesn't fit that shape, **don't grow the bridge around it.** Wrap
it behind a tiny local HTTP server and point `direct_api` at the server.

This is exactly how out-of-band agents (e.g. an email-driven SDR) have been red-teamed: the shim
hides the orchestration (create a record, send a message, poll a channel for the reply) behind a
synchronous `POST /chat`, and the bridge treats it like any other REST target — with a long
`timeout_ms` because one turn can take minutes.

## Recipe

1. Copy [`templates/shim_template.py`](../templates/shim_template.py) and fill in `handle_prompt`
   — your logic to get one prompt to the agent and one reply back, however long that takes.
2. Run it: `python3 shim_template.py` (defaults to `:8099`).
3. Write a `direct_api` config pointing at it with a generous timeout:

   ```json
   {
     "adapter": "direct_api",
     "endpoint": "http://127.0.0.1:8099/chat",
     "method": "POST",
     "body": {"prompt": "{{PROMPT}}"},
     "response_path": "response",
     "timeout_ms": 1800000
   }
   ```
4. `ascend adapter validate --file shim.json`, then `onboard`/`assess` as usual.

## Why a shim and not a new adapter

A one-off orchestration (two auth systems, a side channel, human-timescale latency) would add a
large, rarely-used subsystem to the core and make every other adapter heavier to reason about.
The shim keeps that complexity in one throwaway file next to the engagement, and the bridge stays
small and predictable. The 5% is reachable; it just isn't carried as core weight.
