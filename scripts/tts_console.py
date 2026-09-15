#!/usr/bin/env python3
"""TTS's errors and prints, in the terminal, pointing at the source file.

Tabletop Simulator ships an External Editor API: it listens on 39999 for
commands and pushes every print(), log() and script error to whatever is
listening on 39998. That is what the Atom plugin (and the VSCode
"Tabletop Simulator Lua" extension) hook into. This is the same connection
without the rest of the plugin, because the rest of the plugin wants to own the
script files -- it pulls every object script into its own workspace folder and
pushes them back with Save & Play, which is compile.py's job here.

What it adds on top of just relaying the message is the part that actually
matters in this repo: a location. TTS reports an error against the *compiled*
Global script -- one file of ~10k lines that compile.py assembles from
global.ttslua plus its companions, with the version stamp and map index baked in
-- so "chunk_3:(8214,9)" names a line that exists in no file on disk. This maps
it back, and prints

    TTSLUA/battle_rewind.ttslua:412:9: error: attempt to index a nil value

which VSCode's terminal turns into a clickable link, and which the $lct-tts
problem matcher in .vscode/tasks.json turns into a Problems-panel entry.

    python scripts/tts_console.py                  # listen (ctrl-c to stop)
    python scripts/tts_console.py -e "print(#getAllObjects())"
    python scripts/tts_console.py --raw            # also dump the JSON verbatim

Only one process can hold port 39998, so this and the VSCode extension's
console cannot both run at once.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile as ttscompile  # noqa: E402  (constants + file collection live there)
import term  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# TTS's own ports, fixed by the game. We listen on one and talk on the other.
PORT_FROM_TTS = 39998
PORT_TO_TTS = 39999

# Message IDs TTS sends us (the ones it sends back are in send_to_tts).
MSG_PUSHING_NEW_OBJECT = 0
MSG_LOADING_NEW_GAME = 1
MSG_PRINT = 2
MSG_ERROR = 3
MSG_CUSTOM = 4
MSG_RETURN_VALUE = 5
MSG_GAME_SAVED = 6
MSG_OBJECT_CREATED = 7

# The Global script's stand-in GUID in every message TTS sends.
GLOBAL_GUID = "-1"

# "chunk_3:(8214,9-31)" -- how MoonSharp names a position, in both compile-time
# and runtime errors. The column may be a single number or a span. The trailing
# ": " is part of what gets cut out, so removing the position from a message like
# "function <onLoad>: chunk_3:(8,9): attempt to index" does not leave "... : :".
RE_CHUNK_POS = re.compile(r"chunk_\d+:\((\d+),(\d+)(?:-\d+)?\):?\s*")
# Fallback for anything else that looks like a line reference.
RE_BARE_LINE = re.compile(r":(\d+):")

# An anchor shorter than this is too likely to appear in two files ("end", "});").
MIN_ANCHOR_LEN = 12
# How far back from the failing line to look for one. Past this the answer would
# be too vague to be worth printing.
MAX_ANCHOR_LOOKBACK = 400


class SourceMap:
    """Turns (guid, compiled line) into (repo-relative path, source line).

    Object scripts are injected verbatim, so their line numbers already match
    the file on disk. Only Global needs real work, and it gets it by matching
    line *text*: walk back from the failing line to the nearest line that
    appears exactly once across global.ttslua and its companions, then add the
    distance back. That costs nothing to maintain -- it does not need to know
    that stamp_global rewrites the version block or that bake_map_index expands
    a table, only that neither of them rewrites the line it lands on.
    """

    def __init__(self):
        self.by_guid: dict[str, Path] = {}
        for path in ttscompile.collect_lua_files()[1:]:
            try:
                first = path.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                continue
            for guid in ttscompile.REGEX_LUA_GUID.findall(first):
                self.by_guid[guid] = path

        # stripped line text -> [(path, line no), ...], over the Global sources
        self.anchors: dict[str, list[tuple[Path, int]]] = {}
        global_sources = [ttscompile.PATH_LUA / ttscompile.GLOBAL_LUA] + \
            ttscompile.collect_global_companion_files()
        for path in global_sources:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for n, text in enumerate(lines, 1):
                key = text.strip()
                if len(key) < MIN_ANCHOR_LEN:
                    continue
                self.anchors.setdefault(key, []).append((path, n))

        self.compiled_global: list[str] = self._load_compiled_global()

    @staticmethod
    def _load_compiled_global() -> list[str]:
        """The Global script as the running build has it, from the newest build.

        A build older than the source it was made from maps to the wrong lines,
        which is exactly as wrong as the game being older than the source -- and
        the game is running that same build, so the two stay in step.
        """
        builds = sorted(ttscompile.PATH_BUILDS.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        for build in builds:
            try:
                data = json.loads(build.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            script = data.get("LuaScript")
            if isinstance(script, str) and script:
                return script.splitlines()
        return []

    def resolve(self, guid: str, line: int | None):
        """(relative path, line) for a location, or (None, None) if unmappable."""
        if line is None:
            return None, None
        if guid and guid != GLOBAL_GUID:
            path = self.by_guid.get(guid)
            return (self._rel(path), line) if path else (None, None)

        comp = self.compiled_global
        if not comp or not 1 <= line <= len(comp):
            return None, None
        for back in range(min(MAX_ANCHOR_LOOKBACK, line)):
            hits = self.anchors.get(comp[line - 1 - back].strip())
            if hits is not None and len(hits) == 1:
                path, src_line = hits[0]
                return self._rel(path), src_line + back
        return None, None

    @staticmethod
    def _rel(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:
            return path.as_posix()


def extract_position(text: str) -> tuple[int | None, int]:
    """(line, column) out of an error message, column defaulting to 1."""
    m = RE_CHUNK_POS.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = RE_BARE_LINE.search(text)
    if m:
        return int(m.group(1)), 1
    return None, 1


def format_message(payload: dict, smap: SourceMap) -> list[str]:
    """The lines to print for one message from TTS."""
    mid = payload.get("messageID")
    stamp = term.dim(time.strftime("%H:%M:%S"))

    if mid == MSG_ERROR:
        raw = str(payload.get("error", "")).strip()
        guid = str(payload.get("guid", GLOBAL_GUID))
        prefix = str(payload.get("errorMessagePrefix", "")).strip()
        line, col = extract_position(raw)
        path, src_line = smap.resolve(guid, line)
        who = "Global" if guid == GLOBAL_GUID else guid
        # Everything after the position is the message itself; the position is
        # about to be restated in a form the editor can use.
        body = RE_CHUNK_POS.sub("", raw).lstrip(": ").strip() or raw
        if path is not None:
            head = f"{path}:{src_line}:{col}: error: {body}"
        elif line is not None:
            head = f"compiled Global line {line}: error: {body}"
        else:
            head = f"error: {body}"
        out = [f"{stamp} {term.red(head)}  {term.dim('[' + who + ']')}"]
        if prefix and prefix not in raw:
            out.append(f"         {term.dim(prefix)}")
        if path is None and line is not None:
            out.append(f"         {term.dim('no source line for this - rebuild, then reproduce')}")
        return out

    if mid == MSG_PRINT:
        return [f"{stamp} {payload.get('message', '')}"]
    if mid == MSG_CUSTOM:
        return [f"{stamp} {term.cyan('custom')} {json.dumps(payload.get('customMessage'))}"]
    if mid == MSG_RETURN_VALUE:
        return [f"{stamp} {term.green('->')} {json.dumps(payload.get('returnValue'))}"]
    if mid == MSG_GAME_SAVED:
        return [f"{stamp} {term.dim('game saved')}"]
    if mid == MSG_LOADING_NEW_GAME:
        return [f"{stamp} {term.cyan('loading a new game')}"]
    if mid == MSG_PUSHING_NEW_OBJECT:
        return [f"{stamp} {term.dim('script pushed from TTS')}"]
    if mid == MSG_OBJECT_CREATED:
        return [f"{stamp} {term.dim('object created: ' + str(payload.get('guid')))}"]
    return [f"{stamp} {term.dim('unknown message ' + str(mid))}"]


def send_to_tts(payload: dict, timeout: float = 4.0) -> None:
    """One command to TTS. Raises OSError if the game is not listening."""
    with socket.create_connection(("127.0.0.1", PORT_TO_TTS), timeout=timeout) as s:
        s.sendall(json.dumps(payload).encode("utf-8"))


def execute_lua(code: str, guid: str = GLOBAL_GUID) -> None:
    # messageID 3 is executeLuaCode on the way in -- not to be confused with
    # messageID 3 (errorMessage) on the way out; the two directions number
    # their messages separately.
    send_to_tts({"messageID": 3, "guid": guid, "script": code})


def listen(smap: SourceMap, raw: bool) -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", PORT_FROM_TTS))
    except OSError as exc:
        print(term.red(f"Cannot listen on {PORT_FROM_TTS}: {exc}"))
        print("Something else already holds it - most likely the VSCode "
              "\"Tabletop Simulator Lua\" extension's console, which uses the "
              "same port. Close that and try again.")
        return 1
    server.listen(8)

    print(term.bold(f"Listening for Tabletop Simulator on {PORT_FROM_TTS}."))
    if not smap.compiled_global:
        print(term.yellow("No build found in builds/, so Global errors can only "
                          "be reported as compiled line numbers. Run "
                          "scripts/compile.py first."))
    print(term.dim("Start TTS and load the build. Ctrl-C to stop.\n"))

    try:
        while True:
            conn, _ = server.accept()
            # TTS opens a connection per message and closes it when done, so the
            # message is simply everything up to EOF.
            chunks = []
            with conn:
                conn.settimeout(5.0)
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                except OSError:
                    pass
            body = b"".join(chunks).decode("utf-8", errors="replace").strip()
            if not body:
                continue
            if raw:
                print(term.dim(body))
            try:
                payload = json.loads(body)
            except ValueError:
                print(term.yellow(f"Unparseable message from TTS: {body[:400]}"))
                continue
            for out in format_message(payload, smap):
                print(out)
            sys.stdout.flush()
    except KeyboardInterrupt:
        print(term.dim("\nStopped."))
    finally:
        server.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Relay Tabletop Simulator's prints and errors into this terminal, "
                    "with error locations mapped back to the .ttslua sources.")
    ap.add_argument("-e", "--exec", dest="code", metavar="LUA",
                    help="run this Lua in TTS and exit (the result comes back as a "
                         "print/return message, so pair it with a running console)")
    ap.add_argument("--guid", default=GLOBAL_GUID,
                    help="object GUID for --exec (default: Global)")
    ap.add_argument("--raw", action="store_true",
                    help="also print each message's JSON verbatim")
    args = ap.parse_args(argv)

    if args.code:
        try:
            execute_lua(args.code, args.guid)
        except OSError as exc:
            print(term.red(f"Could not reach TTS on {PORT_TO_TTS}: {exc}"))
            print("Is Tabletop Simulator running with a game loaded?")
            return 1
        print(term.green("Sent."))
        return 0

    return listen(SourceMap(), args.raw)


if __name__ == "__main__":
    sys.exit(main())
