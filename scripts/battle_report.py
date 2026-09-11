#!/usr/bin/env python3
"""Battle report: schema, datasheet parsing and HTML/SVG rendering.

The in-game Battle Log (see the BATTLE LOG section of TTSLUA/global.ttslua)
accumulates one snapshot per phase into Global's saved state. This module turns
that structure into a readable report and is shared by both front ends:

  * export_battle_report.py  -- offline, reads an exported TTS save (this repo)
  * battle_report_server.py  -- the in-game EXPORT button POSTs to this, run
                                 locally or hosted -- lives in the separate
                                 lct-report-server repo, which keeps its own
                                 copy of this file

Coordinates are TTS world units, which are inches on this table (1 unit = 1").
The board is centred on the origin, so x spans [-w/2, +w/2] and z spans
[-h/2, +h/2]. Models parked outside that rectangle are in reserve.
"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import map_terrain

SCHEMA_VERSION = 2

# Board colours. Red/Blue match the table's own player colours closely enough to
# be recognisable without being so saturated that overlapping bases turn to mud.
TEAM_FILL = {"Red": "#c8393c", "Blue": "#2f6fd0"}
TEAM_EDGE = {"Red": "#f2a3a5", "Blue": "#9dc0f5"}
DEAD_FILL = "#4a4a52"
DEAD_EDGE = "#6f6f78"

# Terrain tags that describe the table surface rather than a piece of terrain.
SURFACE_TAGS = {"battlemaster_battlemat"}

# What the board view draws. Terrain AREAS -- the flat plates the ruins stand on --
# are drawn as grey outlines; the ruins themselves are not drawn at all for now.
KIND_SURFACE = "surface"
KIND_AREA = "area"
KIND_OBJECTIVE = "objective"

# A piece covering this much of the board in both axes is the table itself. The
# battlemat is not always tagged -- a good number of the shipped maps leave it
# bare -- and an untagged 60x44 rectangle drawn as terrain covers the whole report.
SURFACE_SPAN = 0.9

COLOR_TAG_RE = re.compile(r"\[[0-9a-fA-F]+\]|\[-\]")
WOUND_NAME_RE = re.compile(r"^\[[0-9a-fA-F]+\](\d+)/(\d+)\[-\]\s*(.*)$")


class BattleReportError(Exception):
    """Raised when a log is missing, malformed, or of an unknown schema."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def strip_color_tags(text):
    """Drop the [rrggbb]...[-] markup TTS uses for rich text."""
    return COLOR_TAG_RE.sub("", text or "")


def parse_model_name(raw):
    """Split a ForceOrg nickname into (name, current_wounds, max_wounds).

    ForceOrg stamps the live wound counter into the nickname, e.g.
    "[00ff16]2/2[-] Intercessor Sergeant". Models from any other source have no
    such prefix and report (name, None, None).
    """
    if not raw:
        return "", None, None
    m = WOUND_NAME_RE.match(raw)
    if m:
        name = m.group(3).strip() or raw
        return name, int(m.group(1)), int(m.group(2))
    return strip_color_tags(raw).strip(), None, None


def parse_datasheet(description):
    """Parse a ForceOrg datasheet Description into a structured profile.

    Mirrors parseFigureData in TTSLUA/statHelper.ttslua: the second non-empty
    line is the statline (M T Sv W Ld OC), and "[hex]Section[-]" headers begin
    weapon/ability blocks. Returns {} for anything unparseable rather than
    raising -- a report should still render if one unit's datasheet is odd.
    """
    if not description:
        return {}

    lines = [ln for ln in (description or "").splitlines() if ln.strip()]
    if not lines:
        return {}

    profile = {}

    # Statline: the header order used by statHelper for 10e/11e.
    if len(lines) >= 2:
        stats = strip_color_tags(lines[1]).split()
        for key, value in zip(("m", "t", "sv", "w", "ld", "oc"), stats):
            profile[key] = value

    # Section blocks. Headers look like "[dc61ed]Abilities[-]".
    sections = {}
    current = None
    for line in lines:
        header = re.match(r"^\[[0-9a-fA-F]+\]([A-Za-z][A-Za-z /]*)\[-\]\s*$", line.strip())
        if header:
            current = header.group(1).strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(strip_color_tags(line).strip())
    if sections:
        profile["sections"] = {k: [ln for ln in v if ln] for k, v in sections.items()}

    return profile


def load_log(raw):
    """Validate and normalise a decoded battleLog table."""
    if not isinstance(raw, dict):
        raise BattleReportError("battle log is not an object")
    version = raw.get("v")
    if version != SCHEMA_VERSION:
        raise BattleReportError(
            f"unsupported battle log schema {version!r}; this tool understands {SCHEMA_VERSION}"
        )
    log = {
        "v": version,
        "game": raw.get("game") or {},
        "board": raw.get("board") or [],
        "units": raw.get("units") or {},
        "roster": raw.get("roster") or {},
        "dead": raw.get("dead") or {},
        "names": raw.get("names") or [],
        "snaps": raw.get("snaps") or [],
    }
    if not log["snaps"]:
        raise BattleReportError("battle log contains no snapshots")
    return log


def extract_log_from_save(save):
    """Pull svBattleLog out of an exported TTS save's Global script state."""
    if not isinstance(save, dict):
        raise BattleReportError("save file is not a JSON object")
    state = save.get("LuaScriptState")
    if not state:
        raise BattleReportError("save has no Global LuaScriptState; nothing was recorded")
    try:
        decoded = json.loads(state)
    except json.JSONDecodeError as exc:
        raise BattleReportError(f"Global LuaScriptState is not valid JSON: {exc}") from exc
    log = decoded.get("svBattleLog")
    if log is None:
        raise BattleReportError(
            "save has no svBattleLog. Register an army and play at least one phase "
            "with a build that includes the battle log."
        )
    return load_log(log)


# --------------------------------------------------------------------------
# Reconstruction
# --------------------------------------------------------------------------

def board_size(log):
    """Board (width, height) in inches, falling back to Strike Force."""
    board = (log.get("game") or {}).get("board") or {}
    w = board.get("w")
    h = board.get("h")
    if not w or not h:
        return 60.0, 44.0
    return float(w), float(h)


def classify_terrain(piece, board=None):
    """Which kind of board object a captured piece is.

    Only two of these are drawn now. Terrain geometry comes from the map's shipped
    payload (see map_terrain), not from the capture, so this no longer has to
    identify plates or work out materials -- it only has to find the objective
    markers and keep the table surface from being drawn as a board-sized slab.
    """
    tags = [str(t) for t in (piece.get("t") or [])]
    if any(t in SURFACE_TAGS for t in tags):
        return KIND_SURFACE
    if board:
        w, h = board
        if ((piece.get("bx") or 0) * 2 >= w * SURFACE_SPAN
                and (piece.get("bz") or 0) * 2 >= h * SURFACE_SPAN):
            return KIND_SURFACE
    # The physical objective markers are spawned separately from the map payload,
    # so the capture is the only place they exist. They say what they are in their
    # nickname ("Home Objective", "Center Objective", "Expansion Objective").
    if "objective" in (piece.get("n") or "").lower():
        return KIND_OBJECTIVE
    return KIND_AREA


def terrain_label(piece):
    """Tooltip text: what the piece is, plus its material where one was recorded."""
    all_tags = [str(t) for t in (piece.get("t") or []) if t not in SURFACE_TAGS]
    tags = [t for t in all_tags if not t.startswith("obj_")]
    # Some pieces carry two tags because they are two things bolted together
    # ("Short Barrier, Tower"), and naming only the first hides why it is dense.
    name = " / ".join(tags) if tags else (piece.get("n") or "").strip()
    if not name:
        # An obj_*-tagged area stands in for an objective when no physical marker
        # was captured, so it should say which objective rather than "terrain".
        objectives = [t for t in all_tags if t.startswith("obj_")]
        name = " / ".join(objectives) if objectives else "terrain area"
    desc = " / ".join(part.strip() for part in (piece.get("d") or "").splitlines()
                      if part.strip())
    return f"{name} - {desc}" if desc else name


def _board_pieces(log, w, h):
    """Board objects with the report's own classification and label attached.

    Done here rather than in the viewer so it is testable, and so the rules stay in
    one place instead of being restated in JavaScript.
    """
    out = []
    for piece in (log.get("board") or []):
        if not isinstance(piece, dict):
            continue
        entry = dict(piece)
        entry["kind"] = classify_terrain(piece, (w, h))
        entry["label"] = terrain_label(piece)
        out.append(entry)
    return out


def map_plates(log):
    """Exact terrain outlines for the map this log was recorded on, or None.

    None means the map could not be resolved -- a Combat Patrol map, whose one-off
    meshes are out of scope, or a log with no map recorded. The caller draws no
    terrain in that case rather than falling back to boxes: a board missing some of
    its terrain reads as a map with less terrain on it, which is worse than a board
    that says it could not draw any.
    """
    guid = (log.get("game") or {}).get("mapGuid")
    try:
        return map_terrain.map_terrain(guid)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _objective_anchors(pieces):
    """Where to draw objective markers.

    The physical markers are the truth when the log captured them. Otherwise fall
    back to the obj_*-tagged areas, which are the outlines the map draws around
    each objective and sit within an inch of the marker they surround.
    """
    marks = [p for p in pieces if p["kind"] == KIND_OBJECTIVE]
    if not marks:
        marks = [p for p in pieces
                 if any(str(t).startswith("obj_") for t in (p.get("t") or []))]
    return [{"x": p.get("x", 0), "z": p.get("z", 0), "label": p.get("label") or "objective"}
            for p in marks]


def _model_entry(values, names):
    """A schema 2 snapshot model row.

    ``[x, y, z, rotY, nameIndex]``, with ``[rx, rz]`` appended together when
    either is non-zero -- the same shape captureBoard uses for tilted board
    pieces. Y is what puts a model back on a ruin's upper floor rather than on
    the ground, and it is drawn nowhere in the report; it is carried anyway so
    the report and the in-game rewind read the same rows.

    ``nameIndex`` points into the log's ``names`` pool: the model's nickname as
    it was DISPLAYED at that moment, wound prefix and colour markup included.
    That replaced schema 1's bare trailing wound count, so the wounds here are
    parsed back out of the name.
    """
    if not isinstance(values, (list, tuple)) or len(values) < 5:
        return None
    idx = values[4]
    raw_name = ""
    if isinstance(idx, int) and 1 <= idx <= len(names):
        raw_name = names[idx - 1] or ""
    name, cur, mx = parse_model_name(raw_name)
    return {
        "x": float(values[0]),
        "y": float(values[1]),
        "z": float(values[2]),
        "ry": float(values[3]),
        "rx": float(values[5]) if len(values) > 5 and values[5] is not None else 0.0,
        "rz": float(values[6]) if len(values) > 6 and values[6] is not None else 0.0,
        "name": name,
        "w": cur,
        "mw": mx,
    }


def build_frames(log):
    """Expand the log into one self-contained frame per snapshot.

    Casualties accumulate: a model listed in a snapshot's ``dead`` stays dead in
    every later frame, so the report shows losses building up over the game.
    """
    w, h = board_size(log)
    roster = log["roster"]
    names = log.get("names") or []
    frames = []
    dead = set()

    for snap in log["snaps"]:
        dead.update(snap.get("dead") or [])
        models = []
        for guid, values in (snap.get("m") or {}).items():
            entry = _model_entry(values, names)
            if entry is None:
                continue
            rec = roster.get(guid) or {}
            base = rec.get("b") or [0.63, 0.63]
            # The snapshot's own nickname wins over the registration name: it is
            # what the model was actually called at that moment, so a unit
            # renamed mid-game reads correctly frame by frame. The registration
            # name is the fallback for a model whose nickname did not intern
            # (the pool is capped).
            reg_name, _, reg_max = parse_model_name(rec.get("n") or "")
            models.append({
                "guid": guid,
                "name": entry["name"] or reg_name or rec.get("n") or guid,
                "color": rec.get("c") or "Red",
                "unit": rec.get("u"),
                "x": entry["x"],
                "y": entry["y"],
                "z": entry["z"],
                "ry": entry["ry"],
                "wounds": entry["w"],
                "max_wounds": entry["mw"] if entry["mw"] is not None else (rec.get("w") or reg_max),
                "bx": float(base[0]) if len(base) > 0 else 0.63,
                "bz": float(base[1]) if len(base) > 1 else 0.63,
                "reserve": abs(entry["x"]) > w / 2 or abs(entry["z"]) > h / 2,
            })

        casualties = []
        for guid in sorted(dead):
            rec = roster.get(guid) or {}
            casualties.append({
                "guid": guid,
                "name": rec.get("n") or guid,
                "color": rec.get("c") or "Red",
                "unit": rec.get("u"),
            })

        models.sort(key=lambda m: (m["color"], m["name"]))
        # The deployment baseline is the board as it stood when Start Game was
        # pressed -- before any turn. The round counter already reads 1 by then,
        # so it is pulled out into its own round 0, which the viewer labels
        # "Deploy" and shows ahead of Round 1.
        why = snap.get("why", "phase")
        rnd = 0 if why == "deploy" else snap.get("r", 0)
        frames.append({
            "round": rnd,
            "turn": snap.get("t", ""),
            "phase": snap.get("p", 0),
            # The Lua label reads "Round 1, ... - Command Phase" because the
            # counter is already at 1; say what the frame actually is instead.
            "label": "Deployment" if why == "deploy" else snap.get("lbl", ""),
            "why": why,
            "ts": snap.get("ts"),
            "vp": snap.get("vp") or {},
            "cp": snap.get("cp") or {},
            "models": models,
            "casualties": casualties,
            "reserves": _frame_reserves(snap, roster),
            "secondaries": _frame_secondaries(snap),
        })
    return frames


def _frame_reserves(snap, roster):
    """Per-side occupancy of the two Reinforcements and Reserves boards."""
    out = {}
    for color, entry in (snap.get("res") or {}).items():
        if not isinstance(entry, dict):
            continue
        guids = entry.get("models") or []
        out[color] = {
            "guid": entry.get("g"),
            "x": entry.get("x"),
            "z": entry.get("z"),
            "count": len(guids),
            "models": [
                {"guid": g, "name": (roster.get(g) or {}).get("n") or g}
                for g in guids
            ],
        }
    return out


def _frame_secondaries(snap):
    """Cards sitting in each side's secondary zones, when LCT is tracking them."""
    out = {}
    for color, cards in (snap.get("sec") or {}).items():
        if not cards:
            continue
        # Schema 2 records which of the eight slots each card sat in. Sorting by
        # it makes the report's list match the board's top-to-bottom order rather
        # than whatever order the zone sweep happened to return.
        entries = [
            {
                "slot": c.get("i"),
                "name": c.get("n") or "Unnamed secondary",
                "face_down": bool(c.get("fd")),
            }
            for c in cards
            if isinstance(c, dict)
        ]
        entries.sort(key=lambda e: (e["slot"] is None, e["slot"] or 0))
        out[color] = entries
    return out


def build_report(log):
    """Full reconstructed report: the stable interface for any future viewer."""
    w, h = board_size(log)
    units = {}
    for uuid, unit in (log.get("units") or {}).items():
        units[uuid] = {
            "name": unit.get("n"),
            "color": unit.get("c"),
            "description": unit.get("d"),
            "profile": parse_datasheet(unit.get("d")),
        }
    game = log.get("game") or {}
    pieces = _board_pieces(log, w, h)
    # Terrain comes from the map's shipped payload, which is exact; the capture is
    # still what supplies the objective markers, which the payload does not hold.
    resolved = map_plates(log)
    if resolved is not None:
        terrain = resolved["plates"]
        if resolved["board"]:
            w, h = resolved["board"]
    else:
        terrain = []
    return {
        "schema": SCHEMA_VERSION,
        "game": game,
        "deployment": game.get("deploy"),
        "board": {"width": w, "height": h, "terrain": terrain,
                  "terrain_known": resolved is not None,
                  "objectives": _objective_anchors(pieces)},
        "units": units,
        "roster": log.get("roster") or {},
        "frames": build_frames(log),
    }


# --------------------------------------------------------------------------
# Rendering
#
# The page is a small client-side viewer rather than a stack of pre-rendered
# boards: the report JSON is embedded once and the SVG for the selected phase is
# built in the browser. That keeps the file a fraction of the size of 50 inlined
# boards, makes switching instant, and means the overlay toggles are live.
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: #10141e; --panel: #161c28; --line: #27324a;
  --ink: #e6ecf5; --dim: #93a1b5;
  --red: #c8393c; --blue: #2f6fd0;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
       overflow: hidden; }

.app { display: grid; grid-template-rows: auto auto 1fr; height: 100vh; }

header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
         padding: 7px 14px; border-bottom: 1px solid var(--line); }
header h1 { font-size: 15px; margin: 0; font-weight: 650; }
header .meta { color: var(--dim); font-size: 12px; }
header .gen { font-size: 10px; opacity: .55; margin-left: 10px; }
.chip { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

nav { padding: 6px 14px; border-bottom: 1px solid var(--line);
      display: flex; flex-direction: column; gap: 5px; }
.row { display: flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.row .label { color: var(--dim); font-size: 10.5px; text-transform: uppercase;
              letter-spacing: .06em; min-width: 52px; }
.sep { width: 1px; height: 18px; background: var(--line); margin: 0 8px; }
button { font: inherit; font-size: 12px; color: var(--ink); background: #1d2434;
         border: 1px solid var(--line); border-radius: 5px;
         padding: 3px 9px; cursor: pointer; }
button:hover { background: #26304a; }
button.on { background: #2f6fd0; border-color: #5b8fde; color: #fff; }
button.side-Red.on { background: var(--red); border-color: #f2a3a5; }
button:disabled { opacity: .35; cursor: default; }
.spacer { flex: 1; }

main { display: grid; grid-template-columns: 1fr 232px; min-height: 0; }
.stage { min-height: 0; min-width: 0; position: relative; padding: 4px; }
svg.board { width: 100%; height: 100%; display: block; cursor: grab;
            touch-action: none; }
svg.board.dragging { cursor: grabbing; }
.hint { position: absolute; right: 10px; bottom: 8px; color: var(--dim);
        font-size: 11px; pointer-events: none; opacity: .75; }

aside { border-left: 1px solid var(--line); padding: 10px; overflow-y: auto;
        min-height: 0; font-size: 12.5px; }
aside h2 { font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em;
           color: var(--dim); margin: 12px 0 5px; font-weight: 600; }
aside h2:first-child { margin-top: 0; }
.score { display: flex; gap: 8px; }
.score .side { flex: 1; background: var(--panel); border: 1px solid var(--line);
               border-radius: 7px; padding: 7px; }
.score .side b { display: block; font-size: 18px; }
.score .Red b { color: var(--red); } .score .Blue b { color: var(--blue); }
.score .side span { color: var(--dim); font-size: 11px; }
.score .side em { display: block; color: var(--dim); font-size: 10px;
                 font-style: normal; opacity: .8; }
ul.list { list-style: none; margin: 0; padding: 0; }
ul.list li { padding: 2px 0; border-bottom: 1px solid #1e2739; }
ul.list li:last-child { border-bottom: 0; }
.fd { color: var(--dim); font-size: 11px; }
.none { color: var(--dim); font-style: italic; }
.cas .Red { color: #e08a8c; } .cas .Blue { color: #8fb4ee; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
          margin-right: 6px; vertical-align: -1px; }

/* Board geometry is in inches (the viewBox is the table), so every stroke and
   font size below is an inch measurement, not pixels. A 32mm base is ~0.63in
   across, which is the scale these numbers are chosen against. */
.mat { fill: #1d2434; stroke: #38455f; stroke-width: 0.18; }
/* Terrain areas, in the map's own grey. The fill stays near-transparent so
   overlapping plates and the models standing on them all stay readable. */
.t-area  { fill: #8e9bb114; stroke: #97a3b8; stroke-width: 0.08; }
.objective { fill: #d8b45a33; stroke: #d8b45a; stroke-width: 0.1; }
.quarter { stroke: #ffffff; stroke-width: 0.07; opacity: .4; }
.territory { stroke: #ffd479; stroke-width: 0.11; opacity: .85; }
.reserveline { fill: none; stroke: #7fe0c0; stroke-width: 0.09;
               stroke-dasharray: 0.7 0.45; opacity: .85; }
.denial { fill: none; stroke: #ff9d5c; stroke-width: 0.09;
          stroke-dasharray: 0.45 0.35; opacity: .85; }
.dz { fill: none; stroke-width: 0.16; }
.dz-wide { fill: none; stroke-width: 0.3; }
.mlabel { fill: #cdd8e8; font-size: 0.85px; text-anchor: middle;
          paint-order: stroke; stroke: #10141e; stroke-width: 0.28px; }
.zlabel { font-size: 1.5px; text-anchor: middle; font-weight: 600;
          paint-order: stroke; stroke: #10141e; stroke-width: 0.5px; }
.caption { fill: #93a1b5; font-size: 1.5px; text-anchor: middle; }

/* Terrain key, in the side panel where it does not eat board space. */
.key { display: flex; flex-wrap: wrap; gap: 4px 12px; }
.key span { display: inline-flex; align-items: center; gap: 5px;
            color: var(--dim); font-size: 11px; }
.key i { width: 11px; height: 8px; border-radius: 2px; display: inline-block;
         border: 1px solid currentColor; }
.key .area  { color: #97a3b8; background: #8e9bb133; }
.key .note { color: var(--dim); font-size: 11px; }
"""


def render_html(report, show_labels=True, script_nonce=None):
    # Stamped into the header. The in-game button renders through a long-running
    # server, so "am I looking at a fresh page or a stale one?" is a real question
    # and the answer belongs on the page itself.
    generated = "rendered " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    game = report.get("game") or {}
    red = game.get("red") or "Red"
    blue = game.get("blue") or "Blue"
    payload = json.dumps({
        "board": report["board"],
        "deployment": report.get("deployment"),
        "frames": report["frames"],
        "game": {"map": game.get("map"), "red": red, "blue": blue},
        "showLabels": bool(show_labels),
    }, separators=(",", ":"))
    # Model names come from imported army lists, so treat them as hostile. Inside a
    # <script> element the byte sequence "</script>" closes the block regardless of
    # JSON quoting, which would let a crafted nickname inject markup into the page.
    # Escaping the three markup-significant characters as \uXXXX leaves the JSON
    # meaning identical while making that sequence unrepresentable.
    payload = (payload.replace("&", "\\u0026")
                      .replace("<", "\\u003c")
                      .replace(">", "\\u003e"))

    # Only the hosted server has a CSP strict enough to need this (script-src
    # 'nonce-...'); the local file:// output has no CSP at all, and passing
    # None here (write_report's default) must leave that output byte-identical
    # to before this parameter existed.
    nonce_attr = f' nonce="{html.escape(script_nonce)}"' if script_nonce else ""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Battle Report - {html.escape(str(game.get('map') or 'LCT'))}</title>
<style>{CSS}</style></head><body>
<div class="app">
  <header>
    <h1>Battle Report</h1>
    <span class="chip"><span class="dot" style="background:var(--red)"></span>{html.escape(str(red))}</span>
    <span class="chip"><span class="dot" style="background:var(--blue)"></span>{html.escape(str(blue))}</span>
    <span class="meta">{html.escape(str(game.get('map') or 'unknown map'))}</span>
    <span class="meta" id="deployName"></span>
    <span class="spacer"></span>
    <span class="meta" id="frameCount"></span>
    <span class="meta gen" title="When this page was rendered">{html.escape(generated)}</span>
  </header>
  <nav>
    <div class="row">
      <span class="label">Round</span><span id="rounds" class="row"></span>
      <span class="sep"></span>
      <span class="label">Player</span><span id="players" class="row"></span>
    </div>
    <div class="row"><span class="label">Phase</span><span id="phases" class="row"></span></div>
    <div class="row">
      <span class="label">View</span>
      <button id="deployView">Deployment map</button>
      <span class="sep"></span>
      <span id="toggles" class="row"></span>
      <span class="spacer"></span>
      <button id="reset">Reset view</button>
      <button id="prev">&#9664; Prev</button>
      <button id="next">Next phase &#9654;</button>
    </div>
  </nav>
  <main>
    <div class="stage">
      <svg id="board" class="board"></svg>
      <div class="hint" id="hint">scroll to zoom &middot; drag to pan</div>
    </div>
    <aside>
      <h2>Score</h2>
      <div class="score">
        <div class="side Red"><b id="vpR">0</b><span>VP &middot; <i id="cpR">0</i> CP</span>
          <em id="splitR"></em></div>
        <div class="side Blue"><b id="vpB">0</b><span>VP &middot; <i id="cpB">0</i> CP</span>
          <em id="splitB"></em></div>
      </div>
      <div id="panels"></div>
    </aside>
  </main>
</div>
<script type="application/json" id="data"{nonce_attr}>{payload}</script>
<script{nonce_attr}>{VIEWER_JS}</script>
</body></html>"""


VIEWER_JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const W = D.board.width, H = D.board.height;
const PAD = 3;
const NS = 'http://www.w3.org/2000/svg';
const svg = document.getElementById('board');

// LCT overlay geometry, reproduced from the interactive-button scripts so the
// report offers the same toggles the table does: strategic reserves is 6" from
// each edge and area denial is 3"/6" from centre, matching those scripts.
const TOGGLES = [
  {id:'terrain',   label:'Terrain',        on:true},
  {id:'deploy',    label:'Deployment',     on:true},
  {id:'objectives',label:'Objectives',     on:true},
  {id:'territory', label:'Territory',      on:false},
  {id:'quarters',  label:'Quarters',       on:false},
  {id:'reserves',  label:'Strat res 6"',   on:false},
  {id:'denial',    label:'Centre 3"/6"',   on:false},
  {id:'labels',    label:'Names',          on:true},
];
const state = {frame:0, deployMap:false, on:{}};
TOGGLES.forEach(t => state.on[t.id] = (t.id === 'labels') ? (D.showLabels !== false) : t.on);

const el = (tag, attrs, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
// SVG y grows downward, so z is negated: TTS +z is the far edge of the table and
// belongs at the TOP of the picture. Mapping it straight through mirrored the whole
// board -- and every model's facing with it, since a mirrored board is the view from
// underneath the table. With the negation, a TTS yaw is a plain SVG rotate().
const sx = x => (x + W/2) + PAD;
const sz = z => (H/2 - z) + PAD;

function line(g, x1, z1, x2, z2, cls, color) {
  const n = el('line', {x1:sx(x1), y1:sz(z1), x2:sx(x2), y2:sz(z2), class:cls}, g);
  if (color) n.setAttribute('stroke', color);
  return n;
}

/* ---------------- pan & zoom (viewBox driven) ---------------- */
const FULL = {x:0, y:0, w:W + PAD*2, h:H + PAD*2};
const view = Object.assign({}, FULL);

function applyView() {
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
}
function resetView() { Object.assign(view, FULL); applyView(); }

// Exact under xMidYMid meet: the board is letterboxed inside the element, so the
// scale is the smaller ratio and the remainder is split evenly as an offset.
function metrics() {
  const r = svg.getBoundingClientRect();
  const s = Math.min(r.width / view.w, r.height / view.h);
  return {r, s, ox: (r.width - view.w*s)/2, oy: (r.height - view.h*s)/2};
}
function toUser(clientX, clientY) {
  const m = metrics();
  return {x: view.x + (clientX - m.r.left - m.ox)/m.s,
          y: view.y + (clientY - m.r.top  - m.oy)/m.s};
}

svg.addEventListener('wheel', e => {
  e.preventDefault();
  const p = toUser(e.clientX, e.clientY);
  const k = e.deltaY < 0 ? 0.85 : 1/0.85;
  const nw = Math.min(Math.max(view.w * k, 6), FULL.w * 4);
  const nh = nw * (view.h / view.w);
  view.x = p.x - (p.x - view.x) * (nw / view.w);
  view.y = p.y - (p.y - view.y) * (nh / view.h);
  view.w = nw; view.h = nh;
  applyView();
}, {passive:false});

let drag = null;
svg.addEventListener('pointerdown', e => {
  drag = {cx:e.clientX, cy:e.clientY, vx:view.x, vy:view.y};
  svg.classList.add('dragging');
  svg.setPointerCapture(e.pointerId);
});
svg.addEventListener('pointermove', e => {
  if (!drag) return;
  const s = metrics().s;
  view.x = drag.vx - (e.clientX - drag.cx)/s;
  view.y = drag.vy - (e.clientY - drag.cy)/s;
  applyView();
});
function endDrag(e) {
  if (!drag) return;
  drag = null;
  svg.classList.remove('dragging');
  if (e && e.pointerId !== undefined && svg.hasPointerCapture(e.pointerId))
    svg.releasePointerCapture(e.pointerId);
}
svg.addEventListener('pointerup', endDrag);
svg.addEventListener('pointercancel', endDrag);
svg.addEventListener('dblclick', resetView);

/* ---------------- deployment zones ---------------- */
function DEPLOY_COLOR(name) {
  const n = (name || '').toLowerCase();
  // Teal is remapped in game by getDeploymentOverlayColor; the rest are TTS
  // colour names passed straight to spawnLine.
  if (n === 'red') return '#e2484b';
  if (n === 'teal') return '#59a6ff';
  if (n === 'blue') return '#3f7fe0';
  if (n === 'yellow') return '#e8c53a';
  if (n === 'black') return '#8a90a0';
  if (n === 'white') return '#dfe6f2';
  return '#dfe6f2';
}

// LCT draws only a zone's boundary lines, so the report does the same, using the
// same maths as drawLine / drawSteps / drawQuarter in startMenu.ttslua.
// "No Deployment Zone" stores draw as a bare {type = "none"} table rather than a
// list of shapes, so every reader goes through this instead of touching .draw.
function drawShapes(spec) {
  const d = spec && spec.draw;
  return Array.isArray(d) ? d : [];
}

function drawDeployment(g, spec, cls) {
  if (!spec || !spec.draw) return;
  cls = cls || 'dz';
  for (const d of drawShapes(spec)) {
    const c = DEPLOY_COLOR(d.color);
    const zFacing = (d.position === 'z' || d.position === '-z');
    const mapHeight = zFacing ? H : W;   // axis the offset is measured along
    const mapBase   = zFacing ? W : H;   // axis the line runs along
    const sign = (d.position || '').startsWith('-') ? -1 : 1;

    if (d.type === 'line') {
      const off = (d.fromSide !== undefined && d.fromSide !== 0)
        ? (mapHeight/2 - d.fromSide) : (d.fromCenter || 0);
      if (zFacing) line(g, -mapBase/2, sign*off, mapBase/2, sign*off, cls, c);
      else         line(g, sign*off, -mapBase/2, sign*off, mapBase/2, cls, c);
    } else if (d.type === 'stepped') {
      const steps = d.steps || [];
      const len = mapBase / steps.length;
      let run = mapBase/2 - len/2;
      for (let i = 0; i < steps.length; i++) {
        const off = steps[i].fromCenter !== undefined
          ? steps[i].fromCenter : (mapHeight/2 - steps[i].fromSide);
        if (zFacing) line(g, run - len/2, sign*off, run + len/2, sign*off, cls, c);
        else         line(g, sign*off, run - len/2, sign*off, run + len/2, cls, c);
        run -= len/2;
        if (i < steps.length - 1) {
          const nx = steps[i+1].fromCenter !== undefined
            ? steps[i+1].fromCenter : (mapHeight/2 - steps[i+1].fromSide);
          if (zFacing) line(g, run, sign*off, run, sign*nx, cls, c);
          else         line(g, sign*off, run, sign*nx, run, cls, c);
        }
        run -= len/2;
      }
    } else if (d.type === 'quarter') {
      const fc = d.fromCenter || 0;
      const zs = (d.position.indexOf('-z') >= 0) ? -1 : 1;
      const xs = (d.position.indexOf('-x') >= 0) ? -1 : 1;
      line(g, 0, zs*fc, 0, zs*H/2, cls, c);
      line(g, xs*fc, 0, xs*W/2, 0, cls, c);
    } else if (d.type === 'circle') {
      el('circle', {cx:sx(0), cy:sz(0), r:(d.fromCenter||0), class:cls, stroke:c}, g);
    } else if (d.type === 'triangle') {
      // Reconstructed: LCT draws one diagonal of length sqrt(base^2+height^2)
      // centred at mapHeight/4. Worth eyeballing on Crucible of Battle.
      if (zFacing) line(g, -W/2, 0, W/2, sign*H/2, cls, c);
      else         line(g, 0, -H/2, sign*W/2, H/2, cls, c);
    } else if (d.type === 'cornerRectangle') {
      // drawCornerRectangle: a box from the centre offset out to the two edges.
      const fc = d.fromCenter || 0;
      const rw = W/2 - fc, rh = H/2 - fc;
      const xs = d.position.indexOf('-x') >= 0 ? -1 : 1;
      const zs = d.position.indexOf('-z') >= 0 ? -1 : 1;
      const cx = xs * (fc + rw/2), cz = zs * (fc + rh/2);
      // y takes the HIGHER z of the pair: sz() flips, so that is the top edge.
      el('rect', {x:sx(cx - rw/2), y:sz(cz + rh/2), width:rw, height:rh,
                  class:cls, stroke:c, fill:'none'}, g);
    } else if (d.type === 'circleDeployment') {
      // drawCircleDeployment: an off-centre circle; note the game negates z for
      // the -x / x cases, so the sign convention is copied rather than guessed.
      const fc = d.fromCenter || 0;
      let cx = 0, cz = 0;
      if (d.position === 'z')       { cz = fc; }
      else if (d.position === '-z') { cz = -fc; }
      else if (d.position === '-x') { cx = -fc; cz = -(d.fromCenterZ || 0); }
      else if (d.position === 'x')  { cx = fc;  cz = -(d.fromCenterZ || 0); }
      // radius follows the same setScale convention the plain circle uses.
      el('circle', {cx:sx(cx), cy:sz(cz), r:(d.radius || 0),
                    class:cls, stroke:c}, g);
    } else if (d.type === 'cornerPolyline') {
      // drawCornerPolyline: points are read off the mission image with the
      // origin at the named corner, x across the short edge, y up the long one.
      drawCornerPolyline(g, d, c, cls);
    }
  }
}

function drawCornerPolyline(g, d, c, cls) {
  const pts = d.points || [];
  if (pts.length < 2) return;
  const topRight = (d.position || 'bottomLeft') === 'topRight';
  const pt = p => {
    const fromLeft = Math.min(p[0] || 0, H), fromBottom = Math.min(p[1] || 0, W);
    return topRight ? {x: W/2 - fromBottom, z: -H/2 + fromLeft}
                    : {x: -W/2 + fromBottom, z: H/2 - fromLeft};
  };
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pt(pts[i]), b = pt(pts[i+1]);
    line(g, a.x, a.z, b.x, b.z, cls, c);
  }
}

/* ---------------- scenes ---------------- */
function clear() { while (svg.firstChild) svg.removeChild(svg.firstChild); }
function mat() { el('rect', {x:PAD, y:PAD, width:W, height:H, class:'mat'}, svg); }

function drawObjectives(g) {
  for (const o of (D.board.objectives || [])) {
    const n = el('circle', {cx:sx(o.x||0), cy:sz(o.z||0), r:0.8, class:'objective'}, g);
    el('title', {}, n).textContent = o.label || 'objective';
  }
}

// Terrain areas are the flat plates the ruins stand on. Their outlines arrive
// already placed in board coordinates -- the exact mesh silhouette, positioned and
// rotated when the report was built -- so there is nothing to transform here. The
// ruins themselves are deliberately not drawn.
function drawTerrain(g) {
  for (const t of D.board.terrain) {
    const pts = (t.points || []).map(p => `${sx(p[0])},${sz(p[1])}`).join(' ');
    if (!pts) continue;
    const n = el('polygon', {points:pts, class:'t-area'}, g);
    const tags = (t.tags || []).filter(s => s.indexOf('obj_') === 0);
    el('title', {}, n).textContent =
      tags.length ? `terrain area (${tags.join(', ')})` : 'terrain area';
  }
}

/* ---------------- territory divider ---------------- */
// Ported from drawTerritoryLine in startMenu.ttslua. The divider is not simply the
// centre line: it is the line through the table centre equidistant from the two
// deployment zones, so its angle comes from the deployment that was played.

function territoryReferenceEntry(spec) {
  const shapes = drawShapes(spec);
  if (!shapes.length) return null;
  for (const d of shapes)
    if (d.type !== 'circle' && d.type !== 'circleInZone' && d.type !== 'circleDeployment')
      return d;
  return shapes[0];
}

function stepBoundaryFromCentre(step, halfEdge) {
  return step.fromCenter !== undefined ? step.fromCenter
                                       : halfEdge - (step.fromSide || 0);
}

// Length of a centre-crossing line at thetaDeg (0 = +X, 90 = +Z), clipped to the
// board rectangle.
function territoryChordLength(thetaDeg, hx, hz) {
  const r = thetaDeg * Math.PI / 180;
  const c = Math.abs(Math.cos(r)), s = Math.abs(Math.sin(r));
  const tx = c > 1e-6 ? hx / c : Infinity;
  const tz = s > 1e-6 ? hz / s : Infinity;
  return 2 * Math.min(tx, tz);
}

function drawTerritory(g) {
  const spec = D.deployment;
  // The Combat Patrol zones declare their divider outright, and where that exists
  // it wins: their cornerPolyline shapes have no angle to derive.
  if (spec && Array.isArray(spec.territory)) {
    for (const t of spec.territory) drawCornerPolyline(g, t, null, 'territory');
    return;
  }
  const hx = W/2, hz = H/2;
  const entry = territoryReferenceEntry(spec);
  const pos = (entry && entry.position) || 'x';
  const type = (entry && entry.type) || 'line';
  let theta;
  if (type === 'triangle') {
    theta = Math.atan(H / (W/2)) * 180 / Math.PI;        // Crucible of Battle
  } else if (type === 'quarter') {
    theta = Math.atan(H / W) * 180 / Math.PI;            // Search and Destroy
  } else if (type === 'stepped' && entry.steps && entry.steps[1]) {
    // Join the edge midpoints between the two staggered boundaries. The sign
    // carries the table's rot.y handedness so the slight tilt leans the right way.
    if (pos === 'z' || pos === '-z') {
      const m = (stepBoundaryFromCentre(entry.steps[0], hz)
                 - stepBoundaryFromCentre(entry.steps[1], hz)) / 2;
      theta = Math.atan(((pos === 'z' ? -1 : 1) * m) / hx) * 180 / Math.PI;
    } else {
      const m = (stepBoundaryFromCentre(entry.steps[0], hx)
                 - stepBoundaryFromCentre(entry.steps[1], hx)) / 2;
      theta = Math.abs(m) < 1e-6 ? 90
            : Math.atan(hz / ((pos === 'x' ? -1 : 1) * m)) * 180 / Math.PI;
    }
  } else if (pos === 'z' || pos === '-z') {
    theta = 0;                                            // Dawn of War
  } else {
    theta = 90;                                           // Hammer and Anvil
  }
  const len = territoryChordLength(theta, hx, hz);
  // TTS yaw turns +z toward +x, so a rot.y of theta points the line along
  // (cos, -sin). Checked against Search and Destroy, where it lands exactly on the
  // corner-to-corner diagonal.
  const r = theta * Math.PI / 180;
  const dx = Math.cos(r) * len/2, dz = -Math.sin(r) * len/2;
  line(g, -dx, -dz, dx, dz, 'territory');
}

// The deployment map deliberately replaces the battle view rather than layering
// on it: it is the mission's setup diagram, not a moment in the game.
function renderDeployMap() {
  clear();
  mat();
  drawTerrain(el('g', {}, svg));
  const g = el('g', {}, svg);
  line(g, -W/2, 0, W/2, 0, 'quarter');
  line(g, 0, -H/2, 0, H/2, 'quarter');
  drawTerritory(el('g', {}, svg));
  drawObjectives(el('g', {}, svg));
  drawDeployment(el('g', {}, svg), D.deployment, 'dz-wide');

  const name = (D.deployment && D.deployment.name) || 'No deployment recorded';
  const t = el('text', {x:sx(0), y:sz(H/2) - 0.9, class:'caption'}, svg);
  t.textContent = name;

  for (const d of drawShapes(D.deployment)) {
    if (!d.color) continue;
    const zFacing = (d.position === 'z' || d.position === '-z');
    const sign = (d.position || '').startsWith('-') ? -1 : 1;
    const lx = zFacing ? 0 : sign * (W/2 - 4);
    const lz = zFacing ? sign * (H/2 - 3) : 0;
    const lbl = el('text', {x:sx(lx), y:sz(lz), class:'zlabel',
                            fill:DEPLOY_COLOR(d.color)}, svg);
    lbl.textContent = d.color;
  }
  applyView();
  sidePanel(null);
  syncTabs();
}

function renderFrame() {
  const f = D.frames[state.frame];
  clear();
  mat();

  if (state.on.terrain) drawTerrain(el('g', {}, svg));
  if (state.on.quarters) {
    const g = el('g', {}, svg);
    line(g, -W/2, 0, W/2, 0, 'quarter');
    line(g, 0, -H/2, 0, H/2, 'quarter');
  }
  if (state.on.territory) drawTerritory(el('g', {}, svg));
  if (state.on.reserves)
    el('rect', {x:sx(-W/2+6), y:sz(H/2-6), width:W-12, height:H-12,
                class:'reserveline'}, el('g', {}, svg));
  if (state.on.denial) {
    const g = el('g', {}, svg);
    el('circle', {cx:sx(0), cy:sz(0), r:3, class:'denial'}, g);
    el('circle', {cx:sx(0), cy:sz(0), r:6, class:'denial'}, g);
  }
  if (state.on.deploy) drawDeployment(el('g', {}, svg), D.deployment);
  if (state.on.objectives) drawObjectives(el('g', {}, svg));

  const gm = el('g', {}, svg);
  for (const m of f.models) {
    if (m.reserve) continue;
    const cx = sx(m.x), cy = sz(m.z);
    const rx = Math.max(m.bx, 0.2), ry = Math.max(m.bz, 0.2);
    const e = el('ellipse', {cx, cy, rx, ry,
      fill: m.color === 'Red' ? '#c8393c' : '#2f6fd0',
      stroke: m.color === 'Red' ? '#f2a3a5' : '#9dc0f5',
      'stroke-width': 0.06,
      transform:`rotate(${m.ry} ${cx} ${cy})`}, gm);
    const w = (m.wounds != null && m.max_wounds) ? ` (${m.wounds}/${m.max_wounds})` : '';
    el('title', {}, e).textContent = m.name + w;
    if (state.on.labels) {
      const t = el('text', {x:cx, y:cy - ry - 0.25, class:'mlabel'}, gm);
      t.textContent = m.name.length > 18 ? m.name.slice(0,18) + '…' : m.name;
    }
  }

  applyView();
  sidePanel(f);
  syncTabs();
}

function render() {
  state.deployMap ? renderDeployMap() : renderFrame();
}

/* ---------------- side panel ---------------- */
function esc(s) {
  return String(s).replace(/[&<>"]/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}

// Colour key for the board, kept in the panel so it costs no board space. Only
// the flat terrain AREAS are drawn; the ruins standing on them are not, so there
// is nothing else to key. A map whose terrain could not be resolved says so here
// rather than silently showing an empty table.
const TERRAIN_KEY = D.board.terrain_known
  ? '<div class="key"><span><i class="area"></i>terrain area</span></div>'
  : '<div class="key"><span class="note">No terrain outlines ship for this map.</span></div>';

function setSplit(id, vp, pk, sk) {
  const e = document.getElementById(id);
  e.textContent = (vp && vp[pk] !== undefined && vp[sk] !== undefined)
    ? `PRI ${vp[pk]} · SEC ${vp[sk]}` : '';
}

function sidePanel(f) {
  const P = [];
  if (!f) {
    document.getElementById('vpR').textContent = '-';
    document.getElementById('vpB').textContent = '-';
    document.getElementById('splitR').textContent = '';
    document.getElementById('splitB').textContent = '';
    document.getElementById('cpR').textContent = '-';
    document.getElementById('cpB').textContent = '-';
    P.push('<h2>Terrain</h2>' + TERRAIN_KEY);
    P.push('<h2>Deployment</h2>');
    const dep = D.deployment;
    if (!dep) P.push('<div class="none">no deployment recorded</div>');
    else {
      P.push(`<div><b>${esc(dep.name || 'unnamed')}</b></div><ul class="list">` +
        drawShapes(dep).map(d => {
          const bits = [d.type];
          if (d.position) bits.push(d.position);
          if (d.fromSide !== undefined) bits.push(d.fromSide + '" from side');
          if (d.fromCenter !== undefined) bits.push(d.fromCenter + '" from centre');
          if (d.radius !== undefined) bits.push(d.radius + '" radius');
          if (d.steps) bits.push(d.steps.map(s => s.fromSide + '"').join(' / '));
          if (d.points) bits.push(d.points.length + ' points');
          return `<li><span class="swatch" style="background:${DEPLOY_COLOR(d.color)}"></span>` +
                 `${esc(bits.join(' · '))}</li>`;
        }).join('') + '</ul>');
      P.push('<div class="fd">Boundary lines only, as drawn on the table.</div>');
    }
    document.getElementById('panels').innerHTML = P.join('');
    return;
  }

  document.getElementById('vpR').textContent = (f.vp && f.vp.r) || 0;
  document.getElementById('vpB').textContent = (f.vp && f.vp.b) || 0;
  document.getElementById('cpR').textContent = (f.cp && f.cp.r) || 0;
  document.getElementById('cpB').textContent = (f.cp && f.cp.b) || 0;
  // The primary/secondary split is only in logs recorded after it was captured,
  // so an older log simply shows no split rather than a pair of zeroes.
  setSplit('splitR', f.vp, 'rp', 'rs');
  setSplit('splitB', f.vp, 'bp', 'bs');

  const onBoard = f.models.filter(m => !m.reserve).length;
  P.push(`<h2>Board</h2><ul class="list"><li>${onBoard} model(s) deployed</li>` +
         `<li>${f.models.length - onBoard} off table</li>` +
         `<li class="fd">trigger: ${esc(f.why)}</li></ul>`);
  P.push('<h2>Terrain</h2>' + TERRAIN_KEY);

  const res = f.reserves || {}, resKeys = Object.keys(res);
  P.push('<h2>Reserves boards</h2>');
  if (!resKeys.length) P.push('<div class="none">not tracked</div>');
  else P.push('<ul class="list">' + resKeys.map(c =>
      `<li><b style="color:var(--${c.toLowerCase()})">${esc(c)}</b>: ${res[c].count} model(s)` +
      (res[c].count ? `<div class="fd">${res[c].models.map(m=>esc(m.name)).join(', ')}</div>` : '') +
      `</li>`).join('') + '</ul>');

  const sec = f.secondaries || {}, secKeys = Object.keys(sec);
  P.push('<h2>Active secondaries</h2>');
  if (!secKeys.length) P.push('<div class="none">not tracked by LCT</div>');
  else P.push(secKeys.map(c =>
      `<div><b style="color:var(--${c.toLowerCase()})">${esc(c)}</b><ul class="list">` +
      sec[c].map(s => `<li>${esc(s.name)}${s.face_down ? ' <span class="fd">(face down)</span>' : ''}</li>`).join('') +
      '</ul></div>').join(''));

  P.push('<h2>Casualties</h2>');
  if (!f.casualties.length) P.push('<div class="none">none yet</div>');
  else P.push('<ul class="list cas">' + f.casualties.map(c =>
      `<li class="${c.color}">${esc(c.name)}</li>`).join('') + '</ul>');

  document.getElementById('panels').innerHTML = P.join('');
}

/* ---------------- navigation ---------------- */
const PHASE_NAMES = ['Command','Movement','Shooting','Charge','Fight'];
const phaseName = f => PHASE_NAMES[(f.phase || 1) - 1] || ('Phase ' + f.phase);
const rounds = [...new Set(D.frames.map(f => f.round))].sort((a,b) => a-b);
const playersIn = r => [...new Set(D.frames.filter(f => f.round === r).map(f => f.turn))];

function goto(pred) {
  const i = D.frames.findIndex(pred);
  if (i >= 0) { state.frame = i; render(); }
}

function buildTabs() {
  const rd = document.getElementById('rounds');
  rounds.forEach(r => {
    const b = document.createElement('button');
    b.textContent = r === 0 ? 'Deploy' : 'R' + r;
    b.dataset.round = r;
    b.onclick = () => { state.deployMap = false; goto(f => f.round === r); };
    rd.appendChild(b);
  });

  const tg = document.getElementById('toggles');
  TOGGLES.forEach(t => {
    const b = document.createElement('button');
    b.textContent = t.label;
    b.dataset.toggle = t.id;
    b.onclick = () => { state.on[t.id] = !state.on[t.id]; render(); };
    tg.appendChild(b);
  });

  document.getElementById('deployView').onclick = () => {
    state.deployMap = !state.deployMap;
    render();
  };
  document.getElementById('reset').onclick = () => { resetView(); };
  document.getElementById('prev').onclick = () => {
    if (state.deployMap) return;
    if (state.frame > 0) { state.frame--; render(); }
  };
  document.getElementById('next').onclick = () => {
    if (state.deployMap) return;
    if (state.frame < D.frames.length - 1) { state.frame++; render(); }
  };
  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight') document.getElementById('next').click();
    if (e.key === 'ArrowLeft') document.getElementById('prev').click();
    if (e.key === '0') resetView();
  });

  document.getElementById('deployName').textContent =
    D.deployment && D.deployment.name ? '· ' + D.deployment.name : '';
}

function syncTabs() {
  const f = D.frames[state.frame];
  const dm = state.deployMap;

  document.getElementById('deployView').classList.toggle('on', dm);
  document.querySelectorAll('#rounds button').forEach(b =>
    b.classList.toggle('on', !dm && Number(b.dataset.round) === f.round));
  document.querySelectorAll('#toggles button').forEach(b => {
    b.classList.toggle('on', !!state.on[b.dataset.toggle]);
    b.disabled = dm;
  });

  const pl = document.getElementById('players');
  pl.innerHTML = '';
  playersIn(f.round).forEach(t => {
    const b = document.createElement('button');
    b.textContent = t;
    b.className = 'side-' + t + (!dm && t === f.turn ? ' on' : '');
    b.disabled = dm;
    b.onclick = () => goto(x => x.round === f.round && x.turn === t);
    pl.appendChild(b);
  });

  const ph = document.getElementById('phases');
  ph.innerHTML = '';
  D.frames.forEach((fr, i) => {
    if (fr.round !== f.round || fr.turn !== f.turn) return;
    const b = document.createElement('button');
    b.textContent = (fr.why === 'deploy') ? 'Deploy'
                  : phaseName(fr) + (fr.why === 'manual' ? ' *' : '');
    b.className = 'side-' + fr.turn + (!dm && i === state.frame ? ' on' : '');
    b.disabled = dm;
    b.onclick = () => { state.frame = i; render(); };
    ph.appendChild(b);
  });

  document.getElementById('prev').disabled = dm || state.frame === 0;
  document.getElementById('next').disabled = dm || state.frame === D.frames.length - 1;
  document.getElementById('frameCount').textContent = dm
    ? 'Deployment map'
    : `${state.frame + 1} / ${D.frames.length} — ${f.label}`;
}

buildTabs();
resetView();
render();
"""


def render_board_svg(report, frame, show_labels=True):
    """Static single-board SVG.

    The HTML page renders its boards in the browser; this is kept as a
    server-side renderer for tests and for anything that needs a standalone
    image of one phase.
    """
    w = report["board"]["width"]
    h = report["board"]["height"]
    pad = 6

    def sx(x):
        return (x + w / 2) + pad

    def sz(z):
        # Negated for the same reason as the viewer's sz: SVG y grows downward, so
        # TTS +z (the far edge) belongs at the top of the picture.
        return (h / 2 - z) + pad

    parts = [
        f'<svg viewBox="0 0 {w + pad * 2:.0f} {h + pad * 2:.0f}" class="board" '
        f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img">',
        f'<rect x="{pad}" y="{pad}" width="{w:.1f}" height="{h:.1f}" class="mat"/>',
    ]

    # Terrain areas arrive already placed in board coordinates, so there is nothing
    # to rotate or mirror here. The ruins standing on them are not drawn.
    for piece in report["board"]["terrain"]:
        points = piece.get("points") or []
        if len(points) < 3:
            continue
        pts = " ".join(f"{sx(px):.2f},{sz(pz):.2f}" for px, pz in points)
        tags = [t for t in (piece.get("tags") or []) if str(t).startswith("obj_")]
        label = f"terrain area ({', '.join(tags)})" if tags else "terrain area"
        parts.append(f'<polygon points="{pts}" class="t-area">'
                     f'<title>{html.escape(label)}</title></polygon>')

    for obj in report["board"].get("objectives") or []:
        parts.append(
            f'<circle cx="{sx(obj["x"]):.1f}" cy="{sz(obj["z"]):.1f}" r="0.8" '
            f'class="objective"><title>{html.escape(obj["label"])}</title></circle>'
        )

    for model in frame["models"]:
        if model["reserve"]:
            continue
        cx, cy = sx(model["x"]), sz(model["z"])
        rx = max(model["bx"], 0.2)
        ry = max(model["bz"], 0.2)
        fill = TEAM_FILL.get(model["color"], "#888")
        edge = TEAM_EDGE.get(model["color"], "#bbb")
        wounds = ""
        if model["wounds"] is not None and model["max_wounds"]:
            wounds = f' ({model["wounds"]}/{model["max_wounds"]})'
        parts.append(
            f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.2f}" ry="{ry:.2f}" '
            f'fill="{fill}" stroke="{edge}" stroke-width="0.18" '
            f'transform="rotate({model["ry"]:.0f} {cx:.1f} {cy:.1f})">'
            f'<title>{html.escape(model["name"] + wounds)}</title></ellipse>'
        )
        if show_labels:
            parts.append(
                f'<text x="{cx:.1f}" y="{cy - ry - 0.7:.1f}" class="mlabel">'
                f'{html.escape(model["name"][:16])}</text>'
            )

    parts.append("</svg>")
    return "\n".join(parts)


def write_report(log, out_dir, show_labels=True):
    """Write snapshots.json + report.html into out_dir. Returns the html path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(log)
    (out_dir / "snapshots.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    html_path = out_dir / "report.html"
    html_path.write_text(render_html(report, show_labels=show_labels), encoding="utf-8")
    return html_path
