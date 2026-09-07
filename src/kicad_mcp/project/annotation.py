"""Forward and back annotation between a KiCad schematic and its board.

"Update PCB from Schematic" and "Update Schematic from PCB" are the two dialogs
with no headless route at all: no ``kicad-cli`` verb, no IPC command, and the
KiCad actions are dialog-driven so ``RunAction`` only opens the window. Without
them a design cannot get from schematic to board unattended.

The bridge is ``kicad-cli sch export netlist``, which is headless and gives the
authoritative component and connectivity model.

Nets are matched by **pad identity**, never by name. Board net names are not
derivable from netlist names: a KiCad-native hierarchical design keeps the full
sheet path (``/CM5/M2_LX``) while a board imported from Altium carries the bare
leaf (``D1_N``) for the same netlist entry ``/Connectors/D1_N``. Measured on two
real boards, name-based matching scored 40/40 on one and 15/241 on the other.
Pad identity — (reference, pad number) — is provenance-independent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..utils.sexpr_tree import Atom, Node, SList

# KiCad invents these for pins with no connection; they are not real nets.
_UNCONNECTED_PREFIX = "unconnected-"


@dataclass(frozen=True)
class SchematicComponent:
    """One component as the schematic's netlist describes it."""

    reference: str
    value: str
    footprint: str
    uuid: str


@dataclass
class SchematicModel:
    """The schematic's view of the design, from its exported netlist."""

    components: dict[str, SchematicComponent] = field(default_factory=dict)
    # (reference, pad) -> net name, omitting KiCad's synthetic unconnected nets.
    pad_nets: dict[tuple[str, str], str] = field(default_factory=dict)
    net_pads: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class BoardModel:
    """The board's view of the same design."""

    footprints: dict[str, str] = field(default_factory=dict)  # ref -> footprint id
    values: dict[str, str] = field(default_factory=dict)  # ref -> value
    pad_nets: dict[tuple[str, str], str] = field(default_factory=dict)
    net_pads: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class PadNetChange:
    """A pad whose net differs between schematic and board."""

    reference: str
    pad: str
    board_net: str
    schematic_net: str
    # The board-side name to write, resolved through pad-identity matching.
    resolved_net: str


@dataclass
class AnnotationDiff:
    """Everything that differs between the schematic and the board."""

    missing_on_board: list[str] = field(default_factory=list)
    extra_on_board: list[str] = field(default_factory=list)
    footprint_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    value_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    pad_net_changes: list[PadNetChange] = field(default_factory=list)
    net_name_map: dict[str, str] = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        return not (
            self.missing_on_board
            or self.extra_on_board
            or self.footprint_mismatches
            or self.value_mismatches
            or self.pad_net_changes
        )

    @property
    def total_differences(self) -> int:
        return (
            len(self.missing_on_board)
            + len(self.extra_on_board)
            + len(self.footprint_mismatches)
            + len(self.value_mismatches)
            + len(self.pad_net_changes)
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _atom_value(node: SList | None) -> str:
    """First atom argument of a node, e.g. ``(ref "J1")`` -> ``J1``."""
    if node is None or len(node) < 2:
        return ""
    token = node[1]
    return token.value if isinstance(token, Atom) else ""


def parse_netlist(netlist: SList) -> SchematicModel:
    """Build the schematic model from a ``kicad-cli sch export netlist`` tree."""
    model = SchematicModel()

    components = netlist.child("components")
    if components is not None:
        for comp in components.children("comp"):
            reference = _atom_value(comp.child("ref"))
            if not reference:
                continue
            model.components[reference] = SchematicComponent(
                reference=reference,
                value=_atom_value(comp.child("value")),
                footprint=_atom_value(comp.child("footprint")),
                uuid=_atom_value(comp.child("tstamps")),
            )

    nets = netlist.child("nets")
    if nets is not None:
        for net in nets.children("net"):
            name = _atom_value(net.child("name"))
            if not name or name.startswith(_UNCONNECTED_PREFIX):
                continue
            pads: set[tuple[str, str]] = set()
            for node in net.children("node"):
                reference = _atom_value(node.child("ref"))
                pad = _atom_value(node.child("pin"))
                if reference and pad:
                    key = (reference, pad)
                    model.pad_nets[key] = name
                    pads.add(key)
            if pads:
                model.net_pads[name] = pads
    return model


def parse_board(board: SList) -> BoardModel:
    """Build the board model from a parsed ``.kicad_pcb`` tree."""
    model = BoardModel()
    for footprint in board.children("footprint"):
        reference = ""
        value = ""
        for prop in footprint.children("property"):
            fields = [a.value for a in prop[1:] if isinstance(a, Atom)]
            if not fields:
                continue
            if fields[0] == "Reference" and len(fields) > 1:
                reference = fields[1]
            elif fields[0] == "Value" and len(fields) > 1:
                value = fields[1]
        if not reference:
            continue
        identifier = footprint[1].value if isinstance(footprint[1], Atom) else ""
        model.footprints[reference] = identifier
        model.values[reference] = value

        for pad in footprint.children("pad"):
            number = pad[1].value if len(pad) > 1 and isinstance(pad[1], Atom) else ""
            if not number:
                continue
            net = pad.child("net")
            net_name = ""
            if net is not None:
                tokens = [a.value for a in net[1:] if isinstance(a, Atom)]
                if tokens:
                    net_name = tokens[-1]
            key = (reference, number)
            model.pad_nets[key] = net_name
            if net_name:
                model.net_pads.setdefault(net_name, set()).add(key)
    return model


# ---------------------------------------------------------------------------
# Matching and diffing
# ---------------------------------------------------------------------------


def _name_candidates(net_name: str) -> list[str]:
    """The board spellings a schematic net name might legitimately take."""
    leaf = net_name.rsplit("/", 1)[-1]
    stripped = net_name[1:] if net_name.startswith("/") else net_name
    # Ordered most to least specific; duplicates removed, order kept.
    seen: list[str] = []
    for candidate in (net_name, stripped, leaf):
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def map_net_names(schematic: SchematicModel, board: BoardModel) -> dict[str, str]:
    """Map schematic net names to the board's spelling.

    Name first, pad overlap only as a fallback. Overlap alone cannot tell a *swap*
    from a *rename*: if two nets exchange pads, each schematic net overlaps
    perfectly with the other's board net, so the swap resolves to a consistent
    renaming and the error disappears. Names break that tie.

    The name forms differ by provenance - a KiCad-native hierarchical design keeps
    the full sheet path, an Altium-imported board may carry only the leaf - so each
    plausible spelling is tried before falling back to topology.
    """
    mapping: dict[str, str] = {}
    unmatched: list[str] = []

    for net_name in schematic.net_pads:
        for candidate in _name_candidates(net_name):
            if candidate in board.net_pads:
                mapping[net_name] = candidate
                break
        else:
            unmatched.append(net_name)

    # Only nets whose name matches nothing on the board fall back to topology,
    # and they may not claim a board net an earlier name match already owns.
    claimed = set(mapping.values())
    for net_name in unmatched:
        scores: dict[str, int] = {}
        for pad in schematic.net_pads[net_name]:
            board_net = board.pad_nets.get(pad)
            if board_net and board_net not in claimed:
                scores[board_net] = scores.get(board_net, 0) + 1
        if scores:
            best = max(scores.items(), key=lambda item: (item[1], item[0]))
            mapping[net_name] = best[0]
            claimed.add(best[0])
        else:
            # A genuinely new net: KiCad names a root-level label without its prefix.
            mapping[net_name] = _name_candidates(net_name)[1 if net_name.startswith("/") else 0]
    return mapping


def _same_footprint(board_id: str, schematic_id: str) -> bool:
    """Compare footprint identifiers tolerantly across notations.

    A board may store ``Lib:Name`` where the netlist gives a bare ``Name`` for the
    same part - an Altium-imported board does exactly this for all 287 of its
    components. Only the library-qualified names are compared when both sides
    carry one; otherwise the bare names decide.
    """
    board_name = board_id.rsplit(":", 1)[-1]
    schematic_name = schematic_id.rsplit(":", 1)[-1]
    if ":" in board_id and ":" in schematic_id:
        return board_id == schematic_id
    return board_name == schematic_name


def diff(schematic: SchematicModel, board: BoardModel) -> AnnotationDiff:
    """Compare a schematic against a board, pad by pad."""
    result = AnnotationDiff()
    result.net_name_map = map_net_names(schematic, board)

    schematic_refs = set(schematic.components)
    board_refs = set(board.footprints)
    result.missing_on_board = sorted(schematic_refs - board_refs)
    result.extra_on_board = sorted(board_refs - schematic_refs)

    for reference in sorted(schematic_refs & board_refs):
        component = schematic.components[reference]
        board_footprint = board.footprints.get(reference, "")
        if (
            component.footprint
            and board_footprint
            and not _same_footprint(board_footprint, component.footprint)
        ):
            result.footprint_mismatches.append((reference, board_footprint, component.footprint))
        board_value = board.values.get(reference, "")
        if component.value and board_value != component.value:
            result.value_mismatches.append((reference, board_value, component.value))

    # Walk the union, not just the schematic side. A pad that carries a net on the
    # board but none in the schematic is a real difference - an import that dropped
    # a connection looks exactly like this - and iterating the schematic alone made
    # it invisible.
    for key in sorted(set(schematic.pad_nets) | set(board.pad_nets)):
        reference, pad = key
        if reference not in board.footprints:
            continue  # its whole component is missing; reported above
        if key not in board.pad_nets:
            continue  # pad absent from the board footprint
        board_net = board.pad_nets[key]
        schematic_net = schematic.pad_nets.get(key, "")
        resolved = (
            result.net_name_map.get(schematic_net, schematic_net) if schematic_net else ""
        )
        if board_net != resolved:
            result.pad_net_changes.append(
                PadNetChange(
                    reference=reference,
                    pad=pad,
                    board_net=board_net,
                    schematic_net=schematic_net,
                    resolved_net=resolved,
                )
            )
    return result


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_pad_nets(board: SList, changes: Iterable[PadNetChange]) -> int:
    """Write the schematic's connectivity onto the board's pads."""
    wanted = {(c.reference, c.pad): c.resolved_net for c in changes}
    if not wanted:
        return 0
    applied = 0
    for footprint in board.children("footprint"):
        reference = ""
        for prop in footprint.children("property"):
            fields = [a.value for a in prop[1:] if isinstance(a, Atom)]
            if fields and fields[0] == "Reference" and len(fields) > 1:
                reference = fields[1]
                break
        if not reference:
            continue
        for pad in footprint.children("pad"):
            number = pad[1].value if len(pad) > 1 and isinstance(pad[1], Atom) else ""
            target = wanted.get((reference, number))
            if target is None:
                continue
            net = pad.child("net")
            if target == "":
                if net is not None:
                    pad[:] = [n for n in pad if n is not net]
                    applied += 1
                continue
            if net is None:
                pad.append(SList([Atom("net"), Atom(target, True)]))
            else:
                net[:] = [Atom("net"), Atom(target, True)]
            applied += 1
    return applied


def apply_values(board: SList, mismatches: Iterable[tuple[str, str, str]]) -> int:
    """Copy component values from the schematic onto the board's footprints."""
    wanted = {reference: value for reference, _board, value in mismatches}
    if not wanted:
        return 0
    applied = 0
    for footprint in board.children("footprint"):
        reference = ""
        value_prop = None
        for prop in footprint.children("property"):
            fields = [a.value for a in prop[1:] if isinstance(a, Atom)]
            if not fields:
                continue
            if fields[0] == "Reference" and len(fields) > 1:
                reference = fields[1]
            elif fields[0] == "Value":
                value_prop = prop
        target = wanted.get(reference)
        if target is None or value_prop is None:
            continue
        if len(value_prop) > 2 and isinstance(value_prop[2], Atom):
            value_prop[2] = Atom(target, True)
            applied += 1
    return applied


def remove_footprints(board: SList, references: Iterable[str]) -> int:
    """Delete board footprints that the schematic no longer contains."""
    targets = set(references)
    if not targets:
        return 0
    removed = 0
    survivors: list[Node] = []
    for node in board:
        if isinstance(node, SList) and node.tag == "footprint":
            reference = ""
            for prop in node.children("property"):
                fields = [a.value for a in prop[1:] if isinstance(a, Atom)]
                if fields and fields[0] == "Reference" and len(fields) > 1:
                    reference = fields[1]
                    break
            if reference in targets:
                removed += 1
                continue
        survivors.append(node)
    if removed:
        board[:] = survivors
    return removed


__all__ = [
    "AnnotationDiff",
    "BoardModel",
    "PadNetChange",
    "SchematicComponent",
    "SchematicModel",
    "apply_pad_nets",
    "apply_values",
    "diff",
    "map_net_names",
    "parse_board",
    "parse_netlist",
    "remove_footprints",
]
