"""test_egress_ssrf — the SSRF guard that respects enterprise topology: allow localhost +
RFC-1918 (internal targets are legitimate), block link-local / cloud-metadata unless
--allow-internal."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from discovery.egress import check_egress


def test_blocks_aws_metadata_ip():
    assert check_egress("http://169.254.169.254/latest/meta-data/") is not None

def test_blocks_gcp_metadata_host():
    assert check_egress("http://metadata.google.internal/computeMetadata/v1/") is not None

def test_allows_localhost():
    assert check_egress("http://127.0.0.1:8790/chat") is None

def test_allows_rfc1918_internal():
    assert check_egress("http://10.1.2.3/api/chat") is None
    assert check_egress("http://192.168.1.10/chat") is None

def test_allows_public():
    assert check_egress("https://api.anthropic.com/v1/messages") is None

def test_allow_internal_override():
    assert check_egress("http://169.254.169.254/x", allow_internal=True) is None
