#!/usr/bin/env python3
"""
test_fixtures.py — one command that stands up a local target for every pattern the bridge
claims to cover, so you can test `ascend` end-to-end with no customer system involved.

    python3 scripts/test_fixtures.py          # runs until Ctrl-C

Ports:
  8790  REST echo                 POST /chat        {"message"} -> {"response"}
  8791  SSE (data: JSON frames)   POST /chat        token/done frames
  8792  SSE (NAMED events)        POST /chat        event: status|done, answer in `done`
  8793  SSE (raw plaintext)       POST /chat        bare text chunks
  8794  auth-gated (bearer)       POST /chat        401 + WWW-Authenticate w/o Bearer
                                  POST /login      {"code":"1234"} -> {"token": ...}
  8795  async POST-then-GET       POST /chat        202 {"conversationId": ...}
                                  GET  /history    the reply, after the POST
  8796  nested-JSON response      POST /chat        {"status":{"data":{"answer": ...}}}
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "testtoken-xyz"
LOGIN_CODE = "1234"
_convos = {}


class Base(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        b = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _stream(self, chunks, ctype="text/event-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        for c in chunks:
            self.wfile.write(c.encode() if isinstance(c, str) else c)
            self.wfile.flush()
            time.sleep(0.02)

    def _prompt(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            d = {}
        return d, (d.get("message") or d.get("prompt") or d.get("input") or "")

    def do_GET(self):
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass


class Echo(Base):
    def do_POST(self):
        _, p = self._prompt()
        self._send(200, {"response": f"DemoBot: you said {p}"})


class SSEJson(Base):
    def do_POST(self):
        _, p = self._prompt()
        frames = [f'data: {json.dumps({"type": "token", "content": w + " "})}\n\n'
                  for w in ("Streaming", "reply", "to:", p)]
        frames += ['data: {"type":"status","content":"thinking"}\n\n', 'data: {"type":"done"}\n\n']
        self._stream(frames)


class SSENamed(Base):
    def do_POST(self):
        _, p = self._prompt()
        self._stream(['event: status\ndata: {"state":"working"}\n\n',
                      f'event: done\ndata: {json.dumps({"answer": f"NamedBot: {p}"})}\n\n'])


class SSEPlain(Base):
    def do_POST(self):
        _, p = self._prompt()
        self._stream([f"PlainBot: ", "you ", "said ", p, "\n[DONE]\n"], ctype="text/plain")


class AuthGated(Base):
    def do_POST(self):
        if self.path == "/login":
            d, _ = self._prompt()
            if str(d.get("code")) == LOGIN_CODE:
                return self._send(200, {"token": TOKEN})
            return self._send(403, {"error": "bad code"})
        if self.path != "/chat":
            return self._send(404, {"error": "not found"})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "unauthorized"},
                              extra={"WWW-Authenticate": 'Bearer realm="agent"'})
        _, p = self._prompt()
        self._send(200, {"response": f"AuthBot: you said {p}"})


class AsyncPoll(Base):
    def do_POST(self):
        _, p = self._prompt()
        cid = f"conv-{len(_convos) + 1}"
        _convos[cid] = p
        self._send(202, {"conversationId": cid, "status": "queued"})

    def do_GET(self):
        if self.path.startswith("/history"):
            cid = self.path.split("conversation_id=")[-1] if "conversation_id=" in self.path else None
            p = _convos.get(cid, next(iter(_convos.values()), ""))
            return self._send(200, {"messages": [
                {"role": "user", "text": p},
                {"role": "assistant", "text": f"AsyncBot: you said {p}"}]})
        self._send(200, {"ok": True})


class Nested(Base):
    def do_POST(self):
        _, p = self._prompt()
        self._send(200, {"status": {"data": {"answer": f"NestedBot: you said {p}"}}})


SERVERS = [(8790, Echo, "REST echo"), (8791, SSEJson, "SSE data: JSON"),
           (8792, SSENamed, "SSE named events"), (8793, SSEPlain, "SSE plaintext"),
           (8794, AuthGated, "auth-gated (bearer + /login)"),
           (8795, AsyncPoll, "async POST-then-GET"), (8796, Nested, "nested JSON")]


def main():
    threads = []
    for port, handler, label in SERVERS:
        try:
            srv = HTTPServer(("127.0.0.1", port), handler)
        except OSError as e:
            print(f"  :{port:<5} SKIP  {label} ({e})")
            continue
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        threads.append(t)
        print(f"  :{port:<5} up    {label}")
    if not threads:
        print("no fixtures started (ports busy?)")
        return 1
    print("\nfixtures ready — Ctrl-C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
