"""Reading a board's nets from file text, across both KiCad net formats.

KiCad 10 dropped the board-level ``(net N "name")`` table and writes the name on
each item. Code matching only the table form finds nothing on a 10.x board and
reports it as having no nets -- an empty answer that reads as a fact rather than
as a parse failure. That failure mode has now appeared in four separate places,
so the shared reader is pinned down here.
"""

from __future__ import annotations

from kicad_mcp.utils.board_nets import board_nets_from_text

LEGACY = """\
(kicad_pcb
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VCC")
\t(segment (start 0 0) (end 1 0) (net 1))
)
"""

KICAD_10 = """\
(kicad_pcb
\t(segment (start 0 0) (end 1 0) (net "GND"))
\t(segment (start 1 0) (end 2 0) (net "GND"))
\t(segment (start 2 0) (end 3 0) (net "VCC"))
\t(via (at 1 1) (net "GND"))
)
"""


def test_a_legacy_board_reports_its_declared_table() -> None:
    nets = board_nets_from_text(LEGACY)

    assert [net["name"] for net in nets] == ["", "GND", "VCC"]
    assert [net["code"] for net in nets] == [0, 1, 2]


def test_a_kicad_10_board_reports_nets_named_on_items() -> None:
    """The regression that mattered: this used to come back empty."""
    nets = board_nets_from_text(KICAD_10)

    assert sorted(str(net["name"]) for net in nets) == ["GND", "VCC"]


def test_nets_named_on_items_are_not_duplicated() -> None:
    nets = board_nets_from_text(KICAD_10)

    assert len(nets) == len({str(net["name"]) for net in nets})


def test_a_board_with_no_nets_reports_none() -> None:
    assert board_nets_from_text("(kicad_pcb)") == []


def test_the_declared_table_wins_when_a_board_has_both() -> None:
    """A transitional board keeps real codes rather than synthesised ones."""
    both = LEGACY.replace("\t(segment", '\t(segment (net "GND")\n\t(segment', 1)

    nets = board_nets_from_text(both)

    assert [net["code"] for net in nets] == [0, 1, 2]
