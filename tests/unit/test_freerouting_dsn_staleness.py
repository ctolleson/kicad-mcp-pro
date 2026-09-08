"""A DSN describing a previous board must be refused, not silently routed."""

from __future__ import annotations

from pathlib import Path

from kicad_mcp.utils.freerouting import FreeRoutingRunner


def _board(tmp_path: Path, places: dict[str, tuple[float, float]]) -> Path:
    blocks = "".join(
        f'\t(footprint "L:F"\n\t\t(at {x} {y} 0)\n'
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t)\n\t)\n'
        for ref, (x, y) in places.items()
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(f"(kicad_pcb\n{blocks})")
    return path


def _dsn(tmp_path: Path, places: dict[str, tuple[float, float]], name: str = "b.dsn") -> Path:
    body = "".join(f"      (place {ref} {x} {y} front 0)\n" for ref, (x, y) in places.items())
    path = tmp_path / name
    path.write_text(f"(pcb x\n  (placement\n{body}  )\n)")
    return path


# Specctra writes relative to the aux axis with y negated, in micrometres.
def _as_dsn(places: dict[str, tuple[float, float]], dx: float = 0.46, dy: float = 8.5):
    return {r: ((x - dx) * 1000, -(y - dy) * 1000) for r, (x, y) in places.items()}


LAYOUT = {
    "J1": (122.505, 83.45),
    "J2": (122.505, 108.85),
    "J3": (161.24, 83.45),
    "J4": (119.965, 73.29),
}


def test_a_dsn_matching_the_board_is_current(tmp_path: Path) -> None:
    """The origin shift and unit scale must not by themselves look like staleness."""
    assert FreeRoutingRunner._is_current(_dsn(tmp_path, _as_dsn(LAYOUT)), _board(tmp_path, LAYOUT))


def test_a_dsn_holding_deleted_components_is_stale(tmp_path: Path) -> None:
    """The real case: mounting holes removed from the schematic still in the DSN."""
    with_holes = {**LAYOUT, "H1": (108.995, 63.52), "H2": (173.765, 63.52)}
    assert not FreeRoutingRunner._is_current(
        _dsn(tmp_path, _as_dsn(with_holes)), _board(tmp_path, LAYOUT)
    )


def test_a_dsn_is_stale_when_one_component_has_moved(tmp_path: Path) -> None:
    """Same components, but one relocated - routing it would target the old spot."""
    moved = dict(LAYOUT)
    moved["J4"] = (157.39, 123.0)
    assert not FreeRoutingRunner._is_current(
        _dsn(tmp_path, _as_dsn(moved)), _board(tmp_path, LAYOUT)
    )


def test_a_wholesale_translation_is_not_stale(tmp_path: Path) -> None:
    """Shifting the whole board keeps every relative distance, so it still matches."""
    shifted = {r: (x + 25.0, y - 12.0) for r, (x, y) in LAYOUT.items()}
    assert FreeRoutingRunner._is_current(
        _dsn(tmp_path, _as_dsn(shifted, dx=0, dy=0)), _board(tmp_path, LAYOUT)
    )


def test_a_missing_dsn_is_not_current(tmp_path: Path) -> None:
    assert not FreeRoutingRunner._is_current(tmp_path / "gone.dsn", _board(tmp_path, LAYOUT))
