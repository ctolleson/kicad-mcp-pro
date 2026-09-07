"""Parsing a board stackup straight out of the .kicad_pcb text.

This is the headless read path: with no KiCad running, the stackup must still be
recoverable from the file. KiCad writes stackup layers inside
``(setup (stackup ...))`` as ``(layer "F.Cu"`` — the bare name, with no ordinal
index, and dielectrics as the quoted name ``"dielectric 1"``. A parser that
required an index (the indexed form only appears in the board's top-level
``(layers ...)`` list) matched nothing and made every headless stackup read fail.
"""

from __future__ import annotations

import pytest

from kicad_mcp.models.pcb import StackupLayerSpec
from kicad_mcp.tools.pcb import _parse_stackup_specs_from_board_text

KICAD_10_BOARD = """\
(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(general
\t\t(thickness 1.6)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.SilkS"
\t\t\t\t(type "Top Silk Screen")
\t\t\t)
\t\t\t(layer "F.Paste"
\t\t\t\t(type "Top Solder Paste")
\t\t\t)
\t\t\t(layer "F.Mask"
\t\t\t\t(type "Top Solder Mask")
\t\t\t\t(thickness 0.01)
\t\t\t)
\t\t\t(layer "F.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.035)
\t\t\t)
\t\t\t(layer "dielectric 1"
\t\t\t\t(type "prepreg")
\t\t\t\t(thickness 0.0994)
\t\t\t\t(material "2116 RC58%")
\t\t\t\t(epsilon_r 4.45)
\t\t\t\t(loss_tangent 0.02)
\t\t\t)
\t\t\t(layer "In1.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.0152)
\t\t\t)
\t\t\t(layer "dielectric 2"
\t\t\t\t(type "core")
\t\t\t\t(thickness 1.51)
\t\t\t\t(material "FR4")
\t\t\t\t(epsilon_r 4.6)
\t\t\t\t(loss_tangent 0.02)
\t\t\t)
\t\t\t(layer "B.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.035)
\t\t\t)
\t\t\t(layer "B.Mask"
\t\t\t\t(type "Bottom Solder Mask")
\t\t\t\t(thickness 0.01)
\t\t\t)
\t\t)
\t)
)
"""


@pytest.fixture(scope="module")
def specs() -> list[StackupLayerSpec]:
    parsed = _parse_stackup_specs_from_board_text(KICAD_10_BOARD)
    assert parsed is not None, "KiCad 10 stackup block failed to parse"
    return parsed


def test_unindexed_copper_layers_are_parsed(specs) -> None:
    copper = [spec for spec in specs if spec.type == "copper"]
    assert [spec.name for spec in copper] == ["F_Cu", "In1_Cu", "B_Cu"]


def test_quoted_dielectric_names_are_normalised(specs) -> None:
    dielectrics = [spec for spec in specs if spec.name.startswith("dielectric_")]
    assert [spec.name for spec in dielectrics] == ["dielectric_1", "dielectric_2"]


def test_dielectric_material_properties_are_captured(specs) -> None:
    prepreg = next(spec for spec in specs if spec.name == "dielectric_1")
    assert prepreg.type == "prepreg"
    assert prepreg.material == "2116 RC58%"
    assert prepreg.epsilon_r == pytest.approx(4.45)
    assert prepreg.loss_tangent == pytest.approx(0.02)


def test_thicknessless_layers_are_skipped(specs) -> None:
    """Silkscreen and paste carry no thickness and must not enter the stack."""
    names = {spec.name for spec in specs}
    assert "F_SilkS" not in names
    assert "F_Paste" not in names


def test_solder_mask_layers_are_kept(specs) -> None:
    names = {spec.name for spec in specs}
    assert {"F_Mask", "B_Mask"} <= names


def test_total_thickness_matches_the_layer_sum(specs) -> None:
    from kicad_mcp.tools.pcb import _total_stackup_thickness_mm

    assert _total_stackup_thickness_mm(specs) == pytest.approx(1.7146)


def test_legacy_unquoted_dielectric_form_still_parses() -> None:
    """Older boards wrote `(layer dielectric 1` unquoted; keep reading those."""
    legacy = KICAD_10_BOARD.replace('(layer "dielectric 1"', "(layer dielectric 1")
    parsed = _parse_stackup_specs_from_board_text(legacy)
    assert parsed is not None
    assert any(spec.name == "dielectric_1" for spec in parsed)


def test_board_without_a_stackup_returns_none() -> None:
    assert _parse_stackup_specs_from_board_text("(kicad_pcb (version 20241229))") is None
