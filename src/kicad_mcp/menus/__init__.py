"""KiCad menu-surface catalog and headless bindings.

KiCad's GUI exposes roughly 350 distinct menu commands across eight application
frames. This package makes that surface addressable from an MCP agent: what the
menus contain, which of those commands can be driven without the GUI, and how.

The data is generated, not hand-maintained:

    scripts/extract_kicad_menus.py   KiCad C++ source -> menu_catalog.json
    scripts/build_menu_index.py      + kicad-menu-bindings.yaml -> menu_index.json

Only ``menu_index.json`` is read at runtime, so the server keeps no YAML dependency.
"""

from __future__ import annotations

from .catalog import (
    MenuAction,
    coverage,
    find_actions,
    frame_ids,
    get_action,
    get_index,
    kicad_version,
    render_tree,
)

__all__ = [
    "MenuAction",
    "coverage",
    "find_actions",
    "frame_ids",
    "get_action",
    "get_index",
    "kicad_version",
    "render_tree",
]
