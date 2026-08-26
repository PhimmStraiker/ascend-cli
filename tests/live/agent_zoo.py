"""Agent Zoo — one mock server exposing MANY distinct agent API contract shapes,
each on its own path, to stress the adapter-build capability across formats.
NO customer data — all synthetic. Run: python3 agent_zoo.py 8800"""
import json, re, sys, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CONV = {}

def reply_text(prompt):
    return f"You said: {prompt[:80]}"

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self, code, body_bytes, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body_bytes))); self.end_headers()
        self.wfile.write(body_bytes)
    def _json(self, obj, code=200): self._send(code, json.dumps(obj).encode())
    def _read(self):
        n=int(self.headers.get("Content-Length",0)); raw=self.rfile.read(n) if n else b""
        return raw
    def _prompt_json(self, raw, *keys):
        try: b=json.loads(raw or b"{}")
        except Exception: b={}
        # dig for the first present key path
        for k in keys:
            cur=b; ok=True
            for part in k.split("."):
                if isinstance(cur,dict) and part in cur: cur=cur[part]
                elif isinstance(cur,list) and part.isdigit() and int(part)<len(cur): cur=cur[int(part)]
                else: ok=False; break
            if ok and isinstance(cur,(str,int,float)): return str(cur), b
        return "", b

    def do_GET(self):
        path=urlparse(self.path).path; q=parse_qs(urlparse(self.path).query)
        # 14. query-param prompt + api-key in query
        if path=="/query":
            if q.get("api_key",[""])[0] != "zoo-query-key":
                return self._json({"error":"bad api_key"},401)
            p=q.get("q",[""])[0]
            return self._json({"answer": reply_text(p)})
        if re.match(r"/async/([^/]+)/messages", path):
            cid=re.match(r"/async/([^/]+)/messages", path).group(1); st=CONV.get(cid) or {"turns":[]}
            return self._json({"messages":st["turns"]})
        return self._json({"ok":True})

    def do_POST(self):
        path=urlparse(self.path).path; raw=self._read()
        H_ = self.headers

        # 1. OpenAI-compatible chat/completions
        if path=="/openai/v1/chat/completions":
            p,_=self._prompt_json(raw,"messages.0.content","messages.1.content","prompt")
            return self._json({"id":"cmpl-1","choices":[{"message":{"role":"assistant","content":reply_text(p)}}]})
        # 2. Anthropic-style messages
        if path=="/anthropic/v1/messages":
            p,_=self._prompt_json(raw,"messages.0.content","prompt")
            return self._json({"content":[{"type":"text","text":reply_text(p)}],"role":"assistant"})
        # 3. simple {reply}
        if path=="/simple":
            p,_=self._prompt_json(raw,"prompt","message","input"); return self._json({"reply":reply_text(p)})
        # 4. deeply nested response path
        if path=="/nested":
            p,_=self._prompt_json(raw,"query","prompt")
            return self._json({"data":{"result":{"output":{"answer":reply_text(p)}}}})
        # 5. response is an array
        if path=="/array":
            p,_=self._prompt_json(raw,"prompt")
            return self._json({"messages":[{"role":"user","text":p},{"role":"assistant","text":reply_text(p)}]})
        # 6. response is PLAIN TEXT (not json)
        if path=="/plaintext":
            p,_=self._prompt_json(raw,"prompt","message")
            return self._send(200, reply_text(p).encode(), "text/plain")
        # 7. bearer-auth required
        if path=="/bearer":
            if H_.get("Authorization","")!="Bearer zoo-secret-token":
                return self._json({"error":"unauthorized"},401)
            p,_=self._prompt_json(raw,"prompt"); return self._json({"response":reply_text(p)})
        # 8. api-key header required
        if path=="/apikey-header":
            if H_.get("X-Api-Key","")!="zoo-header-key":
                return self._json({"error":"forbidden"},403)
            p,_=self._prompt_json(raw,"prompt"); return self._json({"response":reply_text(p)})
        # 9. form-encoded request (not json)
        if path=="/form":
            from urllib.parse import parse_qs as pq
            d=pq(raw.decode("utf-8","ignore")); p=(d.get("prompt") or [""])[0]
            return self._json({"response":reply_text(p)})
        # 10. warmup/greeting: first call to a session returns a greeting to be discarded
        if path=="/warmup/message":
            p,b=self._prompt_json(raw,"prompt")
            sid=b.get("session_id","default"); CONV.setdefault(sid,0); CONV[sid]+=1
            if CONV[sid]==1: return self._json({"response":"👋 Hi! I'm your assistant. How can I help?"})
            return self._json({"response":reply_text(p)})
        # 11. SSE stream
        if path=="/sse":
            p,_=self._prompt_json(raw,"prompt","message")
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
            for tok in reply_text(p).split(" "):
                self.wfile.write(f"data: {json.dumps({'type':'token','content':tok+' '})}\n\n".encode()); self.wfile.flush()
            self.wfile.write(b"data: {\"type\":\"done\"}\n\n"); self.wfile.flush(); return
        # 12. NDJSON stream
        if path=="/ndjson":
            p,_=self._prompt_json(raw,"prompt","message")
            self.send_response(200); self.send_header("Content-Type","application/x-ndjson"); self.end_headers()
            for tok in reply_text(p).split(" "):
                self.wfile.write((json.dumps({"type":"token","content":tok+" "})+"\n").encode()); self.wfile.flush()
            self.wfile.write((json.dumps({"type":"done"})+"\n").encode()); self.wfile.flush(); return
        # 13. multi-step session: create -> post with id in URL
        if path=="/conv/create": 
            cid="c-"+uuid.uuid4().hex[:10]; return self._json({"conversation_id":cid})
        m=re.match(r"/conv/([^/]+)/send", path)
        if m:
            p,_=self._prompt_json(raw,"prompt"); return self._json({"reply":reply_text(p),"conversation_id":m.group(1)})
        # 15. multi-step session: id in BODY (not URL)
        if path=="/session/create":
            sid="s-"+uuid.uuid4().hex[:10]; return self._json({"sessionId":sid})
        if path=="/session/send":
            p,b=self._prompt_json(raw,"prompt"); return self._json({"data":{"text":reply_text(p)},"sessionId":b.get("sessionId")})
        # 16. envelope with status + nested content
        if path=="/envelope":
            p,_=self._prompt_json(raw,"prompt")
            return self._json({"status":"ok","payload":{"messages":[{"content":reply_text(p)}]}})
        # 17. ASYNC: create -> send returns ack -> reply only appears on later GET poll
        if path=="/async/create":
            cid="a-"+uuid.uuid4().hex[:10]; CONV[cid]={"turns":[],"pending":None}
            return self._json({"conversation_id":cid})
        m=re.match(r"/async/([^/]+)/send", path)
        if m:
            cid=m.group(1); p,_=self._prompt_json(raw,"message","prompt")
            st=CONV.get(cid) or {"turns":[],"pending":None}
            st["turns"].append({"role":"user","text":p})
            # schedule the bot reply to appear after ~1.5s (async processing)
            import threading,time as _t
            def _later(cid=cid,p=p,st=st):
                _t.sleep(1.5); st["turns"].append({"role":"assistant","text":reply_text(p)})
            threading.Thread(target=_later,daemon=True).start()
            CONV[cid]=st
            return self._json({"accepted":True,"conversation_id":cid})  # NO reply here
        m=re.match(r"/async/([^/]+)/messages", path)
        if m:  # POST poll (some poll via POST)
            cid=m.group(1); st=CONV.get(cid) or {"turns":[]}
            return self._json({"messages":st["turns"]})
        return self._json({"error":"no such path","path":path},404)

if __name__=="__main__":
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8800
    print(f"agent zoo on :{port}")
    ThreadingHTTPServer(("127.0.0.1",port),H).serve_forever()
