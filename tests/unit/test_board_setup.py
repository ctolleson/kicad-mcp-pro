"""Board Setup editing against the project file.

KiCad's Board Setup dialog is modal, so net classes, pre-defined sizes and design
constraints have no cli or IPC path — they are edited in the .kicad_pro JSON. These
tests pin the schema KiCad 10 actually reads, since writing a plausible-looking key
that KiCad ignores would fail silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.project.board_setup import (
    BoardSetupError,
    apply_manufacturer_rules,
    assign_patterns,
    delete_net_class,
    describe,
    read_project,
    set_diff_pair_dimensions,
    set_net_class,
    set_track_widths,
    set_via_dimensions,
    write_project,
)


@pytest.fixture
def project() -> dict[str, Any]:
    """A project shaped the way KiCad 10 writes one."""
    return {
        "board": {
            "design_settings": {
                "track_widths": [],
                "via_dimensions": [],
                "diff_pair_dimensions": [],
                "rules": {"min_track_width": 0.2, "min_clearance": 0.0},
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "track_width": 0.2,
                    "clearance": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "diff_pair_width": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                }
            ],
            "netclass_patterns": [],
        },
    }


# --- Pre-defined sizes -----------------------------------------------------


def test_track_widths_are_sorted_and_deduplicated(project) -> None:
    result = set_track_widths(project, [0.5, 0.2, 0.5, 0.8])
    assert result == [0.2, 0.5, 0.8]
    assert project["board"]["design_settings"]["track_widths"] == [0.2, 0.5, 0.8]


def test_via_dimensions_use_kicad_key_names(project) -> None:
    """KiCad reads 'diameter' and 'drill'; anything else is silently ignored."""
    set_via_dimensions(project, [{"diameter_mm": 0.6, "drill_mm": 0.3}])
    stored = project["board"]["design_settings"]["via_dimensions"]
    assert stored == [{"diameter": 0.6, "drill": 0.3}]


def test_diff_pair_dimensions_use_kicad_key_names(project) -> None:
    set_diff_pair_dimensions(project, [{"width_mm": 0.2, "gap_mm": 0.15, "via_gap_mm": 0.25}])
    stored = project["board"]["design_settings"]["diff_pair_dimensions"]
    assert stored == [{"width": 0.2, "gap": 0.15, "via_gap": 0.25}]


def test_via_drill_must_be_smaller_than_its_diameter(project) -> None:
    """Otherwise there is no annular ring and the fab cannot build it."""
    with pytest.raises(BoardSetupError, match="smaller than its diameter"):
        set_via_dimensions(project, [{"diameter_mm": 0.3, "drill_mm": 0.3}])


def test_zero_and_negative_sizes_are_rejected(project) -> None:
    with pytest.raises(BoardSetupError, match="greater than zero"):
        set_track_widths(project, [0.25, 0])
    with pytest.raises(BoardSetupError, match="greater than zero"):
        set_via_dimensions(project, [{"diameter_mm": -0.6, "drill_mm": 0.3}])


def test_empty_list_clears_the_predefined_sizes(project) -> None:
    set_track_widths(project, [0.5])
    assert set_track_widths(project, []) == []


# --- Net classes -----------------------------------------------------------


def test_creating_a_class_fills_in_kicad_defaults(project) -> None:
    result = set_net_class(project, "Power", track_width_mm=0.5)
    assert result["name"] == "Power"
    assert result["track_width"] == 0.5
    # Untouched fields still get KiCad's defaults so the class looks native.
    assert result["via_diameter"] == 0.6
    assert "priority" in result and "pcb_color" in result


def test_updating_a_class_leaves_other_fields_alone(project) -> None:
    set_net_class(project, "Default", track_width_mm=0.25)
    default = project["net_settings"]["classes"][0]
    assert default["track_width"] == 0.25
    assert default["clearance"] == 0.2  # untouched
    assert default["via_diameter"] == 0.6


def test_class_via_drill_must_be_smaller_than_diameter(project) -> None:
    with pytest.raises(BoardSetupError, match="must be smaller than"):
        set_net_class(project, "Bad", via_diameter_mm=0.3, via_drill_mm=0.4)


def test_unknown_field_is_rejected_rather_than_silently_dropped(project) -> None:
    with pytest.raises(BoardSetupError, match="Unknown net-class field"):
        set_net_class(project, "Power", trackwidth=0.5)


def test_a_class_needs_a_name(project) -> None:
    with pytest.raises(BoardSetupError, match="needs a name"):
        set_net_class(project, "   ", track_width_mm=0.3)


def test_default_class_cannot_be_deleted(project) -> None:
    with pytest.raises(BoardSetupError, match="Default net class cannot be removed"):
        delete_net_class(project, "Default")


def test_deleting_a_class_also_drops_its_patterns(project) -> None:
    set_net_class(project, "Power", track_width_mm=0.5)
    assign_patterns(project, "Power", ["VDD*", "GND"])
    assert delete_net_class(project, "Power") is True
    assert project["net_settings"]["netclass_patterns"] == []
    assert [c["name"] for c in project["net_settings"]["classes"]] == ["Default"]


def test_deleting_a_missing_class_reports_no_change(project) -> None:
    assert delete_net_class(project, "Nope") is False


# --- Net assignment --------------------------------------------------------


def test_patterns_use_the_kicad_schema(project) -> None:
    set_net_class(project, "Power", track_width_mm=0.5)
    assign_patterns(project, "Power", ["VDD:IO", "5V0*"])
    assert project["net_settings"]["netclass_patterns"] == [
        {"pattern": "VDD:IO", "netclass": "Power"},
        {"pattern": "5V0*", "netclass": "Power"},
    ]


def test_assigning_one_class_leaves_other_classes_assignments_intact(project) -> None:
    set_net_class(project, "Power", track_width_mm=0.5)
    set_net_class(project, "HighSpeed", track_width_mm=0.15)
    assign_patterns(project, "Power", ["VDD*"])
    assign_patterns(project, "HighSpeed", ["ETH*"])
    assign_patterns(project, "Power", ["VDD*", "5V*"])  # re-assign Power only

    by_class: dict[str, list[str]] = {}
    for entry in project["net_settings"]["netclass_patterns"]:
        by_class.setdefault(entry["netclass"], []).append(entry["pattern"])
    assert by_class["HighSpeed"] == ["ETH*"]
    assert sorted(by_class["Power"]) == ["5V*", "VDD*"]


def test_assigning_to_an_unknown_class_is_refused(project) -> None:
    with pytest.raises(BoardSetupError, match="No net class named"):
        assign_patterns(project, "Ghost", ["GND"])


# --- Manufacturer constraints ----------------------------------------------


def test_manufacturer_rules_map_onto_kicad_constraint_keys(project) -> None:
    profile = {
        "manufacturer": "JLCPCB",
        "rules": {
            "min_trace_width_mm": 0.127,
            "min_trace_clearance_mm": 0.127,
            "min_drill_mm": 0.3,
            "min_annular_ring_mm": 0.15,
            "copper_to_edge_mm": 0.3,
        },
    }
    applied = apply_manufacturer_rules(project, profile)
    rules = project["board"]["design_settings"]["rules"]
    assert rules["min_track_width"] == 0.127
    assert rules["min_clearance"] == 0.127
    assert rules["min_through_hole_diameter"] == 0.3
    assert rules["min_via_annular_width"] == 0.15
    assert rules["min_copper_edge_clearance"] == 0.3
    assert set(applied) <= set(rules)


def test_profile_without_rules_is_refused(project) -> None:
    with pytest.raises(BoardSetupError, match="no 'rules' section"):
        apply_manufacturer_rules(project, {"manufacturer": "X"})


# --- Round trip ------------------------------------------------------------


def test_round_trip_preserves_unrelated_project_sections(tmp_path: Path, project) -> None:
    """Board Setup edits must not disturb the rest of the project file."""
    project["schematic"] = {"legacy_lib_list": ["keep me"]}
    project["text_variables"] = {"REV": "A"}
    path = tmp_path / "demo.kicad_pro"
    path.write_text(json.dumps(project, indent=2), encoding="utf-8")

    loaded = read_project(path)
    set_net_class(loaded, "Power", track_width_mm=0.5)
    set_track_widths(loaded, [0.2, 0.5])
    write_project(path, loaded)

    reloaded = read_project(path)
    assert reloaded["schematic"] == {"legacy_lib_list": ["keep me"]}
    assert reloaded["text_variables"] == {"REV": "A"}
    assert describe(reloaded)["track_widths_mm"] == [0.2, 0.5]


def test_invalid_json_is_reported_clearly(tmp_path: Path) -> None:
    path = tmp_path / "broken.kicad_pro"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BoardSetupError, match="not valid JSON"):
        read_project(path)
