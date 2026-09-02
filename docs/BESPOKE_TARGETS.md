# Bespoke targets — the local-shim escape hatch

The 15 shipped adapters natively cover HTTP request/response, streaming (SSE, NDJSON, WebSocket and
marker-framed plain-text bodies), multi-step session APIs, create-send-then-poll transcripts,
browser widgets, the model providers, and the vendor platforms — the ~95% you actually meet. For
the rare target that doesn't fit that shape, **don't grow the bridge around it.** Wrap it behind a
tiny local HTTP server and point `direct_api` at the server.

This is exactly how out-of-band agents (e.g. an email-driven SDR) have been red-teamed: the shim
hides the orchestration (create a record, send a message, poll a channel for the reply) behind a
synchronous `POST /chat`, and the bridge treats it like any other REST target.

## Recipe

1. Copy [`templates/shim_template.py`](../templates/shim_template.py) and fill in `handle_prompt`
   — your logic to get one prompt to the agent and one reply back.
2. Run it: `python3 shim_template.py` (defaults to `:8099`).
3. Write a `direct_api` config pointing at it, in your config directory (`ascend adapter configs`
   names the one writes land in):

   ```json
   {
     "adapter": "direct_api",
     "endpoint": "http://127.0.0.1:8099/chat",
     "method": "POST",
     "body": {"prompt": "{{PROMPT}}"},
     "response_path": "response"
   }
   ```

   Leave `timeout_ms` out. With no `timeout_ms` the adapter derives its budget from the platform's
   per-probe window, which is the bound that actually decides the run (below).
4. `ascend adapter validate --config shim` proves it against the live shim and times the call.
5. `ascend target add shim` registers it and stores the bridge key. Add `--run` to continue
   straight into an assessment.

## The shim hides orchestration. It cannot hide latency.

A long `timeout_ms` used to look like the answer for a turn that takes minutes. It is not, and
setting one is now clamped to the bridge's give-up point. The platform gives each probe a bounded
window (~110–120s) and starts that clock when the probe is **queued**, not when the bridge calls
the shim. A shim that answers after the window produces a synthetic timeout indistinguishable from
the target failing, which feeds the target-health streak and auto-pauses the assessment — so the
run reports nothing, having measured nothing.

`adapter validate` (and `target check`) print the measured reply time and warn at the window and
again at 60% of it. Take that warning seriously before starting a run. Where the orchestration is
genuinely slower than the window, the options are to make the shim faster — cache the login, keep
the record open between turns, poll harder — or to have the window raised platform-side, which
`$ASCEND_PLATFORM_PROBE_WINDOW_MS` then tells the CLI about. Raising the adapter timeout alone
changes nothing: the router has already abandoned the probe.

## Why a shim and not a new adapter

A one-off orchestration (two auth systems, an out-of-band reply channel, a record that has to exist
first) would add a large, rarely-used subsystem to the core and make every other adapter heavier to
reason about.
The shim keeps that complexity in one throwaway file next to the engagement, and the bridge stays
small and predictable. The 5% is reachable; it just isn't carried as core weight.
