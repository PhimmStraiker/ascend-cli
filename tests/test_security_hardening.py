"""
Security hardening pins — from the pre-ship security review.

These guard against regressions in: config/module file permissions (customer auth is baked in),
reading an adapter's metadata WITHOUT executing it, and the SSRF metadata block list.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))


class TestPrivateWrites:
    def test_write_private_is_0600(self, tmp_path, monkeypatch):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cli", REPO / "shells" / "cli" / "ascend.py")
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        p = tmp_path / "cfg.json"
        cli._write_private(p, '{"adapter":"direct_api"}')
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o600, f"config written {oct(mode)}, must be 0600 (it can carry auth)"


class TestMetaReadWithoutExec:
    def test_load_meta_does_not_execute_the_module(self, tmp_path):
        from adapters.custom_module import load_meta
        m = tmp_path / "evil.py"
        # top-level code that would leave a marker IF the module were imported
        marker = tmp_path / "RAN"
        m.write_text(f'META={{"target":"x"}}\nopen({str(marker)!r},"w").write("ran")\n'
                     'def send_prompt(p):\n    return p\n')
        meta = load_meta({"adapter_module": str(m)})
        assert meta == {"target": "x"}
        assert not marker.exists(), "load_meta executed the module — it must parse statically"


class TestSSRFMetadataBlock:
    def test_link_local_and_metadata_ips_blocked(self):
        from discovery.egress import check_egress
        for u in ("http://169.254.169.254/latest/meta-data/",
                  "http://100.100.100.200/",
                  "http://metadata.google.internal/"):
            assert check_egress(u), f"{u} should be blocked"

    def test_internal_and_external_allowed(self):
        from discovery.egress import check_egress
        assert check_egress("http://127.0.0.1:8600/chat") is None
        assert check_egress("https://api.example.com/chat") is None

    def test_allow_internal_opts_out(self):
        from discovery.egress import check_egress
        assert check_egress("http://169.254.169.254/", allow_internal=True) is None


class TestStateDirNamespacing:
    def test_override_still_namespaces_by_tenant(self, tmp_path, monkeypatch):
        import tenant
        monkeypatch.setenv("ASCEND_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(tenant, "_pinned_fingerprint", lambda: "abcdef0123456789ff")
        root = tenant.state_root()
        assert root != tmp_path, "override must still namespace by fingerprint, not share one dir"
        assert "abcdef0123456789"[:16] in str(root)
