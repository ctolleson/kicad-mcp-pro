"""Placement transforms for symbol pins: rotation, mirroring, embedded libraries.

Locating a pin needs the placement's ``(mirror ...)`` convention, and the sign is
not guessable: mirroring about the X axis cancels the library's y-up to sheet
y-down negation, so symbol y ends up mapping to +y. The expected coordinates
below were confirmed on a real design by exporting the netlist and checking which
coordinate the pin's net actually appeared on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.schematic import get_pin_positions

# A 4-pin crystal: pins 1 and 3 sit on the mirror axis, 2 and 4 off it.
SHEET = """\
(kicad_sch
\t(lib_symbols
\t\t(symbol "AltiumLib:XTAL_4"
\t\t\t(symbol "XTAL_4_1_0"
\t\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t\t(name "1" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t\t\t)
\t\t\t\t(pin passive line (at 5.08 -7.112 90) (length 2.54)
\t\t\t\t\t(name "2" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27))))
\t\t\t\t)
\t\t\t\t(pin passive line (at 7.62 0 180) (length 2.54)
\t\t\t\t\t(name "3" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "3" (effects (font (size 1.27 1.27))))
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    path = tmp_path / "sheet.kicad_sch"
    path.write_text(SHEET, encoding="utf-8")
    return path


def test_pins_come_from_the_sheet_when_the_library_is_not_installed(sheet: Path) -> None:
    """Imported designs rarely ship their libraries; the sheet embeds them anyway."""
    positions = get_pin_positions("AltiumLib", "XTAL_4", 100.0, 100.0, schematic_file=sheet)

    assert set(positions) == {"1", "2", "3"}
    assert positions["1"] == (100.0, 100.0)


def test_without_a_sheet_an_uninstalled_library_yields_nothing(sheet: Path) -> None:
    assert get_pin_positions("AltiumLib", "XTAL_4", 100.0, 100.0) == {}


def test_mirror_x_maps_symbol_y_to_plus_y(sheet: Path) -> None:
    """The sign that matters: mirror x cancels the y-up to y-down negation."""
    plain = get_pin_positions("AltiumLib", "XTAL_4", 100.0, 100.0, schematic_file=sheet)
    mirrored = get_pin_positions(
        "AltiumLib", "XTAL_4", 100.0, 100.0, mirror="x", schematic_file=sheet
    )

    # Pin 2 is at symbol (5.08, -7.112): y negates normally, stays positive mirrored.
    assert plain["2"] == (105.08, 107.112)
    assert mirrored["2"] == (105.08, 92.888)
    # Pins on the axis cannot distinguish the two, which is why they are poor evidence.
    assert plain["1"] == mirrored["1"]
    assert plain["3"] == mirrored["3"]


def test_mirror_y_negates_x(sheet: Path) -> None:
    mirrored = get_pin_positions(
        "AltiumLib", "XTAL_4", 100.0, 100.0, mirror="y", schematic_file=sheet
    )

    assert mirrored["3"] == (92.38, 100.0)


def test_rotation_composes_after_the_mirror(sheet: Path) -> None:
    """A 4-pin crystal placed at rot 270 with mirror x, as found on a real board."""
    positions = get_pin_positions(
        "AltiumLib", "XTAL_4", 153.67, 87.4522, 270, mirror="x", schematic_file=sheet
    )

    assert positions["1"] == (153.67, 87.4522)
    assert positions["2"] == (146.558, 82.3722)
    assert positions["3"] == (153.67, 79.8322)


def test_an_unknown_mirror_is_rejected(sheet: Path) -> None:
    with pytest.raises(ValueError, match="mirror must be"):
        get_pin_positions("AltiumLib", "XTAL_4", 0.0, 0.0, mirror="diagonal", schematic_file=sheet)
