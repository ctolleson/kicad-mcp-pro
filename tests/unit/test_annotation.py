"""Forward and back annotation between schematic and board.

The two operations KiCad exposes only through modal dialogs. The risk they carry
is applying the wrong side: a board can be correct and its schematic stale, so
the diff must be trustworthy and the apply must be opt-in.
"""

from __future__ import annotations

import pytest

from kicad_mcp.project.annotation import (
    BoardModel,
    SchematicModel,
    apply_pad_nets,
    apply_values,
    diff,
    map_net_names,
    parse_board,
    parse_netlist,
    remove_footprints,
)
from kicad_mcp.utils.sexpr_tree import parse

NETLIST = """\
(export (version "E")
  (components
    (comp (ref "J1") (value "Header") (footprint "Lib:Hdr_2x2") (tstamps "aaa"))
    (comp (ref "R1") (value "10k") (footprint "Lib:R_0402") (tstamps "bbb"))
  )
  (nets
    (net (code "1") (name "/GND")
      (node (ref "J1") (pin "2")) (node (ref "R1") (pin "2")))
    (net (code "2") (name "/VCC")
      (node (ref "J1") (pin "1")) (node (ref "R1") (pin "1")))
    (net (code "3") (name "unconnected-(J1-Pad3)")
      (node (ref "J1") (pin "3")))
  )
)
"""

BOARD = """\
(kicad_pcb
\t(version 20241229)
\t(footprint "Lib:Hdr_2x2"
\t\t(property "Reference" "J1" (at 0 0))
\t\t(property "Value" "Header" (at 0 0))
\t\t(pad "1" thru_hole circle (at 0 0) (net "VCC"))
\t\t(pad "2" thru_hole circle (at 1 0) (net "GND"))
\t\t(pad "3" thru_hole circle (at 2 0))
\t)
\t(footprint "Lib:R_0402"
\t\t(property "Reference" "R1" (at 0 0))
\t\t(property "Value" "10k" (at 0 0))
\t\t(pad "1" smd rect (at 5 0) (net "VCC"))
\t\t(pad "2" smd rect (at 6 0) (net "GND"))
\t)
)
"""


@pytest.fixture
def models() -> tuple[SchematicModel, BoardModel]:
    return parse_netlist(parse(NETLIST)), parse_board(parse(BOARD))


def test_a_synchronised_design_reports_no_differences(models) -> None:
    schematic, board = models
    assert diff(schematic, board).in_sync


def test_synthetic_unconnected_nets_are_not_real_nets(models) -> None:
    """KiCad invents unconnected-(J1-Pad3); treating it as a net would fight the board."""
    schematic, _ = models
    assert ("J1", "3") not in schematic.pad_nets
    assert not any(n.startswith("unconnected-") for n in schematic.net_pads)


def test_net_names_are_matched_by_pad_identity_not_by_name(models) -> None:
    """The schematic says /GND where the board says GND; the pads decide."""
    schematic, board = models
    mapping = map_net_names(schematic, board)
    assert mapping["/GND"] == "GND"
    assert mapping["/VCC"] == "VCC"


def test_a_flattened_board_name_still_matches() -> None:
    """An imported board may carry only the leaf label for /Sheet/NET."""
    netlist = parse_netlist(
        parse(
            '(export (nets (net (code "1") (name "/Connectors/D1_N") (node (ref "R1") (pin "1")))))'
        )
    )
    board = parse_board(
        parse(
            '(kicad_pcb (footprint "L:F" (property "Reference" "R1" (at 0 0))'
            ' (pad "1" smd rect (at 0 0) (net "D1_N"))))'
        )
    )
    assert map_net_names(netlist, board)["/Connectors/D1_N"] == "D1_N"


def test_a_brand_new_net_falls_back_to_the_root_sheet_name() -> None:
    netlist = parse_netlist(
        parse('(export (nets (net (code "1") (name "/NEW") (node (ref "R9") (pin "1")))))')
    )
    board = parse_board(parse("(kicad_pcb)"))
    assert map_net_names(netlist, board)["/NEW"] == "NEW"


def test_library_prefix_differences_are_not_footprint_mismatches() -> None:
    """An Altium-imported board writes Lib:Name where the netlist gives Name."""
    netlist = parse_netlist(
        parse('(export (components (comp (ref "C1") (value "1u") (footprint "CAPC0603"))))')
    )
    board = parse_board(
        parse(
            '(kicad_pcb (footprint "CAPC0603:CAPC0603"'
            ' (property "Reference" "C1" (at 0 0)) (property "Value" "1u" (at 0 0))))'
        )
    )
    assert diff(netlist, board).footprint_mismatches == []


def test_a_genuine_footprint_change_is_reported() -> None:
    netlist = parse_netlist(
        parse('(export (components (comp (ref "R1") (value "10k") (footprint "Lib:R_0603"))))')
    )
    board = parse_board(
        parse(
            '(kicad_pcb (footprint "Lib:R_0402" (property "Reference" "R1" (at 0 0))'
            ' (property "Value" "10k" (at 0 0))))'
        )
    )
    mismatches = diff(netlist, board).footprint_mismatches
    assert mismatches == [("R1", "Lib:R_0402", "Lib:R_0603")]


def test_components_only_on_one_side_are_reported(models) -> None:
    schematic, board = models
    schematic.components.pop("R1")
    delta = diff(schematic, board)
    assert delta.extra_on_board == ["R1"]
    assert delta.missing_on_board == []


def test_swapped_pins_surface_as_connectivity_differences() -> None:
    """The J9 case: board and schematic disagree about which pad carries which net."""
    netlist = parse_netlist(
        parse(
            '(export (components (comp (ref "J9") (value "P") (footprint "L:P")))'
            ' (nets (net (code "1") (name "/VCC") (node (ref "J9") (pin "1")))'
            '       (net (code "2") (name "/GND") (node (ref "J9") (pin "2")))))'
        )
    )
    board = parse_board(
        parse(
            '(kicad_pcb (footprint "L:P" (property "Reference" "J9" (at 0 0))'
            ' (property "Value" "P" (at 0 0))'
            ' (pad "1" thru_hole circle (at 0 0) (net "GND"))'
            ' (pad "2" thru_hole circle (at 1 0) (net "VCC"))))'
        )
    )
    changes = {
        (c.reference, c.pad): (c.board_net, c.resolved_net)
        for c in diff(netlist, board).pad_net_changes
    }
    assert changes[("J9", "1")] == ("GND", "VCC")
    assert changes[("J9", "2")] == ("VCC", "GND")


# --- Applying --------------------------------------------------------------


def test_applying_pad_nets_writes_the_board_form(models) -> None:
    schematic, board_model = models
    tree = parse(BOARD)
    schematic.pad_nets[("R1", "1")] = "/GND"  # move R1.1 from VCC to GND
    schematic.net_pads["/GND"].add(("R1", "1"))
    delta = diff(schematic, board_model)
    assert apply_pad_nets(tree, delta.pad_net_changes) == 1
    refreshed = parse_board(tree)
    assert refreshed.pad_nets[("R1", "1")] == "GND"


def test_applying_an_empty_net_clears_the_pad() -> None:
    tree = parse(
        '(kicad_pcb (footprint "L:F" (property "Reference" "R1" (at 0 0))'
        ' (pad "1" smd rect (at 0 0) (net "OLD"))))'
    )
    from kicad_mcp.project.annotation import PadNetChange

    change = PadNetChange("R1", "1", "OLD", "", "")
    assert apply_pad_nets(tree, [change]) == 1
    assert parse_board(tree).pad_nets[("R1", "1")] == ""


def test_applying_values_updates_the_board(models) -> None:
    tree = parse(BOARD)
    assert apply_values(tree, [("R1", "10k", "22k")]) == 1
    assert parse_board(tree).values["R1"] == "22k"


def test_removing_footprints_leaves_the_rest_intact(models) -> None:
    tree = parse(BOARD)
    assert remove_footprints(tree, ["R1"]) == 1
    refreshed = parse_board(tree)
    assert set(refreshed.footprints) == {"J1"}


def test_removing_nothing_leaves_the_board_untouched() -> None:
    from kicad_mcp.utils.sexpr_tree import dumps

    tree = parse(BOARD)
    before = dumps(tree)
    assert remove_footprints(tree, []) == 0
    assert dumps(tree) == before


def test_applied_board_still_parses(models) -> None:
    schematic, board_model = models
    tree = parse(BOARD)
    schematic.pad_nets[("R1", "1")] = "/GND"
    apply_pad_nets(tree, diff(schematic, board_model).pad_net_changes)
    apply_values(tree, [("R1", "10k", "22k")])
    from kicad_mcp.utils.sexpr_tree import dump_file

    assert parse(dump_file(tree)).tag == "kicad_pcb"
