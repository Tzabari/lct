#!/usr/bin/env python3
"""Detect unterminated short string literals in .ttslua sources.

TTS reports these only at runtime, as "unfinished string near ...", and the
object's whole script fails to load. Generic Lua parsers are not a reliable
guard: some accept a raw newline inside a "..." literal, which MoonSharp does
not. This scanner is deliberately narrow and checks exactly that one thing.

It understands the constructs that would otherwise cause false positives:
line comments, [[ ]] long-bracket comments and strings (which may legally span
lines), escapes, and both quote styles.

Usage:
    python3 scripts/lua_strings.py [path ...]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
LUA_DIR = ROOT / "TTSLUA"


def _long_bracket_at(text, i):
    """If a long bracket opens at i, return (level, index after it), else None."""
    if text[i] != "[":
        return None
    j = i + 1
    level = 0
    while j < len(text) and text[j] == "=":
        level += 1
        j += 1
    if j < len(text) and text[j] == "[":
        return level, j + 1
    return None


def find_unterminated_strings(text):
    """Return [(line_no, quote_char, snippet), ...] for each unclosed literal."""
    problems = []
    i = 0
    line = 1
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == "\n":
            line += 1
            i += 1
            continue

        # Comments: line, or long-bracket block.
        if text.startswith("--", i):
            lb = _long_bracket_at(text, i + 2)
            if lb:
                level, start = lb
                close = "]" + "=" * level + "]"
                end = text.find(close, start)
                if end == -1:
                    i = n
                else:
                    line += text.count("\n", i, end)
                    i = end + len(close)
            else:
                end = text.find("\n", i)
                i = n if end == -1 else end
            continue

        # Long-bracket string: may legally span lines.
        lb = _long_bracket_at(text, i)
        if lb:
            level, start = lb
            close = "]" + "=" * level + "]"
            end = text.find(close, start)
            if end == -1:
                i = n
            else:
                line += text.count("\n", i, end)
                i = end + len(close)
            continue

        # Short string: must close on the same line.
        if ch in ('"', "'"):
            start_line = line
            j = i + 1
            closed = False
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n":
                    break
                if c == ch:
                    closed = True
                    j += 1
                    break
                j += 1
            if not closed:
                snippet = text[i:text.find("\n", i) if text.find("\n", i) != -1 else n]
                problems.append((start_line, ch, snippet.strip()[:80]))
                # Resume after the line so one bad literal does not cascade.
                nl = text.find("\n", i)
                if nl == -1:
                    break
                i = nl
                continue
            i = j
            continue

        i += 1

    return problems


def check_file(path):
    problems = find_unterminated_strings(path.read_text(encoding="utf-8"))
    return [(path, ln, q, snip) for ln, q, snip in problems]


def main(argv):
    targets = [Path(a) for a in argv[1:]] or sorted(LUA_DIR.rglob("*.ttslua"))
    failures = []
    for path in targets:
        failures.extend(check_file(path))

    for path, line, quote, snippet in failures:
        rel = path.relative_to(ROOT) if ROOT in path.parents else path
        print(f"{rel}:{line}: unterminated {quote} string -> {snippet}", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} unterminated string literal(s).", file=sys.stderr)
        return 1
    print(f"{len(targets)} file(s) checked, no unterminated strings.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
