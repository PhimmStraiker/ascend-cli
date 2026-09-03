"""
test_flow_diagram.py — the block diagram is measured, never hand-counted.

The diagram this replaces drew TWO boxes, "YOUR MACHINE" and "STRAIKER CLOUD", with the target
buried inside the left one as a leaf. That said the target is part of your machine, and it left
the adapter out of the picture entirely — so the one thing readers most needed (an adapter is
built per target, from the protocol that target actually speaks) was carried by prose alone, which
had already failed at it repeatedly.

The replacement is three columns naming where things RUN — straiker cloud, your machine, your
target — with the CLI as a box inside your machine. Inside it, position carries meaning: `bridge`
is the left strip because it faces Straiker, `adapter` is the right strip because it faces the
target. Every edge carries its transport.

WHY THESE TESTS ARE ABOUT WIDTH.

The strips make every row's width interdependent: left wall, strip, gutter, command field, strip,
right wall. `f"{s:11}"` counts escape BYTES, not cells, so the moment colour is applied a
hand-padded row under-pads and the box goes ragged — invisible to anyone developing with colour
off. This exact bug shipped once already on the far simpler box being replaced, and the first
draft of THIS function reproduced it within minutes: body rows came out two cells wider than the
border, because the body and the border computed the interior separately. There is now one
interior builder and these tests measure the result in all three render modes.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("shells/cli", "runtime", "control"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402
import ui      # noqa: E402

SRC = (REPO / "shells" / "cli" / "ascend.py").read_text()


def _body(lines):
    """Every line but the heading row, which legitimately carries no trailing padding."""
    return lines[1:]


class TestTheBoxIsSquareInEveryRenderMode:
    @pytest.mark.parametrize("color", [True, False])
    def test_all_rows_are_one_width(self, color):
        w = {ui.vwidth(l) for l in _body(ascend._flow_diagram(sys.stdout, color=color))}
        assert len(w) == 1, f"ragged with color={color}: {sorted(w)}"

    def test_colour_does_not_change_the_measured_width(self):
        """The regression: padding on escape BYTES silently shrinks a coloured row."""
        plain = {ui.vwidth(l) for l in _body(ascend._flow_diagram(sys.stdout, color=False))}
        tinted = {ui.vwidth(l) for l in _body(ascend._flow_diagram(sys.stdout, color=True))}
        assert plain == tinted

    def test_the_ascii_branch_is_square_too(self):
        real = ui.unicode_ok
        ui.unicode_ok = lambda s: False
        try:
            lines = ascend._flow_diagram(sys.stdout, color=False)
        finally:
            ui.unicode_ok = real
        assert len({ui.vwidth(l) for l in _body(lines)}) == 1
        assert "┌" not in "".join(lines), "the ASCII branch still emitted a box-drawing glyph"

    def test_it_fits_a_conventional_terminal(self):
        w = max(ui.vwidth(l) for l in ascend._flow_diagram(sys.stdout, color=False))
        assert w <= 100, f"{w} cells will wrap on a standard terminal and wrapping destroys it"


class TestPositionCarriesTheMeaning:
    """If the strips are not on the correct edges, the diagram says the wrong thing."""

    @pytest.fixture
    def plain(self):
        return ascend._flow_diagram(sys.stdout, color=False)

    def test_bridge_reads_vertically_on_the_straiker_side(self, plain):
        body = [l for l in plain if "│b│" in l or "│r│" in l or "│i│" in l]
        assert len(body) >= 3, "the bridge strip is not rendered one character per row"

    def test_the_strips_spell_their_words_top_to_bottom(self, plain):
        # Column-read the two strips out of the rendered rows.
        # Read the two strips by COLUMN, locating them from the row that opens them. Matching
        # "│x│" with a regex does not work here: findall is non-overlapping, so in "│ │b│" the
        # first match consumes the strip's own opening bar and the letter is never seen.
        top = next(i for i, l in enumerate(plain) if l.count("┌─┐") == 2)
        c_left = plain[top].index("┌─┐") + 1
        c_right = plain[top].rindex("┌─┐") + 1
        rows = plain[top + 1:next(i for i, l in enumerate(plain) if l.count("└─┘") == 2)]
        left = "".join(l[c_left] for l in rows)
        right = "".join(l[c_right] for l in rows)
        assert left.strip() == "bridge", f"left strip reads {left.strip()!r}"
        assert right.strip() == "adapter", f"right strip reads {right.strip()!r}"

    def test_the_three_columns_name_where_things_run(self, plain):
        head = plain[0]
        assert head.index("straiker cloud") < head.index("your machine") < head.index("your target")

    def test_the_middle_box_is_the_cli_not_a_target(self, plain):
        """It used to be labelled as though the CLI and the machine were the same box."""
        assert any("ascend cli" in l for l in plain)
        assert not any("one target" in l for l in plain)

    def test_the_target_is_its_own_column(self, plain):
        """The old diagram nested the target inside 'YOUR MACHINE', which is not where it lives."""
        assert any("your agent" in l for l in plain)

    def test_every_edge_carries_a_transport(self, plain):
        joined = "\n".join(plain)
        assert "https" in joined, "the straiker edge is unlabelled"
        assert "native" in joined, "the target edge is unlabelled"

    def test_the_straiker_edge_is_not_labelled_as_a_socket(self, plain):
        """The first draft said WSS. It is wrong, and it would have shipped to three places.

        `runtime/lease_client.py` long-polls `/v2/lease` and POSTs `/v2/result` with urllib
        against an https base URL — there is no socket on this edge. The project DOES depend on
        `websockets`, which is what makes the mistake easy: that dependency belongs to the
        websocket ADAPTER and the discovery probe, both of which live on the target edge.

        A diagram is believed more readily than prose, and this one is printed in the terminal,
        embedded in the docs, and recorded in the tour video — so a wrong label here is a wrong
        claim in three places at once, and the docs page already said "plain HTTPS" beside it.
        """
        joined = "\n".join(plain).lower()
        assert "wss" not in joined, (
            "the bridge edge is labelled as a websocket; it is an https long-poll "
            "(see runtime/lease_client.py — urllib, /v2/lease and /v2/result)")

    def test_the_strips_start_and_end_on_the_same_rows(self, plain):
        """Unequal strips (bridge is 6, adapter is 7) make the box read crooked."""
        tops = [i for i, l in enumerate(plain) if l.count("┌─┐") == 2]
        bots = [i for i, l in enumerate(plain) if l.count("└─┘") == 2]
        assert len(tops) == 1 and len(bots) == 1, "the two strips are not aligned as one band"


class TestItDegradesInsteadOfWrapping:
    def test_a_narrow_terminal_gets_the_stacked_form(self):
        narrow = ascend._flow_diagram_narrow(sys.stdout, color=False)
        assert max(ui.vwidth(l) for l in narrow) <= 70

    def test_the_narrow_form_still_says_which_edge_each_gut_faces(self):
        joined = "\n".join(ascend._flow_diagram_narrow(sys.stdout, color=False))
        assert "bridge" in joined and "adapter" in joined and "https" in joined

    def test_the_narrow_form_is_not_labelled_as_a_socket_either(self):
        """Both layouts state the same transport, or one of them is lying."""
        assert "wss" not in "\n".join(
            ascend._flow_diagram_narrow(sys.stdout, color=False)).lower()

    def test_the_chooser_measures_rather_than_assuming(self):
        m = re.search(r"^def _print_flow\(.*?(?=^def )", SRC, re.S | re.M)
        assert m and "vwidth" in m.group(0) and "term_width" in m.group(0), (
            "the layout choice must be measured; a hardcoded threshold goes stale the moment "
            "a label changes")


class TestItIsShownWherePeopleLook:
    def test_the_launch_screen_uses_the_shared_builder(self):
        m = re.search(r"^def _launch_screen\(\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert m and "_print_flow(" in m.group(1)

    def test_top_level_help_prints_it_too(self):
        m = re.search(r"^def main\(.*?(?=^def )", SRC, re.S | re.M)
        assert m and "_print_flow(" in m.group(0), (
            "`ascend --help` is the other entry point people hit first and must teach the same "
            "picture")

    def test_help_stays_tty_gated(self):
        """A piped `ascend --help` must be byte-identical, or the golden corpus and every
        `ascend --help | grep` in a customer runbook break at once."""
        m = re.search(r"^def main\(.*?(?=^def )", SRC, re.S | re.M)
        seg = m.group(0)[m.group(0).index('"--help" in raw'):]
        assert "isatty()" in seg and "_wants_json()" in seg

    def test_it_is_not_wired_into_argparse_text(self):
        """gen_command_map.py introspects the live parser: anything in description/epilog is
        committed into docs/COMMAND_MAP.md and goes stale there forever."""
        m = re.search(r"^def build_parser\(.*?(?=^def )", SRC, re.S | re.M)
        assert "_flow_diagram" not in m.group(0) and "_print_flow" not in m.group(0)

    def test_there_is_exactly_one_definition_of_the_picture(self):
        """Drawn twice is drawn differently — the docs and the terminal then disagree."""
        assert len(re.findall(r"^def _flow_diagram\(", SRC, re.M)) == 1
