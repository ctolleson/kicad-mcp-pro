"""Read a board's nets from its file text, across both KiCad formats.

KiCad 9 and earlier declare a board-level ``(net N "name")`` table and have items
refer to it by number. KiCad 10 dropped that table and writes ``(net "GND")`` on
each item instead. Code that matches only the table form finds nothing at all on a
10.x board and reports it as having no nets, which reads as an empty board rather
than as a parse failure -- the kind of wrong answer that looks like a fact.
"""

from __future__ import annotations

import re

_TABLE = re.compile(r'\(net\s+(\d+)\s+"((?:\\.|[^"\\])*)"\)')
_ON_ITEM = re.compile(r'\(net\s+"((?:\\.|[^"\\])*)"\)')


def board_nets_from_text(content: str) -> list[dict[str, object]]:
    """Every net on the board as ``{"code", "name"}``, newest format included.

    Codes are real when the board declares a table. On a KiCad 10 board there are no
    codes, so they are assigned in first-seen order purely so callers that key on a
    code keep working; they are not stable across edits and must not be written back
    into a board file.
    """
    nets: list[dict[str, object]] = []
    seen_codes: set[int] = set()
    for match in _TABLE.finditer(content):
        code = int(match.group(1))
        if code in seen_codes:
            continue
        seen_codes.add(code)
        nets.append({"code": code, "name": match.group(2)})
    if nets:
        return nets

    seen_names: set[str] = set()
    for match in _ON_ITEM.finditer(content):
        name = match.group(1)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        nets.append({"code": len(nets), "name": name})
    return nets


__all__ = ["board_nets_from_text"]
