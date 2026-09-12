#!/usr/bin/env python3
"""Tests for the Lua-side battle log: recording, not rendering.

The renderer that turns a recorded log into a report -- battle_report.py and
everything it needs (map_terrain.py, the terrain cache, the HTML/JS viewer) --
lives in the separate lct-report-server repo, which is now a standalone
desktop app that opens a TTS save file directly and renders whichever game you
pick. This mod has no export button and no network code of any kind; what
stays here is everything that only source-greps TTSLUA/*.ttslua: the recording
API and the cross-script safety pattern the Lua side depends on.
"""

import re
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))  # allow sibling imports (compile, lua_strings)

ROOT = SCRIPT_DIR.parent
GLOBAL_LUA = ROOT / "TTSLUA" / "global.ttslua"
BATTLE_REWIND_LUA = ROOT / "TTSLUA" / "battle_rewind.ttslua"
START_MENU_LUA = ROOT / "TTSLUA" / "startMenu.ttslua"
TOOLS_LUA = ROOT / "TTSLUA" / "spawnGameTools.ttslua"


def combined_global_text():
    """global.ttslua + battle_rewind.ttslua, in the same order compile.py
    concatenates them before injecting Global's script -- use this for any
    check that shouldn't care which of the two files a symbol lives in."""
    return (GLOBAL_LUA.read_text(encoding="utf-8", errors="replace") + "\n\n"
            + BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace"))


class TestLuaWiring(unittest.TestCase):
    """Lock the Lua-side contracts the (now separate) renderer depends on."""

    def test_battle_log_is_persisted_in_global_on_save(self):
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        self.assertIn("svBattleLog = battleLog", text)
        self.assertIn("restoreBattleLog(loaded_data.svBattleLog)", text)

    def test_every_phase_transition_records_a_snapshot(self):
        text = START_MENU_LUA.read_text(encoding="utf-8")
        for fn in ("nextPhase", "jumpToPhase", "passTurn"):
            start = text.find(f"function {fn}(")
            self.assertNotEqual(start, -1, f"{fn} missing from startMenu.ttslua")
            body = text[start:start + 2000]
            self.assertIn("recordBattleSnapshot", body,
                          f"{fn} does not record a battle log snapshot")

    def test_start_game_resets_and_seeds_the_log(self):
        text = START_MENU_LUA.read_text(encoding="utf-8")
        self.assertIn('Global.call("battleStartGame")', text)

    def test_registration_permits_standing_in_for_an_empty_seat(self):
        text = BATTLE_REWIND_LUA.read_text(encoding="utf-8")
        start = text.find("function canRegisterFor(")
        self.assertNotEqual(start, -1)
        body = text[start:text.find("\nend", start)]
        # Solo play and an unoccupied seat must both be allowed, or one player
        # cannot register both armies.
        self.assertIn("battleSoloModeActive()", body)
        self.assertIn("p.seated", body)

    def test_solo_mode_uses_the_singles_flag_not_just_simulation(self):
        text = BATTLE_REWIND_LUA.read_text(encoding="utf-8")
        start = text.find("function battleSoloModeActive(")
        self.assertNotEqual(start, -1)
        body = text[start:text.find("\nend", start)]
        # `simulation` is reset on startGame's last line, so it alone cannot carry
        # solo play through a game.
        self.assertIn("singlesMode", body)

    def test_tool_buttons_forward_to_global(self):
        text = TOOLS_LUA.read_text(encoding="utf-8")
        for fn in ("registerArmyR", "registerArmyB", "captureSnapshot"):
            self.assertIn(f"function {fn}(", text)
        self.assertIn("battleRegisterFromSelection", text)
        self.assertIn("battleClearArmy", text)


class TestLuaWiringDeployment(unittest.TestCase):
    def test_type_is_compared_against_a_string_not_the_stdlib_table(self):
        # type() returns a string, so comparing it to the bare word `table` (the
        # stdlib table) is always true and would silently drop every deployment.
        src = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")
        body = src[src.index("function battleDeploymentSpec"):]
        body = body[:body.index("\nend")]
        self.assertIn('~= "table"', body)
        self.assertNotIn("~= table\n", body)


class TestNoServerCommunication(unittest.TestCase):
    """The mod was split from lct-report-server entirely: no export button, no
    WebRequest, no networking of any kind. The battle log rides out with the
    save; a standalone desktop app reads the save file directly to render it."""

    @classmethod
    def setUpClass(cls):
        cls.src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        cls.battle_rewind_src = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")
        cls.tools_src = TOOLS_LUA.read_text(encoding="utf-8", errors="replace")

    def test_no_web_request_anywhere_in_the_mod(self):
        self.assertNotIn("WebRequest", self.src)
        self.assertNotIn("WebRequest", self.battle_rewind_src)

    def test_no_export_button_or_report_wiring_left(self):
        for name in ("exportReport", "exportBattleReport", "battleReportEndpoints",
                     "battleProbeThenPost", "battlePostReport", "battleExportLadder",
                     "battleSetReportMode", "BATTLE_REPORT_URL", "BATTLE_REPORT_REMOTE_BASE",
                     "BATTLE_REPORT_TOKEN", "BATTLE_REPORT_MODE"):
            self.assertNotIn(name, self.src, f"{name} should have been removed with the export path")
            self.assertNotIn(name, self.battle_rewind_src, f"{name} should have been removed with the export path")
            self.assertNotIn(name, self.tools_src, f"{name} should have been removed with the export path")


class TestCrossScriptTableSafety(unittest.TestCase):
    """TTS raises "resources owned by different scripts" when one script reads a
    table that belongs to another script's state. Object.call returning a freshly
    built table is fine; returning a long-lived one (DeployZonesData[i]) is not.
    Every such read must therefore sit INSIDE its pcall, not after it."""

    def setUp(self):
        self.src = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")

    def _body(self, fn):
        body = self.src[self.src.index("function " + fn):]
        return body[:body.index("\nend")]

    def test_deployment_is_fetched_as_json_not_as_a_table(self):
        body = self._body("battleDeploymentSpec")
        self.assertIn('sm.call("selectedDeploymentJSON")', body)
        self.assertNotIn('sm.call("selectedDeployment")', body)

    def test_startmenu_exposes_the_json_accessor(self):
        menu = START_MENU_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("function selectedDeploymentJSON", menu)
        self.assertIn("JSON.encode", menu[menu.index("function selectedDeploymentJSON"):])

    def test_returned_tables_are_only_read_inside_a_pcall(self):
        # A pcall that wraps only the call still lets the field reads escape --
        # that is exactly how this spammed an error on every snapshot.
        for fn in ("battleDeploymentSpec", "battleScores", "battleBaseExtents"):
            body = self._body(fn)
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("--"):
                    continue
                self.assertNotIn("= pcall(function() return ", stripped,
                                 f"{fn} reads a returned value outside its pcall")

    def test_a_snapshot_survives_context_gathering_failing(self):
        body = self._body("recordBattleSnapshot")
        self.assertIn("pcall(battleEnsureGameContext)", body)

    def test_the_deployment_lookup_is_not_retried_every_snapshot(self):
        self.assertIn("_battleDeployTried", self._body("battleEnsureGameContext"))
        self.assertIn("_battleDeployTried = false", self._body("resetBattleLog"))


class TestLuaScoreAndContext(unittest.TestCase):
    def test_scores_do_not_go_through_playerSum(self):
        # Object.call passes its params table as the SINGLE argument, so
        # call("playerSum", {1}) hands playerSum the table {1} and it errors
        # indexing scores[pId]. getMatchSummary is the supported entry point.
        src = combined_global_text()
        # Comments explain the trap by name, so only real code is searched.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("--"))
        self.assertNotIn('call("playerSum"', code)
        self.assertIn('call("getMatchSummary")', code)

    def test_a_snapshot_backfills_missing_game_context(self):
        # battleStartGame only fires when someone presses Start Game with this
        # build loaded; joining a game in progress must still get a map name and
        # deployment, or the report has no header and an empty deployment map.
        src = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("function battleEnsureGameContext", src)
        body = src[src.index("function recordBattleSnapshot"):]
        body = body[:body.index("\nend")]
        # Called through pcall so gathering context can never lose a snapshot.
        self.assertIn("pcall(battleEnsureGameContext)", body)

    def test_backfill_never_overwrites_what_start_game_recorded(self):
        src = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")
        body = src[src.index("function battleEnsureGameContext"):]
        body = body[:body.index("\nend\n\nfunction")]
        for field in ("started", "map", "mapGuid", "deploy", "red", "blue"):
            self.assertIn(f"g.{field} == nil", body,
                          f"{field} is assigned without a nil guard")


class TestLuaStringLiterals(unittest.TestCase):
    """TTS reports an unterminated string only at runtime, and the affected object
    loses its entire script. luaparser accepts a raw newline inside a "..." literal,
    so it cannot be relied on as the guard -- this is the check that matters."""

    def test_no_ttslua_file_has_an_unterminated_string(self):
        import lua_strings
        failures = []
        for path in sorted((ROOT / "TTSLUA").rglob("*.ttslua")):
            for _, line_no, quote, snippet in lua_strings.check_file(path):
                failures.append(f"{path.name}:{line_no} {quote} {snippet}")
        self.assertEqual(failures, [], "unterminated Lua string literal(s): " + "; ".join(failures))

    def test_checker_catches_a_raw_newline_inside_a_string(self):
        import lua_strings
        broken = 'local lbl = ' + chr(34) + 'REGISTER ARMY' + chr(10) + 'LMB set' + chr(34)
        self.assertTrue(lua_strings.find_unterminated_strings(broken))

    def test_checker_tolerates_the_constructs_this_repo_actually_uses(self):
        import lua_strings
        ok = [
            'local lbl = ' + chr(34) + 'A' + chr(92) + 'nB' + chr(34),          # escaped newline
            '-- 9' + chr(34) + ' secondary deployment lines',                    # inch mark in a comment
            'local s = [[spans' + chr(10) + 'two lines]]',                       # long-bracket string
            'x(' + chr(34) + 'esc ' + chr(92) + chr(34) + 'q' + chr(92) + chr(34) + chr(34) + ')',
        ]
        for src in ok:
            self.assertEqual(lua_strings.find_unterminated_strings(src), [], src)


class TestSecondaryScan(unittest.TestCase):
    """The secondaries scan reads the slots the way the rest of the mod does."""

    def setUp(self):
        self.source = combined_global_text()
        start = self.source.index("function battleSecondaryState")
        self.body = self.source[start:self.source.index("\nend", start)]

    def block(self, header):
        """The body of a top-level table literal, header line included."""
        text = self.source[self.source.index(header):]
        return text[:text.index("\n}")]

    def test_it_uses_the_mods_own_slot_helper(self):
        self.assertIn("getCardsInSecondarySlot(zone", self.body)

    def test_it_does_not_read_the_zone_directly(self):
        # zone.getObjects() came back empty with all four slots visibly filled: a
        # card dropped onto a zone is not registered as inside it until it settles.
        code = "\n".join(ln for ln in self.body.splitlines()
                         if not ln.lstrip().startswith("--"))
        self.assertNotIn("zone.getObjects()", code)

    def test_the_helper_it_leans_on_still_exists(self):
        self.assertIn("function getCardsInSecondarySlot(zone", self.source)

    def test_it_scans_every_slot_a_player_can_fill(self):
        """A side holds eight secondaries, and the scan used to stop at two.

        It stopped at two because BATTLE_SECONDARY_ZONES spelled out a pair of
        GUIDs of its own instead of pointing at the slot lists that draw, sort,
        discard and the scoreboard all share -- so filling slots 3 to 8 changed
        the table and nothing else. Pinned as the wiring rather than as a count,
        since a count here would just be the same copy made twice over.
        """
        zones = self.block("BATTLE_SECONDARY_ZONES = {")
        self.assertIn("Red  = redSecondaryCardZone_GUIDs", zones)
        self.assertIn("Blue = blueSecondaryCardZone_GUIDs", zones)
        self.assertNotRegex(zones, r'"[0-9a-z]{6}"',
                            "zone GUIDs copied in here again instead of referenced")
        for name in ("redSecondaryCardZone_GUIDs", "blueSecondaryCardZone_GUIDs"):
            slots = re.findall(r'"[0-9a-z]{6}"', self.block(name + " = {"))
            self.assertEqual(len(slots), 8, name)

    def test_the_slot_lists_are_in_place_before_the_battle_log_takes_them(self):
        # Plain globals, assigned as the file loads: taking them earlier in the
        # file than they are defined would quietly leave the table holding nil.
        for name in ("redSecondaryCardZone_GUIDs = {", "blueSecondaryCardZone_GUIDs = {"):
            self.assertLess(self.source.index(name),
                            self.source.index("BATTLE_SECONDARY_ZONES = {"), name)

    def test_it_walks_the_table_once_for_all_sixteen_slots(self):
        # The helper's footprint sweep is a getAllObjects() pass per slot, which
        # was cheap over four slots and wasteful over sixteen.
        self.assertIn("local scene = getAllObjects()", self.body)
        self.assertIn("getCardsInSecondarySlot(zone, scene)", self.body)
        self.assertIn("ipairs(scene or getAllObjects())", self.source)


class TestBoardCapture(unittest.TestCase):
    """captureBoard records only what the report still reads: the fixed board
    size, plus the physical objective markers (terrain comes from the map's own
    shipped payload -- see map_terrain.py -- and nothing has read the rest of a
    full-zone capture since). A measured real game had 47 captured entries of
    which only the markers were ever used; this is what keeps the other ~41 from
    coming back."""

    def setUp(self):
        source = BATTLE_REWIND_LUA.read_text(encoding="utf-8", errors="replace")
        start = source.index("function captureBoard")
        self.body = source[start:source.index("\nend", source.index("Wait.frames", start))]

    def test_it_keeps_only_objective_named_objects(self):
        self.assertIn('string.find(string.lower(name), "objective"', self.body)

    def test_it_records_only_name_and_position(self):
        self.assertIn("{n = name, x = battleRound(p.x), z = battleRound(p.z)}", self.body)

    def test_it_no_longer_records_a_guid_tags_rotation_scale_or_description(self):
        # Each of these was a field on the old, full-zone capture; none of them
        # survive the trim, and none should quietly come back.
        for gone in ("g  = guid,", "t  = battleModelTags(o)", "o.getRotation()",
                     "o.getScale()", "o.getDescription()", "o.getBoundsNormalized()",
                     "BATTLE_BOARD_DESC_MAX"):
            self.assertNotIn(gone, self.body, gone)

    def test_it_no_longer_sniffs_the_mesh_for_a_plate_shape(self):
        # Terrain geometry comes from the map's shipped payload now, so the
        # capture has no reason to open getCustomObject(). Leaving the sniff in
        # would invite the old bounds-guessing path back.
        self.assertNotIn("5mm%-border", self.body)
        self.assertNotIn("sh = sh,", self.body)

    def test_battle_board_desc_max_is_gone_not_just_unused_here(self):
        self.assertNotIn("BATTLE_BOARD_DESC_MAX", combined_global_text())


if __name__ == "__main__":
    unittest.main()
