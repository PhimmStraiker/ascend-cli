"""test_command_map — the visual command map is GENERATED from the CLI parser, so it can never
drift from the tool. This fails if someone changes a command without regenerating."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_command_map_is_not_stale():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "gen_command_map.py"), "--check"],
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, (
        "docs/COMMAND_MAP.md / docs/command-map.html are stale — run:\n"
        "  python3 scripts/gen_command_map.py\n" + r.stderr)


def test_map_covers_the_new_fleet_groups():
    md = (REPO / "docs" / "COMMAND_MAP.md").read_text()
    for group in ("bridge", "keys", "tenant", "map", "reports", "status"):
        assert f"ascend {group}" in md, f"{group} missing from the generated map"
