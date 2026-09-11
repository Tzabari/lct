#!/usr/bin/env python3
"""Extract exact top-down outlines for the terrain plates the shipped maps use.

A terrain "plate" is a flat area piece -- the footprint a ruin stands on. Every
shipped map places them by mesh URL, so the report can draw their true shape
instead of guessing a rectangle from a captured bounding box, provided it has the
outline of each mesh. This tool produces that lookup:

    data/plate_outlines.json
        {"version": 1,
         "shapes": {"<shape id>": [[x, z], ...]},
         "meshes": {"<mesh or assetbundle URL>": "<shape id>"}}

Run it by hand when a new map pack introduces a mesh the file does not cover;
`scripts/test_battle_report.py` fails if any manifest map has an unresolved plate,
so a gap cannot ship unnoticed.

    python3 scripts/extract_plate_outlines.py --mesh-dir <dir> [--write]

Meshes are read from --mesh-dir when present and fetched into it otherwise, so a
rerun costs nothing. Battlemaster's own plates ship as Unity assetbundles, which
carry no parseable geometry; they are the same five authored footprints as the
bordered .obj plates (battlemaster_reconstruct.TERRAIN_ASSETS pairs them by exact
width/height), so each assetbundle URL is mapped onto its .obj sibling's outline.

The outline itself is traced rather than read off the mesh's boundary edges: a
boundary-edge walk needs clean manifold topology and a perfectly flat top face,
and several of the real plates have neither. Rasterizing the triangles and walking
the resulting contour does not care.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import battlemaster_reconstruct as reconstruction
import map_payloads as P
from map_terrain import OBJECT_ENTRY_RE, is_battlemat, is_plate, mesh_url

OUTLINES_PATH = ROOT / "data" / "plate_outlines.json"
OUTLINES_VERSION = 1

# Douglas-Peucker tolerance in inches. 0.08 keeps every real corner (the plates
# come out at 12-22 points) while dropping the stair-stepping the raster adds.
SIMPLIFY_EPS = 0.08
RASTER_CELL = 0.02

# --- mesh geometry ----------------------------------------------------------

def load_obj(path):
    """Vertices and triangles from a Wavefront .obj, ignoring everything else."""
    vertices, faces = [], []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v":
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f":
                idx = [int(tok.split("/")[0]) - 1 for tok in parts[1:]]
                for k in range(1, len(idx) - 1):      # fan-triangulate n-gons
                    faces.append((idx[0], idx[k], idx[k + 1]))
    return vertices, faces


def raster(path, cell=RASTER_CELL, pad=2):
    """Scan-convert the mesh's xz shadow into an occupancy grid."""
    vertices, faces = load_obj(path)
    if not vertices or not faces:
        raise ValueError(f"{path} has no usable geometry")
    xs = [v[0] for v in vertices]
    zs = [v[2] for v in vertices]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    width = int((x1 - x0) / cell) + 1 + 2 * pad
    height = int((z1 - z0) / cell) + 1 + 2 * pad
    grid = bytearray(width * height)

    for a, b, c in faces:
        pts = sorted(((vertices[i][0] - x0) / cell + pad,
                      (vertices[i][2] - z0) / cell + pad) for i in (a, b, c))
        (ax, az), (bx, bz), (cx, cz) = pts
        lo = max(0, int(min(az, bz, cz)))
        hi = min(height, int(max(az, bz, cz)) + 2)
        for row in range(lo, hi):
            y = row + 0.5
            hits = []
            for (px, py), (qx, qy) in (((ax, az), (bx, bz)),
                                       ((bx, bz), (cx, cz)),
                                       ((ax, az), (cx, cz))):
                if (py > y) != (qy > y):
                    hits.append(px + (y - py) * (qx - px) / (qy - py))
            if len(hits) < 2:
                continue
            for col in range(max(0, int(min(hits))), min(width, int(max(hits)) + 1)):
                grid[row * width + col] = 1
    return grid, width, height, x0, z0, cell, pad


def contour(path, cell=RASTER_CELL):
    """Moore-neighbour trace of the filled region's outer boundary."""
    grid, width, height, x0, z0, cell, pad = raster(path, cell)

    def filled(col, row):
        return 0 <= col < width and 0 <= row < height and grid[row * width + col]

    start = next(((c, r) for r in range(height) for c in range(width) if filled(c, r)), None)
    if start is None:
        return []

    # Clockwise neighbour offsets; backtrack one step each time so the walk hugs
    # the boundary instead of cutting across a one-cell neck.
    around = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
    ring = [start]
    current, direction = start, 0
    for _ in range(8 * width * height):
        for step in range(8):
            d = (direction + step) % 8
            nxt = (current[0] + around[d][0], current[1] + around[d][1])
            if filled(*nxt):
                current, direction = nxt, (d + 5) % 8
                break
        else:
            break
        if current == start and len(ring) > 2:
            break
        ring.append(current)

    return [((c - pad) * cell + x0, (r - pad) * cell + z0) for c, r in ring]


def simplify(points, eps=SIMPLIFY_EPS):
    """Douglas-Peucker on a closed ring, split in two so the ends are not fixed."""
    if len(points) < 4:
        return list(points)

    def dp(seq):
        if len(seq) < 3:
            return list(seq)
        (ax, az), (bx, bz) = seq[0], seq[-1]
        dx, dz = bx - ax, bz - az
        norm = (dx * dx + dz * dz) ** 0.5
        worst, worst_i = -1.0, 0
        for i, (px, pz) in enumerate(seq[1:-1], 1):
            if norm == 0:
                dist = ((px - ax) ** 2 + (pz - az) ** 2) ** 0.5
            else:
                dist = abs(dx * (az - pz) - (ax - px) * dz) / norm
            if dist > worst:
                worst, worst_i = dist, i
        if worst <= eps:
            return [seq[0], seq[-1]]
        return dp(seq[:worst_i + 1])[:-1] + dp(seq[worst_i:])

    half = len(points) // 2
    return dp(points[:half + 1])[:-1] + dp(points[half:] + [points[0]])[:-1]


def ring_area(points):
    n = len(points)
    return abs(sum(points[i][0] * points[(i + 1) % n][1]
                   - points[(i + 1) % n][0] * points[i][1] for i in range(n))) / 2


# --- which meshes the shipped maps actually use -----------------------------

def payload_objects(guid, payload_dir=None):
    payload = P.read_payload(guid, payload_dir) if payload_dir else P.read_payload(guid)
    if payload is None:
        return []
    out = []
    for entry in OBJECT_ENTRY_RE.findall(payload):
        try:
            out.append(json.loads(entry))
        except json.JSONDecodeError:
            continue
    return out


def plate_urls_in_manifest_maps():
    """Every distinct plate mesh URL used by a card in data/map_manifest.csv.

    Payloads outside the manifest (the Combat Patrol pool) are deliberately not
    scanned: those maps are out of scope and their one-off meshes would otherwise
    pull a long tail of URLs into the file.
    """
    urls = {}
    for guid in sorted(P.manifest_card_guids()):
        for obj in payload_objects(guid):
            url = mesh_url(obj)
            if url and not is_battlemat(obj) and is_plate(obj):
                urls.setdefault(url, 0)
                urls[url] += 1
    return urls


# --- naming and fetching ----------------------------------------------------

def obj_plate_urls():
    """The LCT bordered .obj plate per shape id, from the reconstruction contract."""
    base = reconstruction.TERRAIN_PLATE_MESH_BASE_URL
    return {asset["id"]: f"{base}battlemaster-rugged-{asset['plate']}-5mm-border.obj"
            for asset in reconstruction.TERRAIN_ASSETS}


def assetbundle_siblings():
    """Assetbundle plate URL -> the shape id whose .obj outline it shares."""
    out = {}
    for asset in reconstruction.TERRAIN_ASSETS:
        for style in ("rugged", "smooth"):
            out[asset[style]] = asset["id"]
    return out


def local_name(url):
    """A stable filename for a mesh URL."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".obj"):
        return tail
    return f"ugc-{tail[:24]}.obj"


def fetch(url, path):
    request = Request(url, headers={"User-Agent": "lct-plate-outlines/1"})
    with urlopen(request, timeout=90) as response:
        data = response.read()
    path.write_bytes(data)
    return len(data)


def ensure_mesh(url, mesh_dir, allow_network=True):
    path = mesh_dir / local_name(url)
    if path.exists() and path.stat().st_size:
        return path
    if not allow_network:
        return None
    mesh_dir.mkdir(parents=True, exist_ok=True)
    size = fetch(url, path)
    print(f"    fetched {path.name} ({size:,} bytes)")
    return path


def shape_id_for(url, ring, obj_urls, siblings, index):
    """A readable, stable id for the shape a mesh URL draws.

    Battlemaster's own five footprints are named by their asset id. Everything
    else (the T5S2 pack ships its own plates) is named after whichever of those
    five it is closest in size to, so the file stays legible -- these are the same
    shapes redrawn, not new ones, and a bare "plate-07" tells a reader nothing.
    """
    for shape, obj_url in obj_urls.items():
        if url == obj_url:
            return shape
    if url in siblings:
        return siblings[url]

    xs = [p[0] for p in ring]
    zs = [p[1] for p in ring]
    width, height = max(xs) - min(xs), max(zs) - min(zs)
    best, best_gap = None, None
    for asset in reconstruction.TERRAIN_ASSETS:
        # Compare against both orientations; a plate's ring is not normalized.
        gap = min(abs(width - asset["width"]) + abs(height - asset["height"]),
                  abs(width - asset["height"]) + abs(height - asset["width"]))
        if best_gap is None or gap < best_gap:
            best, best_gap = asset["id"], gap
    if best is not None and best_gap <= 2.5:
        return f"alt-{best}"
    return f"plate-{index:02d}"


# --- build ------------------------------------------------------------------

def build(mesh_dir, allow_network=True):
    obj_urls = obj_plate_urls()
    siblings = assetbundle_siblings()
    used = plate_urls_in_manifest_maps()
    print(f"Plate mesh URLs used by manifest maps: {len(used)} "
          f"({sum(used.values())} plates)")

    # Assetbundles carry no geometry; borrow the matching .obj plate's outline.
    needed = {}
    for url in used:
        needed[url] = obj_urls[siblings[url]] if url in siblings else url
    for shape, obj_url in obj_urls.items():
        if obj_url in needed.values():
            needed.setdefault(obj_url, obj_url)

    rings_by_source = {}
    for source in sorted(set(needed.values())):
        path = ensure_mesh(source, mesh_dir, allow_network)
        if path is None:
            raise SystemExit(f"missing mesh and --no-network given: {source}")
        ring = simplify(contour(str(path)))
        if len(ring) < 3:
            raise SystemExit(f"traced no usable outline from {path}")
        rings_by_source[source] = [[round(x, 4), round(z, 4)] for x, z in ring]
        xs = [p[0] for p in ring]
        zs = [p[1] for p in ring]
        print(f"    {path.name:<34} {len(ring):3d} pts  "
              f"{max(xs) - min(xs):6.3f} x {max(zs) - min(zs):6.3f} in  "
              f"area {ring_area(ring):7.2f}")

    # Collapse identical rings so duplicated meshes share one shape entry.
    shapes, canonical, index = {}, {}, 1
    for source in sorted(rings_by_source):
        ring = rings_by_source[source]
        key = json.dumps(ring)
        if key in canonical:
            continue
        shape = shape_id_for(source, ring, obj_urls, siblings, index)
        suffix = 2
        while shape in shapes:
            shape = f"{shape_id_for(source, ring, obj_urls, siblings, index)}-{suffix}"
            suffix += 1
        shapes[shape] = ring
        canonical[key] = shape
        index += 1

    meshes = {}
    for url, source in needed.items():
        meshes[url] = canonical[json.dumps(rings_by_source[source])]

    return {"version": OUTLINES_VERSION,
            "shapes": dict(sorted(shapes.items())),
            "meshes": dict(sorted(meshes.items()))}


def dumps(table):
    """Serialize with one [x, z] point per line.

    json.dumps(indent=...) puts every coordinate on its own line, which turns a
    600-point file into 2000 lines of noise and makes a diff after a re-extract
    unreadable. The shape is fixed and small, so format it directly.
    """
    lines = ['{', f'  "version": {table["version"]},', '  "shapes": {']
    shapes = list(table["shapes"].items())
    for i, (shape, ring) in enumerate(shapes):
        lines.append(f'    {json.dumps(shape)}: [')
        for j, (x, z) in enumerate(ring):
            comma = "" if j == len(ring) - 1 else ","
            lines.append(f"      [{x}, {z}]{comma}")
        lines.append("    ]" + ("" if i == len(shapes) - 1 else ","))
    lines.append("  },")
    lines.append('  "meshes": {')
    meshes = list(table["meshes"].items())
    for i, (url, shape) in enumerate(meshes):
        comma = "" if i == len(meshes) - 1 else ","
        lines.append(f"    {json.dumps(url)}: {json.dumps(shape)}{comma}")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mesh-dir", type=Path, required=True,
                        help="Directory holding (or to receive) the downloaded meshes.")
    parser.add_argument("--out", type=Path, default=OUTLINES_PATH,
                        help="Where to write the outline table.")
    parser.add_argument("--no-network", action="store_true",
                        help="Fail rather than download a mesh that is not on disk.")
    parser.add_argument("--write", action="store_true",
                        help="Write the file. Without this, preview only.")
    args = parser.parse_args(argv)

    table = build(args.mesh_dir, allow_network=not args.no_network)
    print(f"\n{len(table['shapes'])} distinct shapes, "
          f"{len(table['meshes'])} mesh URLs mapped.")
    if not args.write:
        print(f"[preview] nothing written; rerun with --write to update {args.out}.")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps(table), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
