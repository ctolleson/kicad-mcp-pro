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

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..utils.sexpr_tree import Atom, Node, SList, parse

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


def board_copper_layers(root: SList) -> list[str]:
    """Copper layer names the board declares, in stack order."""
    layers = root.child("layers")
    if layers is None:
        return []
    names: list[str] = []
    for entry in layers:
        if isinstance(entry, SList) and len(entry) >= 2 and isinstance(entry[1], Atom):
            name = entry[1].value
            if name.endswith(".Cu"):
                names.append(name)
    return names


def board_outline_rectangle(root: SList, *, inset: float = 0.5) -> list[tuple[float, float]]:
    """A rectangle just inside the board edge, for a full-board pour.

    Derived from the bounding box of everything on ``Edge.Cuts``. A rectangle is
    deliberate: a pour only has to be *contained* by the outline, and KiCad clips the
    fill to the real edge anyway, so following a complex outline vertex by vertex buys
    nothing and risks tracing an arc wrongly.
    """
    xs: list[float] = []
    ys: list[float] = []
    for node in root.walk():
        if not isinstance(node, SList):
            continue
        layer = node.child("layer")
        if layer is None or len(layer) < 2 or str(layer[1]) != "Edge.Cuts":
            continue
        for tag in ("start", "end", "center", "mid"):
            point = node.child(tag)
            if point is not None and len(point) >= 3:
                try:
                    xs.append(float(str(point[1])))
                    ys.append(float(str(point[2])))
                except ValueError:
                    continue
        for pts in node.children("pts"):
            for xy in pts.children("xy"):
                if len(xy) >= 3:
                    try:
                        xs.append(float(str(xy[1])))
                        ys.append(float(str(xy[2])))
                    except ValueError:
                        continue
    if not xs or not ys:
        raise ValueError("This board has no Edge.Cuts geometry, so its outline is unknown.")
    x0, x1 = min(xs) + inset, max(xs) - inset
    y0, y1 = min(ys) + inset, max(ys) - inset
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise ValueError(f"An inset of {inset} mm leaves no area inside this board outline.")
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


_PAD_CONNECTION = {"thermal", "solid", "none", "thru_hole_only"}


def create_zone(
    root: SList,
    *,
    net: str,
    layers: Sequence[str],
    polygon: Sequence[tuple[float, float]],
    priority: int = 0,
    min_thickness: float = 0.25,
    clearance: float = 0.5,
    thermal_gap: float = 0.5,
    thermal_bridge_width: float = 0.5,
    pad_connection: str = "thermal",
    name: str = "",
    uuid_str: str | None = None,
) -> EditReport:
    """Add an unfilled copper zone, written straight into the board file.

    The zone is created *unfilled*: computing the fill needs KiCad's geometry engine.
    Fill it headlessly with::

        kicad-cli pcb drc --refill-zones --save-board board.kicad_pcb

    Both flags are required. Plain ``pcb drc`` fills only in memory to run the check
    and leaves the file's copper untouched, so a zone can look filled to DRC and still
    carry no copper on disk.

    The net must already exist on the board. A zone naming a net that is not there
    would look correct in the file and pour nothing, so a typo is rejected rather than
    written.
    """
    if pad_connection not in _PAD_CONNECTION:
        raise ValueError(
            f"pad_connection must be one of {sorted(_PAD_CONNECTION)}, got '{pad_connection}'."
        )
    if min_thickness <= 0:
        raise ValueError("Zone minimum thickness must be greater than zero.")
    if priority < 0:
        raise ValueError("Zone priority must be zero or greater.")

    net_name = next(iter(_resolve_net_names(root, [net])))

    available = board_copper_layers(root)
    requested = [str(layer) for layer in layers]
    if not requested:
        raise ValueError("A copper zone needs at least one layer.")
    unknown = [layer for layer in requested if layer not in available]
    if unknown:
        listed = ", ".join(available) or "(none declared)"
        raise ValueError(f"Unknown copper layer(s) {unknown}. This board has: {listed}")

    points: list[tuple[float, float]] = []
    for x, y in polygon:
        point = (round(float(x), 6), round(float(y), 6))
        if point not in points:
            points.append(point)
    if len(points) < 3:
        raise ValueError("A zone outline needs at least three distinct corners.")

    zone = SList([Atom("zone")])
    declarations = net_declarations(root)
    if declarations:
        codes = {name_: code for code, name_ in declarations.items()}
        zone.append(SList([Atom("net"), Atom(str(codes[net_name]))]))
    else:
        # KiCad 10 records the name on the item; there is no board-level net table.
        zone.append(SList([Atom("net"), Atom(net_name, quoted=True)]))
    if len(requested) == 1:
        zone.append(SList([Atom("layer"), Atom(requested[0], quoted=True)]))
    else:
        zone.append(SList([Atom("layers"), *(Atom(x, quoted=True) for x in requested)]))
    zone.append(SList([Atom("uuid"), Atom(uuid_str or str(uuid.uuid4()), quoted=True)]))
    if name:
        zone.append(SList([Atom("name"), Atom(name, quoted=True)]))
    zone.append(SList([Atom("hatch"), Atom("edge"), Atom("0.5")]))
    if priority:
        zone.append(SList([Atom("priority"), Atom(str(int(priority)))]))

    connect = SList([Atom("connect_pads")])
    if pad_connection == "solid":
        connect.append(Atom("yes"))
    elif pad_connection == "none":
        connect.append(Atom("no"))
    elif pad_connection == "thru_hole_only":
        connect.append(Atom("thru_hole_only"))
    connect.append(SList([Atom("clearance"), Atom(f"{clearance:g}")]))
    zone.append(connect)

    zone.append(SList([Atom("min_thickness"), Atom(f"{min_thickness:g}")]))
    zone.append(
        SList(
            [
                Atom("fill"),
                SList([Atom("thermal_gap"), Atom(f"{thermal_gap:g}")]),
                SList([Atom("thermal_bridge_width"), Atom(f"{thermal_bridge_width:g}")]),
            ]
        )
    )
    pts = SList([Atom("pts")])
    for x, y in points:
        pts.append(SList([Atom("xy"), Atom(f"{x:g}"), Atom(f"{y:g}")]))
    zone.append(SList([Atom("polygon"), pts]))

    root.append(zone)
    report = EditReport()
    report.add(f"zone on {'+'.join(requested)} for net '{net_name}' ({len(points)} corners)")
    report.add("left unfilled - fill with 'kicad-cli pcb drc --refill-zones --save-board'")
    return report


# Side-specific layers are mirrored when a footprint is placed on the back.
_SIDE_FLIP = {
    "F.Cu": "B.Cu", "B.Cu": "F.Cu",
    "F.SilkS": "B.SilkS", "B.SilkS": "F.SilkS",
    "F.Mask": "B.Mask", "B.Mask": "F.Mask",
    "F.Paste": "B.Paste", "B.Paste": "F.Paste",
    "F.CrtYd": "B.CrtYd", "B.CrtYd": "F.CrtYd",
    "F.Fab": "B.Fab", "B.Fab": "F.Fab",
}


def board_references(root: SList) -> set[str]:
    """Every reference designator already placed on the board."""
    found: set[str] = set()
    for footprint in root.children("footprint"):
        for prop in footprint.children("property"):
            if len(prop) >= 3 and str(prop[1]) == "Reference":
                found.add(str(prop[2]))
    return found


def _flip_to_back(node: Node) -> None:
    """Rewrite every side-specific layer token in a subtree to its opposite side."""
    if not isinstance(node, SList):
        return
    if node.tag in {"layer", "layers"}:
        for index, item in enumerate(node[1:], start=1):
            if isinstance(item, Atom) and item.value in _SIDE_FLIP:
                node[index] = Atom(_SIDE_FLIP[item.value], quoted=item.quoted)
        return
    for child in node:
        _flip_to_back(child)


def place_footprint(
    root: SList,
    *,
    footprint_text: str,
    library: str,
    footprint: str,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float = 0.0,
    side: str = "front",
    value: str | None = None,
    uuid_str: str | None = None,
) -> EditReport:
    """Place a library footprint onto the board, headlessly.

    ``footprint_text`` is the ``.kicad_mod`` source. A board footprint is not the same
    shape as a library one: it drops the library's ``version`` and ``generator``, and
    gains a ``uuid`` and an ``(at ...)`` placement. Reference uniqueness is enforced,
    because two footprints sharing a designator make the board disagree with the
    schematic in a way that only shows up much later.

    The pads carry no nets. A footprint placed this way is mechanically present and
    electrically isolated until the schematic is linked, which is the honest state for
    a part the schematic does not yet know about.
    """
    if side not in {"front", "back"}:
        raise ValueError(f"side must be 'front' or 'back', got '{side}'.")
    reference = reference.strip()
    if not reference:
        raise ValueError("A placed footprint needs a reference designator.")
    existing = board_references(root)
    if reference in existing:
        raise ValueError(f"Reference '{reference}' is already on this board.")

    parsed = parse(footprint_text)
    source = parsed if isinstance(parsed, SList) and parsed.tag == "footprint" else None
    if source is None and isinstance(parsed, list):
        source = next(
            (n for n in parsed if isinstance(n, SList) and n.tag == "footprint"),
            None,
        )
    if source is None:
        raise ValueError("That file does not contain a footprint definition.")

    block = SList([Atom("footprint"), Atom(f"{library}:{footprint}", quoted=True)])
    for child in source[2:]:
        if isinstance(child, SList) and child.tag in {"version", "generator", "generator_version"}:
            continue
        block.append(child)

    if side == "back":
        _flip_to_back(block)

    placement = [Atom("at"), Atom(f"{x_mm:g}"), Atom(f"{y_mm:g}")]
    if rotation:
        placement.append(Atom(f"{rotation:g}"))
    # KiCad writes layer, then uuid, then at; keep that order so a round trip is quiet.
    insert_at = 2
    for index, child in enumerate(block):
        if isinstance(child, SList) and child.tag == "layer":
            insert_at = index + 1
            break
    block.insert(insert_at, SList([Atom("uuid"), Atom(uuid_str or str(uuid.uuid4()), quoted=True)]))
    block.insert(insert_at + 1, SList(placement))

    for prop in block.children("property"):
        if len(prop) >= 3 and str(prop[1]) == "Reference":
            prop[2] = Atom(reference, quoted=True)
        elif len(prop) >= 3 and str(prop[1]) == "Value" and value is not None:
            prop[2] = Atom(value, quoted=True)

    root.append(block)
    report = EditReport()
    report.add(f"placed {library}:{footprint} as {reference} at ({x_mm:g}, {y_mm:g}) on the {side}")
    pads = sum(1 for _ in block.children("pad"))
    if pads:
        report.add(f"{pads} pads, none connected to a net yet", pads)
    return report


__all__ = [
    "ITEM_GROUPS",
    "EditReport",
    "board_copper_layers",
    "board_references",
    "board_outline_rectangle",
    "create_zone",
    "place_footprint",
    "cleanup_tracks_and_vias",
    "global_delete",
    "list_zones",
    "board_net_names",
    "net_declarations",
    "set_zone_properties",
    "swap_layers",
]
