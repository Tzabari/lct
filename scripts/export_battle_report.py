#!/usr/bin/env python3
"""Render a battle report from an exported Tabletop Simulator save.

The in-game Battle Log records one snapshot per phase into Global's saved state
(see the BATTLE LOG section of TTSLUA/global.ttslua). Save the game in TTS, then
point this at the resulting save file:

    python3 scripts/export_battle_report.py ~/Documents/My\\ Games/Tabletop\\ Simulator/Saves/TS_Save_12.json

It writes report/report.html (per-phase top-down boards) and report/snapshots.json
(the full reconstruction, and the stable interface for any future in-game viewer).

This path always works and needs nothing running. The in-game EXPORT REPORT button
is the convenience alternative and uses battle_report_server.py instead -- that
script lives in the separate lct-report-server repo, not here.
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import battle_report as BR

ROOT = Path(__file__).parent.parent
DEFAULT_OUT = ROOT / "report"


def find_default_save():
    """Best-effort guess at the newest TTS save, for a friendlier error message."""
    candidates = [
        Path.home() / "Documents" / "My Games" / "Tabletop Simulator" / "Saves",
        Path.home() / "Library" / "Tabletop Simulator" / "Saves",
        Path.home() / ".local" / "share" / "Tabletop Simulator" / "Saves",
    ]
    for folder in candidates:
        if folder.is_dir():
            saves = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if saves:
                return saves[0]
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Render an LCT battle report from an exported TTS save."
    )
    parser.add_argument(
        "save", nargs="?", type=Path,
        help="Exported TTS save JSON. Defaults to the newest save in your TTS Saves folder.",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--no-labels", action="store_true",
        help="Start with model name labels hidden (they stay toggleable in the page).",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Do not open the finished report in a browser.",
    )
    args = parser.parse_args()

    save_path = args.save
    if save_path is None:
        save_path = find_default_save()
        if save_path is None:
            parser.error("no save given and no TTS Saves folder found; pass the save path explicitly")
        print(f"Using newest save: {save_path}")

    if not save_path.is_file():
        parser.error(f"save not found: {save_path}")

    try:
        save = json.loads(save_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: {save_path} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        log = BR.extract_log_from_save(save)
        html_path = BR.write_report(log, args.out, show_labels=not args.no_labels)
    except BR.BattleReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    frames = len(log["snaps"])
    models = len(log["roster"])
    print(f"Wrote {html_path} ({frames} snapshot(s), {models} registered model(s)).")
    print(f"Wrote {args.out / 'snapshots.json'}")

    if not args.no_open:
        webbrowser.open(html_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
