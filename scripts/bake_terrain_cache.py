#!/usr/bin/env python3
"""Precompute terrain for every shipped map into one small lookup file.

map_terrain.map_terrain(guid) reads a ~115 KB data/maps/<guid>.lua, regex-extracts
its ~41 embedded object JSONs, and reduces them to {"plates": [...], "board": (w,
h)}. That result is the only thing the battle report draws -- and across all 228
shipped payloads it comes to 1.04 MB compact (0.25 MB gzipped), against 26 MB of
raw payloads. A hosted report server has no use for the other 25 MB, so this tool
bakes the reduction once and checks it in:

    data/terrain_cache.json
        {"schema": 1, "maps": {"<card guid>": {"plates": [...], "board": [w, h]}}}

    python3 scripts/bake_terrain_cache.py            # write data/terrain_cache.json
    python3 scripts/bake_terrain_cache.py --check     # exit 1 if stale (CI gate)

Rerun (--write) whenever data/maps/ changes -- after scripts/sync_battlemaster_maps.py
adds or updates a map, or after re-running extract_plate_outlines.py covers a
previously-unresolved mesh. --check is also exercised as a unit test
(TestTerrainCache in test_battle_report.py), so a stale cache fails the suite
rather than silently shipping a report server that has lost some terrain.

A guid this tool cannot resolve (an unknown plate mesh) is simply omitted, exactly
like map_terrain() returning None for it today -- map_terrain() falls back to the
raw payload for anything missing from the cache, so this is never a hard failure,
only a coverage gap worth noticing.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import map_payloads as MP
import map_terrain as MT

CACHE_PATH = MT.CACHE_PATH
CACHE_SCHEMA = MT.CACHE_SCHEMA


def payload_guids(payload_dir=MP.PAYLOAD_DIR):
    """Every card guid with a shipped payload file, sorted for a stable diff."""
    return sorted(p.stem for p in Path(payload_dir).glob("*.lua"))


def build(payload_dir=MP.PAYLOAD_DIR):
    """{guid: {"plates": [...], "board": [w, h] or None}} for every resolvable map.

    Bypasses the cache explicitly (use_cache=False) so this always reflects the
    raw payloads on disk, never a previous bake.
    """
    maps = {}
    unresolved = []
    for guid in payload_guids(payload_dir):
        terrain = MT.map_terrain(guid, use_cache=False)
        if terrain is None:
            unresolved.append(guid)
            continue
        board = list(terrain["board"]) if terrain["board"] else None
        maps[guid] = {"plates": terrain["plates"], "board": board}
    return maps, unresolved


def dumps(maps):
    """Compact on purpose -- this is a generated lookup, not a hand-edited file.

    Pretty-printing one [x, z] pair per line (as extract_plate_outlines.py does
    for the much smaller shape table) would turn 3500+ plates into hundreds of
    thousands of lines for no benefit: nobody diffs this file by eye, and
    TestTerrainCache -- not a visual review -- is what catches drift.
    """
    table = {"schema": CACHE_SCHEMA, "maps": dict(sorted(maps.items()))}
    return json.dumps(table, separators=(",", ":"), sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--payload-dir", type=Path, default=MP.PAYLOAD_DIR,
                        help="Directory of <guid>.lua payload files.")
    parser.add_argument("--out", type=Path, default=CACHE_PATH,
                        help="Where to write the cache.")
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if --out does not match a fresh build; write nothing.")
    args = parser.parse_args(argv)

    maps, unresolved = build(args.payload_dir)
    text = dumps(maps)
    if unresolved:
        print(f"{len(unresolved)} map(s) have no resolvable terrain (unchanged "
              f"from map_terrain() today): {', '.join(unresolved)}")

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current == text:
            print(f"{args.out} is up to date ({len(maps)} maps).")
            return 0
        print(f"{args.out} is stale or missing -- rerun without --check to update it.")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out} ({len(maps)} maps, {len(text)} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
