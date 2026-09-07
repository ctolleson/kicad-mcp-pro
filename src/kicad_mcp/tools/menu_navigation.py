"""MCP tools for reaching KiCad's menu surface headlessly.

KiCad's GUI has ~350 distinct menu commands across eight application frames. These
tools let an agent ask what those commands are, whether each one can be driven
without the GUI, and — where kicad-cli is the path — actually run it.

The underlying data is extracted from KiCad's own C++ sources, so the menu tree is
KiCad's, not an approximation of it.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..menus import coverage, find_actions, frame_ids, get_action, get_index, render_tree
from ..menus.catalog import CHANNEL_ORDER, STATUS_ORDER, MenuAction
from ..menus.execution import MenuInvocationError, cli_options, invoke
from .metadata import headless_compatible

_STATUS_LABEL = {
    "covered": "covered",
    "partial": "partial",
    "gap": "gap",
    "gui-only": "GUI-only",
}


def _format_action(action: MenuAction, *, verbose: bool = False) -> list[str]:
    """Render one menu command as markdown lines."""
    lines = [f"**{action.label}** — `{action.action_name}`"]
    lines.append(
        f"  - status `{_STATUS_LABEL[action.status]}` via channel `{action.channel}`"
    )
    for location in action.menu_paths:
        lines.append(f"  - menu: {location}")
    if action.mcp_tool:
        lines.append(f"  - MCP tool: `{action.mcp_tool}`")
    if action.cli_command:
        lines.append(f"  - kicad-cli: `kicad-cli {' '.join(action.cli_command)}`")
    if verbose:
        if action.default_hotkey:
            lines.append(f"  - default hotkey: `{action.default_hotkey}`")
        if action.tooltip:
            lines.append(f"  - KiCad tooltip: {action.tooltip}")
    if action.notes:
        lines.append(f"  - {action.notes}")
    return lines


def register(mcp: FastMCP) -> None:
    """Register the KiCad menu-surface tools."""

    @mcp.tool()
    @headless_compatible
    def kicad_menu_frames() -> str:
        """List KiCad's application windows and how much of each menu bar is automatable.

        Start here to see which KiCad frame (PCB Editor, Schematic Editor, Symbol
        Editor, ...) owns the command you are looking for, then call kicad_menu_tree()
        or kicad_menu_search().
        """
        index = get_index()
        overall = coverage()
        lines = [
            f"# KiCad {index['kicad_version']} menu surface",
            "",
            (
                f"{overall['total']} distinct menu commands. "
                f"{overall['covered']} are driven by an MCP tool, {overall['partial']} partly, "
                f"{overall['gap']} have a headless path with no tool yet, and "
                f"{overall['gui_only']} are GUI-only in KiCad itself."
            ),
            "",
            (
                f"Headless coverage: **{overall['coverage_pct']}%** fully driven "
                f"({overall['driven_pct']}% at least partly) of the "
                f"{overall['denominator']} commands KiCad exposes outside the GUI."
            ),
            "",
            "## Frames",
        ]
        for frame_id, stats in index["coverage"]["frames"].items():
            lines.append(
                f"- `{frame_id}` — {stats['title']}: {stats['total']} commands, "
                f"{stats['coverage_pct']}% covered "
                f"({stats['covered']} covered, {stats['partial']} partial, "
                f"{stats['gap']} gap, {stats['gui_only']} GUI-only)"
            )
        lines.extend(
            [
                "",
                "Call kicad_menu_tree(frame) for one frame's menus, "
                "kicad_menu_search(query) to find a command, or "
                "kicad_menu_invoke(action) to run one headlessly.",
            ]
        )
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def kicad_menu_tree(frame: str, max_depth: int = 3, annotate: bool = True) -> str:
        """Show one KiCad frame's full menu tree, marked with what can be automated.

        Marks: `+` an MCP tool drives it, `~` partial, `!` a headless path exists but
        no tool drives it yet, `.` GUI-only in KiCad. Pass annotate=False for the bare
        menu structure. Frame ids come from kicad_menu_frames().
        """
        try:
            tree = render_tree(frame, max_depth=max_depth, annotate=annotate)
        except KeyError:
            return f"Unknown frame '{frame}'. Available: {', '.join(frame_ids())}"
        legend = (
            "\n\nLegend: + covered · ~ partial · ! headless path, no tool yet · . GUI-only"
        )
        return tree + legend

    @mcp.tool()
    @headless_compatible
    def kicad_menu_search(
        query: str = "",
        frame: str = "",
        channel: str = "",
        status: str = "",
        limit: int = 25,
    ) -> str:
        """Find KiCad menu commands by name, menu path, tooltip or bound tool.

        Filter with frame (e.g. `pcb_editor`), channel (`mcp`, `cli`, `file`, `ipc`,
        `gui-only`) or status (`covered`, `partial`, `gap`, `gui-only`). Searching with
        status="gap" lists what KiCad can do headlessly that no tool here drives yet.
        """
        if channel and channel not in CHANNEL_ORDER:
            return f"Unknown channel '{channel}'. Use one of: {', '.join(CHANNEL_ORDER)}"
        if status and status not in STATUS_ORDER:
            return f"Unknown status '{status}'. Use one of: {', '.join(STATUS_ORDER)}"
        if frame and frame not in frame_ids():
            return f"Unknown frame '{frame}'. Available: {', '.join(frame_ids())}"

        matches = find_actions(
            query, frame=frame, channel=channel, status=status, limit=max(1, limit)
        )
        if not matches:
            return f"No KiCad menu command matches '{query}'."

        total = len(find_actions(query, frame=frame, channel=channel, status=status))
        header = f"# {total} matching menu command(s)"
        if total > len(matches):
            header += f" — showing the first {len(matches)}"
        lines = [header, ""]
        for action in matches:
            lines.extend(_format_action(action))
            lines.append("")
        return "\n".join(lines).rstrip()

    @mcp.tool()
    @headless_compatible
    def kicad_menu_describe(action: str) -> str:
        """Explain one KiCad menu command and exactly how to drive it without the GUI.

        Accepts an action name (`pcbnew.DRCTool.runDRC`), a menu label
        (`Design Rules Checker`) or a menu path (`Inspect > Design Rules Checker`).
        """
        resolved = get_action(action)
        if resolved is None:
            suggestions = find_actions(action, limit=5)
            hint = ""
            if suggestions:
                names = ", ".join(f"`{item.label}`" for item in suggestions)
                hint = f" Closest matches: {names}."
            return f"No KiCad menu command matches '{action}'.{hint}"

        lines = _format_action(resolved, verbose=True)
        lines.append("")
        if resolved.channel == "mcp":
            lines.append(f"**How to run it:** call `{resolved.mcp_tool}`.")
        elif resolved.channel == "cli":
            options = sorted(cli_options(resolved.cli_command))
            lines.append(
                f"**How to run it:** `kicad_menu_invoke('{resolved.action_name}')`, "
                f"which runs `kicad-cli {' '.join(resolved.cli_command)}`."
            )
            if options:
                lines.append(f"  - accepted options: {', '.join(f'`{o}`' for o in options)}")
        elif resolved.channel == "file":
            lines.append(
                "**How to run it:** no tool drives this yet — edit the design file "
                "(.kicad_pcb / .kicad_sch / .kicad_pro) directly."
            )
        elif resolved.channel == "ipc":
            lines.append(
                "**How to run it:** only against a running KiCad over the IPC API; "
                "kicad-cli has no verb for it in this KiCad version."
            )
        else:
            lines.append(
                "**How to run it:** you cannot — KiCad offers no headless path for this "
                "command. This is a KiCad limitation, not a missing feature here."
            )
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def kicad_menu_coverage(frame: str = "", show_gaps: bool = True) -> str:
        """Report how much of KiCad's menu surface this server can drive headlessly.

        Pass a frame id to scope the report. `show_gaps` lists the commands KiCad
        exposes outside the GUI that no tool here drives yet — the actionable backlog.
        """
        try:
            stats = coverage(frame)
        except KeyError:
            return f"Unknown frame '{frame}'. Available: {', '.join(frame_ids())}"

        scope = f"frame `{frame}`" if frame else "all frames"
        lines = [
            f"# KiCad {get_index()['kicad_version']} menu coverage — {scope}",
            "",
            (
                f"- {stats['total']} menu commands total"
                f"\n- {stats['covered']} covered by an MCP tool"
                f"\n- {stats['partial']} partially covered"
                f"\n- {stats['gap']} reachable headlessly but not driven yet"
                f"\n- {stats['gui_only']} GUI-only in KiCad (excluded from the denominator)"
            ),
            "",
            (
                f"**Coverage {stats['coverage_pct']}%** of the {stats['denominator']} "
                f"headlessly-reachable commands ({stats['driven_pct']}% at least partial)."
            ),
        ]
        if show_gaps:
            gaps = find_actions(frame=frame, status="gap")
            if gaps:
                lines.extend(["", f"## Open gaps ({len(gaps)})", ""])
                for action in gaps:
                    note = f" — {action.notes}" if action.notes else ""
                    lines.append(
                        f"- **{action.label}** (`{action.channel}`){note}"
                    )
        return "\n".join(lines)

    @mcp.tool()
    def kicad_menu_invoke(
        action: str,
        options: dict[str, Any] | None = None,
        output: str = "",
        dry_run: bool = False,
    ) -> str:
        """Run a KiCad menu command headlessly through kicad-cli.

        Works for every menu command whose channel is `cli` — check with
        kicad_menu_describe(action). Commands bound to an MCP tool return that tool's
        name instead of running; GUI-only commands say so rather than pretending. Pass
        `options` as kicad-cli long options (`{"format": "svg"}` or `{"--format": "svg"}`),
        validated against the installed kicad-cli's own --help. Use dry_run=True to see
        the exact command first.
        """
        try:
            result = invoke(action, options, output=output, dry_run=dry_run)
        except MenuInvocationError as exc:
            return f"Cannot run '{action}': {exc}"

        lines = [
            f"# {result.label}",
            f"`{result.action_name}` — channel `{result.channel}`, status `{result.status}`",
            "",
            result.summary,
        ]
        if result.command:
            lines.extend(["", "```bash", " ".join(result.command), "```"])
        if result.outputs:
            lines.append("")
            lines.extend(f"Output: `{path}`" for path in result.outputs)
        if result.stdout.strip():
            lines.extend(["", "**stdout**", "```", result.stdout.strip()[:4000], "```"])
        if result.stderr.strip():
            lines.extend(["", "**stderr**", "```", result.stderr.strip()[:4000], "```"])
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def kicad_menu_export_map(frame: str = "") -> str:
        """Return the full menu-to-automation map as JSON, for bulk analysis.

        Emits one row per menu command with its menu paths, channel, status, bound MCP
        tool and kicad-cli command. Useful for auditing coverage or generating docs;
        prefer kicad_menu_search() for ordinary lookups.
        """
        if frame and frame not in frame_ids():
            return f"Unknown frame '{frame}'. Available: {', '.join(frame_ids())}"
        rows = [
            {
                "action": action.action_name,
                "label": action.label,
                "menus": list(action.menu_paths),
                "channel": action.channel,
                "status": action.status,
                "mcp_tool": action.mcp_tool,
                "cli_command": list(action.cli_command),
                "notes": action.notes,
            }
            for action in find_actions(frame=frame)
        ]
        return json.dumps(
            {
                "kicad_version": get_index()["kicad_version"],
                "frame": frame or "all",
                "count": len(rows),
                "commands": rows,
            },
            indent=1,
        )
