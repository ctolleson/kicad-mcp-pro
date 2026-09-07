"""A round-tripping S-expression parser for KiCad design files.

The text helpers in :mod:`kicad_mcp.utils.sexpr` slice balanced blocks out of a
board file, which is enough to replace a named block but not to answer structural
questions — "every track on In1.Cu", "every zone with priority 0". Those need a
tree.

Fidelity: KiCad re-serialises a file whenever it saves it, and the board write
transaction runs ``kicad-cli pcb upgrade`` afterwards, so this writer does not have
to reproduce KiCad's byte layout exactly. It does have to emit *valid* and
*semantically identical* output, which the round-trip tests pin down.

Formatting follows KiCad's own convention: a list whose children are all atoms is
written inline — ``(thickness 0.035)`` — and any list containing a sub-list is
broken across lines with tab indentation.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .sexpr import _escape_sexpr_string, _unescape_sexpr_string


@dataclass(frozen=True, slots=True)
class Atom:
    """A leaf token. ``quoted`` records whether KiCad wrote it in quotes.

    The distinction matters on write-back: ``(layer "F.Cu")`` and ``(layer F.Cu)``
    are not interchangeable for every KiCad token, so quoting is preserved rather
    than inferred.
    """

    value: str
    quoted: bool = False

    def __str__(self) -> str:
        return self.value


# Lazily evaluated, so it can name SList before the class is defined below.
type Node = Atom | SList


class SList(list["Node"]):
    """One parenthesised S-expression list."""

    @property
    def tag(self) -> str:
        """The head symbol, or "" for an empty list."""
        if self and isinstance(self[0], Atom):
            return self[0].value
        return ""

    def children(self, tag: str) -> list[SList]:
        """Direct child lists with the given head symbol."""
        return [n for n in self if isinstance(n, SList) and n.tag == tag]

    def child(self, tag: str) -> SList | None:
        """The first direct child list with the given head symbol."""
        for node in self:
            if isinstance(node, SList) and node.tag == tag:
                return node
        return None

    def value_of(self, tag: str) -> str | None:
        """The first atom argument of the named child, e.g. ``(layer "F.Cu")`` -> F.Cu."""
        found = self.child(tag)
        if found is None or len(found) < 2:
            return None
        second = found[1]
        return second.value if isinstance(second, Atom) else None

    def set_value(self, tag: str, value: str, *, quoted: bool = True) -> None:
        """Set (or append) a simple ``(tag value)`` child."""
        found = self.child(tag)
        if found is None:
            self.append(SList([Atom(tag), Atom(value, quoted)]))
            return
        replacement = Atom(value, quoted)
        if len(found) < 2:
            found.append(replacement)
        else:
            found[1] = replacement

    def walk(self) -> Iterator[SList]:
        """Yield every list in the tree, depth-first, including this one."""
        yield self
        for node in self:
            if isinstance(node, SList):
                yield from node.walk()


class SExprParseError(ValueError):
    """Raised when a design file is not well-formed S-expression text."""


_WHITESPACE = " \t\r\n"


def parse(text: str) -> SList:
    """Parse a KiCad design file into a tree. Returns the single root list."""
    index = 0
    length = len(text)
    stack: list[SList] = []
    root: SList | None = None

    while index < length:
        char = text[index]

        if char in _WHITESPACE:
            index += 1
            continue

        if char == ";":  # KiCad does not emit comments, but tolerate them
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue

        if char == "(":
            node = SList()
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise SExprParseError("multiple root expressions in one file")
            stack.append(node)
            if root is None:
                root = node
            index += 1
            continue

        if char == ")":
            if not stack:
                raise SExprParseError(f"unbalanced ')' at offset {index}")
            stack.pop()
            index += 1
            continue

        if char == '"':
            index += 1
            start = index
            chunks: list[str] = []
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    break
                index += 1
            if index >= length:
                raise SExprParseError("unterminated string literal")
            chunks.append(text[start:index])
            index += 1
            if not stack:
                raise SExprParseError("atom outside any list")
            stack[-1].append(Atom(_unescape_sexpr_string("".join(chunks)), quoted=True))
            continue

        start = index
        while index < length and text[index] not in _WHITESPACE and text[index] not in '()"':
            index += 1
        if index == start:
            raise SExprParseError(f"unexpected character {text[index]!r} at offset {index}")
        if not stack:
            raise SExprParseError("atom outside any list")
        stack[-1].append(Atom(text[start:index], quoted=False))

    if stack:
        raise SExprParseError("unbalanced '(' — file ended inside a list")
    if root is None:
        raise SExprParseError("no S-expression found")
    return root


def _render_atom(atom: Atom) -> str:
    return f'"{_escape_sexpr_string(atom.value)}"' if atom.quoted else atom.value


def _is_inline(node: SList) -> bool:
    """KiCad writes atom-only lists on one line and breaks anything nested."""
    return all(isinstance(child, Atom) for child in node)


def dumps(node: SList, *, indent: int = 0) -> str:
    """Serialise a tree back to KiCad-style S-expression text."""
    pad = "\t" * indent
    if _is_inline(node):
        body = " ".join(_render_atom(child) for child in node if isinstance(child, Atom))
        return f"{pad}({body})"

    parts: list[str] = []
    leading: list[str] = []
    rest: list[Node] = []
    # Leading atoms stay on the opening line: (layer "F.Cu" -> then nested children.
    seen_list = False
    for child in node:
        if isinstance(child, Atom) and not seen_list:
            leading.append(_render_atom(child))
        else:
            seen_list = True
            rest.append(child)

    head = " ".join(leading)
    parts.append(f"{pad}({head}" if head else f"{pad}(")
    for child in rest:
        if isinstance(child, Atom):
            parts.append(f"{'\t' * (indent + 1)}{_render_atom(child)}")
        else:
            parts.append(dumps(child, indent=indent + 1))
    parts.append(f"{pad})")
    return "\n".join(parts)


def dump_file(node: SList) -> str:
    """Serialise a design-file root, with the trailing newline KiCad writes."""
    return dumps(node) + "\n"


__all__ = [
    "Atom",
    "Node",
    "SExprParseError",
    "SList",
    "dump_file",
    "dumps",
    "parse",
]
