"""Board Setup editing: net classes, predefined sizes and manufacturer rules.

KiCad's Board Setup dialog writes to the project file (``.kicad_pro``), not the
board: the net classes, the Pre-defined Sizes lists that populate the track/via
toolbar dropdowns, and the design-rule constraints all live there as JSON. The
dialog is modal, so none of it is reachable through ``kicad-cli`` or the IPC API —
the project file is the only headless route.

These functions are pure transformations over the parsed project dict, so they are
testable without touching disk. Persisting is the caller's job.
"""

from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

# A netclass KiCad always defines; it cannot be removed, only edited.
DEFAULT_CLASS = "Default"

# Field names as KiCad 10 writes them, with the units they are stored in (mm).
_CLASS_FIELDS = {
    "track_width_mm": "track_width",
    "clearance_mm": "clearance",
    "via_diameter_mm": "via_diameter",
    "via_drill_mm": "via_drill",
    "diff_pair_width_mm": "diff_pair_width",
    "diff_pair_gap_mm": "diff_pair_gap",
    "diff_pair_via_gap_mm": "diff_pair_via_gap",
    "microvia_diameter_mm": "microvia_diameter",
    "microvia_drill_mm": "microvia_drill",
}

# Defaults KiCad fills in for a newly created class, so a class we add looks native.
_NEW_CLASS_TEMPLATE: dict[str, Any] = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "priority": 0,
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.2,
    "tuning_profile": "",
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}


class BoardSetupError(ValueError):
    """Raised when a Board Setup edit would produce an invalid project."""


# ---------------------------------------------------------------------------
# Project-file access
# ---------------------------------------------------------------------------


def read_project(path: Path) -> dict[str, Any]:
    """Parse a ``.kicad_pro`` file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BoardSetupError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BoardSetupError(f"{path.name} does not contain a project object.")
    return payload


def write_project(path: Path, payload: dict[str, Any]) -> Path:
    """Write the project file atomically, in KiCad's own formatting."""
    text = json.dumps(payload, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as handle:
        handle.write(text)
        temp = Path(handle.name)
    temp.replace(path)
    return path


def _design_settings(project: dict[str, Any]) -> dict[str, Any]:
    board = project.setdefault("board", {})
    if not isinstance(board, dict):
        raise BoardSetupError("Project 'board' section is malformed.")
    settings = board.setdefault("design_settings", {})
    if not isinstance(settings, dict):
        raise BoardSetupError("Project 'design_settings' section is malformed.")
    return settings


def _net_settings(project: dict[str, Any]) -> dict[str, Any]:
    settings = project.setdefault("net_settings", {})
    if not isinstance(settings, dict):
        raise BoardSetupError("Project 'net_settings' section is malformed.")
    settings.setdefault("classes", [])
    settings.setdefault("netclass_patterns", [])
    return settings


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def describe(project: dict[str, Any]) -> dict[str, Any]:
    """Summarise the Board Setup state an agent needs before editing it."""
    settings = _design_settings(project)
    nets = _net_settings(project)
    return {
        "track_widths_mm": list(settings.get("track_widths", [])),
        "via_dimensions": [dict(v) for v in settings.get("via_dimensions", [])],
        "diff_pair_dimensions": [dict(d) for d in settings.get("diff_pair_dimensions", [])],
        "net_classes": [dict(c) for c in nets.get("classes", [])],
        "netclass_patterns": [dict(p) for p in nets.get("netclass_patterns", [])],
        "rules": dict(settings.get("rules", {})),
    }


# ---------------------------------------------------------------------------
# Pre-defined sizes  (Board Setup > Design Rules > Pre-defined Sizes)
# ---------------------------------------------------------------------------


def _positive(value: float, label: str) -> float:
    number = float(value)
    if number <= 0:
        raise BoardSetupError(f"{label} must be greater than zero (got {number}).")
    return number


def set_track_widths(project: dict[str, Any], widths_mm: list[float]) -> list[float]:
    """Replace the predefined track-width list that fills the toolbar dropdown.

    KiCad treats the netclass width as the implicit first entry, so the list holds
    the *extra* widths a designer can switch to.
    """
    cleaned = sorted({round(_positive(w, "Track width"), 6) for w in widths_mm})
    _design_settings(project)["track_widths"] = cleaned
    return cleaned


def set_via_dimensions(
    project: dict[str, Any], vias: list[dict[str, float]]
) -> list[dict[str, float]]:
    """Replace the predefined via list. Each entry needs a diameter and a drill."""
    cleaned: list[dict[str, float]] = []
    for via in vias:
        diameter = _positive(via.get("diameter_mm", via.get("diameter", 0)), "Via diameter")
        drill = _positive(via.get("drill_mm", via.get("drill", 0)), "Via drill")
        if drill >= diameter:
            raise BoardSetupError(
                f"Via drill {drill} mm must be smaller than its diameter {diameter} mm; "
                "the difference is the annular ring."
            )
        cleaned.append({"diameter": round(diameter, 6), "drill": round(drill, 6)})
    cleaned.sort(key=lambda v: (v["diameter"], v["drill"]))
    _design_settings(project)["via_dimensions"] = cleaned
    return cleaned


def set_diff_pair_dimensions(
    project: dict[str, Any], pairs: list[dict[str, float]]
) -> list[dict[str, float]]:
    """Replace the predefined differential-pair list (width, gap, optional via gap)."""
    cleaned: list[dict[str, float]] = []
    for pair in pairs:
        width = _positive(pair.get("width_mm", pair.get("width", 0)), "Diff pair width")
        gap = _positive(pair.get("gap_mm", pair.get("gap", 0)), "Diff pair gap")
        raw_via_gap = pair.get("via_gap_mm", pair.get("via_gap", 0)) or 0
        via_gap = float(raw_via_gap)
        if via_gap < 0:
            raise BoardSetupError("Diff pair via gap cannot be negative.")
        cleaned.append(
            {
                "width": round(width, 6),
                "gap": round(gap, 6),
                "via_gap": round(via_gap, 6),
            }
        )
    cleaned.sort(key=lambda p: (p["width"], p["gap"]))
    _design_settings(project)["diff_pair_dimensions"] = cleaned
    return cleaned


# ---------------------------------------------------------------------------
# Net classes  (Board Setup > Design Rules > Net Classes)
# ---------------------------------------------------------------------------


def set_net_class(project: dict[str, Any], name: str, **values: float | None) -> dict[str, Any]:
    """Create or update one net class. Only the values supplied are changed."""
    label = name.strip()
    if not label:
        raise BoardSetupError("A net class needs a name.")

    unknown = sorted(set(values) - set(_CLASS_FIELDS))
    if unknown:
        raise BoardSetupError(
            f"Unknown net-class field(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(_CLASS_FIELDS))}."
        )

    nets = _net_settings(project)
    classes: list[dict[str, Any]] = nets["classes"]
    existing = next((c for c in classes if c.get("name") == label), None)
    if existing is None:
        existing = {**deepcopy(_NEW_CLASS_TEMPLATE), "name": label}
        classes.append(existing)

    for key, raw in values.items():
        if raw is None:
            continue
        existing[_CLASS_FIELDS[key]] = round(_positive(raw, key), 6)

    drill = existing.get("via_drill")
    diameter = existing.get("via_diameter")
    if isinstance(drill, int | float) and isinstance(diameter, int | float) and drill >= diameter:
        raise BoardSetupError(
            f"Net class '{label}': via drill {drill} mm must be smaller than "
            f"via diameter {diameter} mm."
        )
    return dict(existing)


def delete_net_class(project: dict[str, Any], name: str) -> bool:
    """Remove a net class and any patterns pointing at it. Default cannot be removed."""
    label = name.strip()
    if label == DEFAULT_CLASS:
        raise BoardSetupError("The Default net class cannot be removed.")
    nets = _net_settings(project)
    classes: list[dict[str, Any]] = nets["classes"]
    remaining = [c for c in classes if c.get("name") != label]
    if len(remaining) == len(classes):
        return False
    nets["classes"] = remaining
    nets["netclass_patterns"] = [p for p in nets["netclass_patterns"] if p.get("netclass") != label]
    return True


def assign_patterns(
    project: dict[str, Any], net_class: str, patterns: list[str]
) -> list[dict[str, str]]:
    """Point net-name patterns at a net class, replacing that class's existing ones.

    Patterns use KiCad's net matcher, so ``GND`` is exact and ``/CM5/ETH*`` globs.
    Other classes' assignments are untouched.
    """
    label = net_class.strip()
    nets = _net_settings(project)
    if not any(c.get("name") == label for c in nets["classes"]):
        known = ", ".join(sorted(str(c.get("name")) for c in nets["classes"]))
        raise BoardSetupError(f"No net class named '{label}'. Existing classes: {known}.")

    kept = [p for p in nets["netclass_patterns"] if p.get("netclass") != label]
    added = [{"pattern": p.strip(), "netclass": label} for p in patterns if p.strip()]
    nets["netclass_patterns"] = kept + added
    return added


# ---------------------------------------------------------------------------
# Manufacturer constraints  (Board Setup > Design Rules > Constraints)
# ---------------------------------------------------------------------------

# DFM profile key -> project rules key. Only constraints KiCad actually stores.
_RULE_MAP = {
    "min_trace_width_mm": "min_track_width",
    "min_trace_clearance_mm": "min_clearance",
    "min_drill_mm": "min_through_hole_diameter",
    "min_annular_ring_mm": "min_via_annular_width",
    "copper_to_edge_mm": "min_copper_edge_clearance",
    "min_silkscreen_text_height_mm": "min_text_height",
}


def apply_manufacturer_rules(project: dict[str, Any], profile: dict[str, Any]) -> dict[str, float]:
    """Write a DFM profile's capability minimums into the design-rule constraints."""
    rules = profile.get("rules")
    if not isinstance(rules, dict):
        raise BoardSetupError("Manufacturer profile has no 'rules' section.")
    target = _design_settings(project).setdefault("rules", {})
    applied: dict[str, float] = {}
    for source_key, project_key in _RULE_MAP.items():
        value = rules.get(source_key)
        if isinstance(value, int | float) and value > 0:
            target[project_key] = float(value)
            applied[project_key] = float(value)
    return applied


__all__ = [
    "DEFAULT_CLASS",
    "BoardSetupError",
    "apply_manufacturer_rules",
    "assign_patterns",
    "delete_net_class",
    "describe",
    "read_project",
    "set_diff_pair_dimensions",
    "set_net_class",
    "set_track_widths",
    "set_via_dimensions",
    "write_project",
]
