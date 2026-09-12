"""Headless, dry-run-first manufacturing cleanup adapters."""

from collections.abc import Callable
from typing import Literal

from mcp.server.fastmcp import FastMCP

from ..pcb.file_edits import EditReport
from ..pcb.manufacturing_cleanup import move_silkscreen_to_fab, set_zone_island_policy
from ..utils.sexpr_tree import SList, dump_file, parse
from .metadata import headless_compatible
from .pcb_file_edits import PcbFileEditDependencies, _render


def register(mcp: FastMCP, dependencies: PcbFileEditDependencies) -> None:
    """Expose bounded cleanup without relying on live IPC tool discovery."""

    def apply(mutate: Callable[[SList], EditReport], dry_run: bool, action: str) -> str:
        try:
            if dry_run:
                report = mutate(parse(dependencies.read_board_text()))
                path = str(dependencies.configured_board_file() or "<no board>")
            else:
                reports: list[EditReport] = []

                def mutator(text: str) -> str:
                    tree = parse(text)
                    reports.append(mutate(tree))
                    return dump_file(tree)

                path = dependencies.transactional_board_write(mutator)
                report = reports[0]
        except ValueError as exc:
            return f"Cannot apply {action}: {exc}"
        return _render(report, action, path, dry_run=dry_run)

    @mcp.tool()
    @headless_compatible
    def pcb_move_silkscreen_to_fab(item_ids: list[str], dry_run: bool = True) -> str:
        """Move selected board/footprint silk artwork to same-side Fab, preserving geometry.

        Use UUIDs from DRC or inspection. Rejects missing/ambiguous IDs, copper,
        board edges and locked objects/parents atomically. Defaults to preview;
        pass dry_run=False after review. Does not edit footprint library files.
        This is NOT automatic clipping or proof of correct assembly orientation.
        Render, check pin-1/reference markings, and rerun DRC after applying.
        """
        return apply(
            lambda tree: move_silkscreen_to_fab(tree, item_ids=item_ids),
            dry_run,
            "Move silk to Fab",
        )

    @mcp.tool()
    @headless_compatible
    def pcb_set_zone_island_policy(
        zone_ids: list[str],
        removal: Literal["always", "never", "below_area"] = "always",
        area_min_mm2: float | None = None,
        dry_run: bool = True,
    ) -> str:
        """Set native island removal on selected copper zone UUIDs, then invalidate fill.

        Defaults to preview. below_area requires a finite non-negative mm^2
        threshold. Rejects rule areas, stale IDs and locked zones before writing.
        Does NOT compute/remove islands itself: refill/save with KiCad and rerun
        DRC/unconnected checks afterwards. No clearance rules are relaxed.
        KiCad can retain fill in entirely unconnected zones even with always.
        """
        return apply(
            lambda tree: set_zone_island_policy(
                tree, zone_ids=zone_ids, removal=removal, area_min_mm2=area_min_mm2
            ),
            dry_run,
            "Set zone island policy",
        )
