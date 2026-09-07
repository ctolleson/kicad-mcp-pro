"""Board-file edits for KiCad menu commands that have no headless verb.

KiCad's Edit and Tools menus carry a family of bulk operations — Swap Layers,
Global Deletions, Cleanup Tracks & Vias, the Zone Manager — that exist only as
modal dialogs. Neither ``kicad-cli`` nor the IPC API exposes them, so the only
headless route is the board file itself.

Everything here is a pure function over a parsed tree (:mod:`kicad_mcp.utils.sexpr_tree`),
so the operations are testable without touching disk. Persisting is the caller's
job, through the existing transactional board write.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..utils.sexpr_tree import Atom, Node, SList

# Top-level board items, grouped the way KiCad's Global Deletions dialog groups them.
ITEM_GROUPS: dict[str, tuple[str, ...]] = {
    "tracks": ("segment", "arc"),
    "vias": ("via",),
    "zones": ("zone",),
    "graphics": ("gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_curve", "gr_bbox"),
    "text": ("gr_text", "gr_text_box"),
    "dimensions": ("dimension",),
    "footprints": ("footprint",),
    "groups": ("group",),
    "images": ("image",),
}

# Layer tokens that mean "resolve at plot time"; a layer swap must not touch them.
_WILDCARD_LAYERS = frozenset({"*.Cu", "*.Mask", "*.Paste", "*.SilkS", "F&B.Cu", "*"})


@dataclass
class EditReport:
    """What an edit changed, for reporting back to the agent."""

    counts: dict[str, int] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)

    def add(self, key: str, amount: int = 1) -> None:
        if amount:
            self.counts[key] = self.counts.get(key, 0) + amount

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# ---------------------------------------------------------------------------
# Net helpers
# ---------------------------------------------------------------------------


def net_declarations(root: SList) -> dict[int, str]:
    """Map net code -> net name from ``(net N "name")`` declarations.

    KiCad 9 and earlier declare the net table at board level and have items refer to
    it by code. KiCad 10 dropped the table and writes the name on the item itself, so
    this returns an empty mapping for a 10.x board — which is correct, not a failure.
    """
    mapping: dict[int, str] = {}
    for net in root.children("net"):
        if len(net) >= 3 and isinstance(net[1], Atom) and isinstance(net[2], Atom):
            try:
                mapping[int(net[1].value)] = net[2].value
            except ValueError:
                continue
    return mapping


def _item_net_name(item: SList, declarations: dict[int, str]) -> str | None:
    """The net an item belongs to, as a name, across both board formats.

    KiCad 10 writes ``(net "GND")``; KiCad 9 and earlier write ``(net 3)`` against the
    board's net table. Both resolve to the same name here.
    """
    net = item.child("net")
    if net is None or len(net) < 2 or not isinstance(net[1], Atom):
        return None
    token = net[1]
    if token.quoted:
        return token.value
    try:
        return declarations.get(int(token.value), token.value)
    except ValueError:
        return token.value


def board_net_names(root: SList) -> set[str]:
    """Every net name on the board, however the file format records it."""
    declarations = net_declarations(root)
    names = set(declarations.values())
    for node in root.walk():
        name = _item_net_name(node, declarations) if node.tag != "net" else None
        if name:
            names.add(name)
    return names


def _resolve_net_names(root: SList, nets: Iterable[str]) -> set[str]:
    """Accept net names, or numeric codes on boards that still declare a net table."""
    declarations = net_declarations(root)
    known = board_net_names(root)
    resolved: set[str] = set()
    for raw in nets:
        text = str(raw).strip()
        if text in known:
            resolved.add(text)
            continue
        if text.isdigit() and int(text) in declarations:
            resolved.add(declarations[int(text)])
            continue
        sample = ", ".join(sorted(known)[:12])
        raise ValueError(f"Unknown net '{text}'. Known nets include: {sample}")
    return resolved


def _item_layers(item: SList) -> list[str]:
    """Every layer an item sits on: a single ``(layer …)`` or a ``(layers …)`` set."""
    found: list[str] = []
    single = item.child("layer")
    if single is not None:
        found.extend(a.value for a in single[1:] if isinstance(a, Atom))
    multi = item.child("layers")
    if multi is not None:
        found.extend(a.value for a in multi[1:] if isinstance(a, Atom))
    return found


# ---------------------------------------------------------------------------
# Swap Layers  (Edit > Swap Layers…)
# ---------------------------------------------------------------------------


def swap_layers(root: SList, mapping: dict[str, str]) -> EditReport:
    """Move items between layers, the way Edit > Swap Layers does.

    The mapping is applied **simultaneously**, so ``{"F.Cu": "B.Cu", "B.Cu": "F.Cu"}``
    is a true swap rather than two sequential renames that collapse into one layer.
    Layer *definitions* in the board's ``(layers …)`` stack are left alone — the
    dialog moves objects, it does not rename the stackup.
    """
    if not mapping:
        raise ValueError("No layer mapping supplied.")

    report = EditReport()
    # The board's layer table is a definition, not item placement; never rewrite it.
    layer_table = root.child("layers")

    for node in root.walk():
        if node is layer_table:
            continue
        if node.tag not in {"layer", "layers"}:
            continue
        # A (layers …) inside the stackup's setup block is also a definition.
        for index, token in enumerate(node[1:], start=1):
            if not isinstance(token, Atom):
                continue
            if token.value in _WILDCARD_LAYERS:
                continue
            replacement = mapping.get(token.value)
            if replacement is not None and replacement != token.value:
                node[index] = Atom(replacement, token.quoted)
                report.add(f"{token.value} -> {replacement}")
    return report


# ---------------------------------------------------------------------------
# Global Deletions  (Edit > Global Deletions…)
# ---------------------------------------------------------------------------


def global_delete(
    root: SList,
    *,
    item_types: Iterable[str],
    layers: Iterable[str] = (),
    nets: Iterable[str] = (),
    locked: bool | None = None,
) -> EditReport:
    """Delete top-level board items by class, optionally filtered by layer and net.

    ``locked=False`` skips locked items, matching the dialog's default of leaving
    locked objects alone; ``locked=None`` ignores lock state entirely.
    """
    requested = [str(t).strip().casefold() for t in item_types]
    unknown = [t for t in requested if t not in ITEM_GROUPS]
    if unknown:
        raise ValueError(
            f"Unknown item type(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(sorted(ITEM_GROUPS))}."
        )
    if not requested:
        raise ValueError("No item types supplied; nothing would be deleted.")

    tags: set[str] = set()
    tag_to_group: dict[str, str] = {}
    for group in requested:
        for tag in ITEM_GROUPS[group]:
            tags.add(tag)
            tag_to_group[tag] = group

    layer_filter = {str(name) for name in layers}
    net_filter = _resolve_net_names(root, nets) if nets else set()
    declarations = net_declarations(root)

    report = EditReport()
    survivors: list[Node] = []
    for node in root:
        if not isinstance(node, SList) or node.tag not in tags:
            survivors.append(node)
            continue
        if layer_filter and not (set(_item_layers(node)) & layer_filter):
            survivors.append(node)
            continue
        if net_filter and _item_net_name(node, declarations) not in net_filter:
            survivors.append(node)
            continue
        if locked is not None:
            is_locked = node.value_of("locked") == "yes" or any(
                isinstance(c, Atom) and c.value == "locked" for c in node
            )
            if is_locked and not locked:
                survivors.append(node)
                continue
        report.add(tag_to_group[node.tag])

    if report.total:
        root[:] = survivors
    return report


# ---------------------------------------------------------------------------
# Cleanup Tracks & Vias  (Tools > Cleanup Tracks & Vias…)
# ---------------------------------------------------------------------------


type _TrackSignature = tuple[str, tuple[tuple[str, ...], ...], str, str, str | None]


def _track_signature(item: SList) -> _TrackSignature | None:
    start = item.child("start")
    end = item.child("end")
    if start is None or end is None:
        return None

    def point(node: SList) -> tuple[str, ...]:
        return tuple(a.value for a in node[1:] if isinstance(a, Atom))

    p0, p1 = point(start), point(end)
    if len(p0) < 2 or len(p1) < 2:
        return None
    layer = item.value_of("layer") or ""
    width = item.value_of("width") or ""
    net = _item_net_name(item, {})
    # A track is the same segment whichever way round it was drawn.
    ends = tuple(sorted((p0, p1)))
    return (item.tag, ends, layer, width, net)


def _is_zero_length(item: SList) -> bool:
    start, end = item.child("start"), item.child("end")
    if start is None or end is None:
        return False

    def coords(node: SList) -> tuple[float, ...] | None:
        try:
            return tuple(float(a.value) for a in node[1:] if isinstance(a, Atom))
        except ValueError:
            return None

    p0, p1 = coords(start), coords(end)
    if p0 is None or p1 is None or len(p0) < 2 or len(p1) < 2:
        return False
    return p0[:2] == p1[:2]


type _ViaSignature = tuple[tuple[str, ...], tuple[str, ...], str, str, str | None]


def _via_signature(item: SList) -> _ViaSignature | None:
    at = item.child("at")
    if at is None:
        return None
    position = tuple(a.value for a in at[1:] if isinstance(a, Atom))
    layers = tuple(_item_layers(item))
    size = item.value_of("size") or ""
    drill = item.value_of("drill") or ""
    return (position, layers, size, drill, _item_net_name(item, {}))


def cleanup_tracks_and_vias(
    root: SList,
    *,
    delete_zero_length: bool = True,
    delete_duplicate_tracks: bool = True,
    delete_duplicate_vias: bool = True,
) -> EditReport:
    """Remove zero-length and exactly-duplicated tracks and vias.

    Scope note: this deliberately does **not** merge collinear segments. Merging is
    only safe when the shared endpoint carries no other connection (a third track, a
    via, or a pad), which needs full connectivity — so it stays a KiCad-side
    operation rather than a half-correct file edit.
    """
    report = EditReport()
    seen_tracks: set[_TrackSignature] = set()
    seen_vias: set[_ViaSignature] = set()
    survivors: list[Node] = []

    for node in root:
        if not isinstance(node, SList):
            survivors.append(node)
            continue

        if node.tag in ITEM_GROUPS["tracks"]:
            if delete_zero_length and _is_zero_length(node):
                report.add("zero-length tracks")
                continue
            if delete_duplicate_tracks:
                track_signature = _track_signature(node)
                if track_signature is not None:
                    if track_signature in seen_tracks:
                        report.add("duplicate tracks")
                        continue
                    seen_tracks.add(track_signature)
        elif node.tag == "via" and delete_duplicate_vias:
            via_signature = _via_signature(node)
            if via_signature is not None:
                if via_signature in seen_vias:
                    report.add("duplicate vias")
                    continue
                seen_vias.add(via_signature)

        survivors.append(node)

    if report.total:
        root[:] = survivors
    return report


# ---------------------------------------------------------------------------
# Zone Manager  (Board > Zone Manager…)
# ---------------------------------------------------------------------------

_ZONE_FIELDS = {
    "priority": ("priority", False),
    "name": ("name", True),
    "min_thickness": ("min_thickness", False),
}


def list_zones(root: SList) -> list[dict[str, object]]:
    """Describe every zone: net, layers, priority, fill state and geometry size."""
    declarations = net_declarations(root)
    zones: list[dict[str, object]] = []
    for index, zone in enumerate(root.children("zone")):
        fill = zone.child("fill")
        filled = bool(fill and any(isinstance(a, Atom) and a.value == "yes" for a in fill[1:2]))
        net_name = _item_net_name(zone, declarations) or ""
        polygons = sum(1 for _ in zone.children("polygon"))
        zones.append(
            {
                "index": index,
                "name": zone.value_of("name") or "",
                "net": net_name,
                "layers": _item_layers(zone),
                "priority": int(zone.value_of("priority") or 0),
                "filled": filled,
                "min_thickness": zone.value_of("min_thickness") or "",
                "outline_polygons": polygons,
                "filled_polygons": sum(1 for _ in zone.children("filled_polygon")),
            }
        )
    return zones


def set_zone_properties(
    root: SList,
    *,
    index: int,
    priority: int | None = None,
    name: str | None = None,
    min_thickness: float | None = None,
    filled: bool | None = None,
) -> EditReport:
    """Edit one zone's properties, addressed by its index from :func:`list_zones`."""
    zones = root.children("zone")
    if not zones:
        raise ValueError("This board has no zones.")
    if not 0 <= index < len(zones):
        raise ValueError(f"Zone index {index} out of range (board has {len(zones)} zones).")

    zone = zones[index]
    report = EditReport()

    if priority is not None:
        if priority < 0:
            raise ValueError("Zone priority must be zero or greater.")
        zone.set_value("priority", str(int(priority)), quoted=False)
        report.add("priority")
    if name is not None:
        zone.set_value("name", name, quoted=True)
        report.add("name")
    if min_thickness is not None:
        if min_thickness <= 0:
            raise ValueError("Zone minimum thickness must be greater than zero.")
        zone.set_value("min_thickness", f"{min_thickness:g}", quoted=False)
        report.add("min_thickness")
    if filled is not None:
        fill = zone.child("fill")
        if fill is None:
            fill = SList([Atom("fill")])
            zone.append(fill)
        # (fill yes …) / (fill …): the bare yes flag follows the tag directly.
        has_flag = len(fill) >= 2 and isinstance(fill[1], Atom) and fill[1].value in {"yes", "no"}
        if filled:
            if has_flag:
                fill[1] = Atom("yes")
            else:
                fill.insert(1, Atom("yes"))
        elif has_flag:
            del fill[1]
        if not filled:
            # An unfilled zone must not keep stale fill geometry.
            removed = [n for n in zone if isinstance(n, SList) and n.tag == "filled_polygon"]
            if removed:
                zone[:] = [
                    n for n in zone if not (isinstance(n, SList) and n.tag == "filled_polygon")
                ]
                report.add("cleared filled polygons", len(removed))
        report.add("fill state")

    return report


__all__ = [
    "ITEM_GROUPS",
    "EditReport",
    "cleanup_tracks_and_vias",
    "global_delete",
    "list_zones",
    "board_net_names",
    "net_declarations",
    "set_zone_properties",
    "swap_layers",
]
