"""Bounded manufacturing cleanup must never modify copper or accept stale IDs."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_mcp.pcb.manufacturing_cleanup import (
    move_silkscreen_to_fab,
    set_zone_island_policy,
)
from kicad_mcp.tools.pcb_file_edits import PcbFileEditDependencies
from kicad_mcp.tools.pcb_manufacturing_cleanup import register
from kicad_mcp.utils.sexpr_tree import dumps, parse

BOARD = """(kicad_pcb
 (segment (uuid "track") (start 0 0) (end 5 0) (layer "F.Cu") (net "GND"))
 (gr_line (uuid "outline") (layer "Edge.Cuts") (start 0 0) (end 5 0))
 (gr_text "note" (uuid "text") (layer "B.SilkS") (at 2 2))
 (footprint "sensor" (uuid "fp") (layer "F.Cu") (at 5 5 90)
   (property "Reference" "U1" (uuid "ref") (layer "F.SilkS"))
   (fp_line (uuid "silk") (layer "F.SilkS") (start 0 0) (end 2 0))
   (pad "1" thru_hole circle (uuid "pad") (layers "*.Cu" "*.Mask") (net "GND")))
 (zone (uuid "zone") (net "GND") (layer "F.Cu") (min_thickness 0.25)
   (fill yes (thermal_gap 0.5) (island_removal_mode 1) (island_area_min 10))
   (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))
   (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 9 0) (xy 9 9)))
   (fill_segments (layer "F.Cu") (pts (xy 1 1) (xy 2 2)))))"""


def test_move_silk_preserves_coordinates_copper_and_fields() -> None:
    tree = parse(BOARD)
    expected = BOARD.replace('(layer "B.SilkS")', '(layer "B.Fab")').replace(
        '(uuid "silk") (layer "F.SilkS")', '(uuid "silk") (layer "F.Fab")'
    )
    report = move_silkscreen_to_fab(tree, item_ids=["text", "silk"])
    assert report.total == 2
    assert dumps(tree) == dumps(parse(expected))


@pytest.mark.parametrize(
    "ids", [[], ["silk", "silk"], ["missing"], ["silk", "pad"], ["track"], ["outline"]]
)
def test_invalid_selection_is_atomic(ids: list[str]) -> None:
    tree = parse(BOARD)
    original = dumps(tree)
    with pytest.raises(ValueError):
        move_silkscreen_to_fab(tree, item_ids=ids)
    assert dumps(tree) == original


@pytest.mark.parametrize("lock", ["locked", "(locked yes)"])
def test_parent_footprint_lock_is_respected(lock: str) -> None:
    tree = parse(BOARD.replace('(footprint "sensor"', '(footprint "sensor" ' + lock))
    original = dumps(tree)
    with pytest.raises(ValueError, match="locked"):
        move_silkscreen_to_fab(tree, item_ids=["text", "silk"])
    assert dumps(tree) == original


def test_duplicate_board_ids_are_rejected() -> None:
    tree = parse(BOARD.replace('(uuid "text")', '(uuid "silk")'))
    with pytest.raises(ValueError, match="ambiguous"):
        move_silkscreen_to_fab(tree, item_ids=["silk"])


@pytest.mark.parametrize("mode,code", [("always", "0"), ("never", "1"), ("below_area", "2")])
def test_zone_policy_preserves_outline_and_invalidates_fill(mode: str, code: str) -> None:
    tree = parse(BOARD)
    zone = tree.child("zone")
    outline = dumps(zone.child("polygon"))
    report = set_zone_island_policy(tree, zone_ids=["zone"], removal=mode, area_min_mm2=2.5)
    assert report.total >= 1
    assert zone.child("fill").value_of("island_removal_mode") == code
    assert zone.child("fill").value_of("island_area_min") == "2.5"
    assert dumps(zone.child("polygon")) == outline
    assert zone.value_of("net") == "GND"
    assert not zone.children("filled_polygon") and not zone.children("fill_segments")
    assert "yes" not in [str(n) for n in zone.child("fill")[:2]]
    assert "refill" in " ".join(report.details).lower()


@pytest.mark.parametrize("area", [-1, float("nan"), float("inf")])
def test_zone_policy_rejects_invalid_area_without_changes(area: float) -> None:
    tree = parse(BOARD)
    original = dumps(tree)
    with pytest.raises(ValueError):
        set_zone_island_policy(tree, zone_ids=["zone"], removal="below_area", area_min_mm2=area)
    assert dumps(tree) == original


@pytest.mark.parametrize("ids", [["zone", "missing"], ["track"], []])
def test_zone_selection_is_atomic(ids: list[str]) -> None:
    tree = parse(BOARD)
    original = dumps(tree)
    with pytest.raises(ValueError):
        set_zone_island_policy(tree, zone_ids=ids, removal="always")
    assert dumps(tree) == original


def test_rule_areas_are_not_copper_zones() -> None:
    tree = parse(BOARD.replace("(zone (uuid", "(zone (keepout (tracks not_allowed)) (uuid"))
    with pytest.raises(ValueError, match="rule area"):
        set_zone_island_policy(tree, zone_ids=["zone"], removal="always")


def test_area_mode_requires_threshold() -> None:
    with pytest.raises(ValueError, match="area_min_mm2"):
        set_zone_island_policy(parse(BOARD), zone_ids=["zone"], removal="below_area")


def test_zone_lock_and_invalid_mode_are_rejected() -> None:
    for board, mode in [
        (BOARD.replace("(zone (uuid", "(zone locked (uuid"), "always"),
        (BOARD, "invalid"),
    ]:
        tree = parse(board)
        before = dumps(tree)
        with pytest.raises(ValueError):
            set_zone_island_policy(tree, zone_ids=["zone"], removal=mode)
        assert dumps(tree) == before


def test_other_zones_are_not_changed() -> None:
    tree = parse(BOARD)
    other = parse('(zone (uuid "other") (layer "B.Cu") (fill yes (island_removal_mode 1)))')
    tree.append(other)
    before = dumps(other)
    set_zone_island_policy(tree, zone_ids=["zone"], removal="always")
    assert dumps(other) == before


def test_non_copper_zone_is_rejected() -> None:
    tree = parse(
        BOARD.replace(
            '(net "GND") (layer "F.Cu") (min_thickness',
            '(net "GND") (layer "F.SilkS") (min_thickness',
        )
    )
    before = dumps(tree)
    with pytest.raises(ValueError, match="copper layers"):
        set_zone_island_policy(tree, zone_ids=["zone"], removal="always")
    assert dumps(tree) == before


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("pcb_move_silkscreen_to_fab", {"item_ids": ["silk"]}),
        ("pcb_set_zone_island_policy", {"zone_ids": ["zone"]}),
    ],
)
@pytest.mark.asyncio
async def test_adapter_dry_run_then_transaction(name: str, arguments: dict, tmp_path: Path) -> None:
    server = FastMCP("cleanup-test")
    board_path = tmp_path / "test.kicad_pcb"
    writes = []

    def write(mutator) -> str:
        writes.append(mutator(BOARD))
        return str(board_path)

    transaction = Mock(side_effect=write)
    register(
        server,
        PcbFileEditDependencies(transaction, lambda: BOARD, lambda: board_path),
    )
    tool = server._tool_manager.get_tool(name)
    assert tool is not None
    assert tool.parameters["properties"]["dry_run"]["default"] is True
    id_parameter = "item_ids" if "item_ids" in arguments else "zone_ids"
    assert tool.parameters["properties"][id_parameter]["items"]["type"] == "string"
    preview = await tool.run(arguments)
    assert "Dry run" in preview
    transaction.assert_not_called()
    result = await tool.run({**arguments, "dry_run": False})
    assert "Board updated" in result
    transaction.assert_called_once()
    assert len(writes) == 1 and writes[0] != BOARD
    writes.clear()
    result = await tool.run({id_parameter: ["missing"], "dry_run": False})
    assert "Cannot apply" in result
    assert writes == []
