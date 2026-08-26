"""test_tls_mtls — the shared TLS/mTLS kwargs used by requests-based adapters and map's
--insecure/--ca-bundle/--client-cert/--client-key flags."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from adapters.base import tls_kwargs


def test_default_verifies():
    assert tls_kwargs({}) == {"verify": True}

def test_insecure():
    assert tls_kwargs({"verify_tls": False}) == {"verify": False}

def test_ca_bundle_overrides_verify():
    assert tls_kwargs({"ca_bundle": "/tmp/ca.pem"})["verify"] == "/tmp/ca.pem"

def test_mtls_pair():
    assert tls_kwargs({"client_cert": "/c.pem", "client_key": "/k.pem"})["cert"] == ("/c.pem", "/k.pem")

def test_mtls_combined_pem():
    assert tls_kwargs({"client_cert": "/combined.pem"})["cert"] == "/combined.pem"
