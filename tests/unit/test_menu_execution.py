"""Headless invocation of KiCad menu commands.

``kicad_menu_invoke`` runs real subprocesses and writes real files, so the contract
that matters is: it never claims to have run something it did not, it refuses
options the installed kicad-cli would reject, and it cannot write outside the
workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.errors import UnsafePathError
from kicad_mcp.menus.execution import MenuInvocationError, invoke


@pytest.fixture
def board_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a throwaway project so no real design is touched."""
    board = tmp_path / "demo.kicad_pcb"
    board.write_text('(kicad_pcb (version 20241229) (generator "test"))\n', encoding="utf-8")
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text('(kicad_sch (version 20241229) (generator "test"))\n', encoding="utf-8")

    monkeypatch.setenv("KICAD_MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("KICAD_MCP_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("KICAD_MCP_PCB_FILE", str(board))
    monkeypatch.setenv("KICAD_MCP_SCH_FILE", str(schematic))

    from kicad_mcp.config import reset_config

    reset_config()
    yield tmp_path
    reset_config()


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(MenuInvocationError, match="No KiCad menu command matches"):
        invoke("definitely.not.a.command")


def test_mcp_backed_command_names_its_tool_instead_of_running() -> None:
    result = invoke("eeschema.EditorControl.annotate")
    assert result.executed is False
    assert result.channel == "mcp"
    assert "sch_annotate" in result.summary


def test_gui_only_command_says_so_plainly() -> None:
    result = invoke("pcbnew.EditorControl.exportHyperlynx")
    assert result.executed is False
    assert result.channel == "gui-only"
    assert "no headless path" in result.summary


def test_file_backed_command_explains_the_file_route() -> None:
    # Swap Layers used to sit here; it now has a tool, so use one still unbound.
    result = invoke("pcbnew.GlobalEdit.changeFootprints")
    assert result.executed is False
    assert result.channel == "file"
    assert ".kicad_pcb" in result.summary


def test_ipc_backed_command_states_the_requirement() -> None:
    result = invoke("common.Control.updatePcbFromSchematic")
    assert result.executed is False
    assert result.channel == "ipc"
    assert "IPC" in result.summary


def test_dry_run_builds_the_command_without_executing(board_project: Path) -> None:
    result = invoke("pcbnew.DRCTool.runDRC", dry_run=True)
    assert result.executed is False
    assert result.return_code is None
    assert result.command[:3] == ("kicad-cli", "pcb", "drc")
    assert result.command[-1].endswith("demo.kicad_pcb")


def test_schematic_command_uses_the_schematic_file(board_project: Path) -> None:
    result = invoke("eeschema.InspectionTool.runERC", dry_run=True)
    assert result.command[:3] == ("kicad-cli", "sch", "erc")
    assert result.command[-1].endswith("demo.kicad_sch")


def test_unknown_option_is_refused_before_running(board_project: Path) -> None:
    with pytest.raises(MenuInvocationError, match="does not accept --not-an-option"):
        invoke("pcbnew.DRCTool.runDRC", {"not_an_option": 1}, dry_run=True)


def test_snake_case_options_are_normalised(board_project: Path) -> None:
    result = invoke("pcbnew.DRCTool.runDRC", {"exit_code_violations": True}, dry_run=True)
    assert "--exit-code-violations" in result.command


def test_false_valued_options_are_dropped(board_project: Path) -> None:
    result = invoke("pcbnew.DRCTool.runDRC", {"exit_code_violations": False}, dry_run=True)
    assert "--exit-code-violations" not in result.command


def test_relative_output_lands_inside_the_workspace(board_project: Path) -> None:
    result = invoke("pcbnew.DRCTool.runDRC", output="reports/drc.json", dry_run=True)
    assert result.outputs
    assert Path(result.outputs[0]).is_relative_to(board_project)


def test_absolute_output_outside_the_workspace_is_blocked(board_project: Path) -> None:
    with pytest.raises(UnsafePathError):
        # An absolute path outside the workspace: the guard must reject it, so nothing
        # is ever written here.
        invoke("pcbnew.DRCTool.runDRC", output="/tmp/escape.json", dry_run=True)  # noqa: S108


def test_missing_project_is_reported_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KICAD_MCP_PCB_FILE", raising=False)
    monkeypatch.setenv("KICAD_MCP_PCB_FILE", "/nonexistent/board.kicad_pcb")
    from kicad_mcp.config import reset_config

    reset_config()
    try:
        with pytest.raises(MenuInvocationError, match="No PCB file is configured"):
            invoke("pcbnew.DRCTool.runDRC", dry_run=True)
    finally:
        reset_config()
