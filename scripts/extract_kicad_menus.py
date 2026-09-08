#!/usr/bin/env python3
"""Extract KiCad's complete menu tree from a KiCad source checkout.

KiCad builds every application menu bar in C++ from ``ACTION_MENU`` objects that
reference ``TOOL_ACTION`` singletons. Both are declared in a highly regular
builder-pattern dialect, so the full menu surface can be recovered statically —
no KiCad build, no running GUI.

The output feeds ``kicad_mcp.menus`` so the MCP server can answer "what can KiCad
do from its menus, and how do I reach that headlessly?".

Usage:
    uv run python scripts/extract_kicad_menus.py \
        --kicad-source /path/to/kicad --version 10.0.6 \
        --out src/kicad_mcp/menus/menu_catalog.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Frames: KiCad application windows that own a menu bar.
# --------------------------------------------------------------------------

FRAMES: tuple[tuple[str, str, str], ...] = (
    ("kicad_manager", "KiCad Project Manager", "kicad/menubar.cpp"),
    ("schematic_editor", "Schematic Editor (Eeschema)", "eeschema/menubar.cpp"),
    (
        "symbol_editor",
        "Symbol Editor",
        "eeschema/symbol_editor/menubar_symbol_editor.cpp",
    ),
    ("pcb_editor", "PCB Editor (Pcbnew)", "pcbnew/menubar_pcb_editor.cpp"),
    ("footprint_editor", "Footprint Editor", "pcbnew/menubar_footprint_editor.cpp"),
    ("gerbview", "Gerber Viewer (GerbView)", "gerbview/menubar.cpp"),
    ("drawing_sheet_editor", "Drawing Sheet Editor", "pagelayout_editor/menubar.cpp"),
    ("footprint_assignment", "Footprint Assignment (CvPcb)", "cvpcb/menubar.cpp"),
)

# Files declaring TOOL_ACTION singletons, keyed by the C++ namespace they populate.
ACTION_SOURCES: tuple[str, ...] = (
    "common/tool/actions.cpp",
    "eeschema/tools/sch_actions.cpp",
    "pcbnew/tools/pcb_actions.cpp",
    "gerbview/tools/gerbview_actions.cpp",
    "kicad/tools/kicad_manager_actions.cpp",
    "pagelayout_editor/tools/pl_actions.cpp",
    "cvpcb/tools/cvpcb_actions.cpp",
    "3d-viewer/3d_viewer/tools/eda_3d_actions.cpp",
)

# The shared Help menu is appended by EDA_BASE_FRAME rather than each menubar file.
HELP_MENU_SOURCE = "common/eda_base_frame.cpp"
HELP_MENU_FUNCTION = "EDA_BASE_FRAME::AddStandardHelpMenu"


# --------------------------------------------------------------------------
# TOOL_ACTION parsing
# --------------------------------------------------------------------------


@dataclass
class Action:
    """One KiCad ``TOOL_ACTION`` — the command behind a menu item."""

    namespace: str
    identifier: str
    name: str = ""
    friendly_name: str = ""
    tooltip: str = ""
    scope: str = ""
    hotkey: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.namespace}::{self.identifier}"


# ``_( "text" )``, tolerating adjacent literal concatenation and wxT/wxS wrappers.
_STRING_RUN = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _joined_string(fragment: str) -> str:
    """Join C++ adjacent string literals into one Python string."""
    parts = _STRING_RUN.findall(fragment)
    if not parts:
        return ""
    return "".join(parts).replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")


def _balanced_span(text: str, open_index: int) -> int:
    """Return the index just past the ``)`` matching the ``(`` at ``open_index``."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _builder_argument(body: str, method: str) -> str:
    """Return the raw argument text of ``.Method( ... )`` inside an action body."""
    match = re.search(rf"\.{method}\s*\(", body)
    if match is None:
        return ""
    open_index = match.end() - 1
    end = _balanced_span(body, open_index)
    return body[open_index + 1 : end - 1].strip()


_ACTION_DECL = re.compile(r"\bTOOL_ACTION\s+(\w+)::(\w+)\s*\(")


def parse_actions(root: Path) -> dict[str, Action]:
    """Parse every ``TOOL_ACTION`` declaration into a registry keyed by ``NS::ident``."""
    registry: dict[str, Action] = {}
    for relative in ACTION_SOURCES:
        path = root / relative
        if not path.is_file():
            print(f"  ! missing action source: {relative}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _ACTION_DECL.finditer(text):
            namespace, identifier = match.group(1), match.group(2)
            body = text[match.end() - 1 : _balanced_span(text, match.end() - 1)]
            action = Action(
                namespace=namespace,
                identifier=identifier,
                name=_joined_string(_builder_argument(body, "Name")),
                friendly_name=_joined_string(_builder_argument(body, "FriendlyName")),
                tooltip=_joined_string(_builder_argument(body, "Tooltip")),
                scope=_builder_argument(body, "Scope"),
                hotkey=_builder_argument(body, "DefaultHotkey"),
                source=relative,
            )
            registry[action.key] = action
    return registry


# --------------------------------------------------------------------------
# ACTION_MENU parsing
# --------------------------------------------------------------------------


@dataclass
class MenuNode:
    """A menu or submenu under construction."""

    variable: str
    title: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)


_NEW_MENU = re.compile(r"(\w+)\s*=\s*new\s+ACTION_MENU\b")
_SET_TITLE = re.compile(r"(\w+)\s*->\s*SetTitle\s*\(")
_ADD_ACTION = re.compile(r"(\w+)\s*->\s*Add\s*\(\s*\*?(\w+)\s*::\s*(\w+)\s*(,|\))")
_ADD_SUBMENU = re.compile(r"(\w+)\s*->\s*Add\s*\(\s*(\w+)\s*(?:->\s*Clone\s*\(\s*\))?\s*\)")
_APPEND_ROOT = re.compile(r"(?:\w+)\s*->\s*Append\s*\(\s*(\w+)\s*,")
_SEPARATOR = re.compile(r"(\w+)\s*->\s*AppendSeparator\s*\(")
_DYNAMIC_BUILDERS = {
    "buildActionPluginMenus": "External action plugins discovered at runtime",
    "AddMenuLanguageList": "Installed UI language list",
    "AddFilesToMenu": "Recent-file history",
}
_ADD_STANDARD_HELP = re.compile(r"AddStandardHelpMenu\s*\(")


def _statements(text: str) -> list[str]:
    """Split C++ source into whitespace-normalised statements."""
    out: list[str] = []
    for raw in text.split(";"):
        collapsed = re.sub(r"\s+", " ", raw).strip()
        if collapsed:
            out.append(collapsed)
    return out


def _function_body(text: str, signature: str) -> str:
    """Return the brace-delimited body of the named function."""
    index = text.find(signature)
    if index < 0:
        return ""
    brace = text.find("{", index)
    if brace < 0:
        return ""
    depth = 0
    for position in range(brace, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[brace : position + 1]
    return text[brace:]


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def parse_menu_source(text: str) -> tuple[dict[str, MenuNode], list[tuple[str, str]], bool]:
    """Parse one menu-building function into nodes plus ordered root attachments."""
    nodes: dict[str, MenuNode] = {}
    roots: list[tuple[str, str]] = []
    wants_help_menu = False

    for statement in _statements(_strip_comments(text)):
        if _ADD_STANDARD_HELP.search(statement):
            wants_help_menu = True

        created = _NEW_MENU.search(statement)
        if created:
            variable = created.group(1)
            # A re-``new``'d static menu (openRecentMenu) keeps its first identity.
            nodes.setdefault(variable, MenuNode(variable=variable))
            continue

        titled = _SET_TITLE.search(statement)
        if titled and titled.group(1) in nodes:
            open_index = titled.end() - 1
            argument = statement[open_index + 1 : _balanced_span(statement, open_index) - 1]
            nodes[titled.group(1)].title = _joined_string(argument)
            continue

        root = _APPEND_ROOT.search(statement)
        if root and root.group(1) in nodes:
            open_index = statement.find("(", root.start())
            argument = statement[open_index + 1 : _balanced_span(statement, open_index) - 1]
            label = _joined_string(argument)
            roots.append((root.group(1), label))
            if label and not nodes[root.group(1)].title:
                nodes[root.group(1)].title = label
            continue

        separator = _SEPARATOR.search(statement)
        if separator and separator.group(1) in nodes:
            nodes[separator.group(1)].items.append({"kind": "separator"})
            continue

        action = _ADD_ACTION.search(statement)
        if action and action.group(1) in nodes:
            owner, namespace, identifier = action.group(1), action.group(2), action.group(3)
            label = ""
            style = "NORMAL"
            style_match = re.search(r"ACTION_MENU::(\w+)", statement)
            if style_match:
                style = style_match.group(1)
            # An explicit menu label override is the last _( "..." ) in the call.
            label_matches = re.findall(r'_\(\s*"((?:[^"\\]|\\.)*)"\s*\)', statement)
            if label_matches:
                label = label_matches[-1]
            nodes[owner].items.append(
                {
                    "kind": "action",
                    "action_ref": f"{namespace}::{identifier}",
                    "label_override": label,
                    "style": style,
                }
            )
            continue

        submenu = _ADD_SUBMENU.search(statement)
        if submenu and submenu.group(1) in nodes and submenu.group(2) in nodes:
            nodes[submenu.group(1)].items.append({"kind": "submenu", "variable": submenu.group(2)})
            continue

        for builder, description in _DYNAMIC_BUILDERS.items():
            if f"{builder} (" in statement or f"{builder}(" in statement:
                target = re.search(rf"{builder}\s*\(\s*(\w+)", statement)
                owner = target.group(1) if target else ""
                if owner in nodes:
                    nodes[owner].items.append(
                        {"kind": "dynamic", "description": description, "builder": builder}
                    )
                break

    return nodes, roots, wants_help_menu


# Every frame-specific ``*_ACTIONS`` class derives from ``ACTIONS``, so a menubar may
# reference an inherited member through the derived name (PCB_ACTIONS::showSearch is
# really ACTIONS::showSearch). Fall back to the base class on a miss.
def _lookup_action(actions: dict[str, Action], reference: str) -> Action | None:
    """Resolve ``NS::ident``, falling back to the inherited ``ACTIONS::ident``."""
    found = actions.get(reference)
    if found is not None:
        return found
    _, _, identifier = reference.partition("::")
    return actions.get(f"ACTIONS::{identifier}")


def _resolve(
    node: MenuNode,
    nodes: dict[str, MenuNode],
    actions: dict[str, Action],
    path: list[str],
    seen: set[str],
    stats: dict[str, int],
) -> list[dict[str, Any]]:
    """Materialise a menu node's items, following submenu references."""
    resolved: list[dict[str, Any]] = []
    for item in node.items:
        kind = item["kind"]
        if kind == "separator":
            continue
        if kind == "dynamic":
            resolved.append(
                {
                    "kind": "dynamic",
                    "label": item["description"],
                    "path": [*path, item["description"]],
                    "builder": item["builder"],
                }
            )
            continue
        if kind == "submenu":
            variable = item["variable"]
            if variable in seen:  # defensive: cyclic Clone() chains
                continue
            child = nodes[variable]
            title = child.title or variable
            resolved.append(
                {
                    "kind": "submenu",
                    "label": title,
                    "path": [*path, title],
                    "items": _resolve(
                        child, nodes, actions, [*path, title], seen | {variable}, stats
                    ),
                }
            )
            continue

        reference = item["action_ref"]
        action = _lookup_action(actions, reference)
        if action is None:
            stats["unresolved"] += 1
            label = item["label_override"] or reference.split("::")[-1]
            resolved.append(
                {
                    "kind": "action",
                    "label": label,
                    "path": [*path, label],
                    "action_ref": reference,
                    "action_name": "",
                    "unresolved": True,
                }
            )
            continue

        label = item["label_override"] or action.friendly_name or action.identifier
        reference = action.key
        stats["actions"] += 1
        entry: dict[str, Any] = {
            "kind": "action",
            "label": label,
            "path": [*path, label],
            "action_ref": reference,
            "action_name": action.name,
            "friendly_name": action.friendly_name,
            "tooltip": action.tooltip,
            "style": item["style"],
        }
        if action.hotkey:
            entry["default_hotkey"] = action.hotkey
        resolved.append(entry)
    return resolved


def build_catalog(root: Path, version: str) -> dict[str, Any]:
    """Build the complete menu catalog for a KiCad source tree."""
    actions = parse_actions(root)
    print(f"  parsed {len(actions)} TOOL_ACTION declarations")

    help_nodes: dict[str, MenuNode] = {}
    help_roots: list[tuple[str, str]] = []
    help_path = root / HELP_MENU_SOURCE
    if help_path.is_file():
        body = _function_body(
            help_path.read_text(encoding="utf-8", errors="replace"), HELP_MENU_FUNCTION
        )
        help_nodes, help_roots, _ = parse_menu_source(body)

    frames: list[dict[str, Any]] = []
    stats = {"actions": 0, "unresolved": 0}

    for frame_id, frame_title, relative in FRAMES:
        path = root / relative
        if not path.is_file():
            print(f"  ! missing menubar source: {relative}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        body = _function_body(text, "doReCreateMenuBar") or _function_body(text, "ReCreateMenuBar")
        if not body:
            print(f"  ! no menubar function found in {relative}", file=sys.stderr)
            continue

        nodes, roots, wants_help = parse_menu_source(body)
        menus: list[dict[str, Any]] = []
        for variable, label in roots:
            node = nodes[variable]
            title = label or node.title or variable
            menus.append(
                {
                    "label": title,
                    "path": [title],
                    "items": _resolve(node, nodes, actions, [title], {variable}, stats),
                }
            )
        if wants_help:
            for variable, label in help_roots:
                node = help_nodes[variable]
                title = label or node.title or variable
                menus.append(
                    {
                        "label": title,
                        "path": [title],
                        "items": _resolve(node, help_nodes, actions, [title], {variable}, stats),
                    }
                )

        frames.append(
            {
                "id": frame_id,
                "title": frame_title,
                "source": relative,
                "menus": menus,
            }
        )
        count = sum(len(list(_walk_actions(menu["items"]))) for menu in menus)
        print(f"  {frame_id:<22} {len(menus)} menus, {count} actions")

    return {
        "schema_version": 1,
        "kicad_version": version,
        "generator": "scripts/extract_kicad_menus.py",
        "frames": frames,
        "actions": {
            key: {
                "name": action.name,
                "friendly_name": action.friendly_name,
                "tooltip": action.tooltip,
                "scope": action.scope,
                "hotkey": action.hotkey,
                "source": action.source,
            }
            for key, action in sorted(actions.items())
        },
        "stats": {
            "declared_actions": len(actions),
            "menu_action_items": stats["actions"],
            "unresolved_action_refs": stats["unresolved"],
        },
    }


def _walk_actions(items: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for item in items:
        if item["kind"] == "action":
            yield item
        elif item["kind"] == "submenu":
            yield from _walk_actions(item["items"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kicad-source", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    root: Path = args.kicad_source
    if not root.is_dir():
        print(f"KiCad source not found: {root}", file=sys.stderr)
        return 1

    print(f"Extracting KiCad {args.version} menus from {root}")
    catalog = build_catalog(root, args.version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KiB)")
    print(f"  stats: {catalog['stats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
