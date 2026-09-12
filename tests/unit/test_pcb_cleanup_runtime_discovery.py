"""Cleanup must remain callable without weakening live IPC requirements."""

from mcp.types import Tool

from kicad_mcp.server import _filter_ipc_runtime_tools


def test_cleanup_discovery_does_not_probe_ipc(monkeypatch) -> None:
    import kicad_mcp.server as server_module

    def unexpected_probe() -> object:
        raise AssertionError("file-backed cleanup must not probe KiCad IPC")

    monkeypatch.setattr(server_module, "get_ipc_capability_state", unexpected_probe)
    tools = [
        Tool(name=name, inputSchema={})
        for name in ("pcb_move_silkscreen_to_fab", "pcb_set_zone_island_policy")
    ]
    assert _filter_ipc_runtime_tools(tools) == tools
