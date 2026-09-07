"""S-expression tree parser round-trip and structural guarantees.

The parser underpins every board-file edit, so the property that matters is that
parse -> dump never loses or corrupts data. Byte-identical output is explicitly
*not* required: KiCad reformats a file whenever it saves, and the board write
transaction runs `kicad-cli pcb upgrade` afterwards, so semantic identity plus a
stable fixed point is the contract.
"""

from __future__ import annotations

import pytest

from kicad_mcp.utils.sexpr_tree import Atom, SExprParseError, SList, dump_file, dumps, parse

BOARD = """\
(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t)
\t(segment
\t\t(start 10 20)
\t\t(end 30 40)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t)
\t(zone
\t\t(net "+5V")
\t\t(layer "F.Cu")
\t\t(priority 3)
\t\t(fill yes
\t\t\t(thermal_gap 0.5)
\t\t)
\t)
)
"""


def test_parses_into_a_tagged_tree() -> None:
    root = parse(BOARD)
    assert root.tag == "kicad_pcb"
    assert root.value_of("version") == "20241229"
    assert root.value_of("generator") == "pcbnew"
    assert len(root.children("segment")) == 1
    assert len(root.children("zone")) == 1


def test_round_trip_is_byte_identical_for_canonical_input() -> None:
    assert dump_file(parse(BOARD)) == BOARD


def test_round_trip_reaches_a_fixed_point() -> None:
    once = dump_file(parse(BOARD))
    twice = dump_file(parse(once))
    assert once == twice


def test_quoting_is_preserved_in_both_directions() -> None:
    """(layer "F.Cu") and (layer F.Cu) are not interchangeable to KiCad."""
    root = parse('(x (a "quoted") (b bare))')
    a, b = root.child("a"), root.child("b")
    assert a is not None and b is not None
    assert isinstance(a[1], Atom) and a[1].quoted
    assert isinstance(b[1], Atom) and not b[1].quoted

    reparsed = parse(dumps(root))
    ra, rb = reparsed.child("a"), reparsed.child("b")
    assert ra is not None and rb is not None
    assert isinstance(ra[1], Atom) and ra[1].quoted
    assert isinstance(rb[1], Atom) and not rb[1].quoted


def test_strings_containing_parens_and_escapes_survive() -> None:
    original = 'a (b) c "quoted" end'
    root = parse(r'(text (value "a (b) c \"quoted\" end"))')
    assert root.tag == "text"
    assert root.value_of("value") == original
    # The escaped quotes and the bare parens must survive a write/read cycle.
    assert parse(dumps(root)).value_of("value") == original


def test_empty_string_atom_round_trips() -> None:
    """An unnamed net is a real value, not a missing one."""
    root = parse('(segment (net ""))')
    assert root.value_of("net") == ""
    assert dumps(root) == '(segment\n\t(net "")\n)'


def test_atom_only_lists_are_written_inline() -> None:
    assert dumps(parse("(width 0.25)")) == "(width 0.25)"


def test_nested_lists_are_written_multiline_with_tabs() -> None:
    text = dumps(parse("(a (b 1) (c 2))"))
    assert text == "(a\n\t(b 1)\n\t(c 2)\n)"


def test_leading_atoms_stay_on_the_opening_line() -> None:
    """KiCad writes (layer "F.Cu" then nested children, not the name on its own line."""
    text = dumps(parse('(layer "F.Cu" (type "copper") (thickness 0.035))'))
    assert text.splitlines()[0] == '(layer "F.Cu"'


def test_value_of_reads_the_first_argument() -> None:
    root = parse(BOARD)
    segment = root.child("segment")
    assert segment is not None
    assert segment.value_of("width") == "0.25"
    assert segment.value_of("layer") == "F.Cu"
    assert segment.value_of("nonexistent") is None


def test_set_value_updates_in_place_and_appends_when_absent() -> None:
    root = parse("(zone (priority 3))")
    root.set_value("priority", "9", quoted=False)
    assert root.value_of("priority") == "9"
    root.set_value("name", "POUR", quoted=True)
    assert root.value_of("name") == "POUR"
    # Containing sub-lists, the zone is written multiline, as KiCad writes it.
    assert dumps(root) == '(zone\n\t(priority 9)\n\t(name "POUR")\n)'


def test_walk_visits_every_nested_list() -> None:
    tags = [node.tag for node in parse(BOARD).walk()]
    assert "kicad_pcb" in tags
    assert "fill" in tags  # two levels down
    assert tags.count("layer") == 2  # segment and zone


def test_children_returns_only_direct_descendants() -> None:
    root = parse("(a (b 1) (c (b 2)))")
    assert len(root.children("b")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "(unclosed",
        "closed)",
        '(unterminated "string',
        "",
        "(a) (b)",
    ],
)
def test_malformed_input_raises_rather_than_corrupting(bad: str) -> None:
    with pytest.raises(SExprParseError):
        parse(bad)


def test_comments_are_tolerated() -> None:
    root = parse("(a ; trailing comment\n (b 1))")
    assert root.child("b") is not None


def test_slist_tag_of_empty_list_is_blank() -> None:
    assert SList().tag == ""
