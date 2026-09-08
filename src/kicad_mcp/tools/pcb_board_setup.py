"""FastMCP adapters for KiCad's Board Setup dialog.

Board Setup is modal, so none of it is reachable through ``kicad-cli`` or the IPC
API. Everything it edits — net classes, the Pre-defined Sizes lists behind the
track/via toolbar dropdowns, and the design-rule constraints — lives in the
project file, which makes it a file-channel job.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import get_config
from ..project.board_setup import (
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
from .metadata import headless_compatible

_PROFILE_DIR = Path(__file__).resolve().parent.parent / "dfm_profiles"


def _project_path() -> Path:
    cfg = get_config()
    if cfg.project_file is None or not cfg.project_file.exists():
        raise BoardSetupError("No .kicad_pro is configured. Call kicad_set_project() first.")
    return cfg.project_file


def _mm(value: object) -> str:
    return f"{float(value):g} mm" if isinstance(value, int | float) else str(value)


def register(mcp: FastMCP) -> None:
    """Register Board Setup tools."""

    def _edit(mutate: object) -> tuple[Any, Path]:
        path = _project_path()
        project = read_project(path)
        result = mutate(project)  # type: ignore[operator]
        write_project(path, project)
        return result, path

    @mcp.tool()
    @headless_compatible
    def pcb_get_board_setup() -> str:
        """Report Board Setup state: net classes, predefined sizes and constraints.

        Covers Board Setup > Design Rules — the Net Classes table, the Pre-defined
        Sizes lists that fill the track/via/diff-pair toolbar dropdowns, and the
        Constraints page. Read this before editing so you extend rather than replace.
        """
        try:
            state = describe(read_project(_project_path()))
        except BoardSetupError as exc:
            return f"Cannot read Board Setup: {exc}"

        lines = ["# Board Setup", "", "## Net classes"]
        for cls in state["net_classes"]:
            lines.append(
                f"- **{cls.get('name')}** — track {_mm(cls.get('track_width'))}, "
                f"clearance {_mm(cls.get('clearance'))}, "
                f"via {_mm(cls.get('via_diameter'))}/{_mm(cls.get('via_drill'))}, "
                f"diff pair {_mm(cls.get('diff_pair_width'))} gap "
                f"{_mm(cls.get('diff_pair_gap'))}"
            )
        patterns = state["netclass_patterns"]
        if patterns:
            lines.extend(["", "## Net assignments"])
            for pat in patterns:
                lines.append(f"- `{pat.get('pattern')}` -> **{pat.get('netclass')}**")
        else:
            lines.extend(["", "No net-class patterns: every net uses Default."])

        lines.extend(["", "## Pre-defined sizes"])
        widths = state["track_widths_mm"]
        vias = state["via_dimensions"]
        pairs = state["diff_pair_dimensions"]
        lines.append(f"- Tracks: {', '.join(_mm(w) for w in widths) if widths else '(none)'}")
        lines.append(
            "- Vias: "
            + (
                ", ".join(f"{_mm(v.get('diameter'))}/{_mm(v.get('drill'))}" for v in vias)
                if vias
                else "(none)"
            )
        )
        lines.append(
            "- Diff pairs: "
            + (
                ", ".join(f"{_mm(p.get('width'))} gap {_mm(p.get('gap'))}" for p in pairs)
                if pairs
                else "(none)"
            )
        )
        if not (widths or vias or pairs):
            lines.append("  (empty lists mean the toolbar offers only the netclass values)")

        rules = state["rules"]
        if rules:
            lines.extend(["", "## Constraints"])
            for key in sorted(rules):
                lines.append(f"- {key}: {_mm(rules[key])}")
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def pcb_set_predefined_sizes(
        track_widths_mm: list[float] | None = None,
        vias: list[dict[str, float]] | None = None,
        diff_pairs: list[dict[str, float]] | None = None,
    ) -> str:
        """Set the Pre-defined Sizes lists (Board Setup > Design Rules > Pre-defined Sizes).

        These fill the track/via/diff-pair dropdowns so a designer can switch sizes
        while routing. `vias` entries take `diameter_mm` and `drill_mm`; `diff_pairs`
        take `width_mm`, `gap_mm` and optional `via_gap_mm`. Each list given is
        replaced wholesale; omit a list to leave it alone. KiCad treats the netclass
        value as the implicit first entry, so these are the extra choices.
        """
        if track_widths_mm is None and vias is None and diff_pairs is None:
            return "Nothing to do: supply track_widths_mm, vias and/or diff_pairs."

        def mutate(project: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            if track_widths_mm is not None:
                out["tracks"] = set_track_widths(project, track_widths_mm)
            if vias is not None:
                out["vias"] = set_via_dimensions(project, vias)
            if diff_pairs is not None:
                out["diff_pairs"] = set_diff_pair_dimensions(project, diff_pairs)
            return out

        try:
            result, path = _edit(mutate)
        except BoardSetupError as exc:
            return f"Pre-defined sizes unchanged: {exc}"

        lines = ["# Pre-defined sizes updated", "", f"Project: `{path}`", ""]
        if "tracks" in result:
            lines.append(f"- Tracks: {', '.join(_mm(w) for w in result['tracks']) or '(cleared)'}")
        if "vias" in result:
            rendered = ", ".join(f"{_mm(v['diameter'])}/{_mm(v['drill'])}" for v in result["vias"])
            lines.append(f"- Vias: {rendered or '(cleared)'}")
        if "diff_pairs" in result:
            rendered = ", ".join(
                f"{_mm(p['width'])} gap {_mm(p['gap'])}" for p in result["diff_pairs"]
            )
            lines.append(f"- Diff pairs: {rendered or '(cleared)'}")
        lines.append("")
        lines.append("Reload the board in KiCad to see the new dropdown entries.")
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def pcb_define_net_class(
        name: str,
        track_width_mm: float | None = None,
        clearance_mm: float | None = None,
        via_diameter_mm: float | None = None,
        via_drill_mm: float | None = None,
        diff_pair_width_mm: float | None = None,
        diff_pair_gap_mm: float | None = None,
        diff_pair_via_gap_mm: float | None = None,
    ) -> str:
        """Create or update a net class definition (Board Setup > Net Classes).

        Only the values you pass are changed, so this edits an existing class without
        disturbing its other fields. A class that does not exist is created with
        KiCad's defaults for anything unspecified. Assign nets to it with
        pcb_assign_nets_to_class().
        """
        try:
            result, path = _edit(
                lambda project: set_net_class(
                    project,
                    name,
                    track_width_mm=track_width_mm,
                    clearance_mm=clearance_mm,
                    via_diameter_mm=via_diameter_mm,
                    via_drill_mm=via_drill_mm,
                    diff_pair_width_mm=diff_pair_width_mm,
                    diff_pair_gap_mm=diff_pair_gap_mm,
                    diff_pair_via_gap_mm=diff_pair_via_gap_mm,
                )
            )
        except BoardSetupError as exc:
            return f"Net class unchanged: {exc}"
        return "\n".join(
            [
                f"# Net class '{result.get('name')}'",
                "",
                f"Project: `{path}`",
                "",
                f"- track width: {_mm(result.get('track_width'))}",
                f"- clearance: {_mm(result.get('clearance'))}",
                f"- via: {_mm(result.get('via_diameter'))} / drill {_mm(result.get('via_drill'))}",
                f"- diff pair: {_mm(result.get('diff_pair_width'))}, "
                f"gap {_mm(result.get('diff_pair_gap'))}, "
                f"via gap {_mm(result.get('diff_pair_via_gap'))}",
            ]
        )

    @mcp.tool()
    @headless_compatible
    def pcb_delete_net_class(name: str) -> str:
        """Remove a net class and any net patterns that point at it.

        The Default class is KiCad's fallback for every unassigned net and cannot be
        removed.
        """
        try:
            removed, path = _edit(lambda project: delete_net_class(project, name))
        except BoardSetupError as exc:
            return f"Net class unchanged: {exc}"
        if not removed:
            return f"No net class named '{name}'; nothing changed."
        return f"Removed net class '{name}' and its patterns from `{path}`."

    @mcp.tool()
    @headless_compatible
    def pcb_assign_nets_to_class(net_class: str, patterns: list[str]) -> str:
        """Assign nets to a net class by name pattern.

        Patterns use KiCad's net matcher: `GND` matches exactly, `VDD*` globs. This
        replaces the patterns for this class only; other classes keep theirs. Nets
        matching nothing stay on Default.
        """
        try:
            added, path = _edit(lambda project: assign_patterns(project, net_class, patterns))
        except BoardSetupError as exc:
            return f"Assignment unchanged: {exc}"
        if not added:
            return f"Cleared all net patterns for '{net_class}' in `{path}`."
        listed = ", ".join(f"`{p['pattern']}`" for p in added)
        return f"Assigned {len(added)} pattern(s) to **{net_class}**: {listed}\n\nProject: `{path}`"

    @mcp.tool()
    @headless_compatible
    def pcb_apply_manufacturer_rules(manufacturer: str = "jlcpcb_standard") -> str:
        """Apply a fab house's capability minimums to the Constraints page.

        Writes the profile's minimum trace width, clearance, drill, annular ring,
        edge clearance and silkscreen text height into Board Setup > Design Rules >
        Constraints, so DRC fails anything the fab cannot build. These are
        manufacturing *limits*, not design targets — set your working values through
        net classes, comfortably above them.
        """
        profile_file = _PROFILE_DIR / f"{manufacturer}.json"
        if not profile_file.exists():
            available = ", ".join(sorted(p.stem for p in _PROFILE_DIR.glob("*.json")))
            return f"Unknown profile '{manufacturer}'. Available: {available}"
        profile = json.loads(profile_file.read_text(encoding="utf-8"))
        try:
            applied, path = _edit(lambda project: apply_manufacturer_rules(project, profile))
        except BoardSetupError as exc:
            return f"Constraints unchanged: {exc}"
        lines = [
            f"# Constraints from {profile.get('manufacturer', manufacturer)}"
            f" ({profile.get('tier', 'standard')})",
            "",
            f"Project: `{path}`",
            "",
        ]
        lines.extend(f"- {key}: {_mm(value)}" for key, value in sorted(applied.items()))
        lines.extend(
            [
                "",
                "These are fab limits. Set your working track and via sizes in the net "
                "classes, above these minimums.",
            ]
        )
        return "\n".join(lines)
