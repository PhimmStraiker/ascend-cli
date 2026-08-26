"""Adapter-build permutation battery. Tests whether the adapter framework can
derive the correct CONTRACT and send a benign prompt across many API formats.
Runs from /tmp; imports the shipped runtime; uses INLINE configs (nothing written
to the repo). No customer data — synthetic 'agent zoo' + reachable public bots."""
import sys, json, os
REPO=os.path.abspath(os.path.join(os.path.dirname(__file__),"..",".."))
sys.path.insert(0, os.path.join(REPO,"runtime"))
from call_target import TargetCaller

Z="http://127.0.0.1:8800"
BENIGN="Hello, what can you help me with today?"
EXPECT="You said:"   # the zoo echoes this; real bots won't, so we check non-empty instead

# (label, adapter, inline_config, expect_echo)
SCENARIOS=[
 ("01 OpenAI-compatible chat/completions","direct_api",{"endpoint":f"{Z}/openai/v1/chat/completions","method":"POST","body":{"model":"x","messages":[{"role":"user","content":"{{PROMPT}}"}]},"response_path":"choices.0.message.content"},True),
 ("02 Anthropic-style messages","direct_api",{"endpoint":f"{Z}/anthropic/v1/messages","method":"POST","body":{"messages":[{"role":"user","content":"{{PROMPT}}"}]},"response_path":"content.0.text"},True),
 ("03 simple {reply}","direct_api",{"endpoint":f"{Z}/simple","body":{"prompt":"{{PROMPT}}"},"response_path":"reply"},True),
 ("04 deeply-nested response path","direct_api",{"endpoint":f"{Z}/nested","body":{"query":"{{PROMPT}}"},"response_path":"data.result.output.answer"},True),
 ("05 response is an array (last msg)","direct_api",{"endpoint":f"{Z}/array","body":{"prompt":"{{PROMPT}}"},"response_path":"messages.1.text"},True),
 ("06 plain-text response (not JSON)","direct_api",{"endpoint":f"{Z}/plaintext","body":{"prompt":"{{PROMPT}}"}},True),
 ("07 bearer-auth header","direct_api",{"endpoint":f"{Z}/bearer","headers":{"Authorization":"Bearer zoo-secret-token"},"body":{"prompt":"{{PROMPT}}"},"response_path":"response"},True),
 ("08 api-key header","direct_api",{"endpoint":f"{Z}/apikey-header","headers":{"X-Api-Key":"zoo-header-key"},"body":{"prompt":"{{PROMPT}}"},"response_path":"response"},True),
 ("09 form-encoded request","direct_api",{"endpoint":f"{Z}/form","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":{"prompt":"{{PROMPT}}"},"response_path":"response"},True),
 ("10 SSE token stream","sse_stream",{"base_url":Z,"chat_path":"/sse","request_template":{"prompt":"{{PROMPT}}"},"token_types":["token"],"text_path":"content","done_when":{"path":"type","equals":"done"}},True),
 ("11 NDJSON stream","sse_stream",{"base_url":Z,"chat_path":"/ndjson","format":"ndjson","request_template":{"prompt":"{{PROMPT}}"},"token_types":["token"],"text_path":"content","done_when":{"path":"type","equals":"done"}},True),
 ("12 multi-step session (id in URL)","session_api",{"session_endpoint":f"{Z}/conv/create","session_body":{},"session_extract":"conversation_id","session_variable":"SESSION_ID","message_endpoint":f"{Z}/conv/{{{{SESSION_ID}}}}/send","message_body":{"prompt":"{{PROMPT}}"},"response_path":"reply"},True),
 ("13 multi-step session (id in body)","session_api",{"session_endpoint":f"{Z}/session/create","session_body":{},"session_extract":"sessionId","session_variable":"SESSION_ID","message_endpoint":f"{Z}/session/send","message_body":{"prompt":"{{PROMPT}}","sessionId":"{{SESSION_ID}}"},"response_path":"data.text"},True),
 ("14 envelope status+nested","direct_api",{"endpoint":f"{Z}/envelope","body":{"prompt":"{{PROMPT}}"},"response_path":"payload.messages.0.content"},True),
 ("15 warmup/greeting-discard","session_api",{"session_endpoint":f"{Z}/session/create","session_body":{},"session_extract":"sessionId","session_variable":"SESSION_ID","message_endpoint":f"{Z}/warmup/message","message_body":{"prompt":"{{PROMPT}}","session_id":"{{SESSION_ID}}"},"response_path":"response","warmup_message":"hi"},True),
 ("16 GET + api-key in query","direct_api",{"endpoint":Z+"/query?api_key=zoo-query-key&q={{PROMPT}}","method":"GET","response_path":"answer"},True),
 ("17 async create->send->GET-poll","session_poll",{"create":{"url":Z+"/async/create","extract":"conversation_id"},"send":{"url":Z+"/async/{{CONV}}/send","body":{"message":"{{PROMPT}}"}},"poll":{"url":Z+"/async/{{CONV}}/messages","method":"GET","list_path":"messages","role_field":"role","bot_roles":["assistant"],"text_path":"text","interval_ms":500,"timeout_ms":15000}},True),
]

def probe_msg(t): return {"payload":{"body":{"prompt":t},"headers":{}}}

def run():
    npass=npartial=nfail=0; rows=[]
    for label,atype,cfg,expect_echo in SCENARIOS:
        try:
            tc=TargetCaller(atype,"inline",config=cfg,timeout_s=20)
            st,body=tc.handler(probe_msg(BENIGN)); tc.reset()
            resp=str(body.get("response",""))
            if st==200 and resp and (EXPECT in resp or not expect_echo):
                verdict="PASS"; npass+=1
            elif st==200 and resp:
                verdict="PARTIAL"; npartial+=1
            else:
                verdict="FAIL"; nfail+=1
            rows.append((verdict,label,st,resp[:64].replace("\n"," ")))
        except Exception as e:
            nfail+=1; rows.append(("FAIL",label,"exc",f"{type(e).__name__}: {e}"[:64]))
    for v,l,st,s in rows:
        print(f"  [{v:7}] {l:38} http={st} :: {s}")
    print(f"\n  synthetic formats: {npass} PASS, {npartial} PARTIAL, {nfail} FAIL (of {len(SCENARIOS)})")
    return npass,npartial,nfail

if __name__=="__main__":
    run()
