"""Registration coverage for the board-setup and annotation-sync tools.

``test_tool_metadata_lint`` requires every name in ``TOOL_CATEGORIES`` to appear
somewhere under ``tests/``. These tools had service-level tests but no test that
named the registered MCP tools, so the lint flagged all nine.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from kicad_mcp.tools.annotation_sync import register as register_annotation
from kicad_mcp.tools.pcb_board_setup import register as register_board_setup

BOARD_SETUP_TOOLS = {
    "pcb_get_board_setup",
    "pcb_define_net_class",
    "pcb_delete_net_class",
    "pcb_assign_nets_to_class",
    "pcb_set_predefined_sizes",
    "pcb_apply_manufacturer_rules",
}

ANNOTATION_TOOLS = {
    "pcb_compare_with_schematic",
    "pcb_update_from_schematic",
    "sch_update_from_pcb",
}


def _names(register) -> set[str]:
    server = FastMCP("registration-test")
    register(server)
    return {tool.name for tool in server._tool_manager.list_tools()}


def test_board_setup_registers_exactly_its_tools() -> None:
    assert _names(register_board_setup) == BOARD_SETUP_TOOLS


def test_annotation_sync_registers_exactly_its_tools() -> None:
    assert _names(register_annotation) == ANNOTATION_TOOLS


def test_every_registered_tool_is_declared_in_a_category() -> None:
    """A tool that registers but is not categorized is invisible to every profile."""
    from kicad_mcp.tools.router import TOOL_CATEGORIES

    declared = {name for category in TOOL_CATEGORIES.values() for name in category["tools"]}

    assert (BOARD_SETUP_TOOLS | ANNOTATION_TOOLS) <= declared


def test_the_destructive_sync_tool_defaults_to_a_dry_run() -> None:
    """pcb_update_from_schematic rewrites board nets, so it must not act by default."""
    server = FastMCP("registration-test")
    register_annotation(server)
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert tools["pcb_update_from_schematic"].parameters["properties"]["dry_run"]["default"] is True
