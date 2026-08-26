# Architecture

How Ascend CLI connects the Straiker Ascend assessment cloud to your AI agent —
what runs where, what data crosses which boundary, and what you have to operate.

> **Adapter reference:** the per-adapter configuration schemas live in
> [`ADAPTER_AUTHORING.md`](ADAPTER_AUTHORING.md) and the layer model in
> [`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md). For the product documentation on
> advanced adapters, see the Straiker documentation site
> (`https://docs.straiker.ai` → Ascend AI → advanced integrations).

---

## 1. The big picture

Straiker generates and scores the attacks. **You run a small bridge** that fetches
probes over plain HTTPS and delivers them to your agent however your agent expects to
be called. Your agent is never exposed to the internet, and Straiker never needs a
route into your network.

```mermaid
flowchart LR
    subgraph straiker["Straiker Ascend cloud"]
        IRIS["Iris engine<br/>generates attacks<br/>scores responses"]
        BRIDGE["bridge<br/>/v2/lease<br/>/v2/result"]
        API["v3 API<br/>apps · assessments<br/>controls · findings"]
        IRIS <--> BRIDGE
    end
    subgraph yours["Your network / laptop"]
        CLI["<b>ascend</b> CLI"]
        BRIDGE["bridge<br/><i>outbound HTTPS only</i>"]
        AD["adapter<br/>(1 of 13)"]
        TARGET(["your AI agent"])
        CLI --> BRIDGE
        BRIDGE --> AD --> TARGET
    end
    BRIDGE -- "lease / result" --> BRIDGE
    CLI -- "PAT" --> API
    linkStyle 0,1 stroke-width:2px
```

**Boundary facts that matter for review:**

| Question | Answer |
|---|---|
| Does Straiker connect *into* my network? | **No.** The bridge makes **outbound** HTTPS calls only. No inbound firewall rule, no public exposure of the agent. |
| What leaves my network? | The probe prompt (authored by Straiker) and your agent's response, which Straiker scores. |
| Where do my credentials live? | Target credentials stay in your adapter config / environment and are used only by the bridge. Straiker never receives them. |
| Can I stop it instantly? | Yes — stopping the bridge stops delivery immediately. |
| What if the bridge dies mid-probe? | The probe is reclaimed server-side after ~90s and redelivered. Nothing is lost. |

---

## 2. The probe lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant I as Iris (Straiker)
    participant R as bridge (yours)
    participant A as adapter
    participant T as your agent

    R->>I: POST /v2/lease (long-poll ≤25s)
    Note over R,I: no persistent socket — nothing to drop
    I-->>R: probes[] (0..10)
    loop each probe
        R->>A: prompt
            A->>T: your real contract (REST/SSE/WS/session/poll)
            T-->>A: reply
            A-->>R: {response, success, duration}
            R->>I: POST /v2/result (200)
    end
    R->>I: POST /v2/lease (repeat)
```

Failures are **submitted, never dropped** — a target error becomes an honest result, so
the assessment completes instead of hanging.

---

## 3. What you operate

```mermaid
flowchart TB
    subgraph shells["Shells — thin, no business logic"]
        CLI["<b>ascend</b> CLI<br/>--help and --json everywhere"]
        SK["skills<br/><i>agent workflows</i>"]
        MCP["MCP shim<br/><i>optional, shell-less hosts</i>"]
    end
    subgraph core["Core"]
        CTRL["control/<br/>v3 client<br/>created→pause→resume"]
        DISC["discovery/<br/>capture · classify<br/>compose · validate"]
        RT["runtime/<br/>lease · dispatch<br/>adapters"]
        ENT["reporting/<br/>SARIF · CI gate"]
    end
    AD["adapters (13)<br/>rest · sse · ndjson · websocket<br/>session · poll · sentinel<br/>browser · terminal · platform presets"]
    CLI --> core
    SK --> CLI
    MCP -.->|exec --json| CLI
    RT --> AD --> TGT(["your agent"])
    CTRL --> SAPI(["Straiker v3 API"])
    RT --> SBR(["/v2/lease · /v2/result"])
```

One core, three thin shells. The CLI is the deterministic substrate; skills and the
optional MCP shim call it rather than reimplementing anything.

---

## 4. Why pull mode

The legacy bridge held a persistent WebSocket that Straiker pushed probes onto. A
dropped socket produced `bad handshake` on reconnect and could **auto-pause a live
assessment**. v2 inverts the flow.

```mermaid
flowchart LR
    subgraph old["Legacy — push over a socket"]
        O1["persistent WebSocket"] --> O2["broken pipe"] --> O3["bad handshake"] --> O4["assessment auto-paused"]
    end
    subgraph new["v2 — pull over plain HTTPS"]
        N1["long-poll lease"] --> N2["network blip"] --> N3["retry next lease"] --> N4["run continues"]
    end
```

There is no long-lived connection, so that entire class of failure cannot occur.
Un-acked probes are reclaimed after ~90s; the bridge adds a QPM throttle and
session-aware concurrency the old bridge lacked.

---

## 5. Getting connected

Most integration effort is *learning your agent's contract*. Four ways, cheapest first:

```mermaid
flowchart LR
    A["--api  endpoint / base URL"] --> V
    B["--curl your curl command"] --> V
    C["--spec OpenAPI / Swagger"] --> V
    D["--har  your own export"] --> V
    E["--url  browser capture"] --> V
    F["--manual you drive"] --> V
    V{"<b>adapter validate</b><br/>replay against the LIVE target"}
    V -- answered --> S(["usable config → chat / assess"])
    V -- did not --> R(["diagnosis + hint<br/><i>nothing written</i>"])
```

A config is only usable once it has produced a real answer from your live target.
See [`DISCOVERY.md`](DISCOVERY.md) for the capture pipeline and its limits.
