# Bounded PCB Manufacturing Cleanup

These file-backed tools address two common imported-board findings without
rerouting the design or loosening manufacturing constraints. They default to
`dry_run=true` and belong to the `pcb_write` category, not the compact review
profile. Use a write-enabled profile such as `agent_full`.

## Workflow

1. Save and close the board in the PCB editor before file-backed writes. Commit
   a checkpoint. These tools do not detect unsaved GUI changes.
2. Run native DRC and inspect/render the affected objects. Use exact item UUIDs
   from DRC or board inspection, not reference guesses or selection indices.
3. Preview `pcb_move_silkscreen_to_fab(item_ids=[...])` for the selected artwork.
   Apply with `dry_run=false` only after reviewing the selection. Both board
   graphics and graphics inside footprints are supported. Geometry, text,
   references, pads, tracks and UUIDs are preserved; only the selected silk
   objects' layers change to same-side Fab. This is not automatic clipping.
4. Preserve readable silkscreen references, connector labels and polarity/pin-1
   marks. Do not move essential assembly markings merely to silence DRC.
   Library files are unchanged; reconcile footprint artwork before later library
   updates to avoid restoring the original collisions.
5. Preview `pcb_set_zone_island_policy(zone_ids=[...], removal="always")`.
   `never` and `below_area` are also supported; `below_area` requires an explicit
   finite, non-negative `area_min_mm2`. Rule areas and locked zones are rejected.
   All IDs and types are checked before any changes are made.
6. Apply, then refill and save using native KiCad. Setting the policy clears
   cached fills; it does not calculate island connectivity or remove copper by
   itself. KiCad preserves fill in entirely unconnected zones regardless of this
   policy; inspect those separately instead of assuming `always` removes them.
   For KiCad versions exposing these CLI options:

   ```sh
   kicad-cli pcb drc --refill-zones --save-board --format json \
     --output after-cleanup.json board.kicad_pcb
   ```

7. Review the new DRC report, unconnected nets, rendered copper and silkscreen,
   and schematic-to-board parity. Regenerate fabrication and assembly outputs
   only from the saved, reviewed board. Native DRC success does not certify BOM
   correctness or placement rotations.

The island policy uses native KiCad values: always=0, never=1, below-area=2.
See the [KiCad file format](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/index.html)
and [island-removal enumeration](https://docs.kicad.org/doxygen/zone__settings_8h.html).

## Tool Discovery

Catalog presence is not proof that a client can call a tool. Check the running
server version, configured profile, read/write permissions, runtime capability
filter, and finally the client's refreshed tool list. Existing IPC editing tools
still require a live editor connection. These two cleanup tools explicitly
declare no live IPC requirement; review/build profile limits are unchanged.
Restart/reconnect the MCP client after deploying the updated server.

## Next Priorities

- Report per-tool availability with exact profile, runtime and policy blockers.
- Add clipped/repositioned artwork previews and footprint-library reconciliation.
- Add expected-board-hash and unsaved-GUI conflict checks to board transactions.
- Validate manufacturer profiles against native KiCad custom-rule syntax.
- Verify schematic/board UUID links before synchronization, with a duplication
  preview and a separately approved reference-based relink operation.
- Add supplier-backed package substitution checks for electrical ratings, stock,
  footprint pad mapping and BOM provenance before downsizing components.
- Produce package-specific CPL pin-1/centroid evidence and assembler-preview
  signoff, rather than applying universal rotation offsets.

For JLCPCB, positive CPL angles are counterclockwise; package-specific zero
orientation still needs verification in the assembly preview. See
[JLCPCB CPL documentation](https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly).
Neither cleanup tool approves a manufacturing release.
