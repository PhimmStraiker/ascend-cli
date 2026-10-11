# Application types

An Ascend application declares **how the platform reaches your target**. There are four types. The
choice determines whether anything runs on your side: only `bridge` does.

```
ascend app create --type bridge|api|gcp|bedrock --name 'My Bot' ...
```

| Type | How Ascend reaches the target | Bridge? | Use it when |
|---|---|---|---|
| `bridge` | Hands prompts to the CLI's bridge running on **your** side, which calls the target through a local adapter (legacy, turn mode) | **Yes** | A login a person completes, device posture, a driven browser, or a Python adapter only your machine can run. A target that is merely private takes the tunnel instead |
| `api` | Ascend calls your endpoint itself: through the request template, or through a hosted adaptor stored on the app (`request_template._adaptor_src`). Direct, or by a tunnel / private-link name | No | The platform can reach the target: a public URL, or a `.tun.straiker.ai` / `.pl.straiker.ai` name. What `target add` registers by default |
| `gcp` | Native Vertex AI / Agent Engine integration | No | Vertex Agent Engine, ADK on Vertex |
| `bedrock` | Native AWS Bedrock integration | No | Bedrock agents and runtimes |

`bridge` is `app create`'s default when `--type` is not given, and it is the legacy path. The CLI's
adapter layer can be configured to speak to any target, and the bridge runs on your side of the
network boundary. Pick `bridge` only when the adapter has to run locally: a Python module, a driven
browser, or a login handshake (`oauth2`, `csrf`, `derived_multihop`) no generated adaptor runs yet.
A target Ascend cannot reach is no longer a reason on its own: run the tunnel agent next to it
(`ascend tunnel`) and the platform calls it as an `api` app by its `.tun.straiker.ai` name.
`ascend assess run` starts and stops the bridge for you (see below); you don't run one by hand for a
normal assessment.

`ascend target add` registers an **`api` app carrying a hosted adaptor** by default (`--via auto`): it
derives and proves the target's contract, generates a JavaScript adaptor from it, gates it, stores
it on the app as `request_template._adaptor_src`, and verifies it through the engine, so nothing
runs on this machine during the assessment. On its own it falls back to a `bridge` app only for an
adapter that can run nowhere else (`custom`, `browser`) or a login handshake no generated adaptor
runs yet, and says so; `--via bridge` asks for one outright and is deprecated; `--via api` registers
a plain template app with no adaptor. A private address is refused before anything is created,
naming the tunnel and `--via bridge` as the ways out. A tunnel or private-link name is registered as
a template `api` app from a contract you supply (a cURL, a HAR, a saved config), since the name
never resolves on this machine; `--via bridge` is refused for it for the same reason. `gcp` and
`bedrock` are a deliberate choice: `app create --type`, or `ascend target cloud add`.

## Required fields per type

These are validated **locally, before the request**. A missing field is caught and named locally, so
it does not surface as a 422 from the API.

| Type | Required |
|---|---|
| `bridge` | `request_template`, `response_template`, `headers`. All defaulted, so `--name` alone works |
| `api` | `url`, `api_key`, `request_template`, `response_template`, `headers` |
| `gcp` | `url`, `service_account_info` |
| `bedrock` | `url`, `bedrock_authentication_method` (`assume-role` or `access-key`) |

```
$ ascend app create --type gcp --name 'Vertex Agent' --url https://…:streamQuery
error: a 'gcp' application needs: url, service_account_info
  missing: service_account_info
```

## The four types in detail

### `bridge` (legacy: the adapter runs on your side)

```bash
ascend target add https://internal.corp/chat --name 'My Bot' --bearer "$TOK" --via bridge \
  --controls sys_prompt_leak,jailbreak
ascend assess run --app 'My Bot' --name 'run 1'
```

`--via bridge` is what makes this a bridge app; without it, `target add` refuses the private address
and names the tunnel. That first call is `adapter build` + `app create` + the key store, in one. The step-by-step form is
unchanged and still the one to use when you want to edit the config in between:

```bash
ascend adapter build --api https://internal.corp/chat --bearer "$TOK" --out mybot.json
ascend app create --name 'My Bot' --config mybot --controls sys_prompt_leak,jailbreak
```

No manual `ascend bridge start`. `ascend assess run` on a bridge app **auto-starts** the bridge
before probes are scheduled, and the bridge **self-stops** when the assessment reaches a terminal
state. While an assessment is paused the bridge stays alive and keeps serving; idle cleanup is opt-in
via `--idle-timeout` (off by default), so a platform stall never stops it. It never self-stops when
it cannot verify state, because an unverifiable stop would risk a false pass.

`ascend assess resume` re-ensures a bridge. Use it after a Console-side resume, since the SaaS can't
start a process on your machine. If state changed in the Console and a bridge is out of sync,
`ascend bridge sync` reconciles: it starts bridges for running/paused apps and stops them for
terminal ones.

A bridge is per-**app**: one bridge is shared across that app's assessments, with no cross-assessment
contamination. The v2 lease/result protocol carries only an opaque `request_id`/`msg_id` that the
bridge echoes back; the platform attributes each probe to its assessment.

The create response carries the bridge key (`tc-…`) **exactly once**. The CLI stores it for you; if
the API ever returns a create without one, the command fails rather than leave an app that no bridge
can serve.

`ascend bridge start` still exists for advanced use: a remote or long-lived host, a continuous
bridge, or pre-starting before a run. It is not a step in the normal flow.

### `api` (no bridge)

This is the type `target add` registers by default, with a hosted adaptor stored on it; `ascend adaptor`
edits and re-proves that adaptor (`docs/CUSTOM_ADAPTOR.md`). The step-by-step form makes a plain
template app: the url, templates and headers come straight from a mapped config, since
`ascend adapter build` already produces exactly those fields:

```bash
ascend adapter build --api https://api.example.com/chat --bearer "$TOK" --out mybot.json
ascend app create --type api --name 'Public Bot' --config mybot --target-api-key "$TOK"
ascend assess run --app 'Public Bot' --name 'run 1'      # no bridge to start
```

Ascend calls the target from its own infrastructure, so the target must be reachable from it — a
public URL, or a private one behind the tunnel agent (`https://<host>.tun.straiker.ai/<path>`, see
`ascend tunnel`) or a Straiker private link (`https://<label>.pl.straiker.ai/<path>`) — and the key
must be one the platform can hold. This type never appears in `bridge ls` and never triggers the
NO-BRIDGE alarm.

### `gcp`

```bash
ascend app create --type gcp --name 'Vertex Agent' \
  --url 'https://us-central1-aiplatform.googleapis.com/v1/projects/P/locations/us-central1/reasoningEngines/ID:streamQuery' \
  --service-account @sa.json
```

`--service-account` takes `@path` so a service-account JSON is never pasted onto a command line
(where it would land in shell history).

### `bedrock`

```bash
# assume-role (preferred)
ascend app create --type bedrock --name 'Bedrock Agent' \
  --url 'arn:aws:bedrock:us-east-1:123456789012:agent/AGENTID' \
  --bedrock-auth assume-role \
  --role-arn 'arn:aws:iam::123456789012:role/StraikerAscend' \
  --external-id "$EXT" --region us-east-1

# static keys
ascend app create --type bedrock --name 'Bedrock Agent' --url 'arn:…' \
  --bedrock-auth access-key --access-key-id "$AKID" --secret-access-key "$SECRET"
```

Credential fields you do not pass are omitted from the request rather than sent empty.

## Cloud targets: `ascend target cloud`

Discover lists an organisation's Bedrock AgentCore runtimes, classic Bedrock agents and Vertex Agent
Engine deployments, but an inventory row for one of them carries **no endpoint and no resource id**
(measured on a live tenant: 126 cloud-platform rows, none with a URL or an ARN). The Discover
connector's role can list those resources; it cannot invoke them, and the one-click Ascend button
exists only where the platform publishes a ready target. The operator's **own** cloud credentials can
finish the job: the `aws` session that deployed a runtime can name its ARN, and the `gcloud` login
that deployed an ADK agent can name its engine. From either, a native application follows.

```bash
ascend target cloud list --aws --region us-east-2                  # AgentCore runtimes + Bedrock agents
ascend target cloud list --gcp --project my-project --region us-east4   # reasoning engines
```

`list` is read-only and prints, in the console's words, where each candidate stands:

| STATE | Meaning |
|---|---|
| `onboarded` | an Ascend application already carries this ARN or endpoint |
| `potential · testable now with your cloud credentials` | a Discover row of that platform has this name; `cloud add` registers it |
| `cloud candidate` | it matches nothing in Discover |

A Discover row on that platform which **no** candidate matched is `potential · needs access`: an
account or region these credentials do not reach, or a resource since deleted. With no PAT in the
shell nothing is claimed and the column reads `-`.

AWS is read through **boto3** when it is installed (`pip install 'ascend-cli[aws]'` — the same
dependency the bedrock adapter needs; it signs and pages for free), else through the **aws CLI** on
PATH; the output says which (`via`). GCP is read through the Vertex REST list (current `gcloud`
releases have no `ai reasoning-engines` verb) with the token `gcloud auth print-access-token` mints,
or `GOOGLE_OAUTH_ACCESS_TOKEN`. A missing credential is one line naming what to set:

```
error: no AWS credential: export AWS_PROFILE (or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY), or run `aws sso login`
error: no GCP credential: run `gcloud auth login` (or set GOOGLE_OAUTH_ACCESS_TOKEN)
```

No credential value is ever printed, by `list`, by `add`, or under `--dry-run`.

```bash
# an AgentCore runtime (or a classic agent: agent/ID, or agent-alias/ID/ALIAS to name the alias)
ascend target cloud add arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/support_agent-AbCd123456 \
  --name 'Support Agent' --auth assume-role \
  --role-arn arn:aws:iam::123456789012:role/StraikerAscend --external-id env:STRAIKER_EXTERNAL_ID

# a name from `list`, resolved with the same flags; --dry-run prints the spec and sends nothing
ascend target cloud add support_agent --aws --region us-east-2 --role-arn arn:aws:iam::123456789012:role/StraikerAscend --dry-run

# a reasoning engine: its resource name or its streamQuery URL, with the service account Ascend will use
ascend target cloud add projects/my-project/locations/us-east4/reasoningEngines/1234567890 \
  --service-account @sa.json --match 'support-bot-v1'
```

`add` registers the native type — `bedrock` with the ARN and how Ascend authenticates (`assume-role`,
the default, needs `--role-arn`; `access-key` needs `--access-key-id` and `--secret-access-key`), or
`gcp` with the engine's `:streamQuery?alt=sse` endpoint and the service-account JSON — and prints
the record the way `target add` does. A value given as `env:NAME` is read from that variable so it
never sits on a command line; `--service-account @path` reads only the file you name. The role you
pass must trust Straiker's cross-account role with your external id and allow
`bedrock-agentcore:InvokeAgentRuntime` (or `bedrock:InvokeAgent`); the Discover connector's role
has the list permissions and not the invoke one.

`--match` names the Discover row this is (by its name in the console); without it, the row with the
candidate's own name is used when there is one. The console joins a registered target to its row
**by name**, so the application is named after the row unless you pass `--name` — and then the
command says the board will not join them. After `add`, `target list`, `target show` and `target rm`
know the target, and `ascend target check <name>` proves from this machine, on its own cloud
credentials, that the runtime or engine answers (the CLI's own bedrock / vertex_ai adapter). That is
a different path from the platform's, which assumes the role or uses the keys on the record; the
check says so.

## Which apps need a bridge

```bash
ascend app list --with-runs     # STATE column
ascend target list              # per target: adapter, registered, and whether it is serving
ascend bridge ls                # bridge-based apps only; flags live runs with no bridge
```

The NO-BRIDGE alarm is deliberately scoped to `bridge` apps. Flagging an `api`/`gcp`/`bedrock` app for
having no bridge would be a false alarm, and false alarms train people to ignore real ones. A live
assessment with nobody answering scores a **false pass**, because unanswered probes are not findings.

## Severity and guardrails at create time

```bash
ascend app create --name 'My Bot' \
  --category-severity data_leak=high \
  --input-guardrail http_status_code=403
```

**`--category-severity`** maps to the app's `category_severities` field. The platform's enum is
`default|low|medium|high`. There is no `critical`, so a policy asking for it is clamped to `high`
and the command says so. Per-**control** severity is not settable anywhere in v3; express that in
`ascend-policy.json` instead, where it applies to `ascend reports` and `ascend ci`.

**`--input-guardrail`** tells the platform how the target signals a block: an HTTP status
(`http_status_code=403`) or text (`response_pattern='I can't help with that'`, pipe-separated for
several). Without it, a guardrail block looks identical to the target genuinely answering, which
produces guardrail false positives in scoring.

To change either later:

```bash
ascend policy set --app 'My Bot' --category data_leak=high
ascend policy push --app 'My Bot'          # sends the CATEGORY half upstream
```

`push` reports which per-control overrides stayed local, so nobody assumes they reached the Console.

## Changing an app's type

You cannot. `api_type` is fixed at creation, because the create body is a discriminated union on it.
Delete and recreate:

```bash
ascend app delete 'My Bot'        # also drops its stored bridge key (or: ascend target rm 'My Bot')
ascend app create --type api --name 'My Bot' --config mybot --target-api-key "$TOK"
```

Deleting removes the stored key too: a `tc-` key without its app cannot be used. Everything *else*
about a live app — name, system prompt, qpm, controls, severities, guardrail signal — changes in
place with `ascend app update`; only the type needs the round trip.
