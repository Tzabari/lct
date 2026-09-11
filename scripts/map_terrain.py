"""Terrain geometry for a loaded map, read from the map's shipped payload.

The battle report draws the board a game was played on. It could infer terrain
from the in-game capture, and used to, but every inference it tried was wrong in
some case: a bounding box cannot tell a trapezoid from the rectangle that contains
it, and a tag name cannot be trusted to say what a piece is.

None of that is necessary. `data/maps/<card guid>.lua` holds the exact object JSON
the map card spawns, the Battle Log already records which card was loaded, and
`spawnObjectJSON` preserves both GUID and transform -- so the payload IS the table.
Measured against a real captured game, all 41 objects matched by GUID with a
maximum positional delta of 0.006", which is the capture's own 2-dp rounding.

So terrain here is a lookup, not a guess: find the payload, pick out the plates,
and place each one's true outline (data/plate_outlines.json) at its transform.
"""

import json
import math
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import battlemaster_reconstruct as reconstruction

OUTLINES_PATH = ROOT / "data" / "plate_outlines.json"

# Precomputed by scripts/bake_terrain_cache.py: {"schema": 1, "maps": {guid: {...}}}.
# A hosted server ships this and never opens data/maps/ (26 MB of raw payloads,
# 228 files) -- see that script's docstring for the measured size difference.
CACHE_PATH = ROOT / "data" / "terrain_cache.json"
CACHE_SCHEMA = 1

# Each terrain object is embedded as a Lua long string inside `objectJSONs = {...}`.
OBJECT_ENTRY_RE = re.compile(r"\[\[(\{.*?\})\]\]", re.DOTALL)

BATTLEMAT_TAG = "battlemaster_battlemat"
BATTLEMAT_MESH = reconstruction.BATTLEMAT_MESH_URL

# A rotation counts as a mirror if it is within this many degrees of 180.
MIRROR_TOLERANCE = 1.0

_outlines = None
_cache = None


def load_outlines(path=OUTLINES_PATH):
    """The shape table, cached. Returns ({shape: ring}, {mesh url: shape})."""
    global _outlines
    if _outlines is None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        shapes = {name: [(float(x), float(z)) for x, z in ring]
                  for name, ring in (data.get("shapes") or {}).items()}
        _outlines = (shapes, dict(data.get("meshes") or {}))
    return _outlines


def load_cache(path=CACHE_PATH):
    """The precomputed {guid: {"plates": [...], "board": [w, h] or None}} map.

    Cached at module level like load_outlines(); returns {} when the file is
    absent (a normal checkout with no cache baked yet, or a guid a fresher sync
    added since the cache was last built) rather than raising, so callers always
    have a payload fallback available.
    """
    global _cache
    if _cache is None:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
        else:
            _cache = data.get("maps") or {} if data.get("schema") == CACHE_SCHEMA else {}
    return _cache


def mesh_url(obj):
    """A board object's mesh, whether it is a custom model or an assetbundle."""
    return ((obj.get("CustomMesh") or {}).get("MeshURL")
            or (obj.get("CustomAssetbundle") or {}).get("AssetbundleURL")
            or "")


def is_battlemat(obj):
    """True for the table surface itself.

    Battlemaster tags its mat, but the T5S2 pack's mat is untagged -- and an
    untagged, description-less object otherwise looks exactly like a plate, so
    without the mesh check every T5S2 map reads as having one unknown plate.
    """
    return BATTLEMAT_TAG in (obj.get("Tags") or []) or mesh_url(obj) == BATTLEMAT_MESH


def is_plate(obj):
    """True for a flat terrain area piece.

    Battlemaster writes a material ("Light"/"Dense") into every ruin part's
    Description and tags it with its part name; a plate gets neither, carrying at
    most the obj_* tag naming the objective it surrounds. Verified exact across
    every shipped payload -- see scripts/extract_plate_outlines.py, which uses the
    same rule to decide which meshes to extract.
    """
    if (obj.get("Description") or "") != "":
        return False
    return all(str(tag).startswith("obj_") for tag in (obj.get("Tags") or []))


def place_ring(ring, transform):
    """Put a mesh-local outline into board coordinates.

    Three things happen to a plate between its mesh file and the table, and all
    three are needed or asymmetric shapes come out mirrored -- which is exactly
    how the trapezoid used to render:

      * .obj files are right-handed and TTS is left-handed, so mesh x is negated;
      * Battlemaster mirrors a plate by adding 180 degrees about x or z rather
        than by scaling negatively (see battlemaster_reconstruct._build_terrain_plate),
        so those rotations are flips in the top-down view, not rotations;
      * what remains is the yaw.

    This convention is not a guess. Of the sixteen combinations of axis flips and
    yaw sign, it is the only one that places every plate of every shipped map
    entirely on the board -- 0.0 square inches outside, against 1152 for the
    runner-up. test_battle_report.py pins that.
    """
    yaw = math.radians(transform.get("rotY", 0) or 0)
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    flip_x = -1.0
    if abs(abs(transform.get("rotZ", 0) or 0) - 180) < MIRROR_TOLERANCE:
        flip_x = -flip_x
    flip_z = 1.0
    if abs(abs(transform.get("rotX", 0) or 0) - 180) < MIRROR_TOLERANCE:
        flip_z = -flip_z
    origin_x = transform.get("posX", 0) or 0
    origin_z = transform.get("posZ", 0) or 0

    placed = []
    for x, z in ring:
        local_x, local_z = x * flip_x, z * flip_z
        placed.append((origin_x + local_x * cos_y + local_z * sin_y,
                       origin_z - local_x * sin_y + local_z * cos_y))
    return placed


def payload_objects(payload):
    """Every terrain object in a payload blob, in spawn order."""
    out = []
    for entry in OBJECT_ENTRY_RE.findall(payload or ""):
        try:
            out.append(json.loads(entry))
        except json.JSONDecodeError:
            continue
    return out


def board_size(objects):
    """Board (width, height) in inches from the battlemat, or None.

    The mat mesh is 36 x 36.046 inches at scale 1; Battlemaster scales it to the
    table size (battlemaster_reconstruct._build_battlemat).
    """
    for obj in objects:
        if is_battlemat(obj):
            transform = obj.get("Transform") or {}
            return (round(float(transform.get("scaleX", 1)) * 36, 3),
                    round(float(transform.get("scaleZ", 1)) * 36.046, 3))
    return None


def plates_from_objects(objects, outlines=None):
    """Placed plate outlines, or None if any plate's mesh is unknown.

    All-or-nothing on purpose: a board drawn with some of its terrain missing
    looks like a map with less terrain on it, which is worse than a board that
    says it could not draw the terrain at all.
    """
    shapes, meshes = outlines or load_outlines()
    placed = []
    for obj in objects:
        if is_battlemat(obj) or not is_plate(obj):
            continue
        url = mesh_url(obj)
        if not url:
            continue
        shape = meshes.get(url)
        if shape is None or shape not in shapes:
            return None
        placed.append({
            "points": [[round(x, 3), round(z, 3)]
                       for x, z in place_ring(shapes[shape], obj.get("Transform") or {})],
            "tags": [str(t) for t in (obj.get("Tags") or [])],
            "shape": shape,
        })
    return placed


def map_terrain(map_guid, read_payload=None, use_cache=True):
    """Terrain for one map card, or None when it cannot be resolved.

    Returns {"plates": [...], "board": (w, h) or None}. None means the card has no
    shipped payload (a Combat Patrol map, or a log with no map recorded) or uses a
    mesh that data/plate_outlines.json does not cover.

    Cache first: scripts/bake_terrain_cache.py precomputes this exact result for
    every shipped map into data/terrain_cache.json (1.04 MB) so a hosted server
    never has to open data/maps/ (26 MB, 228 files) at request time. A guid absent
    from the cache -- not yet baked in after a map sync, or the cache file missing
    entirely -- falls through to the payload, so a normal checkout is never wrong,
    only slower; only a deployment that ships the cache without data/maps/ depends
    on the cache actually being current (see bake_terrain_cache.py --check).
    """
    if not map_guid:
        return None
    guid = str(map_guid)
    if use_cache:
        cached = load_cache().get(guid)
        if cached is not None:
            board = cached.get("board")
            return {"plates": cached.get("plates") or [],
                    "board": tuple(board) if board else None}
    if read_payload is None:
        import map_payloads
        read_payload = map_payloads.read_payload
    payload = read_payload(guid)
    if not payload:
        return None
    objects = payload_objects(payload)
    if not objects:
        return None
    plates = plates_from_objects(objects)
    if plates is None:
        return None
    return {"plates": plates, "board": board_size(objects)}
