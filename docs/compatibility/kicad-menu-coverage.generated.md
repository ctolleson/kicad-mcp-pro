# KiCad Menu Coverage (generated)

Machine-generated from KiCad's own C++ menu sources plus `docs/compatibility/kicad-menu-bindings.yaml`. Refresh with `uv run python scripts/extract_kicad_menus.py` then `uv run python scripts/build_menu_index.py`.

KiCad baseline: `10.0.6`

**Overall: 94 / 152 headlessly-reachable menu commands driven = 61.8%** (32 partial, 26 gap; 194 GUI-only with no KiCad API, excluded from the denominator).

A command is `gui-only` when KiCad itself offers no cli, ipc or file path for it. Those are KiCad limits, not gaps in this server, so they leave the denominator — the same convention the capability-parity matrix uses.

## Coverage by application frame

| Frame | Commands | Coverage | Covered | Partial | Gap | GUI-only |
|---|---:|---:|---:|---:|---:|---:|
| `kicad_manager` | 39 | 46.7% | 7 | 4 | 4 | 24 |
| `schematic_editor` | 129 | 72.1% | 44 | 10 | 7 | 68 |
| `symbol_editor` | 62 | 70.6% | 12 | 5 | 0 | 45 |
| `pcb_editor` | 166 | 48.5% | 32 | 16 | 18 | 100 |
| `footprint_editor` | 83 | 44.0% | 11 | 9 | 5 | 58 |
| `gerbview` | 41 | 0.0% | 0 | 1 | 0 | 40 |
| `drawing_sheet_editor` | 32 | 50.0% | 3 | 2 | 1 | 26 |
| `footprint_assignment` | 17 | 50.0% | 1 | 1 | 0 | 15 |
| **Overall (distinct)** | 346 | **61.8%** | 94 | 32 | 26 | 194 |

## Closeable surface

Menu commands KiCad exposes outside the GUI that no MCP tool drives yet.

| Command | Channel | Menu location | Notes |
|---|---|---|---|
| Add Pad | `file` | footprint_editor: &Place > Add Pad | Adding a pad to a footprint is a footprint-file write. |
| Append Board... | `file` | pcb_editor: &File > Append Board... | Appending another board's contents has no CLI verb; it is a board-file merge. |
| Archive Project... | `file` | kicad_manager: &File > Archive Project... | Project archive/unarchive is a zip of project files; no CLI verb, no tool yet. |
| Change Footprints... | `file` | pcb_editor: &Edit > Change Footprints... | Bulk footprint substitution in the board file. |
| Change Symbols... | `file` | schematic_editor: &Edit > Change Symbols... | Bulk symbol substitution in the schematic file. |
| Cleanup Graphics... | `file` | pcb_editor: &Tools > Cleanup Graphics... | Graphics cleanup has no headless verb. |
| Cleanup Tracks & Vias... | `file` | pcb_editor: &Tools > Cleanup Tracks & Vias... | Track/via cleanup (duplicate, dangling, collinear merge) has no headless verb. |
| Default Pad Properties... | `file` | footprint_editor: &Edit > Default Pad Properties... | Editor default, stored in footprint editor settings. |
| Edit Teardrops... | `file` | pcb_editor: &Edit > Edit Teardrops... | Teardrop settings live in the board file's (teardrops ...) blocks. |
| Geographical Reannotate... | `file` | pcb_editor: &Tools > Geographical Reannotate... | Geographical reannotation of PCB references; no CLI verb, board-file rewrite. |
| Global Deletions... | `file` | pcb_editor: &Edit > Global Deletions... | Global deletions by item class; a board-file rewrite. |
| Manage Design Block Libraries... | `file` | kicad_manager: &Preferences > Manage Design Block Libraries... | Design-block library tables are not modelled by this server. |
| Place Footprints | `file` | pcb_editor: &Place > Place Footprints | Placing a footprint from a library onto the board is a board-file write with no dedicated tool; pcb_get_footprints reads what is already placed. |
| Remove Unused Pads... | `file` | pcb_editor: &Tools > Remove Unused Pads... | Unused-pad removal is a per-pad board-file property. |
| Renumber Pads... | `file` | footprint_editor: &Edit > Renumber Pads... | Pad renumbering inside a footprint; a footprint-file rewrite. |
| Rescue | `file` | pcb_editor: &File > Rescue | Autosave recovery is a file operation on the _autosave_ sidecar. |
| Save a Copy... | `file` | pcb_editor: &File > Save a Copy... |  |
| Save As... | `file` | kicad_manager: &File > Save As... | Save-as/copy-out of a document has no dedicated tool. |
| Save Current Sheet Copy As... | `file` | schematic_editor: &File > Save Current Sheet Copy As... | Sheet copy-out is a file operation with no dedicated tool. |
| Swap Layers... | `file` | pcb_editor: &Edit > Swap Layers... | Layer swap is a bulk rewrite of layer references in .kicad_pcb. |
| Unarchive Project... | `file` | kicad_manager: &File > Unarchive Project... |  |
| Update Footprints from Library... | `file` | pcb_editor: &Tools > Update Footprints from Library... | Update Footprints from Library rewrites footprint definitions in .kicad_pcb; no CLI verb exists. |
| Update PCB from Schematic... | `ipc` | schematic_editor: &Tools > Update PCB from Schematic... | Forward annotation (schematic -> board) is the single largest headless gap in KiCad 10.0.6: there is no kicad-cli verb for it. pcb_transfer_quality_gate and validate_footprints_vs_schematic detect when the board is stale, but applying the update still needs the GUI or an IPC-driven KiCad session. |
| Update Schematic from PCB... | `ipc` | schematic_editor: &Tools > Update Schematic from PCB... | Back annotation (board -> schematic). Same limitation as forward annotation; schematic_back_annotation covers reading the delta, not applying it headlessly. |
| Update Symbols from Library... | `file` | schematic_editor: &Tools > Update Symbols from Library... | Update Symbols from Library rewrites symbol definitions embedded in .kicad_sch. |
| Zone Manager... | `file` | pcb_editor: &Tools > Zone Manager... | Zone Manager edits zone priority, fill mode and net assignment in the board file. No MCP tool drives it yet; the data lives in the (zone ...) blocks of .kicad_pcb. |

## Partial coverage

| Command | MCP tool | Notes |
|---|---|---|
| Board Setup... | `pcb_get_stackup` | Board Setup is a multi-page dialog. Headless equivalents exist per page: stackup -> pcb_get_stackup / pcb_set_stackup / si_generate_stackup / si_synthesize_stackup_for_interfaces; design rules -> pcb_get_design_rules, drc_rule_create; net classes -> route_set_net_class_rules; constraints -> generate_board_constraints. There is no single tool that opens the whole dialog, hence partial. |
| Bulk Edit Symbol Library Links... | `sch_update_properties` | Library link rewriting is possible through property edits but has no dedicated tool. |
| Calculator Tools | `si_calculate_trace_impedance` |  |
| Calculator Tools | `si_calculate_trace_impedance` | The PCB Calculator's electrical maths is covered by si_calculate_trace_impedance, si_calculate_trace_width_for_impedance, thermal_calculate_via_count and pdn_calculate_voltage_drop; the other calculator pages are not. |
| Clone Project from Repository... | `vcs_init_git` | Git is driven headlessly, but cloning a project template repository is not exposed. |
| Compare Symbol with Library | `sch_visual_baseline_compare` | Compare Symbol with Library. sch_visual_baseline_compare diffs rendered output; a field-level symbol-vs-library diff is not exposed as its own tool. |
| Drill/Place File Origin | `pcb_get_origin` | The drill/place origin drives drill and position-file coordinates. pcb_get_origin reports it; setting it headlessly is a board-file edit with no tool yet. |
| Edit Text & Graphics Properties... | `sch_normalize_text_sizes` | The schematic-side normaliser exists; the PCB-side bulk text/graphics edit does not. |
| Edit Track & Via Properties... | `route_set_net_class_rules` | Net-class-driven track and via sizing is covered; per-selection overrides from the dialog are not. |
| Edit Variant Description... | `variant_create` | Variant metadata is set at creation; there is no rename/describe tool. |
| Exclude from Position Files | `mfg_correct_cpl_rotations` | Position-file exclusion is a footprint attribute; only the rotation-correction path is exposed. |
| Fill All Zones | — | KiCad has no standalone "fill zones" CLI verb. `kicad-cli pcb drc` and the plot/ export commands refill zones as a side effect, so a DRC run is the headless way to force a refill before export. |
| Footprint Report (.rpt)... | `pcb_export_stats` | The .rpt footprint report has no kicad-cli verb. pcb_export_stats and pcb_get_footprints supply the same content in structured form. |
| Grid Origin | `pcb_get_origin` |  |
| Grid Origin... | `pcb_get_origin` | The grid origin is stored in the board file; pcb_get_origin reads it, no tool writes it. |
| Increment Annotations From... | `sch_annotate` | Re-annotation from a starting reference is covered; the increment-from dialog's scoping options are not. |
| Load footprint from current PCB | `pcb_get_footprints` | Board footprints are readable; loading one into the editor is a GUI action. |
| Manage Footprint Libraries... | — | fp-lib-table is a plain S-expression file; lib_list_libraries reads it, no tool writes it. |
| Manage Symbol Libraries... | — | sym-lib-table is a plain S-expression file; lib_list_libraries reads it, no tool writes it. |
| Netlist... | `validate_footprints_vs_schematic` | Importing a netlist is the update-board-from-schematic path. See common.Control.updatePcbFromSchematic: no headless KiCad verb performs the full board update in 10.0.6. |
| New Library... | `lib_create_custom_symbol` | Creating a library file is possible via authoring tools; there is no explicit create-library tool. |
| Place Directive Labels | `sch_add_label` | Directive (netclass) labels are written as labels; no dedicated netclass-label tool. |
| Place Off-Board Footprints | `sch_auto_place_symbols` | Schematic auto-placement exists; PCB footprint autoplacement is not exposed headlessly. |
| Print... | — | Printing to a physical device is GUI-only; exporting a PDF is the headless equivalent. |
| Repair Board | `pcb_upgrade` | Repair Board rebuilds corrupt connectivity in memory. pcb_upgrade re-saves the board through KiCad's own loader, which fixes format-level damage but not every in-memory repair the GUI performs. |
| Repair Footprint | `fp_upgrade` | fp_upgrade re-saves through KiCad's loader; not a full geometric repair. |
| Reset Drill Origin | `pcb_get_origin` |  |
| Reset Grid Origin | `pcb_get_origin` |  |
| Save As... | `sym_export` |  |
| Save Copy As... | `sym_export` |  |
| Schematic Setup... | `erc_list_rules` | Schematic Setup pages map to erc_list_rules / erc_set_rule_severity / erc_reset_rules (violation severities) and sch_set_title_block_info (page/title block). Field-name templates and bus alias editing are not exposed. |
| View as PNG... | `sym_export_svg` | SVG is exported headlessly; PNG rasterisation of a symbol view is not. |
