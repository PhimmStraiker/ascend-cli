# Ascend CLI — Test Suite

A fully **offline** regression + permutation suite (700+ cases). Every HTTP call
(`requests` + `urllib`) and every WebSocket is mocked in `conftest.py`, so the
suite runs with **no network** — on a locked-down CI runner or a laptop in
airplane mode, the result is identical and deterministic.

## Running

From the repo root:

```bash
# preferred
python3 -m pytest tests

# verbose (shows every parametrized case)
python3 -m pytest tests -v

# a single file / a single test
python3 -m pytest tests/test_prompt_escape.py
python3 -m pytest tests/test_lease_client.py -k capture

# stdlib-only fallback (no pytest installed) — the pure-function files still run
python3 -m unittest discover -s tests
```

`pytest.ini` (repo root) sets `testpaths = tests` and quiet output. No plugins,
no network, no fixtures that touch the filesystem outside `tmp_path`.

> The `unittest` fallback runs the plain assertion-style tests; the heavily
> parametrized coverage (hundreds of cases) needs `pytest`, which is the
> supported path. `pip install pytest` if it is missing.

## Layout

| File | What it locks down |
|------|--------------------|
| `conftest.py` | Import wiring (`control/` + `runtime/` on the path), the async runner, `FakeResponse` / `install_fake_requests` (mock `requests`), `FakeUrlopen` (mock `urllib`), and the shared adversarial-prompt matrix. |
| `test_dispatch.py` | `extract_prompt` across every body shape (str, `{prompt}`/`{message}`/`{input}`/`{text}`/`{query}`/`{content}`/`{question}`, nested fallback, explicit `prompt_field`, error cases); `shape_result` success/failure/custom-field/upstream-status; `conversation_key` header/body/none; `load_config`. |
| `test_prompt_escape.py` | **H1 security regression.** A large adversarial-prompt matrix (quotes, backslashes, control chars, unicode, JSON-injection, `{{PROMPT}}`-in-payload, very long) is rendered through `direct_api` + `session_api` with mocked HTTP; asserts the outbound body has **exactly** the template keys and the prompt lands byte-for-byte — no sibling-key injection, no JSON break. Also covers the `_json_escape` primitive directly. |
| `test_lease_client.py` | Empty poll ≠ error; single & batch probes answered/submitted; handler exception → 500 submitted (never dropped); 401/403 fatal; transient error backoff; `stop()`; capture file is `0600` and redacts `Authorization`/`Cookie`/token headers; stats counters. |
| `test_control_api.py` | PAT→JWT RFC-8693 exchange shape; a 401 forces exactly one re-exchange then stops; direct bearer used as-is; `run()` drives create → **pause** → resume → poll in that order; `_safe_transition` swallows 409; `validate_controls` flags deprecated/unknown/zero-probe; spec builders + `_clean_templates`. |
| `test_adapters_config.py` | All 11 adapters honour the `{response, success, error, duration_ms, metadata}` contract — happy path, missing-config fast-fail, transport error — and never raise out of `send_prompt`. `direct_api` config permutations (methods, timeouts, response paths, HTTP errors, non-JSON). |
| `test_discovery.py` | `runtime/discovery` per-layer classifiers over synthetic HAR/evidence: transport (`rest_json`/`sse`/`ndjson`/`websocket`), auth (`static`/`oauth2`/`csrf`/`none`), session (`create_conversation`/`create_session`/`stateless`); `validate_config` live-gate with mocked HTTP. Skips cleanly if the module is absent. |

## Conventions

- **No network, ever.** If you add a test that would open a socket, mock it in
  `conftest.py` first. `install_fake_requests` (for `requests`) and monkeypatching
  `urllib.request.urlopen` / `websockets.connect` cover every transport in the repo.
- **Permute, don't repeat.** New cases are added as `@pytest.mark.parametrize`
  rows so one bug trips at least one case.
- **Document quirks, don't assert fixes.** Where a test documents current
  behaviour of a shipped module (e.g. the `session_api` `{{SESSION_ID}}`
  substitution-collision), it says so — it locks the behaviour so a future change
  is a conscious one.
