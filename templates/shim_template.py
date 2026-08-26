#!/usr/bin/env python3
"""
shim_template.py — wrap a bespoke target behind a synchronous /chat so the bridge can reach it.

Use this for the rare target that isn't plain HTTP request/response: an email-driven agent, a
two-channel / async workflow, a target that needs orchestration the config model can't express.
Hide all of that in here; the bridge just talks `direct_api` to http://127.0.0.1:8099/chat.

    python3 shim_template.py                       # starts on :8099
    ascend adapter validate --file shim.json       # points direct_api at the shim (below)

shim.json:
    {
      "adapter": "direct_api",
      "endpoint": "http://127.0.0.1:8099/chat",
      "method": "POST",
      "body": {"prompt": "{{PROMPT}}"},
      "response_path": "response",
      "timeout_ms": 1800000
    }

Keep the contract dead simple: POST {"prompt": "..."} -> 200 {"response": "..."}.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def handle_prompt(prompt: str) -> str:
    """YOUR bespoke logic goes here. Whatever it takes to get one prompt to the agent and one
    reply back — create a ticket and poll it, send an email and watch an inbox, drive a device.
    Return the agent's reply as a plain string. Take as long as you need (set a big timeout_ms)."""
    # --- replace this stub -------------------------------------------------
    return f"echo: {prompt}"
    # -----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/chat":
            return self._json(404, {"error": f"not found: {self.path}"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": f"bad json: {e}"})
        prompt = data.get("prompt") or data.get("message") or ""
        if not prompt:
            return self._json(400, {"error": "missing 'prompt'"})
        try:
            reply = handle_prompt(prompt)
        except Exception as e:  # never crash the shim on one bad turn
            return self._json(502, {"error": f"target error: {e}"})
        self._json(200, {"response": reply})

    def do_GET(self):
        self._json(200, {"ok": True}) if self.path == "/" else self._json(404, {})

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    print(f"shim listening on http://127.0.0.1:{args.port}/chat")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
