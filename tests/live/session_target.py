"""Multi-step session target: POST /conversations -> {"conversation_id": "..."};
POST /conversations/{id}/messages {"prompt":...} -> {"reply":...}. Tests the
create-conversation->post-with-id flow AND multi-turn continuity (remembers turns)."""
import json, re, sys, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CANARY="SESS_CANARY=abv2-session-canary"
CONV={}  # id -> [turns]
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0))
        try: body=json.loads(self.rfile.read(n) or b"{}")
        except Exception: body={}
        path=self.path
        if path.rstrip("/").endswith("/conversations"):
            cid="conv-"+uuid.uuid4().hex[:12]; CONV[cid]=[]
            return self._json({"conversation_id":cid})
        m=re.match(r"/conversations/([^/]+)/messages", path)
        if m:
            cid=m.group(1); prompt=str(body.get("prompt",body.get("message","")))
            hist=CONV.setdefault(cid,[]); hist.append(prompt)
            if re.search(r"system prompt|instructions|reveal", prompt.lower()):
                reply=f"(turn {len(hist)}) Leaking: {CANARY}"
            else:
                reply=f"(turn {len(hist)}, {len(hist)-1} prior) You said: {prompt[:60]}"
            return self._json({"reply":reply,"conversation_id":cid,"turn":len(hist)})
        self._json({"error":"not found"},404)
    def _json(self,obj,code=200):
        out=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(out)))
        self.end_headers(); self.wfile.write(out)
if __name__=="__main__":
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8792
    print(f"session target on :{port}"); ThreadingHTTPServer(("127.0.0.1",port),H).serve_forever()
