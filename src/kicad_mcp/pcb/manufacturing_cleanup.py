"""Bounded, non-routing PCB cleanup over the standard parsed board representation."""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence

from ..utils.sexpr_tree import Atom, SList
from .file_edits import EditReport

_SILK_TO_FAB = {"F.SilkS": "F.Fab", "B.SilkS": "B.Fab"}
_GRAPHICS = {
    f"{prefix}_{kind}"
    for prefix in ("gr", "fp")
    for kind in ("line", "arc", "circle", "rect", "poly", "curve", "text", "text_box")
} | {"property"}
_ISLAND_MODES = {"always": "0", "never": "1", "below_area": "2"}
_COPPER_LAYERS = {"F.Cu", "B.Cu"} | {f"In{n}.Cu" for n in range(1, 31)}


def _locked(node: SList) -> bool:
    return node.value_of("locked") == "yes" or any(
        isinstance(n, Atom) and n.value == "locked" for n in node[1:]
    )


def _walk(node: SList, inherited_lock: bool = False) -> Iterator[tuple[SList, bool]]:
    locked = inherited_lock or _locked(node)
    yield node, locked
    for child in node:
        if isinstance(child, SList):
            yield from _walk(child, locked)


def _select(root: SList, identifiers: Sequence[str]) -> list[SList]:
    """Resolve the entire selection before a caller is allowed to mutate anything."""
    if root.tag != "kicad_pcb":
        raise ValueError("Expected a kicad_pcb board.")
    if not identifiers or any(not identifier.strip() for identifier in identifiers):
        raise ValueError("Supply a non-empty list of item UUIDs.")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate item UUIDs in selection.")
    matches: dict[str, list[tuple[SList, bool]]] = {key: [] for key in identifiers}
    for node, locked in _walk(root):
        key = node.value_of("uuid") or node.value_of("tstamp")
        if key in matches:
            matches[key].append((node, locked))
    selected = []
    for key, items in matches.items():
        if len(items) != 1:
            raise ValueError(f"Item UUID {key!r} is missing or ambiguous; inspect the board again.")
        node, locked = items[0]
        if locked:
            raise ValueError(f"Item UUID {key!r} or its parent is locked.")
        selected.append(node)
    return selected


def move_silkscreen_to_fab(root: SList, *, item_ids: Sequence[str]) -> EditReport:
    """Move explicitly selected silk artwork to same-side fabrication documentation.

    Footprint-owned artwork is supported. Geometry, text, UUIDs and reference
    values are retained. This does not clip artwork or certify replacement pin-1
    markings: rendering and assembly review remain necessary.
    """
    selected = _select(root, item_ids)
    for node in selected:
        if node.tag not in _GRAPHICS or node.value_of("layer") not in _SILK_TO_FAB:
            raise ValueError("Only silkscreen graphics/text/fields may be moved to Fab.")
    report = EditReport()
    for identifier, node in zip(item_ids, selected, strict=True):
        source = node.value_of("layer") or ""
        target = _SILK_TO_FAB[source]
        node.set_value("layer", target)
        report.add("artwork moved")
        report.details.append(f"{identifier}: {source} -> {target}")
    report.details.append(
        "Render and run DRC; retain readable references and polarity/pin-1 markings. "
        "Footprint library artwork is not changed; reconcile it before a library update."
    )
    return report


def set_zone_island_policy(
    root: SList,
    *,
    zone_ids: Sequence[str],
    removal: str,
    area_min_mm2: float | None = None,
) -> EditReport:
    """Set native island policy, invalidating cached fill without inventing copper.

    KiCad file-format values: always=0, never=1, below-area=2. Area is mm^2.
    KiCad must refill and save the board afterwards; no connectivity or release
    claim can be made from this policy change alone.
    """
    if removal not in _ISLAND_MODES:
        raise ValueError("removal must be always, never, or below_area.")
    if area_min_mm2 is not None and (not math.isfinite(area_min_mm2) or area_min_mm2 < 0):
        raise ValueError("area_min_mm2 must be finite and non-negative.")
    if removal == "below_area" and area_min_mm2 is None:
        raise ValueError("below_area requires area_min_mm2.")
    selected = _select(root, zone_ids)
    for zone in selected:
        if zone.tag != "zone" or not any(zone is child for child in root.children("zone")):
            raise ValueError("Only top-level copper zones may be selected.")
        if zone.child("keepout") is not None:
            raise ValueError("A rule area is not a copper zone.")
        layers = zone.child("layers") or zone.child("layer")
        if (
            layers is None
            or len(layers) < 2
            or any(
                not isinstance(layer, Atom) or layer.value not in _COPPER_LAYERS
                for layer in layers[1:]
            )
        ):
            raise ValueError("Only zones on explicit copper layers may be selected.")
    report = EditReport()
    for identifier, zone in zip(zone_ids, selected, strict=True):
        fill = zone.child("fill")
        if fill is None:
            fill = SList([Atom("fill")])
            zone.append(fill)
        fill.set_value("island_removal_mode", _ISLAND_MODES[removal], quoted=False)
        if area_min_mm2 is not None:
            fill.set_value("island_area_min", f"{area_min_mm2:g}", quoted=False)
        if len(fill) > 1 and isinstance(fill[1], Atom) and fill[1].value in {"yes", "no"}:
            del fill[1]
        zone[:] = [
            node
            for node in zone
            if not (isinstance(node, SList) and node.tag in {"filled_polygon", "fill_segments"})
        ]
        report.add("zone policies changed")
        report.details.append(f"{identifier}: {removal}; cached fill cleared")
    report.details.append("Refill with KiCad, save the board, and rerun DRC/unconnected checks.")
    return report
