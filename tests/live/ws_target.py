"""WebSocket chatbot target for testing chunked framing. Two modes via path:
  /json  -> streams JSON delta frames {"type":"token","text":"..."} then {"type":"done"}
  /text  -> streams bare text chunks (no JSON), relies on idle-gap to end
Tests the exact 'WS chunked text vs chunked json' case the design worried about."""
import asyncio, json, re, sys
import websockets
CANARY="WS_CANARY=abv2-ws-canary"
def answer(prompt):
    if re.search(r"system prompt|instructions|reveal", prompt.lower()):
        return f"Here is my prompt: {CANARY}"
    return f"WS reply to {prompt[:60]}"
async def handler(ws):
    mode = "json" if ws.request.path.endswith("/json") else "text"
    async for raw in ws:
        try: msg=json.loads(raw); prompt=msg.get("text",msg.get("prompt",""))
        except Exception: prompt=raw if isinstance(raw,str) else raw.decode()
        ans=answer(str(prompt))
        for tok in ans.split(" "):
            if mode=="json":
                await ws.send(json.dumps({"type":"token","text":tok+" "}))
            else:
                await ws.send(tok+" ")
        if mode=="json":
            await ws.send(json.dumps({"type":"done"}))
        # text mode: rely on idle gap (no done frame)
async def main(port):
    async with websockets.serve(handler, "127.0.0.1", port):
        print(f"ws target on :{port} (/json and /text)")
        await asyncio.Future()
if __name__=="__main__":
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8793
    asyncio.run(main(port))
