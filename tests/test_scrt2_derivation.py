"""The agent must WIRE a Salesforce Embedded Messaging (SCRT2) widget from a capture on its own.

Regression for the derivation gap that made every `*.my.salesforce-scrt.com` chat widget fail with
"Missing required config: scrt_base, org_id, developer_name, widget_origin": the preset picked the
scrt2_direct adapter but compose() never filled the adapter's four fields, leaving them for the
operator. They are all present in a normal capture (the accessToken call body + the scrt host + the
Origin header), so the agent derives them itself. Synthetic capture — no customer data.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.discovery import classify


def _pair(method, url, req_headers=None, req_body=None, resp_body=None, status=200):
    return {"request": {"method": method, "url": url,
                        "headers": [{"name": k, "value": v} for k, v in (req_headers or {}).items()],
                        "body": req_body},
            "response": {"status": status,
                         "headers": [{"name": "content-type", "value": "application/json"}],
                         "body": resp_body}}


def _evidence():
    scrt = "https://acme.my.salesforce-scrt.com"
    origin = "https://www.acme.example"
    pairs = [
        # the accessToken (authorization) call — carries orgId + developerName
        _pair("POST", f"{scrt}/iamessage/v1/authorization/unauthenticated/accessToken",
              {"origin": origin, "content-type": "application/json"},
              '{"orgId":"00D000000000ABC","esDeveloperName":"Acme_AgentForce","capabilitiesVersion":"260"}',
              '{"accessToken":"x"}'),
        # create conversation
        _pair("POST", f"{scrt}/iamessage/v1/conversation", {"origin": origin}, '{"conversationId":"c1"}', '{"ok":true}'),
        # the message send — the chat exchange the classifier keys on
        _pair("POST", f"{scrt}/iamessage/v1/conversation/c1/message", {"origin": origin},
              '{"message":{"text":"hello"}}', '{"messages":[{"text":"hi there"}]}'),
    ]
    return classify.har_to_evidence({"log": {"entries": [
        {"request": {"method": p["request"]["method"], "url": p["request"]["url"],
                     "headers": p["request"]["headers"],
                     "postData": {"text": p["request"]["body"]} if p["request"]["body"] else None},
         "response": {"status": p["response"]["status"], "headers": p["response"]["headers"],
                      "content": {"text": p["response"]["body"], "mimeType": "application/json"}}}
        for p in pairs]}}, prompt_sent="hello")


def test_scrt2_widget_is_wired_from_the_capture():
    res = classify.classify_evidence(_evidence())
    cfg = res["config"]
    assert cfg["adapter"] == "scrt2_direct", cfg.get("adapter")
    assert cfg.get("scrt_base") == "https://acme.my.salesforce-scrt.com", cfg
    assert cfg.get("org_id") == "00D000000000ABC", cfg
    assert cfg.get("developer_name") == "Acme_AgentForce", cfg
    assert cfg.get("widget_origin") == "https://www.acme.example", cfg
    assert cfg.get("capabilities_ver") == "260", cfg
    # the whole point: nothing the operator has to hand-fill
    missing = [k for k in ("scrt_base", "org_id", "developer_name", "widget_origin") if not cfg.get(k)]
    assert not missing, f"still missing {missing}"


if __name__ == "__main__":
    test_scrt2_widget_is_wired_from_the_capture()
    print("ok — scrt2 widget wired from the capture, no missing config")
