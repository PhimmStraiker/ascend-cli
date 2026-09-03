"""
example-custom_module.py — the adapter contract, in full.

One function. Anything may happen inside it. This example shows the shape a real bespoke
adapter takes: a bootstrap call for a token, then the message call, then pulling the reply
out of a nested envelope.

    ascend target add --module ./my_adapter.py --name 'My Bot'
"""
import json
import urllib.request

META = {"target": "https://your-bot.example.com/chat", "kind": "custom",
        "generated_from": "example"}

BASE = "https://your-bot.example.com"


def _post(path: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def send_prompt(prompt: str) -> str:
    """Send ONE prompt and return the reply as text.

    Return the agent's WORDS only. A status line, a progress frame or a JSON envelope
    returned from here is what the scorer will read as the agent's answer — which is how a
    run comes back clean having measured nothing.
    """
    # 1. whatever bootstrap this target needs (token, session, nonce, signature...)
    token = _post("/api/session", {})["access_token"]

    # 2. the turn itself
    body = _post("/api/chat", {"message": prompt},
                 headers={"Authorization": f"Bearer {token}"})

    # 3. dig the reply out of wherever it actually lives
    return body["data"]["result"]["messages"][-1]["content"]
