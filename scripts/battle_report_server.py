#!/usr/bin/env python3
"""Local helper for the in-game EXPORT REPORT button.

Tabletop Simulator's Lua sandbox cannot write files or launch programs; the only
outbound channel it has is WebRequest. So the in-game button POSTs the battle log
to this tiny loopback server, which renders the report and opens it in a browser.

    python3 scripts/battle_report_server.py

Leave it running while you play, then press EXPORT REPORT on the spawn-game-tools
object. If it is not running the in-game button fails softly and tells you to use
export_battle_report.py on a saved game instead -- that path needs no server.

Binds to 127.0.0.1 only: this accepts a battle log and writes files, so it is not
something to expose beyond the local machine.
"""

import argparse
import importlib
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import battle_report as BR


def renderer():
    """Return battle_report, re-imported so edits land without a restart.

    This server is meant to be left running for a whole game -- and, while the
    report is being worked on, across edits to the renderer. A module imported
    once at startup would keep serving the version that was on disk when the
    server booted, which looks exactly like the report "not updating".

    The reloaded module object is returned rather than read from the global so
    that a caller uses one consistent module for both the call and its `except`
    clause: reloading rebuilds BattleReportError, and the old class would no
    longer catch the new one.
    """
    global BR
    try:
        BR = importlib.reload(BR)
    except Exception as exc:  # noqa: BLE001 - a broken edit must not kill the server
        print(f"  warning: reload failed ({exc}); serving the previously loaded copy")
    return BR


ROOT = Path(__file__).parent.parent
DEFAULT_OUT = ROOT / "report"
DEFAULT_PORT = 8787

# Matches BATTLE_REPORT_URL in TTSLUA/global.ttslua.
REPORT_PATH = "/report"

# A two-army five-round log is a few hundred KB; this is generous headroom while
# still refusing anything absurd.
MAX_BODY_BYTES = 32 * 1024 * 1024


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "LCTBattleReport/1.0"
    out_dir = DEFAULT_OUT
    open_browser = True

    def _respond(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A liveness probe, so the button (or a human) can check the server is up.
        if self.path.rstrip("/") in ("", REPORT_PATH):
            self._respond(200, {"ok": True, "service": "lct-battle-report"})
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != REPORT_PATH:
            self._respond(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"ok": False, "error": "bad Content-Length"})
            return
        if length <= 0:
            self._respond(400, {"ok": False, "error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._respond(413, {"ok": False, "error": "battle log too large"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._respond(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        br = renderer()
        try:
            log = br.load_log(payload)
            html_path = br.write_report(log, self.out_dir)
        except br.BattleReportError as exc:
            print(f"  rejected: {exc}")
            self._respond(422, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - never take the server down mid-game
            print(f"  failed: {exc}")
            self._respond(500, {"ok": False, "error": str(exc)})
            return

        print(f"  wrote {html_path} ({len(log['snaps'])} snapshot(s))")
        if self.open_browser:
            webbrowser.open(html_path.resolve().as_uri())
        self._respond(200, {"ok": True, "snapshots": len(log["snaps"]), "path": str(html_path)})

    def log_message(self, fmt, *args):
        print(f"[battle-report] {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="Local battle report renderer for LCT.")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT}).")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output directory (default: {DEFAULT_OUT}).")
    parser.add_argument("--no-open", action="store_true",
                        help="Write the report but do not open a browser.")
    args = parser.parse_args()

    ReportHandler.out_dir = args.out
    ReportHandler.open_browser = not args.no_open

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReportHandler)
    print(f"LCT battle report server listening on http://127.0.0.1:{args.port}{REPORT_PATH}")
    print(f"Reports will be written to {args.out}")
    print("Press EXPORT REPORT in TTS. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
