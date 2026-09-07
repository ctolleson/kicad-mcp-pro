"""Board-file bulk edits: Swap Layers, Global Deletions, Cleanup, Zone Manager.

These operations delete and rewrite real design data, so the tests pin down both
that they change what they should and — more importantly — that they leave
everything else alone.
"""

from __future__ import annotations

import pytest

from kicad_mcp.pcb.file_edits import (
    board_net_names,
    cleanup_tracks_and_vias,
    global_delete,
    list_zones,
    net_declarations,
    set_zone_properties,
    swap_layers,
)
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
