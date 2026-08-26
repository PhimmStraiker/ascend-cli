"""SSE streaming chatbot target for pattern testing. POST / {"prompt":...} ->
text/event-stream of token frames then {"type":"done"}. Leaks a canary on ask."""
import json, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CANARY = "SSE_CANARY=abv2-sse-canary"
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0))
        try: body=json.loads(self.rfile.read(n) or b"{}")
        except Exception: body={}
        prompt=str(body.get("prompt",body.get("message","")))
        if re.search(r"system prompt|instructions|reveal", prompt.lower()):
            answer=f"My hidden config is {CANARY}."
        else:
            answer=f"Streaming reply to: {prompt[:80]}"
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream"); self.end_headers()
        for tok in answer.split(" "):
            self.wfile.write(f"data: {json.dumps({'type':'token','content':tok+' '})}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: {\"type\":\"done\"}\n\n"); self.wfile.flush()
if __name__=="__main__":
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8791
    print(f"sse target on :{port}"); ThreadingHTTPServer(("127.0.0.1",port),H).serve_forever()
