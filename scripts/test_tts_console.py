"""The TTS console's message decoding and compiled-line -> source mapping.

TTS itself is not running during a test, so the messages are the recorded shapes
its External Editor API sends and the mapping is exercised against the real
source tree.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tts_console  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LUA_DIR = ROOT / "TTSLUA"


class TestPositionExtraction(unittest.TestCase):
    """MoonSharp writes a position as chunk_N:(line,col) in both the syntax
    errors TTS reports on load and the runtime ones it reports mid-game."""

    def test_a_compile_error(self):
        line, col = tts_console.extract_position(
            "chunk_0:(36,4-8): unexpected symbol near 'deck'")
        self.assertEqual((line, col), (36, 4))

    def test_a_runtime_error(self):
        line, col = tts_console.extract_position(
            "Error in Script (Global) function <onLoad>: "
            "chunk_3:(8214,9): attempt to index a nil value")
        self.assertEqual((line, col), (8214, 9))

    def test_a_single_column_with_no_span(self):
        line, col = tts_console.extract_position("chunk_1:(7,2): oops")
        self.assertEqual((line, col), (7, 2))

    def test_a_message_with_no_position_at_all(self):
        line, _ = tts_console.extract_position("Lua Scripting Error, see host log")
        self.assertIsNone(line)


class TestSourceMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smap = tts_console.SourceMap()

    def test_object_guids_resolve_to_the_file_that_declares_them(self):
        """compile.py pairs a script to an object by the GUIDs on its first
        line, and injects it verbatim -- so the line number needs no mapping."""
        path, line = self.smap.resolve("be2cdb", 27)
        self.assertEqual(path, "TTSLUA/trashBin.ttslua")
        self.assertEqual(line, 27)

    def test_an_unknown_guid_maps_to_nothing_rather_than_guessing(self):
        path, _ = self.smap.resolve("zzzzzz", 10)
        self.assertIsNone(path)

    def test_a_missing_line_number_maps_to_nothing(self):
        path, line = self.smap.resolve("-1", None)
        self.assertIsNone(path)
        self.assertIsNone(line)

    @unittest.skipUnless(
        any((ROOT / "builds").glob("*.json")),
        "no build in builds/ to map compiled Global lines against")
    def test_a_compiled_global_line_maps_back_into_the_sources(self):
        """The whole point: TTS names a line in the assembled Global script,
        which exists in no file on disk. Take a line from the middle of the
        companion's text, find where it landed in the build, and check the map
        sends it home."""
        self.assertTrue(self.smap.compiled_global, "build has no Global LuaScript")
        needle = "function battleLogEncoded()"
        source_line = None
        companion = LUA_DIR / "battle_rewind.ttslua"
        for n, text in enumerate(companion.read_text(encoding="utf-8").splitlines(), 1):
            if text.strip() == needle:
                source_line = n
                break
        self.assertIsNotNone(source_line, f"{needle} is gone from battle_rewind.ttslua")

        compiled_line = None
        for n, text in enumerate(self.smap.compiled_global, 1):
            if text.strip() == needle:
                compiled_line = n
                break
        self.assertIsNotNone(compiled_line,
                             "build predates the source - rebuild before running this")
        self.assertNotEqual(compiled_line, source_line,
                            "the companion is concatenated after global.ttslua, so "
                            "its compiled line cannot equal its source line")

        path, mapped = self.smap.resolve("-1", compiled_line)
        self.assertEqual(path, "TTSLUA/battle_rewind.ttslua")
        self.assertEqual(mapped, source_line)

    @unittest.skipUnless(
        any((ROOT / "builds").glob("*.json")),
        "no build in builds/ to map compiled Global lines against")
    def test_a_line_that_is_not_itself_distinctive_still_maps(self):
        """Most failing lines are not unique text -- an `end`, a `return`. The
        map walks back to the nearest line that is, then adds the distance, so
        it has to land on the right one and not merely on the anchor."""
        needle = "function battleLogTouch()"
        compiled_line = None
        for n, text in enumerate(self.smap.compiled_global, 1):
            if text.strip() == needle:
                compiled_line = n
                break
        self.assertIsNotNone(compiled_line, "rebuild before running this")

        path, mapped = self.smap.resolve("-1", compiled_line + 1)
        self.assertEqual(path, "TTSLUA/battle_rewind.ttslua")
        src = (LUA_DIR / "battle_rewind.ttslua").read_text(encoding="utf-8").splitlines()
        self.assertEqual(src[mapped - 2].strip(), needle)

    def test_a_line_past_the_end_of_the_script_maps_to_nothing(self):
        path, _ = self.smap.resolve("-1", 10 ** 9)
        self.assertIsNone(path)


class TestFormatting(unittest.TestCase):
    """Error lines must come out as file:line:col: severity: message, because
    that is what the .vscode problem matcher and VSCode's own terminal link
    detection both read."""

    @classmethod
    def setUpClass(cls):
        cls.smap = tts_console.SourceMap()

    def test_an_object_error_names_its_source_file_and_line(self):
        out = tts_console.format_message({
            "messageID": 3,
            "error": "chunk_0:(27,5-20): attempt to index a nil value",
            "guid": "be2cdb",
            "errorMessagePrefix": "Error in Script (trash bin): ",
        }, self.smap)
        joined = "\n".join(out)
        self.assertIn("TTSLUA/trashBin.ttslua:27:5: error:", joined)
        self.assertIn("attempt to index a nil value", joined)

    def test_the_position_is_not_printed_twice(self):
        out = "\n".join(tts_console.format_message({
            "messageID": 3,
            "error": "chunk_0:(27,5-20): attempt to index a nil value",
            "guid": "be2cdb",
        }, self.smap))
        self.assertNotIn("chunk_0", out)

    def test_an_unmappable_error_still_reports_the_compiled_line(self):
        """Better a line number in the build than nothing at all -- this is what
        a stale build or an error inside the baked map index looks like."""
        out = "\n".join(tts_console.format_message({
            "messageID": 3,
            "error": "chunk_0:(999999,1): something broke",
            "guid": "-1",
        }, self.smap))
        self.assertIn("999999", out)
        self.assertIn("something broke", out)

    def test_a_print_is_relayed_as_written(self):
        out = "\n".join(tts_console.format_message(
            {"messageID": 2, "message": "battle log: 12 snapshot(s)"}, self.smap))
        self.assertIn("battle log: 12 snapshot(s)", out)

    def test_every_message_id_tts_sends_produces_a_line(self):
        for mid in range(0, 8):
            out = tts_console.format_message({"messageID": mid}, self.smap)
            self.assertTrue(out, f"messageID {mid} produced nothing")
            self.assertTrue(all(isinstance(x, str) for x in out))

    def test_an_unrecognised_message_id_does_not_raise(self):
        out = tts_console.format_message({"messageID": 42}, self.smap)
        self.assertTrue(out)


class TestOutgoing(unittest.TestCase):
    def test_execute_lua_sends_the_executeluacode_message(self):
        sent = {}

        def fake_send(payload, timeout=4.0):
            sent.update(payload)

        original = tts_console.send_to_tts
        tts_console.send_to_tts = fake_send
        try:
            tts_console.execute_lua("print(1)")
        finally:
            tts_console.send_to_tts = original
        # 3 is executeLuaCode on the way IN; errorMessage also happens to be 3
        # on the way out. The directions number their messages separately.
        self.assertEqual(sent["messageID"], 3)
        self.assertEqual(sent["guid"], "-1")
        self.assertEqual(sent["script"], "print(1)")
        self.assertEqual(json.loads(json.dumps(sent))["script"], "print(1)")


if __name__ == "__main__":
    unittest.main()
