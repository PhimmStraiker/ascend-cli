"""API Zoo — a LOCAL mock API server for exercising `discovery.probe` end-to-end
over a real socket. Every scenario the offline suite mocks, served for real.

All data is synthetic. No customer names, no real hosts, no outbound network.

Run:
    python3 tests/live/api_zoo.py 8899

Then, from a Python shell with `runtime/` on sys.path:

    from discovery.probe import probe_api, build_config

    # 1. THE HEADLINE CASE — a parent URL; the endpoint lives two segments down.
    r = probe_api("http://127.0.0.1:8899/openai", rate_limit_s=0)
    assert r.ok and r.endpoint.endswith("/v1/chat/completions")
    assert r.response_path == "choices.0.message.content"

    # 2. The target teaches us its own body shape via a 422.
    r = probe_api("http://127.0.0.1:8899/support/api/chat", rate_limit_s=0)
    assert r.shape_label == "error_hint:question" and r.response_path == "data.reply"

    # 3. Streaming transports.
    probe_api("http://127.0.0.1:8899/stream", rate_limit_s=0).transport   # 'sse'
    probe_api("http://127.0.0.1:8899/ndjson", rate_limit_s=0).transport   # 'ndjson'
    probe_api("http://127.0.0.1:8899/plain",  rate_limit_s=0).transport   # 'text'

    # 4. A GET/query-parameter API.
    probe_api("http://127.0.0.1:8899/getapi", method="GET", rate_limit_s=0).ok

    # 5. Traps — each returns HTTP 200 and must still be REJECTED.
    for trap in ("echo", "empty", "envelope", "html"):
        assert not probe_api(f"http://127.0.0.1:8899/traps/{trap}", rate_limit_s=0).ok

    # 6. Failure diagnoses.
    probe_api("http://127.0.0.1:8899/secure",    rate_limit_s=0).diagnosis  # auth_required
    probe_api("http://127.0.0.1:8899/throttled", rate_limit_s=0).diagnosis  # rate_limited
    probe_api("http://127.0.0.1:8899/broken",    rate_limit_s=0).diagnosis  # server_error
    probe_api("http://127.0.0.1:8899/nothing",   rate_limit_s=0).diagnosis  # not_found

    # 7. The importers, against a spec this zoo publishes at /openapi.json.
    from discovery.importers import discover_spec, configs_from_spec
    discover_spec("http://127.0.0.1:8899")["ok"]

`--selftest` runs all of the above in-process and prints a pass/fail table:

    python3 tests/live/api_zoo.py 8899 --selftest

KNOWN FAILURE (a real defect in the module under test, not in this zoo):
`trap:empty` reports `CRASHED RuntimeError: The content for this response was
already consumed`. Against a LIVE empty 200, `probe._read_body` exhausts
`resp.iter_lines()`, gets no lines, and then falls back to `resp.text` — which
`requests` refuses once a streamed body has been consumed. The offline suite
cannot see this because `conftest.FakeResponse.text` is a plain attribute.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ANSWER = ("I can help with order status, returns, billing questions and account "
          "changes. What would you like to look at?")

API_KEY = "zoo-secret-key"

#: A tiny OpenAPI document so `discovery.importers.discover_spec` has something real
#: to fetch. Mirrors the /support endpoint's actual contract.
OPENAPI = {
    "openapi": "3.0.1",
    "info": {"title": "API Zoo", "version": "1.0.0"},
    "paths": {
        "/health": {"get": {"summary": "Liveness probe", "operationId": "getHealth",
                            "responses": {"200": {"description": "ok"}}}},
        "/support/api/chat": {"post": {
            "summary": "Send a message to the assistant",
            "operationId": "createChat",
            "tags": ["chat"],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["question"],
                "properties": {"question": {"type": "string"}}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"data": {"type": "object", "properties": {
                    "reply": {"type": "string"}}}}}}}}},
        }},
    },
}


def _dig(obj, *paths):
    """First present dot-path value in a parsed body, as a string ('' if none)."""
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and isinstance(cur, (str, int, float)):
            return str(cur)
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the console quiet
        pass

    # -- plumbing ---------------------------------------------------------- #
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _chunks(self, lines, ctype):
        """Stream text back a line at a time, the way a real SSE/NDJSON server does."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for line in lines:
            blob = line.encode("utf-8")
            self.wfile.write(b"%x\r\n%s\r\n" % (len(blob), blob))
            self.wfile.flush()
            time.sleep(0.01)
        self.wfile.write(b"0\r\n\r\n")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}"), raw.decode("utf-8", "replace")
        except ValueError:
            return {}, raw.decode("utf-8", "replace")

    def _mode(self):
        """Whole-host failure simulation via `X-Zoo-Mode`.

        Path-scoped failures (/broken/*, /secure/*) can never produce a *pure*
        diagnosis, because the prober also sweeps origin-level paths that answer
        404. A header applies the failure to every path at once, which is what a
        genuinely sick host looks like.
        """
        mode = (self.headers.get("X-Zoo-Mode") or "").strip().lower()
        if mode == "broken":
            self._json({"error": "internal error"}, 500)
            return True
        if mode == "unauthorized":
            self._json({"error": "unauthorized"}, 401)
            return True
        if mode == "throttled":
            self._json({"error": "too many requests"}, 429)
            return True
        return False

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        if self._mode():
            return
        parts = urlparse(self.path)
        path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)

        if path == "/openapi.json":
            return self._json(OPENAPI)
        if path == "/health":
            return self._json({"status": "ok"})
        if path == "/getapi/ask":
            prompt = (query.get("q") or query.get("message") or query.get("prompt") or [""])[0]
            if not prompt:
                return self._json({"error": "query parameter 'q' is required"}, 400)
            return self._json({"answer": ANSWER})
        if path == "/":
            return self._send(200, "<!doctype html><html><body>API Zoo</body></html>",
                              "text/html")
        return self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        if self._mode():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        body, raw = self._body()

        # 1. OpenAI-compatible, two segments below the base URL the operator has.
        if path == "/openai/v1/chat/completions":
            if not body.get("messages"):
                return self._json({"error": {"message": "'messages' is a required property",
                                             "type": "invalid_request_error"}}, 400)
            return self._json({
                "id": "chatcmpl-zoo", "object": "chat.completion", "model": "zoo-1",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": ANSWER}}],
                "usage": {"total_tokens": 42},
            })

        # 2. Declares its own contract in the rejection (FastAPI style).
        if path == "/support/api/chat":
            if "question" not in body:
                return self._json({"detail": [{"loc": ["body", "question"],
                                               "msg": "field required",
                                               "type": "value_error.missing"}]}, 422)
            return self._json({"status": "ok", "request_id": "req-000000000001",
                               "data": {"reply": ANSWER, "conversation_id": "conv-1"}})

        # 3. Streaming transports.
        if path == "/stream/chat":
            words = ANSWER.split(" ")
            frames = [f'data: {json.dumps({"delta": w + " "})}\n\n' for w in words]
            return self._chunks(frames + ["data: [DONE]\n\n"], "text/event-stream")
        if path == "/ndjson/chat":
            words = ANSWER.split(" ")
            frames = [json.dumps({"text": w + " "}) + "\n" for w in words]
            return self._chunks(frames + [json.dumps({"type": "done"}) + "\n"],
                                "application/x-ndjson")
        if path == "/plain/chat":
            return self._send(200, ANSWER, "text/plain")

        # 4. Traps — all HTTP 200, none of them an answer.
        if path == "/traps/echo":
            return self._json({"reply": _dig(body, "message", "prompt", "text", "query")})
        if path == "/traps/empty":
            return self._send(200, b"", "application/json")
        if path == "/traps/envelope":
            return self._json({"error": {"code": "overloaded",
                                         "message": "the assistant is busy, try again"}})
        if path == "/traps/html":
            return self._send(200, "<!doctype html><html><body>Sign in</body></html>",
                              "text/html")
        if path == "/traps/status":
            return self._json({"status": "accepted", "id": "b1946ac92492d2347c6235b4d2611184",
                               "created": "2026-01-01T00:00:00Z"})

        # 5. Failure modes.
        if path.startswith("/secure/"):
            if self.headers.get("X-Api-Key") != API_KEY:
                return self._json({"error": "unauthorized"}, 401)
            return self._json({"answer": ANSWER})
        if path.startswith("/throttled/"):
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "30")
            payload = json.dumps({"error": "too many requests"}).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if path.startswith("/broken/"):
            return self._json({"error": "internal error"}, 500)

        return self._json({"detail": "Not Found"}, 404)

    # curl commands copied from a browser often start with an OPTIONS preflight
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")


def serve(port):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


# --------------------------------------------------------------------------- #
# --selftest: drive the real prober against this server and print the result
# --------------------------------------------------------------------------- #
def _selftest(port):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for p in (root, root / "runtime"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from discovery.probe import probe_api, build_config  # noqa: E402

    base = f"http://127.0.0.1:{port}"
    checks = []

    class _Crash:
        """Stand-in when probe_api RAISED. It promises never to raise for a
        target-side problem, so a crash is itself a finding — the harness reports
        it instead of dying on the first one."""

        def __init__(self, exc):
            self.ok = False
            self.diagnosis = f"CRASHED {type(exc).__name__}: {exc}"
            self.endpoint = self.response_path = self.transport = None
            self.shape_label = self.method = None

    def probe(*a, **kw):
        try:
            return probe_api(*a, **kw)
        except Exception as exc:  # noqa: BLE001 - a crash is the thing we want to see
            return _Crash(exc)

    def check(name, cond, detail=""):
        crashed = str(detail).startswith("CRASHED")
        checks.append((name, bool(cond) and not crashed, detail))

    r = probe(f"{base}/openai", rate_limit_s=0)
    check("parent-url path discovery", r.ok and r.endpoint.endswith("/v1/chat/completions"),
          f"{r.diagnosis} {r.endpoint}")
    check("openai response_path", r.response_path == "choices.0.message.content",
          str(r.response_path))
    try:
        check("build_config", build_config(r)["adapter"] == "direct_api")
    except Exception as exc:  # noqa: BLE001
        check("build_config", False, f"CRASHED {type(exc).__name__}: {exc}")

    r = probe(f"{base}/support/api/chat", rate_limit_s=0)
    check("learns shape from a 422", r.ok and r.shape_label == "error_hint:question",
          f"{r.diagnosis} {r.shape_label}")
    check("nested response_path", r.response_path == "data.reply", str(r.response_path))

    for name, transport in (("stream", "sse"), ("ndjson", "ndjson"), ("plain", "text")):
        r = probe(f"{base}/{name}", rate_limit_s=0)
        check(f"{name} transport", r.ok and r.transport == transport,
              f"{r.diagnosis} {r.transport}")

    r = probe(f"{base}/getapi", method="GET", rate_limit_s=0, max_attempts=60)
    check("GET query API", r.ok and r.method == "GET", r.diagnosis)

    for trap in ("echo", "empty", "envelope", "html", "status"):
        r = probe(f"{base}/traps/{trap}", rate_limit_s=0, max_attempts=8)
        check(f"trap:{trap} rejected", not r.ok, r.diagnosis)

    for mode, expected in (("unauthorized", "auth_required"), ("throttled", "rate_limited"),
                           ("broken", "server_error")):
        r = probe(f"{base}/chat", rate_limit_s=0, max_attempts=10,
                  headers={"X-Zoo-Mode": mode})
        check(f"diagnosis:{mode}", r.diagnosis == expected, r.diagnosis)
    r = probe(f"{base}/secure/chat", rate_limit_s=0, max_attempts=10)
    check("diagnosis:secure", r.diagnosis == "auth_required", r.diagnosis)
    r = probe(f"{base}/nothing/chat", rate_limit_s=0, max_attempts=10)
    check("diagnosis:nothing", r.diagnosis == "not_found", r.diagnosis)

    r = probe(f"{base}/secure/chat", rate_limit_s=0, headers={"X-Api-Key": API_KEY})
    check("auth header unlocks it", r.ok, r.diagnosis)

    from discovery.importers import discover_spec, configs_from_spec  # noqa: E402
    spec = discover_spec(base)
    check("discover_spec finds the doc", spec["ok"], str(spec["error"]))
    if spec["ok"]:
        cfgs = configs_from_spec(base, spec["spec"])
        check("spec -> config with {{PROMPT}}",
              cfgs and "{{PROMPT}}" in json.dumps(cfgs[0]["body"]))

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    httpd = serve(port)
    if "--selftest" in sys.argv:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.2)
        try:
            code = _selftest(port)
        finally:
            httpd.shutdown()
        return code
    print(f"API Zoo listening on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
