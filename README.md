# LCT - 40k TTS Base

A fork of Hutber's FTC table (shared with permission), with extra features, refinements, and a slightly different direction — it also filled a gap, since no other table supported 11th edition when the rules dropped. Feel free to take the project and use it as you wish.

## Development

Run the compiler from the `scripts` folder:

```bash
python3 compile.py             # prompt for a version, write the compiled JSON
python3 compile.py --test      # tag as "test", copy to your TTS saves folder
python3 compile.py --release   # version + patch notes from CHANGELOG.md, then copy
python3 compile.py --no-validate   # skip the map-card validation gate
```

`compile.py` stitches `TTSLUA/*.ttslua` back into `TTSJSON/ftc_base.json`, stamps the version, and writes `lct_base_<version>_compiled.json` into `builds/`, printing a build summary at the end.

### Map terrain payloads (`data/maps/`)

Each map card's `LuaScript` is a canonical load/clear machinery head followed by an `objectJSONs = { ... }` terrain blob. Those blobs total ~38 MB, so they live **outside** `ftc_base.json`, one file per map: `data/maps/<card_guid>.lua`. `validate_maps.py` folds each payload back in for its checks; `compile.py` re-injects `head + payload` **byte-for-byte** during the build (before the Load Map hook pass), so the compiled save is identical to the old inline one (a stripped card with no payload file is a build error).

Pull terrain back out after re-exporting from TTS or after an import (add `--dry-run` to preview, `audit_map_payloads.py --sizes|--strict` to inspect):

```bash
python3 extract_map_payloads.py   # strip terrain to data/maps/, shrink the save
```

### The map manifest & `MAP_INDEX`

`data/map_manifest.csv` is the authoritative map-card inventory. Each row records `map_creator_tag`, `map_type_tag`, `creator_display` (full UI name), and `eligible` (`true`/`false` — a per-map on/off switch that excludes a card from generation without deleting it). Keep it in sync whenever the save changes.

At build time `bake_map_index` generates a GUID-keyed `MAP_INDEX` table (`{creator, display, type, eligible}`) from the CSV and stamps it into the `@@MAP_INDEX@@` marker in `TTSLUA/global.ttslua`. Runtime systems (mission generation, map filter) read it via `Global.getTable("MAP_INDEX")` — this lets them look up a card's creator/eligibility even while it's still inside a deck. The source keeps an empty `MAP_INDEX = {}` default so uncompiled builds stay valid.

Map-card nicknames and manifest `card_name` values carry a trailing creator credit (e.g. ` - T5S2`, ` - BTTF`); runtime matching strips it when resolving layout art and deployment zones. Creator tag→display mappings must stay aligned between `MAP_CREATOR_DISPLAY_NAMES` (`validate_maps.py`) and `mapCreatorDisplaySuffixes` (`startMenu.ttslua`); validation rejects mismatches.

### Battle reports

Players register their armies once per game, and the table then records a snapshot
at every phase/turn change: each registered model's position, facing, base size and
current wounds, plus the terrain layout, round, VP and CP. The log lives in Global's
saved state (`svBattleLog`) and rides out with the save.

In game, the three buttons sit on the game-tools object beside that side's
HIDE / SHOW ARMY pair, one row further in:

- **REGISTER ARMY** (one per side) — select your models, left-click to register them,
  right-click to clear. A player may always register their own side; an empty seat may
  be registered by whoever is present, so solo games work from either seat.
- **CAPTURE** — record an extra snapshot mid-phase.
- **EXPORT REPORT** — render the report and open it in a browser (needs the local
  helper below; the button fails softly and tells you what to run if it is not up).

The tooling is standard-library only. Create the environment once:

```bash
uv venv .venv
```

Then either leave the helper running while you play and use the in-game button
(it re-reads the renderer on every export, so edits to `battle_report.py` take
effect without restarting it):

```bash
.venv/Scripts/python.exe scripts/battle_report_server.py     # Windows
.venv/bin/python scripts/battle_report_server.py             # macOS / Linux
```

...or skip the server entirely, save the game in TTS, and render the exported save:

```bash
.venv/Scripts/python.exe scripts/export_battle_report.py            # newest TTS save
.venv/Scripts/python.exe scripts/export_battle_report.py <save.json> -o report/
```

Both write `report/report.html` and `report/snapshots.json` (the full
reconstruction, and the stable interface for a future in-game viewer). The page
header carries the time it was rendered, so a stale tab is easy to spot.

The page is a viewer, not a scroll: one board fills the window, picked with three
rows of tabs - Round, then player, then the phases of that round - so Round 2 ->
Blue -> Charge is three clicks. The board as it stood when Start Game was pressed
is its own **Deploy** round ahead of Round 1; it is only recorded if an army was
registered by then, and the table says so in chat if none was. Prev / Next phase step through in order and the
arrow keys do the same. The board zooms on the mouse wheel about the cursor, pans
on drag, and resets on double-click, the `0` key or the Reset view button.

The board is drawn looking down on the table the way you sit at it: +z is the far
edge, at the top of the picture.

Terrain areas - the flat plates the ruins stand on - are drawn as their exact
outlines, and none of it is inferred from the capture. The log records which map
card was loaded, and that card's own spawn payload (`data/maps/<card_guid>.lua`)
already holds every plate's position, rotation and mesh, so the report reads the
geometry rather than guessing it: measured against a real recorded game, all 41
objects matched by GUID to within 0.006", which is the capture's own rounding.

Each plate's true silhouette lives in `data/plate_outlines.json`, traced from the
real meshes by `scripts/extract_plate_outlines.py`. Eleven shapes cover all 3586
plates of all 225 shipped maps, so nothing falls back to a box - and the plate the
maps call a "triangle" draws as the right trapezoid it actually is, facing the
right way. A test places every plate of every map and fails if any of them lands
off the table, which is what pins the mirroring down.

The ruins standing on those plates are not drawn for now, so the board stays
readable under the models. Hovering an area names the objective it surrounds.

A side panel carries VP/CP with each side's primary/secondary split, both
Reinforcements and Reserves boards, the
secondaries currently in each side's zones, and casualties as they accumulate.
Overlay toggles mirror the in-game buttons - deployment zones (redrawn from the
mission's own draw spec), objectives, territory line, table quarters, the 6"
strategic reserves inset, and the 3"/6" centre rings. The territory line is
derived from the deployment that was played, exactly as the table derives it, so
it tilts with a stepped zone instead of sitting on the centre line.

**Deployment map** is a separate view, off by default: the mission's setup as a
drawn diagram - terrain, deployment zones drawn heavier and labelled by colour,
quarters, divider and objectives - with the round, player and phase tabs parked
until you turn it off. The mission's own layout-art card is deliberately not used
here: one art card serves three different terrain layouts (the three Battlemaster
packs, LCT Pack 1 and T5S2 all share a mission name but not a layout), so it would
be wrong for two maps in three. Board markings are drawn in inches at
true table scale, so a name label or a zone line stays in proportion to a 32mm base
however far you zoom.

Model datasheets come from ForceOrg/yellowscribe: unit grouping uses each model's
`uuid:<unit>` tag and wounds are read from the `[00ff16]2/2[-] Name` nickname prefix,
so models imported by other means still record position but report no wounds.

### Validation

Every build validates the baked-in map cards (inventory, tags, terrain, zone size, GUID collisions, mission-matrix references) unless `--no-validate` is passed; errors abort the build. `--test`/`--release` add strict checks (`validate_maps.py --require-map-tags`) that also fail if a manifest map isn't fully wired into `startMenu.ttslua` — each card's head matches `data/map_card_machinery.lua` (no foreign/self-excluding loaders), every source bag is in `deploymentMatrixDecks`, `randomDeploymentDecks` and `GAME_MODE_OBJECTS`, all 25 disposition matchups have a dedicated deck, and each map's logical name has matching layout art in deck `fb4b5d`. Add new checks with the `@check` decorator; runtime behaviors the validator can't model are locked by `scripts/test_validate_maps.py`.

### Adding / migrating maps

Every map card uses **one** canonical load/clear machinery (`data/map_card_machinery.lua`): `loadMap` wipes the zone except mats and `MapExclude`-tagged objects, then spawns terrain only **after the board is verified clear**. Imported maps often ship their own loader — normalize them, never hand-edit:

1. **Normalize** foreign cards onto the machinery (fixes head, GMNotes, tags, credit nicknames, hex GUIDs). `--write` edits the source save in place (or use `--out`); it does not touch `ftc_base.json`:
   ```bash
   python3 normalize_map_card.py ../Legacy/SomeSave.json \
       --container <bagGUID> --creator map_crt_<creator> --type map_type_<type> --write
   ```
2. **Copy** the normalized bag + layout-art tiles into `TTSJSON/ftc_base.json`.
3. **Record** the printed rows in `data/map_manifest.csv`.
4. **Wire** the bag into `startMenu.ttslua` per the printed checklist (`deploymentMatrixDecks`, `randomDeploymentDecks`, `GAME_MODE_OBJECTS`, layout art in deck `fb4b5d`).
5. **Verify**: `python3 validate_maps.py --require-map-tags && python3 compile.py --test`.

New creators must first be added to `MAP_CREATOR_DISPLAY_NAMES` (`validate_maps.py`) and `mapCreatorDisplaySuffixes` (`startMenu.ttslua`).

### Retiring a map pack

Pack removal crosses several source layers; deleting only the cards leaves either orphaned payloads or runtime fallbacks that resolve deleted GUIDs:

1. Remove the pack's cards from the 15 shared matchup bags in `ftc_base.json`, plus assets used only by that pack.
2. Remove its manifest rows and matching `data/maps/<guid>.lua` payloads.
3. Remove its creator display/suffix and any pack-specific mat logic. If a retired pack supplied the hand-written deployment fallbacks, replace those GUIDs with a retained complete 45-card pack.
4. For an API-derived pack, remove its `PACKS` entry from `sync_battlemaster_maps.py` and its legacy cache theme/debug control.
5. Run strict validation, both unit suites, `audit_map_payloads.py --strict`, and `compile.py --test`.

Upgrading v1 map cards to v2 (deferred wipe that loads/clears reliably) is a separate, explicit step — never done by a normal build:

```bash
python3 upgrade_map_zones.py            # rewrite v1 cards to v2 in ftc_base.json
python3 upgrade_map_zones.py --dry-run  # show what would change, write nothing
```

### Battlemaster imports

Battlemaster maps are baked into normal static LCT cards, not spawned dynamically at runtime. The supported updater now runs entirely outside TTS: it fetches the public Battlemaster API, reconstructs the compact terrain payloads in Python, and prepares normal source cards and payload files.

Run it from the repository root. Every command is preview-only unless `--write` is present:

```bash
# One 45-card pack; granular flags can be combined.
python3 scripts/sync_battlemaster_maps.py --bttf
python3 scripts/sync_battlemaster_maps.py --bttf --armageddon-ruins --write

# All three shipped Battlemaster packs (135 cards).
python3 scripts/sync_battlemaster_maps.py --all-battlemaster

# Atomic Ice layout 1 + Lava layout 2 + Mars layout 3 (45 cards).
python3 scripts/sync_battlemaster_maps.py --lct-pack-1

# All configured packs, including LCT Pack 1 (180 cards).
python3 scripts/sync_battlemaster_maps.py --all
```

The granular selectors are `--bttf-ruins`, `--bttf`, `--armageddon-ruins`, and
`--lct-pack-1` (`--lct-p1` is an alias). `--pack KEY` is a repeatable generic
selector generated from `PACKS`; the former `--all-four` spelling remains a
deprecated alias for `--all-battlemaster`.

Footprint states are selected by pack, with an explicit contract:

- The three Battlemaster packs contain two states: rugged terrain is the
  top-level/default state and smooth terrain is state 2.
- LCT Pack 1 contains three states: its theme-specific custom bordered floor is
  the top-level/default state, rugged terrain is state 2, and smooth terrain is
  state 3.

Preview validates every reconstructed footprint against that state count,
ordering, asset pairing, transform, and objective-tag contract. A mismatched
profile or partially reconstructed map aborts before any source file is changed.

For a reproducible review/apply split, save the normalized API responses during
preview and apply that exact snapshot later without another network request:

```bash
python3 scripts/sync_battlemaster_maps.py --all \
    --snapshot-out /tmp/lct-battlemaster.json
python3 scripts/sync_battlemaster_maps.py --all \
    --snapshot-in /tmp/lct-battlemaster.json --write
```

Before any source write, the updater requires complete 15-pair slot sets,
matching catalog/payload identities, zero skipped terrain parts, valid
deployment/layout-art mappings, unique identities, and a clean full-map
validation against a temporary payload overlay. It preserves existing public
card GUIDs and deck IDs. Semantically unchanged terrain retains its original
bytes, avoiding a large no-op diff.

`--write` stages every replacement before changing source, updates
`TTSJSON/ftc_base.json`, `data/map_manifest.csv`, and `data/maps/` as one
rollback-capable operation, then runs strict map validation, both Python test
suites, the payload audit, and `compile.py --test`. A failed post-write check
restores every affected source file. Use `--skip-compile-test` only when a debug
build is deliberately unnecessary.

To register a new Battlemaster pack, add one `PackSpec` to `PACKS`. Standard
packs join both aggregate selectors automatically and are immediately selectable
with `--pack KEY`; add a convenience flag only if desired. Also register the new
creator tag/display in the Python validator and Lua suffix table. Add it to the
legacy theme list/debug batch only when offline TTS-cache recovery is required.

`LCT - Pack 1` remains a composite pack with fixed slots:

- Layout 1: `lct - ice colony`
- Layout 2: `lct - lava temple v2.1`
- Layout 3: `lct - mars base`

The DEBUG-gated `All BM`/`LCT P1` cache buttons and
`import_battlemaster_static_maps.py` remain only as a legacy/offline fallback
when a cache has been rebuilt with the current spawner. They are no longer part
of the normal update path: their large persisted `LuaScriptState` forces TTS to
repeatedly serialize tens of megabytes during save/rewind, which is the source
of the long UI stalls. New
legacy cache archives also record their reconstruction schema and footprint
profile, so the importer rejects stale caches built with the former all-three-
state behavior.
