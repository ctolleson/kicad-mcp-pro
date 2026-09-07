"""Forward and back annotation between schematic and board.

Update PCB from Schematic and Update Schematic from PCB are dialog-only in KiCad:
no ``kicad-cli`` verb, no IPC command, and ``RunAction`` merely opens the window.
They are the last thing standing between a schematic and an unattended board.

``kicad-cli sch export netlist`` supplies the authoritative model, and the rest is
a file edit. Every mutating tool here defaults to ``dry_run=True``: a schematic and
a board can disagree because the *schematic* is wrong, and applying blindly is how
a working board gets broken.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import get_config
from ..project.annotation import (
    AnnotationDiff,
    apply_pad_nets,
    apply_values,
    diff,
    parse_board,
    parse_netlist,
    remove_footprints,
)
from ..utils.sexpr_tree import Atom, SList, dump_file, parse
from .metadata import headless_compatible


class AnnotationError(ValueError):
    """Raised when annotation cannot proceed."""


def _export_netlist() -> SList:
    """Ask KiCad for the schematic's netlist -- the authoritative connectivity."""
    from .export_support import _run_cli

    cfg = get_config()
    if cfg.sch_file is None or not cfg.sch_file.exists():
        raise AnnotationError("No schematic is configured. Call kicad_set_project() first.")
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / "netlist.net"
        code, _out, err = _run_cli("sch", "export", "netlist", "-o", str(target), str(cfg.sch_file))
        if code != 0 or not target.exists():
            raise AnnotationError(f"Could not export the schematic netlist: {err.strip()}")
        return parse(target.read_text(encoding="utf-8", errors="ignore"))


def _board_tree() -> tuple[SList, Path]:
    cfg = get_config()
    if cfg.pcb_file is None or not cfg.pcb_file.exists():
        raise AnnotationError("No PCB file is configured. Call kicad_set_project() first.")
    return parse(cfg.pcb_file.read_text(encoding="utf-8", errors="ignore")), cfg.pcb_file


def _render_diff(delta: AnnotationDiff) -> list[str]:
    lines: list[str] = []
    if delta.in_sync:
        lines.append("**The board and schematic agree.** Nothing to annotate.")
        return lines
    lines.append(f"**{delta.total_differences} difference(s).**")
    if delta.missing_on_board:
        lines.extend(["", "### In the schematic, missing from the board"])
        lines.append(", ".join(f"`{r}`" for r in delta.missing_on_board))
    if delta.extra_on_board:
        lines.extend(["", "### On the board, absent from the schematic"])
        lines.append(", ".join(f"`{r}`" for r in delta.extra_on_board))
    if delta.footprint_mismatches:
        lines.extend(["", "### Footprint differences", ""])
        lines.extend(
            f"- `{ref}`: board `{board}` vs schematic `{sch}`"
            for ref, board, sch in delta.footprint_mismatches
        )
    if delta.value_mismatches:
        lines.extend(["", "### Value differences", ""])
        lines.extend(
            f"- `{ref}`: board `{board or '<empty>'}` vs schematic `{sch}`"
            for ref, board, sch in delta.value_mismatches[:20]
        )
        if len(delta.value_mismatches) > 20:
            lines.append(f"- … and {len(delta.value_mismatches) - 20} more")
    if delta.pad_net_changes:
        lines.extend(["", "### Connectivity differences", ""])
        for change in delta.pad_net_changes[:25]:
            lines.append(
                f"- `{change.reference}.{change.pad}`: board "
                f"`{change.board_net or '<none>'}` vs schematic "
                f"`{change.schematic_net}` (would write `{change.resolved_net or '<none>'}`)"
            )
        if len(delta.pad_net_changes) > 25:
            lines.append(f"- … and {len(delta.pad_net_changes) - 25} more")
        lines.extend(
            [
                "",
                "> Connectivity differences mean one side is wrong. Check which before "
                "applying: the schematic is not automatically right.",
            ]
        )
    return lines


def register(mcp: FastMCP) -> None:
    """Register forward and back annotation tools."""

    @mcp.tool()
    @headless_compatible
    def pcb_compare_with_schematic() -> str:
        """Compare the board against the schematic — the schematic-to-PCB gate.

        Reports components present on only one side, footprint and value
        differences, and pad-level connectivity differences. Read-only.

        Nets are matched by pad identity, not by name: board net names are not
        derivable from schematic net names, because a KiCad-native hierarchical
        design keeps the full sheet path while an imported board may carry only the
        leaf label for the same net.
        """
        try:
            netlist = _export_netlist()
            board, _path = _board_tree()
        except AnnotationError as exc:
            return f"Cannot compare: {exc}"
        delta = diff(parse_netlist(netlist), parse_board(board))
        return "\n".join(["# Schematic vs board", "", *_render_diff(delta)])

    @mcp.tool()
    def pcb_update_from_schematic(
        update_nets: bool = True,
        update_values: bool = False,
        remove_extra_footprints: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Apply the schematic to the board (Tools > Update PCB from Schematic).

        Defaults to a dry run because a disagreement does not tell you which side is
        wrong — a board can be correct and its schematic stale. Review
        pcb_compare_with_schematic() first, then pass dry_run=False.

        Adding footprints for components missing from the board is not supported;
        those are reported so you can place them. `remove_extra_footprints` deletes
        board footprints the schematic no longer has, and is off by default because
        it is destructive.
        """
        try:
            netlist = _export_netlist()
            board, path = _board_tree()
        except AnnotationError as exc:
            return f"Cannot update: {exc}"

        delta = diff(parse_netlist(netlist), parse_board(board))
        lines = ["# Update PCB from schematic", ""]
        if delta.in_sync:
            return "\n".join([*lines, "The board already matches the schematic."])

        planned: list[str] = []
        if update_nets and delta.pad_net_changes:
            planned.append(f"{len(delta.pad_net_changes)} pad net assignment(s)")
        if update_values and delta.value_mismatches:
            planned.append(f"{len(delta.value_mismatches)} component value(s)")
        if remove_extra_footprints and delta.extra_on_board:
            planned.append(f"{len(delta.extra_on_board)} footprint removal(s)")

        if dry_run:
            lines.append("**Dry run — the board was not modified.**")
            lines.append("")
            lines.append("Would apply: " + (", ".join(planned) if planned else "nothing"))
            lines.extend(["", *_render_diff(delta)])
            lines.extend(["", "Pass dry_run=False to write these changes."])
            return "\n".join(lines)

        applied: list[str] = []
        if update_nets and delta.pad_net_changes:
            count = apply_pad_nets(board, delta.pad_net_changes)
            applied.append(f"{count} pad net assignment(s)")
        if update_values and delta.value_mismatches:
            count = apply_values(board, delta.value_mismatches)
            applied.append(f"{count} component value(s)")
        if remove_extra_footprints and delta.extra_on_board:
            count = remove_footprints(board, delta.extra_on_board)
            applied.append(f"{count} footprint(s) removed")

        if not applied:
            return "\n".join([*lines, "Nothing selected to apply; the board is unchanged."])

        path.write_text(dump_file(board), encoding="utf-8")
        lines.append(f"Board updated: `{path}`")
        lines.extend(["", *(f"- {item}" for item in applied)])
        if delta.missing_on_board:
            lines.extend(
                [
                    "",
                    "Still missing from the board (place these yourself): "
                    + ", ".join(f"`{r}`" for r in delta.missing_on_board),
                ]
            )
        lines.extend(["", "Run run_drc() to confirm the board is still clean."])
        return "\n".join(lines)

    @mcp.tool()
    def sch_update_from_pcb(
        update_footprints: bool = True,
        update_values: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Push board-side changes back to the schematic (Tools > Update Schematic from PCB).

        Copies the board's footprint assignments — and optionally values — onto the
        matching schematic symbols. Use after changing footprints in the PCB editor
        so the schematic stops disagreeing.

        Connectivity is never pushed backwards: the schematic defines the netlist,
        and rewriting it from board copper would invert the source of truth.
        """
        cfg = get_config()
        try:
            netlist = _export_netlist()
            board_tree, _ = _board_tree()
        except AnnotationError as exc:
            return f"Cannot update: {exc}"
        if cfg.sch_file is None:
            return "No schematic is configured."

        delta = diff(parse_netlist(netlist), parse_board(board_tree))
        wanted_fp = {ref: board for ref, board, _sch in delta.footprint_mismatches}
        wanted_val = {ref: board for ref, board, _sch in delta.value_mismatches}
        targets = (wanted_fp if update_footprints else {}) | (wanted_val if update_values else {})
        if not targets:
            return "The schematic already matches the board for the selected fields."

        lines = ["# Update schematic from board", ""]
        if dry_run:
            lines.append("**Dry run — the schematic was not modified.**")
            lines.append("")
            if update_footprints:
                lines.extend(
                    f"- `{ref}` footprint -> `{value}`" for ref, value in wanted_fp.items()
                )
            if update_values:
                lines.extend(f"- `{ref}` value -> `{value}`" for ref, value in wanted_val.items())
            lines.extend(["", "Pass dry_run=False to write these changes."])
            return "\n".join(lines)

        schematic = parse(cfg.sch_file.read_text(encoding="utf-8", errors="ignore"))
        changed = 0
        for symbol in schematic.children("symbol"):
            reference = ""
            props: dict[str, SList] = {}
            for prop in symbol.children("property"):
                fields = [a.value for a in prop[1:] if isinstance(a, Atom)]
                if not fields:
                    continue
                props[fields[0]] = prop
                if fields[0] == "Reference" and len(fields) > 1:
                    reference = fields[1]
            if not reference:
                continue
            if update_footprints and reference in wanted_fp:
                footprint_prop = props.get("Footprint")
                if (
                    footprint_prop is not None
                    and len(footprint_prop) > 2
                    and isinstance(footprint_prop[2], Atom)
                ):
                    footprint_prop[2] = Atom(wanted_fp[reference], True)
                    changed += 1
            if update_values and reference in wanted_val:
                value_prop = props.get("Value")
                if (
                    value_prop is not None
                    and len(value_prop) > 2
                    and isinstance(value_prop[2], Atom)
                ):
                    value_prop[2] = Atom(wanted_val[reference], True)
                    changed += 1

        if not changed:
            return "No matching schematic symbols were found; nothing changed."
        cfg.sch_file.write_text(dump_file(schematic), encoding="utf-8")
        return "\n".join(
            [
                *lines,
                f"Schematic updated: `{cfg.sch_file}`",
                "",
                f"- {changed} field(s) written",
                "",
                "Run run_erc() to confirm the schematic is still clean.",
            ]
        )
