"""Board-file bulk edits: Swap Layers, Global Deletions, Cleanup, Zone Manager.

These operations delete and rewrite real design data, so the tests pin down both
that they change what they should and — more importantly — that they leave
everything else alone.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_mcp.pcb.file_edits import (
    board_copper_layers,
    board_net_names,
    board_outline_rectangle,
    cleanup_tracks_and_vias,
    create_zone,
    global_delete,
    list_zones,
    net_declarations,
    set_zone_properties,
    swap_layers,
)
from kicad_mcp.tools.pcb_file_edits import PcbFileEditDependencies
from kicad_mcp.tools.pcb_file_edits import register as register_zone_tools
from kicad_mcp.tools.router import TOOL_CATEGORIES
from kicad_mcp.utils.sexpr_tree import SList, dumps, parse

# KiCad 10 records the net name on each item and writes no board-level net table.
BOARD_V10 = """\
(kicad_pcb
\t(version 20241229)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t)
\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "F.Cu") (net "GND"))
\t(segment (start 2 2) (end 3 3) (width 0.25) (layer "B.Cu") (net "VCC"))
\t(segment (start 5 5) (end 5 5) (width 0.25) (layer "F.Cu") (net "GND"))
\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "F.Cu") (net "GND"))
\t(via (at 4 4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
\t(via (at 4 4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
\t(gr_line (start 0 0) (end 1 0) (layer "Edge.Cuts"))
\t(gr_text "hi" (at 1 1) (layer "F.SilkS"))
\t(footprint "R_0402"
\t\t(layer "F.Cu")
\t\t(pad "1" smd rect (at 0 0) (layers "F.Cu" "F.Mask" "F.Paste") (net "GND"))
\t\t(pad "2" smd rect (at 1 0) (layers "*.Cu" "*.Mask"))
\t)
\t(zone
\t\t(net "GND")
\t\t(layer "F.Cu")
\t\t(priority 1)
\t\t(min_thickness 0.25)
\t\t(fill yes (thermal_gap 0.5))
\t\t(polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))
\t\t(filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 9 0) (xy 9 9)))
\t)
)
"""

# KiCad 9 and earlier: a board-level net table, with items referring to codes.
BOARD_V9 = """\
(kicad_pcb
\t(version 20221018)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VCC")
\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "F.Cu") (net 1))
\t(segment (start 3 3) (end 4 4) (width 0.25) (layer "F.Cu") (net 2))
)
"""


@pytest.fixture
def board() -> SList:
    return parse(BOARD_V10)


# --- Swap Layers -----------------------------------------------------------


def test_swap_layers_is_simultaneous_not_sequential(board) -> None:
    """F.Cu<->B.Cu must exchange, not collapse both onto one layer."""
    swap_layers(board, {"F.Cu": "B.Cu", "B.Cu": "F.Cu"})
    layers = [s.value_of("layer") for s in board.children("segment")]
    assert layers == ["B.Cu", "F.Cu", "B.Cu", "B.Cu"]


def test_swap_layers_leaves_the_stackup_definition_alone(board) -> None:
    """The dialog moves objects between layers; it does not rename the stackup."""
    swap_layers(board, {"F.Cu": "B.Cu"})
    table = board.child("layers")
    assert table is not None
    assert dumps(table).count('"F.Cu"') == 1  # the definition survives untouched


def test_swap_layers_skips_wildcard_pad_layers(board) -> None:
    swap_layers(board, {"F.Cu": "B.Cu"})
    footprint = board.child("footprint")
    assert footprint is not None
    pads = footprint.children("pad")
    assert "*.Cu" in dumps(pads[1])  # wildcard untouched
    assert "B.Cu" in dumps(pads[0])  # concrete layer moved


def test_swap_layers_reaches_inside_footprints(board) -> None:
    swap_layers(board, {"F.Cu": "B.Cu"})
    footprint = board.child("footprint")
    assert footprint is not None
    assert footprint.value_of("layer") == "B.Cu"


def test_swap_layers_reports_each_direction(board) -> None:
    report = swap_layers(board, {"F.Cu": "B.Cu", "B.Cu": "F.Cu"})
    assert report.counts["F.Cu -> B.Cu"] > 0
    assert report.counts["B.Cu -> F.Cu"] > 0


def test_swap_layers_rejects_an_empty_mapping(board) -> None:
    with pytest.raises(ValueError, match="No layer mapping"):
        swap_layers(board, {})


# --- Net resolution across board formats -----------------------------------


def test_net_names_are_read_from_items_on_kicad_10() -> None:
    assert net_declarations(parse(BOARD_V10)) == {}
    assert board_net_names(parse(BOARD_V10)) == {"GND", "VCC"}


def test_net_names_are_read_from_the_table_on_older_boards() -> None:
    root = parse(BOARD_V9)
    assert net_declarations(root) == {0: "", 1: "GND", 2: "VCC"}
    assert {"GND", "VCC"} <= board_net_names(root)


def test_numeric_net_codes_still_filter_on_older_boards() -> None:
    root = parse(BOARD_V9)
    report = global_delete(root, item_types=["tracks"], nets=["1"])
    assert report.counts == {"tracks": 1}
    assert len(root.children("segment")) == 1


# --- Global Deletions ------------------------------------------------------


def test_global_delete_filters_by_net(board) -> None:
    report = global_delete(board, item_types=["tracks"], nets=["VCC"])
    assert report.counts == {"tracks": 1}
    assert all(s.value_of("net") != "VCC" for s in board.children("segment"))


def test_global_delete_filters_by_layer(board) -> None:
    report = global_delete(board, item_types=["tracks"], layers=["B.Cu"])
    assert report.counts == {"tracks": 1}


def test_global_delete_handles_multiple_classes(board) -> None:
    report = global_delete(board, item_types=["vias", "graphics"])
    assert report.counts == {"vias": 2, "graphics": 1}


def test_global_delete_ignores_items_inside_footprints(board) -> None:
    """Like the dialog, footprint-owned pads are part of the footprint."""
    global_delete(board, item_types=["tracks"], nets=["GND"])
    footprint = board.child("footprint")
    assert footprint is not None
    assert len(footprint.children("pad")) == 2


def test_global_delete_rejects_unknown_item_types(board) -> None:
    with pytest.raises(ValueError, match="Unknown item type"):
        global_delete(board, item_types=["widgets"])


def test_global_delete_requires_at_least_one_type(board) -> None:
    with pytest.raises(ValueError, match="No item types"):
        global_delete(board, item_types=[])


def test_global_delete_rejects_an_unknown_net(board) -> None:
    with pytest.raises(ValueError, match="Unknown net"):
        global_delete(board, item_types=["tracks"], nets=["NOT_A_NET"])


def test_global_delete_leaves_the_board_untouched_when_nothing_matches(board) -> None:
    before = dumps(board)
    report = global_delete(board, item_types=["dimensions"])
    assert report.total == 0
    assert dumps(board) == before


# --- Cleanup Tracks & Vias -------------------------------------------------


def test_cleanup_removes_zero_length_and_duplicate_tracks(board) -> None:
    report = cleanup_tracks_and_vias(board)
    assert report.counts["zero-length tracks"] == 1
    assert report.counts["duplicate tracks"] == 1
    assert len(board.children("segment")) == 2


def test_cleanup_removes_duplicate_vias(board) -> None:
    report = cleanup_tracks_and_vias(board)
    assert report.counts["duplicate vias"] == 1
    assert len(board.children("via")) == 1


def test_cleanup_treats_a_reversed_track_as_the_same_segment() -> None:
    root = parse(
        "(kicad_pcb\n"
        '\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "F.Cu") (net "N"))\n'
        '\t(segment (start 2 2) (end 1 1) (width 0.25) (layer "F.Cu") (net "N"))\n'
        ")"
    )
    report = cleanup_tracks_and_vias(root)
    assert report.counts["duplicate tracks"] == 1


def test_cleanup_keeps_same_geometry_on_different_layers() -> None:
    root = parse(
        "(kicad_pcb\n"
        '\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "F.Cu") (net "N"))\n'
        '\t(segment (start 1 1) (end 2 2) (width 0.25) (layer "B.Cu") (net "N"))\n'
        ")"
    )
    assert cleanup_tracks_and_vias(root).total == 0


def test_cleanup_options_can_be_disabled(board) -> None:
    report = cleanup_tracks_and_vias(
        board,
        delete_zero_length=False,
        delete_duplicate_tracks=False,
        delete_duplicate_vias=False,
    )
    assert report.total == 0
    assert len(board.children("segment")) == 4


# --- Zone Manager ----------------------------------------------------------


def test_list_zones_reports_net_layers_priority_and_fill(board) -> None:
    (zone,) = list_zones(board)
    assert zone["net"] == "GND"
    assert zone["layers"] == ["F.Cu"]
    assert zone["priority"] == 1
    assert zone["filled"] is True
    assert zone["outline_polygons"] == 1
    assert zone["filled_polygons"] == 1


def test_set_zone_properties_updates_named_fields(board) -> None:
    set_zone_properties(board, index=0, priority=7, name="POUR", min_thickness=0.3)
    (zone,) = list_zones(board)
    assert zone["priority"] == 7
    assert zone["name"] == "POUR"
    assert zone["min_thickness"] == "0.3"


def test_unfilling_a_zone_drops_stale_fill_geometry(board) -> None:
    """Leaving filled_polygon behind would misrepresent the board to DRC and plots."""
    report = set_zone_properties(board, index=0, filled=False)
    (zone,) = list_zones(board)
    assert zone["filled"] is False
    assert zone["filled_polygons"] == 0
    assert report.counts["cleared filled polygons"] == 1
    assert zone["outline_polygons"] == 1  # the outline itself is retained


def test_refilling_flag_can_be_set_back(board) -> None:
    set_zone_properties(board, index=0, filled=False)
    set_zone_properties(board, index=0, filled=True)
    assert list_zones(board)[0]["filled"] is True


def test_set_zone_properties_validates_the_index(board) -> None:
    with pytest.raises(ValueError, match="out of range"):
        set_zone_properties(board, index=5, priority=1)


def test_set_zone_properties_rejects_nonsense_values(board) -> None:
    with pytest.raises(ValueError, match="zero or greater"):
        set_zone_properties(board, index=0, priority=-1)
    with pytest.raises(ValueError, match="greater than zero"):
        set_zone_properties(board, index=0, min_thickness=0)


def test_edits_keep_the_document_parseable(board) -> None:
    """Every mutation must leave text KiCad's own parser would still accept."""
    swap_layers(board, {"F.Cu": "In1.Cu"})
    global_delete(board, item_types=["graphics"])
    cleanup_tracks_and_vias(board)
    set_zone_properties(board, index=0, priority=3)
    reparsed = parse(dumps(board))
    assert reparsed.tag == "kicad_pcb"
    assert len(reparsed.children("zone")) == 1


# --- Locked items ----------------------------------------------------------

LOCKED_BOARD = """\
(kicad_pcb
\t(version 20241229)
\t(segment (start 1 1) (end 2 2) (layer "F.Cu") (net "N") (locked yes))
\t(segment (start 3 3) (end 4 4) (layer "F.Cu") (net "N"))
\t(segment (start 5 5) (end 6 6) (layer "F.Cu") (net "N") (locked no))
\t(via (at 7 7) (layers "F.Cu" "B.Cu") (net "N") locked)
\t(via (at 8 8) (layers "F.Cu" "B.Cu") (net "N") (unlocked yes))
)
"""


def test_locked_items_are_kept_by_default() -> None:
    """(locked yes) and the legacy bare `locked` flag both protect an item."""
    root = parse(LOCKED_BOARD)
    report = global_delete(root, item_types=["tracks", "vias"], locked=False)
    assert report.counts == {"tracks": 2, "vias": 1}
    remaining = dumps(root)
    assert "(locked yes)" in remaining  # the locked track survived
    assert "(at 7 7)" in remaining  # the bare-flag via survived


def test_locked_items_are_deleted_when_lock_state_is_ignored() -> None:
    root = parse(LOCKED_BOARD)
    report = global_delete(root, item_types=["tracks", "vias"], locked=None)
    assert report.counts == {"tracks": 3, "vias": 2}


def test_unlocked_is_not_mistaken_for_locked() -> None:
    """KiCad writes (unlocked yes) on pads; it must not read as a lock."""
    root = parse(LOCKED_BOARD)
    global_delete(root, item_types=["vias"], locked=False)
    assert "(at 8 8)" not in dumps(root)  # the (unlocked yes) via was deleted


def test_locked_no_is_treated_as_unlocked() -> None:
    root = parse(LOCKED_BOARD)
    global_delete(root, item_types=["tracks"], locked=False)
    assert "(start 5 5)" not in dumps(root)


# --- Registration ----------------------------------------------------------

FILE_EDIT_TOOLS = {
    "pcb_swap_layers",
    "pcb_global_delete",
    "pcb_cleanup_tracks_and_vias",
    "pcb_list_zones",
    "pcb_set_zone_properties",
    "pcb_list_nets_on_board",
}


def test_file_edit_tools_are_registered() -> None:
    from kicad_mcp.server import build_server

    server = build_server("agent_full")
    server.ensure_registered()
    registered = {tool.name for tool in server._tool_manager.list_tools()}
    assert FILE_EDIT_TOOLS <= registered


def test_file_edit_tools_do_not_require_a_running_kicad() -> None:
    """They edit the .kicad_pcb directly, so discovery must not hide them when
    KiCad is closed — the exact case they exist to serve."""
    from kicad_mcp.capabilities import RuntimeRequirement
    from kicad_mcp.capabilities import get as get_capability_record

    for name in FILE_EDIT_TOOLS | {"pcb_set_stackup"}:
        record = get_capability_record(name)
        assert record is not None, f"{name} has no capability record"
        assert record.runtime is not RuntimeRequirement.KICAD_IPC, (
            f"{name} is file-backed but is marked as requiring a live KiCad session"
        )


# --- zone creation ---------------------------------------------------------

ZONE_BOARD = """\
(kicad_pcb
\t(version 20241229)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(1 "In1.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(gr_rect
\t\t(start 10 10)
\t\t(end 60 40)
\t\t(layer "Edge.Cuts")
\t)
\t(segment
\t\t(start 20 20)
\t\t(end 30 20)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t)
)
"""

SQUARE = [(12.0, 12.0), (58.0, 12.0), (58.0, 38.0), (12.0, 38.0)]


def _zone_board() -> SList:
    return parse(ZONE_BOARD)


def test_create_zone_writes_a_zone_kicad_can_read_back() -> None:
    root = _zone_board()
    create_zone(root, net="GND", layers=["B.Cu"], polygon=SQUARE, name="GND_pour")

    reparsed = parse(dumps(root))
    zones = list_zones(reparsed)
    assert len(zones) == 1
    assert zones[0]["net"] == "GND"
    assert zones[0]["layers"] == ["B.Cu"]
    assert zones[0]["name"] == "GND_pour"
    assert zones[0]["outline_polygons"] == 1


def test_a_new_zone_is_unfilled() -> None:
    """Fill geometry comes from KiCad, so a freshly written zone must claim none."""
    root = _zone_board()
    create_zone(root, net="GND", layers=["B.Cu"], polygon=SQUARE)

    zones = list_zones(parse(dumps(root)))
    assert zones[0]["filled"] is False
    assert zones[0]["filled_polygons"] == 0


def test_create_zone_on_a_kicad_10_board_writes_the_net_name() -> None:
    """KiCad 10 has no board-level net table; the name belongs on the item."""
    root = _zone_board()
    create_zone(root, net="GND", layers=["F.Cu"], polygon=SQUARE)

    text = dumps(root)
    assert '(net "GND")' in text.split("(zone")[-1]


def test_create_zone_on_a_legacy_board_writes_the_net_code() -> None:
    """A board that still declares a net table refers to nets by code, not name."""
    root = parse(ZONE_BOARD.replace("\t(gr_rect", '\t(net 0 "")\n\t(net 7 "GND")\n\t(gr_rect'))
    assert net_declarations(root)[7] == "GND"

    create_zone(root, net="GND", layers=["F.Cu"], polygon=SQUARE)

    zone_text = dumps(root).split("(zone")[-1]
    assert "(net 7)" in zone_text
    assert '(net "GND")' not in zone_text


def test_create_zone_rejects_a_net_the_board_does_not_have() -> None:
    """A zone naming a missing net parses fine and pours nothing - refuse it."""
    root = _zone_board()
    with pytest.raises(ValueError, match="Unknown net"):
        create_zone(root, net="GNDD", layers=["B.Cu"], polygon=SQUARE)
    assert not root.children("zone")


def test_create_zone_rejects_a_layer_the_board_does_not_have() -> None:
    root = _zone_board()
    with pytest.raises(ValueError, match="Unknown copper layer"):
        create_zone(root, net="GND", layers=["In4.Cu"], polygon=SQUARE)
    assert not root.children("zone")


def test_create_zone_rejects_an_outline_that_is_not_a_polygon() -> None:
    root = _zone_board()
    with pytest.raises(ValueError, match="at least three distinct corners"):
        create_zone(root, net="GND", layers=["B.Cu"], polygon=[(0.0, 0.0), (1.0, 1.0)])


def test_create_zone_ignores_a_repeated_closing_corner() -> None:
    """Callers often close the ring; the duplicate must not become a real corner."""
    root = _zone_board()
    create_zone(root, net="GND", layers=["B.Cu"], polygon=[*SQUARE, SQUARE[0]])

    zone = root.children("zone")[0]
    pts = zone.child("polygon").child("pts")
    assert len(pts.children("xy")) == 4


def test_create_zone_spanning_layers_uses_the_plural_form() -> None:
    root = _zone_board()
    create_zone(root, net="GND", layers=["In1.Cu", "B.Cu"], polygon=SQUARE)

    zones = list_zones(parse(dumps(root)))
    assert zones[0]["layers"] == ["In1.Cu", "B.Cu"]


def test_create_zone_rejects_an_unknown_pad_connection() -> None:
    root = _zone_board()
    with pytest.raises(ValueError, match="pad_connection must be one of"):
        create_zone(root, net="GND", layers=["B.Cu"], polygon=SQUARE, pad_connection="maybe")


def test_board_copper_layers_excludes_non_copper() -> None:
    assert board_copper_layers(_zone_board()) == ["F.Cu", "In1.Cu", "B.Cu"]


def test_board_outline_rectangle_insets_from_the_edge() -> None:
    assert board_outline_rectangle(_zone_board(), inset=0.5) == [
        (10.5, 10.5),
        (59.5, 10.5),
        (59.5, 39.5),
        (10.5, 39.5),
    ]


def test_board_outline_rectangle_refuses_an_inset_that_swallows_the_board() -> None:
    with pytest.raises(ValueError, match="leaves no area"):
        board_outline_rectangle(_zone_board(), inset=40.0)


def test_board_outline_rectangle_needs_an_outline() -> None:
    root = parse('(kicad_pcb\n\t(layers\n\t\t(0 "F.Cu" signal)\n\t)\n)')
    with pytest.raises(ValueError, match="no Edge.Cuts geometry"):
        board_outline_rectangle(root)


def _zone_server(tmp_path: Path, written: list[str] | None = None) -> FastMCP:
    """A server wired to an in-memory board, so no test touches a real file."""
    board = tmp_path / "board.kicad_pcb"

    def transaction(mutator: Callable[[str], str]) -> str:
        result = mutator(ZONE_BOARD)
        if written is not None:
            written.append(result)
        return str(board)

    server = FastMCP("zone-test")
    register_zone_tools(
        server,
        PcbFileEditDependencies(
            transactional_board_write=transaction,
            read_board_text=lambda: ZONE_BOARD,
            configured_board_file=lambda: board,
        ),
    )
    return server


def _tool(server: FastMCP, name: str) -> object:
    return {tool.name: tool for tool in server._tool_manager.list_tools()}[name]


def test_zone_tools_are_registered_and_declared(tmp_path: Path) -> None:
    """A tool absent from TOOL_CATEGORIES registers but is invisible to every profile."""
    names = {tool.name for tool in _zone_server(tmp_path)._tool_manager.list_tools()}
    assert {"pcb_create_zone", "pcb_fill_zones"} <= names

    declared = {name for category in TOOL_CATEGORIES.values() for name in category["tools"]}
    assert {"pcb_create_zone", "pcb_fill_zones"} <= declared


def test_pcb_create_zone_reports_a_dry_run_without_writing(tmp_path: Path) -> None:
    """The board must be untouched when dry_run is set."""
    written: list[str] = []
    tool = _tool(_zone_server(tmp_path, written), "pcb_create_zone")

    result = tool.fn(net="GND", layers=["B.Cu"], follow_board_outline=True, dry_run=True)

    assert "Dry run" in result
    assert not written


def test_pcb_create_zone_writes_when_not_a_dry_run(tmp_path: Path) -> None:
    written: list[str] = []
    tool = _tool(_zone_server(tmp_path, written), "pcb_create_zone")

    result = tool.fn(net="GND", layers=["B.Cu"], follow_board_outline=True)

    assert "Board updated" in result
    assert len(written) == 1
    assert "(zone" in written[0]


def test_pcb_create_zone_rejects_both_outline_sources(tmp_path: Path) -> None:
    tool = _tool(_zone_server(tmp_path), "pcb_create_zone")

    assert "not both" in tool.fn(
        net="GND", layers=["B.Cu"], corners=[[0.0, 0.0]], follow_board_outline=True
    )
    assert "follow_board_outline=True" in tool.fn(net="GND", layers=["B.Cu"])


def test_pcb_create_zone_surfaces_a_bad_net_as_a_message(tmp_path: Path) -> None:
    """Validation errors should read as guidance, not raise out of the tool."""
    tool = _tool(_zone_server(tmp_path), "pcb_create_zone")

    assert "Unknown net" in tool.fn(net="NOPE", layers=["B.Cu"], follow_board_outline=True)
