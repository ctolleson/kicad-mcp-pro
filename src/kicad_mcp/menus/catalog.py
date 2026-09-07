"""Query helpers over the generated KiCad menu index.

The index joins KiCad's real menu tree (extracted from the C++ sources) with a
curated binding for each command saying how — or whether — it can be reached
headlessly. Everything here is pure and side-effect free; execution lives in
:mod:`kicad_mcp.menus.execution`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_INDEX_PATH = Path(__file__).with_name("menu_index.json")

# Ordered worst-first so a coverage report leads with what is missing.
STATUS_ORDER: tuple[str, ...] = ("gap", "partial", "covered", "gui-only")
CHANNEL_ORDER: tuple[str, ...] = ("mcp", "cli", "file", "ipc", "gui-only")


@dataclass(frozen=True)
class MenuAction:
    """One KiCad menu command and its headless binding."""

    action_name: str
    label: str
    friendly_name: str
    tooltip: str
    default_hotkey: str
    channel: str
    status: str
    mcp_tool: str
    cli_command: tuple[str, ...]
    notes: str
    placements: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def is_headless(self) -> bool:
        """Whether KiCad exposes any non-GUI path for this command."""
        return self.channel != "gui-only"

    @property
    def menu_paths(self) -> tuple[str, ...]:
        """Human-readable ``Frame: File > Export > ...`` locations."""
        return tuple(f"{frame}: {' > '.join(path)}" for frame, path in self.placements)


@lru_cache(maxsize=1)
def get_index() -> dict[str, Any]:
    """Load and cache the generated menu index."""
    loaded: dict[str, Any] = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    return loaded


def kicad_version() -> str:
    """Return the KiCad version the catalog was extracted from."""
    return str(get_index()["kicad_version"])


def frame_ids() -> tuple[str, ...]:
    """Return the ids of every KiCad frame with a menu bar."""
    return tuple(frame["id"] for frame in get_index()["frames"])


def _to_action(raw: dict[str, Any]) -> MenuAction:
    return MenuAction(
        action_name=raw["action_name"],
        label=raw["label"],
        friendly_name=raw["friendly_name"],
        tooltip=raw["tooltip"],
        default_hotkey=raw["default_hotkey"],
        channel=raw["channel"],
        status=raw["status"],
        mcp_tool=raw["mcp_tool"],
        cli_command=tuple(raw["cli_command"]),
        notes=raw["notes"],
        placements=tuple(
            (placement["frame"], tuple(placement["path"])) for placement in raw["placements"]
        ),
    )


def get_action(name_or_label: str) -> MenuAction | None:
    """Resolve a command by action name, then by exact label, then by menu path.

    Accepts ``pcbnew.DRCTool.runDRC``, ``Design Rules Checker``, or a menu path such
    as ``Inspect > Design Rules Checker`` (case-insensitive, ``/`` also accepted).
    """
    actions: dict[str, Any] = get_index()["actions"]
    if name_or_label in actions:
        return _to_action(actions[name_or_label])

    needle = name_or_label.strip().casefold()
    if not needle:
        return None

    for raw in actions.values():
        if raw["label"].casefold() == needle or raw["friendly_name"].casefold() == needle:
            return _to_action(raw)

    normalized = _normalize_path(name_or_label)
    for raw in actions.values():
        for placement in raw["placements"]:
            if _normalize_path(" > ".join(placement["path"])).endswith(normalized):
                return _to_action(raw)
    return None


def _normalize_path(path: str) -> str:
    """Normalise a menu path for comparison: drop &-accelerators, ... and case."""
    cleaned = path.replace("/", ">").replace("&", "").replace("...", "")
    parts = [part.strip().casefold() for part in cleaned.split(">") if part.strip()]
    return " > ".join(parts)


def find_actions(
    query: str = "",
    *,
    frame: str = "",
    channel: str = "",
    status: str = "",
    limit: int | None = None,
) -> list[MenuAction]:
    """Search menu commands by text, optionally filtered by frame/channel/status."""
    needle = query.strip().casefold()
    results: list[MenuAction] = []
    for raw in get_index()["actions"].values():
        if channel and raw["channel"] != channel:
            continue
        if status and raw["status"] != status:
            continue
        if frame and not any(p["frame"] == frame for p in raw["placements"]):
            continue
        if needle:
            haystack = " ".join(
                [
                    raw["action_name"],
                    raw["label"],
                    raw["friendly_name"],
                    raw["tooltip"],
                    raw["mcp_tool"],
                    raw["notes"],
                    *(" > ".join(p["path"]) for p in raw["placements"]),
                ]
            ).casefold()
            if needle not in haystack:
                continue
        results.append(_to_action(raw))

    # Most useful first: actionable rows before GUI-only ones, then alphabetical.
    results.sort(key=lambda action: (STATUS_ORDER.index(action.status), action.label.casefold()))
    return results[:limit] if limit else results


def coverage(frame: str = "") -> dict[str, Any]:
    """Return overall coverage, or one frame's coverage when ``frame`` is given."""
    data = get_index()["coverage"]
    if not frame:
        return dict(data["overall"])
    frames: dict[str, Any] = data["frames"]
    if frame not in frames:
        raise KeyError(frame)
    return dict(frames[frame])


_STATUS_MARK = {"covered": "+", "partial": "~", "gap": "!", "gui-only": "."}


def render_tree(frame: str, *, max_depth: int = 3, annotate: bool = True) -> str:
    """Render one frame's menu tree as indented text.

    Each command is marked ``+`` covered, ``~`` partial, ``!`` gap (headless path
    exists, no tool yet) or ``.`` GUI-only.
    """
    index = get_index()
    match = next((item for item in index["frames"] if item["id"] == frame), None)
    if match is None:
        raise KeyError(frame)
    actions: dict[str, Any] = index["actions"]

    lines: list[str] = [f"{match['title']}  [{frame}]  KiCad {index['kicad_version']}"]

    def walk(items: list[dict[str, Any]], depth: int) -> None:
        if depth > max_depth:
            return
        pad = "  " * depth
        for item in items:
            kind = item["kind"]
            if kind == "submenu":
                lines.append(f"{pad}{item['label']}/")
                walk(item["items"], depth + 1)
            elif kind == "dynamic":
                lines.append(f"{pad}  ({item['label']})")
            else:
                entry = actions.get(item["action_name"], {})
                mark = _STATUS_MARK.get(entry.get("status", ""), "?")
                suffix = ""
                if annotate:
                    tool = entry.get("mcp_tool", "")
                    if tool:
                        suffix = f"  -> {tool}"
                    elif entry.get("cli_command"):
                        suffix = f"  -> kicad-cli {' '.join(entry['cli_command'])}"
                lines.append(f"{pad}{mark} {item['label']}{suffix}")

    for menu in match["menus"]:
        lines.append(f"\n{menu['label'].replace('&', '')}")
        walk(menu["items"], 1)
    return "\n".join(lines)
