# Drive KiCad menus headlessly

KiCad's GUI carries about 350 distinct menu commands across eight application
windows. This server ships a machine-generated map of that surface, so an agent can
ask *what can KiCad do, and can I do it without opening KiCad?* — and then act on the
answer.

The map is extracted from KiCad's own C++ sources, not transcribed by hand, so it is
exactly what KiCad 10.0.6 ships.

## The five routes to a menu command

| Channel | Meaning |
|---|---|
| `mcp` | A registered MCP tool drives it directly. |
| `cli` | A `kicad-cli` subcommand drives it. Fully headless. |
| `file` | Reachable by editing `.kicad_pcb` / `.kicad_sch` / `.kicad_pro`. |
| `ipc` | Needs a running KiCad reachable over the IPC API. |
| `gui-only` | KiCad exposes no headless path at all. |

`gui-only` is an honest verdict about KiCad, not an apology for this server. Those
commands are excluded from the coverage denominator.

## Finding a command

Start broad, then narrow:

```text
kicad_menu_frames()                      # which window owns what, and its coverage
kicad_menu_tree("pcb_editor")            # the full menu tree, marked with coverage
kicad_menu_search("gerber")              # find a command by name, path or tooltip
kicad_menu_describe("Board Setup...")    # one command, and exactly how to reach it
```

`kicad_menu_describe` accepts an action name (`pcbnew.DRCTool.runDRC`), a menu label
(`Design Rules Checker`) or a menu path (`Inspect > Design Rules Checker`).
Accelerator `&` and trailing `...` are ignored, so you can paste a label straight out
of KiCad.

## Running one

`kicad_menu_invoke` runs the command when a `kicad-cli` path exists:

```text
kicad_menu_invoke("Inspect > Design Rules Checker",
                  {"format": "json", "severity_all": true},
                  output="reports/drc.json")
```

Three things it will not do:

- **Guess at options.** They are validated against the installed `kicad-cli`'s own
  `--help`, so an unknown flag fails before anything runs, with suggestions.
- **Write outside the workspace.** `output` goes through the same path guard as every
  other write in this server.
- **Pretend.** For an `mcp`-backed command it names the tool to call; for `file`,
  `ipc` and `gui-only` commands it explains the route instead of reporting a run that
  never happened. Pass `dry_run=true` to see the exact command first.

## Auditing coverage

```text
kicad_menu_coverage()                    # overall, plus the open gaps
kicad_menu_coverage("pcb_editor")        # one frame
kicad_menu_search(status="gap")          # everything reachable but not yet driven
kicad_menu_export_map()                  # the whole map as JSON
```

The same numbers are published in
[KiCad Menu Coverage](../compatibility/kicad-menu-coverage.generated.md).

## Regenerating for a new KiCad release

The catalog is generated in two steps, so a new KiCad version is a re-run, not a
rewrite:

```bash
uv run python scripts/extract_kicad_menus.py \
    --kicad-source /path/to/kicad --version 10.0.7 \
    --out src/kicad_mcp/menus/menu_catalog.json
```

```bash
uv run python scripts/build_menu_index.py
```

The first parses `menubar_*.cpp` and `*actions.cpp` from a KiCad checkout. The second
joins that tree with the curated bindings in
`docs/compatibility/kicad-menu-bindings.yaml` and writes both the shipped
`menu_index.json` and the coverage report.

`tests/unit/test_menu_catalog.py` then holds the result honest: every MCP tool the
bindings name must really be registered, every `kicad-cli` command must exist in the
installed KiCad, every command must carry an explicit binding, and the shipped index
must match a fresh build of the YAML.
