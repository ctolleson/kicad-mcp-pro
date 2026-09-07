"""KiCad menu-surface catalog invariants.

The catalog claims, for ~350 real KiCad menu commands, whether each can be driven
headlessly and how. The failure mode that matters is a *false claim of coverage*:
naming a tool that is not registered, or a kicad-cli command that does not exist.
These tests make such a claim fail loudly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.menus import (
    coverage,
    find_actions,
    frame_ids,
    get_action,
    get_index,
    kicad_version,
    render_tree,
)
from kicad_mcp.menus.catalog import CHANNEL_ORDER, STATUS_ORDER
from kicad_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[2]

MENU_TOOLS = {
    "kicad_menu_frames",
    "kicad_menu_tree",
    "kicad_menu_search",
    "kicad_menu_describe",
    "kicad_menu_coverage",
    "kicad_menu_invoke",
    "kicad_menu_export_map",
}


@pytest.fixture(scope="module")
def index() -> dict[str, Any]:
    return get_index()


@pytest.fixture(scope="module")
def registered_tool_names() -> set[str]:
    server = build_server("agent_full")
    server.ensure_registered()
    return {tool.name for tool in server._tool_manager.list_tools()}


def test_catalog_covers_every_kicad_frame(index: dict[str, Any]) -> None:
    assert index["kicad_version"] == "10.0.6"
    assert set(frame_ids()) == {
        "kicad_manager",
        "schematic_editor",
        "symbol_editor",
        "pcb_editor",
        "footprint_editor",
        "gerbview",
        "drawing_sheet_editor",
        "footprint_assignment",
    }


def test_every_menu_command_has_an_explicit_binding(index: dict[str, Any]) -> None:
    """No command may silently fall through to the catch-all default binding."""
    defaulted = [
        name
        for name, entry in index["actions"].items()
        if entry["binding_source"] == "default"
    ]
    assert not defaulted, (
        f"{len(defaulted)} menu commands have no explicit binding: {defaulted[:10]}"
    )


def test_channels_and_statuses_are_in_vocabulary(index: dict[str, Any]) -> None:
    for name, entry in index["actions"].items():
        assert entry["channel"] in CHANNEL_ORDER, f"{name}: bad channel"
        assert entry["status"] in STATUS_ORDER, f"{name}: bad status"


def test_gui_only_channel_and_status_agree(index: dict[str, Any]) -> None:
    """A command is GUI-only in both senses or neither — never half."""
    for name, entry in index["actions"].items():
        assert (entry["channel"] == "gui-only") == (entry["status"] == "gui-only"), (
            f"{name}: channel {entry['channel']} disagrees with status {entry['status']}"
        )


def test_named_mcp_tools_are_actually_registered(
    index: dict[str, Any], registered_tool_names: set[str]
) -> None:
    """The cardinal sin: claiming a menu command is covered by a tool that does not exist."""
    missing: list[str] = []
    for name, entry in index["actions"].items():
        tool = entry["mcp_tool"]
        if tool and tool not in registered_tool_names:
            missing.append(f"{name} -> {tool}")
    assert not missing, f"Menu bindings name unregistered tools: {missing}"


def test_mcp_channel_always_names_a_tool(index: dict[str, Any]) -> None:
    for name, entry in index["actions"].items():
        if entry["channel"] == "mcp":
            assert entry["mcp_tool"], f"{name}: channel 'mcp' with no tool named"


def test_gui_only_commands_claim_no_automation(index: dict[str, Any]) -> None:
    for name, entry in index["actions"].items():
        if entry["channel"] == "gui-only":
            assert not entry["cli_command"], f"{name}: GUI-only yet names a CLI command"


def test_coverage_math_is_self_consistent(index: dict[str, Any]) -> None:
    overall = coverage()
    actions = index["actions"]
    assert overall["total"] == len(actions)
    assert (
        overall["covered"] + overall["partial"] + overall["gap"] + overall["gui_only"]
        == overall["total"]
    )
    # GUI-only rows are a KiCad limit, not a gap here, so they leave the denominator.
    assert overall["denominator"] == overall["total"] - overall["gui_only"]
    expected = round(100.0 * overall["covered"] / overall["denominator"], 1)
    assert overall["coverage_pct"] == expected


def test_per_frame_coverage_sums_to_the_distinct_total(index: dict[str, Any]) -> None:
    """Frames share commands, so per-frame totals must not be assumed to sum."""
    for frame in frame_ids():
        stats = coverage(frame)
        assert stats["total"] > 0
        assert stats["denominator"] <= stats["total"]


def test_lookup_by_action_name_label_and_menu_path() -> None:
    by_name = get_action("pcbnew.DRCTool.runDRC")
    by_label = get_action("Design Rules Checker")
    by_path = get_action("Inspect > Design Rules Checker")
    assert by_name is not None
    assert by_name == by_label == by_path
    assert by_name.mcp_tool == "run_drc"
    assert by_name.cli_command == ("pcb", "drc")
    assert by_name.is_headless


def test_lookup_tolerates_accelerators_and_ellipses() -> None:
    assert get_action("&Inspect > Design Rules Checker") is not None
    assert get_action("File > Fabrication Outputs > Gerbers (.gbr)...") is not None


def test_unknown_lookup_returns_none() -> None:
    assert get_action("no such menu command") is None


def test_search_filters_by_frame_channel_and_status() -> None:
    gaps = find_actions(status="gap")
    assert gaps, "expected at least one open gap"
    assert all(action.status == "gap" for action in gaps)

    cli_actions = find_actions(channel="cli")
    assert all(action.channel == "cli" for action in cli_actions)

    pcb_actions = find_actions(frame="pcb_editor")
    assert all(
        any(frame == "pcb_editor" for frame, _ in action.placements) for action in pcb_actions
    )


def test_search_orders_actionable_results_before_gui_only() -> None:
    results = find_actions("export")
    statuses = [action.status for action in results]
    # gui-only rows sort last, so the first gui-only index must exceed every other.
    if "gui-only" in statuses:
        first_gui = statuses.index("gui-only")
        assert all(status == "gui-only" for status in statuses[first_gui:])


def test_render_tree_marks_every_command() -> None:
    tree = render_tree("pcb_editor")
    assert "PCB Editor (Pcbnew)" in tree
    assert "Design Rules Checker" in tree
    assert "-> run_drc" in tree


def test_render_tree_rejects_unknown_frame() -> None:
    with pytest.raises(KeyError):
        render_tree("not_a_frame")


def test_menu_tools_are_registered(registered_tool_names: set[str]) -> None:
    assert MENU_TOOLS <= registered_tool_names


def test_menu_tools_are_routed_and_have_capability_records() -> None:
    from kicad_mcp.capabilities import get as get_capability_record
    from kicad_mcp.tools.router import TOOL_CATEGORIES

    routed = {name for info in TOOL_CATEGORIES.values() for name in info["tools"]}
    assert MENU_TOOLS <= routed
    for name in MENU_TOOLS:
        assert get_capability_record(name) is not None, f"{name} has no capability record"


@pytest.mark.skipif(
    shutil.which("kicad-cli") is None
    and not Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli").exists(),
    reason="kicad-cli is required to verify CLI bindings against the real binary",
)
def test_every_cli_binding_is_a_real_kicad_cli_command(index: dict[str, Any]) -> None:
    """A bound kicad-cli command must exist in the installed KiCad."""
    from kicad_mcp.config import get_config

    cli = get_config().kicad_cli
    if not cli.exists():
        pytest.skip("configured kicad-cli path does not exist")

    checked: set[tuple[str, ...]] = set()
    for name, entry in index["actions"].items():
        command = tuple(entry["cli_command"])
        if not command or command in checked:
            continue
        checked.add(command)
        result = subprocess.run(  # noqa: S603
            [str(cli), *command, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{name}: `kicad-cli {' '.join(command)}` is not a command"


def test_generated_index_matches_the_yaml_source_of_truth() -> None:
    """The shipped index must be a faithful build of the curated bindings."""
    yaml = pytest.importorskip("yaml")
    import scripts.build_menu_index as builder

    catalog = json.loads(
        (REPO_ROOT / "src/kicad_mcp/menus/menu_catalog.json").read_text(encoding="utf-8")
    )
    bindings = yaml.safe_load(
        (REPO_ROOT / "docs/compatibility/kicad-menu-bindings.yaml").read_text(encoding="utf-8")
    )
    rebuilt = builder.build_index(catalog, bindings)
    shipped = json.loads(
        (REPO_ROOT / "src/kicad_mcp/menus/menu_index.json").read_text(encoding="utf-8")
    )
    assert rebuilt == shipped, (
        "menu_index.json is stale — regenerate with scripts/build_menu_index.py"
    )


def test_catalog_and_bindings_target_the_same_kicad_version() -> None:
    yaml = pytest.importorskip("yaml")

    bindings = yaml.safe_load(
        (REPO_ROOT / "docs/compatibility/kicad-menu-bindings.yaml").read_text(encoding="utf-8")
    )
    assert bindings["kicad_version"] == kicad_version()


def test_every_binding_override_names_a_real_menu_command() -> None:
    """A stale override would silently stop applying; catch it instead."""
    yaml = pytest.importorskip("yaml")

    bindings = yaml.safe_load(
        (REPO_ROOT / "docs/compatibility/kicad-menu-bindings.yaml").read_text(encoding="utf-8")
    )
    known = set(get_index()["actions"])
    unknown = sorted(set(bindings["overrides"]) - known)
    assert not unknown, f"bindings.yaml overrides commands KiCad does not have: {unknown}"
