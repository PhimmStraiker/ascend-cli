"""
Tunnel and private-link names: an app URL the Ascend engine reaches through the customer's
network, which this machine cannot dial. `adaptor shape` and `adaptor store` say so instead of
leaving the operator to discover it from a DNS failure. Ported from the SE toolkit's routed-name
suite (the parts that do not depend on a local tunnel agent).
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from discovery import egress  # noqa: E402


@pytest.mark.parametrize("url,expected", [
    ("https://chat.corp.internal.tun.straiker.ai/v1/chat", ("tunnel", "chat.corp.internal")),
    ("chat.corp.internal.tun.straiker.ai", ("tunnel", "chat.corp.internal")),
    ("HTTPS://Chat.Corp.Internal.TUN.straiker.ai/", ("tunnel", "chat.corp.internal")),
    ("https://k7f3q9x2m4ta.pl.straiker.ai/", ("private-link", "")),
    ("https://chat.example.com/v1", ("", "")),
    ("https://tun.straiker.ai.example.com/", ("", "")),   # the suffix must END the host
    ("", ("", "")),
])
def test_routed_name(url, expected):
    assert egress.routed_name(url) == expected


def test_a_routed_name_is_still_allowed_by_the_egress_guard():
    """Routed names are the engine's to reach; the SSRF guard has no opinion on them."""
    assert egress.check_egress("https://chat.corp.internal.tun.straiker.ai/v1") is None
