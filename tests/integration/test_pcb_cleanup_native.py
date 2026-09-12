"""Opt-in native round trip; synthetic board, no customer design data."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_mcp.pcb.manufacturing_cleanup import (
    move_silkscreen_to_fab,
    set_zone_island_policy,
)
from kicad_mcp.utils.sexpr_tree import dump_file, parse

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_MCP_NATIVE_CLEANUP_TEST") != "1",
    reason="set KICAD_MCP_NATIVE_CLEANUP_TEST=1 with kicad-cli available",
)

BOARD = """(kicad_pcb (version 20241229) (generator "pcbnew")
 (general (thickness 1.6)) (paper "A4")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
  (37 "F.SilkS" user "f.silkscreen") (49 "F.Fab" user)
  (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND") (net 2 "VCC")
 (footprint "Synthetic:test" (layer "F.Cu") (at 3 5)
  (uuid "10000000-0000-0000-0000-000000000004")
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  (pad "2" smd circle (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "GND")))
 (segment (start 10 0) (end 10 10) (width 0.5) (layer "F.Cu") (net 2)
  (uuid "10000000-0000-0000-0000-000000000005"))
 (gr_rect (start 0 0) (end 20 10)
  (stroke (width 0.05) (type solid)) (fill none) (layer "Edge.Cuts")
  (uuid "10000000-0000-0000-0000-000000000001"))
 (gr_text "TEST" (at 3 3) (layer "F.SilkS")
  (uuid "10000000-0000-0000-0000-000000000002")
  (effects (font (size 1 1) (thickness 0.15))))
 (zone (net 1) (net_name "GND") (layer "F.Cu")
  (uuid "10000000-0000-0000-0000-000000000003") (hatch edge 0.5)
  (connect_pads (clearance 0.2)) (min_thickness 0.2)
  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)
   (island_removal_mode 1) (island_area_min 0))
  (polygon (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9)))))"""


def test_native_refill_removes_isolated_copper_and_accepts_silk_edit(tmp_path: Path) -> None:
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    assert cli, "Native test requested but kicad-cli is unavailable"
    board = tmp_path / "cleanup.kicad_pcb"
    board.write_text(BOARD, encoding="utf-8")

    def refill() -> None:
        result = subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--refill-zones",
                "--save-board",
                "--format",
                "json",
                "--output",
                str(tmp_path / "drc.json"),
                str(board),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    refill()
    root = parse(board.read_text(encoding="utf-8"))
    zone = root.child("zone")
    assert zone is not None and len(zone.children("filled_polygon")) == 2
    move_silkscreen_to_fab(root, item_ids=["10000000-0000-0000-0000-000000000002"])
    set_zone_island_policy(
        root, zone_ids=["10000000-0000-0000-0000-000000000003"], removal="always"
    )
    board.write_text(dump_file(root), encoding="utf-8")
    refill()
    final = parse(board.read_text(encoding="utf-8"))
    zone = final.child("zone")
    assert zone is not None and len(zone.children("filled_polygon")) == 1
    text = final.child("gr_text")
    assert text is not None and text.value_of("layer") == "F.Fab"
    assert zone.child("fill").value_of("island_removal_mode") == "0"
