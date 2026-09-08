"""FastMCP adapters for board-file bulk edits.

These cover KiCad menu commands that exist only as modal dialogs — Swap Layers,
Global Deletions, Cleanup Tracks & Vias and the Zone Manager — and so have no
``kicad-cli`` verb and no IPC command. The board file is the only headless route.

Each mutating tool takes ``dry_run``: these operations can remove thousands of
items in one call, so an agent should be able to see the count before committing.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from mcp.server.fastmcp import FastMCP

from ..pcb.file_edits import (
    EditReport,
    board_net_names,
    board_outline_rectangle,
    cleanup_tracks_and_vias,
    create_zone,
    global_delete,
    list_zones,
    set_zone_properties,
    swap_layers,
)
from ..utils.sexpr_tree import SList, dump_file, parse
from .export_support import _run_cli
from .metadata import headless_compatible


class BoardTextTransaction(Protocol):
    """Apply one validated board-text mutation atomically."""

    def __call__(self, mutator: Callable[[str], str]) -> str: ...


@dataclass(frozen=True)
class PcbFileEditDependencies:
    """Board-file access injected by the PCB composition root."""

    transactional_board_write: BoardTextTransaction
    read_board_text: Callable[[], str]
    configured_board_file: Callable[[], Path | None]


def _render(report: EditReport, action: str, board: str, *, dry_run: bool) -> str:
    if not report.total:
        return f"{action}: nothing matched — the board is unchanged."
    status = (
        f"**Dry run** — nothing was written to `{board}`."
        if dry_run
        else f"Board updated: `{board}`"
    )
    lines = [f"# {action}", "", status, ""]
    lines.extend(f"- {key}: {value}" for key, value in sorted(report.counts.items()))
    lines.append(f"- **total items affected: {report.total}**")
    lines.extend(report.details)
    return "\n".join(lines)


def register(mcp: FastMCP, dependencies: PcbFileEditDependencies) -> None:
    """Register board-file bulk-edit tools."""
    write = dependencies.transactional_board_write
    read = dependencies.read_board_text
    board_path = dependencies.configured_board_file

    def _apply(mutate: Callable[[SList], EditReport], dry_run: bool) -> tuple[EditReport, str]:
        """Run a tree mutation, persisting only when this is not a dry run."""
        if dry_run:
            configured = board_path()
            return mutate(parse(read())), str(configured) if configured else "<no board>"
        captured: list[EditReport] = []

        def mutator(text: str) -> str:
            tree = parse(text)
            captured.append(mutate(tree))
            return dump_file(tree)

        path = write(mutator)
        return captured[0], path

    @mcp.tool()
    @headless_compatible
    def pcb_swap_layers(mapping: dict[str, str], dry_run: bool = False) -> str:
        """Move board items between layers (Edit > Swap Layers).

        `mapping` is applied simultaneously, so {"F.Cu": "B.Cu", "B.Cu": "F.Cu"} is a
        true swap rather than two renames collapsing onto one layer. Layer definitions
        in the stackup are untouched — this moves objects, it does not rename layers.
        Wildcard pad layers (`*.Cu`, `F&B.Cu`) are left alone.
        """
        try:
            report, path = _apply(lambda tree: swap_layers(tree, mapping), dry_run)
        except ValueError as exc:
            return f"Cannot swap layers: {exc}"
        return _render(report, "Swap layers", path, dry_run=dry_run)

    @mcp.tool()
    @headless_compatible
    def pcb_global_delete(
        item_types: list[str],
        layers: list[str] | None = None,
        nets: list[str] | None = None,
        include_locked: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Delete board items in bulk by class (Edit > Global Deletions).

        `item_types` chooses from tracks, vias, zones, graphics, text, dimensions,
        footprints, groups, images. Narrow with `layers` and/or `nets` (net names, or
        numeric codes on pre-KiCad-10 boards). Locked items are kept unless
        `include_locked` is set. Defaults to dry_run=True because this is destructive
        and unbounded — pass dry_run=False to actually delete.

        Operates on top-level board items, like the dialog: silkscreen text belonging
        to a footprint is part of that footprint and is not matched by `text`.
        """
        try:
            report, path = _apply(
                lambda tree: global_delete(
                    tree,
                    item_types=item_types,
                    layers=layers or [],
                    nets=nets or [],
                    locked=None if include_locked else False,
                ),
                dry_run,
            )
        except ValueError as exc:
            return f"Cannot delete: {exc}"
        return _render(report, "Global deletions", path, dry_run=dry_run)

    @mcp.tool()
    @headless_compatible
    def pcb_cleanup_tracks_and_vias(
        delete_zero_length: bool = True,
        delete_duplicate_tracks: bool = True,
        delete_duplicate_vias: bool = True,
        dry_run: bool = False,
    ) -> str:
        """Remove zero-length and duplicated tracks and vias (Tools > Cleanup Tracks & Vias).

        Does not merge collinear segments: that is only safe when the shared endpoint
        carries no other connection, which needs full connectivity analysis, so it
        stays a KiCad-side operation rather than a half-correct file edit.
        """
        report, path = _apply(
            lambda tree: cleanup_tracks_and_vias(
                tree,
                delete_zero_length=delete_zero_length,
                delete_duplicate_tracks=delete_duplicate_tracks,
                delete_duplicate_vias=delete_duplicate_vias,
            ),
            dry_run,
        )
        return _render(report, "Cleanup tracks and vias", path, dry_run=dry_run)

    @mcp.tool()
    @headless_compatible
    def pcb_list_zones() -> str:
        """List copper zones with net, layers, priority and fill state (Board > Zone Manager)."""
        tree = parse(read())
        zones = list_zones(tree)
        if not zones:
            return "This board has no zones."
        lines = [f"# Zones ({len(zones)})", ""]
        for zone in zones:
            label = f" `{zone['name']}`" if zone["name"] else ""
            layers = cast(list[str], zone["layers"])
            lines.append(
                f"- **[{zone['index']}]**{label} net `{zone['net'] or '<none>'}` on "
                f"{', '.join(layers) or '<no layer>'} — priority {zone['priority']}, "
                f"{'filled' if zone['filled'] else 'not filled'}, "
                f"{zone['outline_polygons']} outline / {zone['filled_polygons']} filled polygons"
            )
        lines.extend(["", "Edit one with pcb_set_zone_properties(index=...)."])
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def pcb_create_zone(
        net: str,
        layers: list[str],
        corners: list[list[float]] | None = None,
        follow_board_outline: bool = False,
        outline_inset_mm: float = 0.5,
        priority: int = 0,
        min_thickness_mm: float = 0.25,
        clearance_mm: float = 0.5,
        thermal_gap_mm: float = 0.5,
        thermal_bridge_width_mm: float = 0.5,
        pad_connection: str = "thermal",
        name: str = "",
        dry_run: bool = False,
    ) -> str:
        """Add a copper zone (pour) to the board file, headlessly.

        Unlike pcb_add_zone, which needs a running KiCad over the IPC API, this writes
        the zone straight into the board file. Give an explicit polygon in `corners`
        ([[x, y], ...] in mm), or set follow_board_outline=True for a full-board pour
        inset from Edge.Cuts by `outline_inset_mm`.

        `pad_connection` is one of thermal, solid, none, thru_hole_only.

        The zone is written UNFILLED. Fill it with pcb_fill_zones(), which is a
        separate step because computing the fill needs KiCad's geometry engine.
        """
        if follow_board_outline and corners:
            return "Give either `corners` or follow_board_outline=True, not both."
        if not follow_board_outline and not corners:
            return "Provide `corners` as [[x, y], ...] in mm, or set follow_board_outline=True."

        def mutate(tree: SList) -> EditReport:
            polygon = (
                board_outline_rectangle(tree, inset=outline_inset_mm)
                if follow_board_outline
                else [(float(point[0]), float(point[1])) for point in (corners or [])]
            )
            return create_zone(
                tree,
                net=net,
                layers=layers,
                polygon=polygon,
                priority=priority,
                min_thickness=min_thickness_mm,
                clearance=clearance_mm,
                thermal_gap=thermal_gap_mm,
                thermal_bridge_width=thermal_bridge_width_mm,
                pad_connection=pad_connection,
                name=name,
            )

        try:
            report, board = _apply(mutate, dry_run)
        except ValueError as exc:
            return f"Zone not created: {exc}"
        return _render(report, "Create copper zone", board, dry_run=dry_run)

    @mcp.tool()
    @headless_compatible
    def pcb_fill_zones() -> str:
        """Fill every zone on the board and save it (Edit > Fill All Zones).

        Runs `kicad-cli pcb drc --refill-zones --save-board`. Both flags matter: plain
        `pcb drc` fills only in memory to run the check, so the board on disk keeps
        whatever fill geometry it already had - a zone can satisfy DRC and still carry
        no copper in the file.
        """
        configured = board_path()
        if configured is None:
            return "No board file is configured. Call kicad_set_project() first."
        before = read().count("(filled_polygon")
        code, _stdout, stderr = _run_cli(
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "-o",
            "/dev/null",
            str(configured),
        )
        if code != 0 and "Saved board" not in (stderr or ""):
            return f"Zone fill failed (exit {code}): {stderr.strip() or 'no output'}"
        after = read().count("(filled_polygon")
        zones = len(list_zones(parse(read())))
        return (
            f"# Fill zones\n\nBoard updated: `{configured}`\n\n"
            f"- zones on the board: {zones}\n"
            f"- filled polygons: {before} -> {after}\n"
        )

    @mcp.tool()
    @headless_compatible
    def pcb_set_zone_properties(
        index: int,
        priority: int | None = None,
        name: str | None = None,
        min_thickness: float | None = None,
        filled: bool | None = None,
    ) -> str:
        """Edit one zone's properties (Board > Zone Manager).

        Address the zone by its index from pcb_list_zones(). Setting `filled=False`
        also drops the stale fill geometry, so the board does not carry fill polygons
        that no longer reflect the outline; re-fill in KiCad or via a DRC run.
        """
        try:
            report, path = _apply(
                lambda tree: set_zone_properties(
                    tree,
                    index=index,
                    priority=priority,
                    name=name,
                    min_thickness=min_thickness,
                    filled=filled,
                ),
                False,
            )
        except ValueError as exc:
            return f"Cannot edit zone: {exc}"
        if not report.total:
            return "No zone properties were supplied; nothing changed."
        return _render(report, f"Zone {index} updated", path, dry_run=False)

    @mcp.tool()
    @headless_compatible
    def pcb_list_nets_on_board() -> str:
        """List every net name carried by board items, for use as a delete/filter key.

        KiCad 10 records nets by name on each item and no longer writes a board-level
        net table, so this reads the names the items actually carry.
        """
        names = sorted(board_net_names(parse(read())))
        if not names:
            return "No nets found on this board."
        head = ", ".join(f"`{n}`" for n in names[:60])
        more = f" … and {len(names) - 60} more." if len(names) > 60 else ""
        return f"# {len(names)} nets\n\n{head}{more}"
