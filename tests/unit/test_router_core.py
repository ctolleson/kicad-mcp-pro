"""Headless Specctra SES -> .kicad_pcb applier (work order P4-T1)."""

from __future__ import annotations

from kicad_mcp.utils.router_core import (
    apply_ses_to_pcb,
    parse_net_numbers,
    parse_ses,
    render_pcb_items,
)

# 1 mm = 1000 units at (resolution um 1); board Y is negated in the session
# (board y=5 mm -> -5000). The applier scales by the declared resolution and un-flips Y.
_SES = """(session test.dsn
  (routes
    (resolution um 1)
    (network_out
      (net "GND"
        (wire (path F.Cu 250 2500 -5000 11000 -5000) (type protect))
        (wire (path B.Cu 250 11000 -5000 11000 -6000))
        (via "Via[0-1]_600:300_um" 11000 -5000)
      )
      (net "VCC"
        (wire (path "F.Cu" 200 20000 -20000 25000 -20000))
      )
    )
  )
)"""

_PCB = '(kicad_pcb\n\t(version 20250216)\n\t(net 0 "")\n\t(net 1 "GND")\n\t(net 2 "VCC")\n)\n'


def test_parse_ses_scales_and_unflips_coordinates() -> None:
    route = parse_ses(_SES)
    assert route.units_per_mm == 1000.0  # (resolution um 1) -> 1000 units per mm
    assert len(route.segments) == 3
    assert len(route.vias) == 1

    gnd_f = next(s for s in route.segments if s.net_name == "GND" and s.layer == "F.Cu")
    # 2500 units / 1000 = 2.5 mm; session Y is negated, so -5000 -> +5.0 mm board Y.
    assert gnd_f.start == (2.5, 5.0)
    assert gnd_f.end == (11.0, 5.0)
    assert gnd_f.width_mm == 0.25  # 250 units / 1000

    via = route.vias[0]
    assert via.at == (11.0, 5.0)
    # Padstack Via[0-1]_600:300_um -> 0.6 mm size, 0.3 mm drill.
    assert via.size_mm == 0.6
    assert via.drill_mm == 0.3
    assert via.layers == ("F.Cu", "B.Cu")


def test_parse_net_numbers_reads_the_pcb_net_table() -> None:
    assert parse_net_numbers(_PCB) == {"": 0, "GND": 1, "VCC": 2}


def test_apply_ses_is_deterministic_and_round_trip_safe() -> None:
    out_a, route = apply_ses_to_pcb(_PCB, _SES)
    out_b, _ = apply_ses_to_pcb(_PCB, _SES)

    # Byte-identical on re-apply (deterministic UUIDs, sorted, fixed formatting).
    assert out_a == out_b

    # The original document is preserved verbatim; routing is appended inside the block.
    assert "(version 20250216)" in out_a
    assert '(net 1 "GND")' in out_a
    assert out_a.rstrip().endswith(")")

    # Net names are mapped to numbers; GND -> 1, VCC -> 2.
    assert '(layer "F.Cu") (net 1)' in out_a  # GND segment
    assert '(layer "F.Cu") (net 2)' in out_a  # VCC segment
    assert '(via (at 11 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)' in out_a
    # Three segments + one via were added.
    assert out_a.count("(segment ") == 3
    assert out_a.count("(via ") == 1


def test_apply_ses_is_idempotent_on_re_application() -> None:
    # Applying the SES to an already-routed board replaces (not duplicates) our items.
    once, _ = apply_ses_to_pcb(_PCB, _SES)
    twice, _ = apply_ses_to_pcb(once, _SES)
    assert once == twice
    assert twice.count("(segment ") == 3
    assert twice.count("(via ") == 1


def test_nets_absent_from_the_pcb_table_are_skipped() -> None:
    # A board that only declares GND drops the VCC routing rather than emitting (net None).
    pcb_gnd_only = '(kicad_pcb\n\t(version 20250216)\n\t(net 1 "GND")\n)\n'
    out, _ = apply_ses_to_pcb(pcb_gnd_only, _SES)
    assert "(net 1)" in out
    assert out.count("(segment ") == 2  # only the two GND segments
    assert "(net 2)" not in out


def test_empty_route_leaves_the_board_unchanged() -> None:
    empty_ses = "(session x.dsn (routes (resolution um 1) (network_out)))"
    out, route = apply_ses_to_pcb(_PCB, empty_ses)
    assert out == _PCB
    assert route.segments == []


def test_render_pcb_items_orders_segments_before_vias_deterministically() -> None:
    route = parse_ses(_SES)
    rendered = render_pcb_items(route, {"GND": 1, "VCC": 2})
    assert rendered.index("(segment ") < rendered.index("(via ")
    # Stable order: same input -> same string.
    assert rendered == render_pcb_items(parse_ses(_SES), {"GND": 1, "VCC": 2})


# --- Real-world Freerouting output -----------------------------------------

# Freerouting 2.4.1 applies the resolution factor: with (resolution um 10) a point at
# 173.765 mm is written as 1737650, i.e. 1000 x 10 = 10000 units per mm. Ignoring the
# factor scaled every routed track 10x and placed the routing off-board.
_SES_FREEROUTING = """(session hat.dsn
  (routes
    (resolution um 10)
    (network_out
      (net "GND"
        (wire (path F.Cu 2000 1515400 -749500 1502700 -762200))
        (via "Via[0-3]_600:300_um" 1473207 -750198)
      )
    )
  )
)"""

# KiCad 10 writes the net name on each item and declares no board-level net table.
_PCB_KICAD10 = '(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n)\n'


def test_resolution_factor_is_applied_to_coordinates() -> None:
    route = parse_ses(_SES_FREEROUTING)
    assert route.units_per_mm == 10000.0
    segment = route.segments[0]
    assert segment.start == (151.54, 74.95)
    assert segment.end == (150.27, 76.22)
    assert segment.width_mm == 0.2
    assert route.vias[0].at == (147.3207, 75.0198)


def test_kicad_10_boards_have_no_net_table() -> None:
    """Not a parse failure - KiCad 10 genuinely stopped writing one."""
    assert parse_net_numbers(_PCB_KICAD10) == {}


def test_routing_applies_to_a_kicad_10_board_by_net_name() -> None:
    """Regression: this silently dropped every segment and reported success.

    render_pcb_items skipped any net missing from the board's net table. On KiCad 10
    there is no table, so nothing rendered, apply_ses_to_pcb returned the board
    unchanged, and route_apply_ses read that as 'already applied'.
    """
    out, route = apply_ses_to_pcb(_PCB_KICAD10, _SES_FREEROUTING)
    assert out != _PCB_KICAD10, "routing was silently discarded"
    assert len(route.segments) == 1
    assert '(net "GND")' in out
    assert out.count("(segment ") == 1
    assert out.count("(via ") == 1


def test_kicad_10_routing_is_still_idempotent() -> None:
    once, _ = apply_ses_to_pcb(_PCB_KICAD10, _SES_FREEROUTING)
    twice, _ = apply_ses_to_pcb(once, _SES_FREEROUTING)
    assert once == twice
    assert twice.count("(segment ") == 1


def test_numbered_boards_still_use_numeric_net_tokens() -> None:
    """A KiCad 9 board must keep the (net N) form, not switch to names."""
    out, _ = apply_ses_to_pcb(_PCB, _SES)
    assert "(net 1)" in out
    assert '(net "GND")' not in out


def test_unknown_net_is_still_skipped_when_the_board_declares_a_table() -> None:
    """With a table present, an unmatched net is a real mismatch and must not be
    silently emitted by name."""
    pcb_gnd_only = '(kicad_pcb\n\t(version 20250216)\n\t(net 1 "GND")\n)\n'
    out, _ = apply_ses_to_pcb(pcb_gnd_only, _SES)
    assert '(net "VCC")' not in out
    assert out.count("(segment ") == 2
