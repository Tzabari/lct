#!/usr/bin/env python3
"""Behavioural tests for the battle report pipeline.

These lock the runtime contracts the Lua side depends on: the ForceOrg nickname
format, casualty accumulation, reserve detection from the board rectangle, and
tolerance of logs that are partly malformed (a report should still render).
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import battle_report as BR

ROOT = SCRIPT_DIR.parent
GLOBAL_LUA = ROOT / "TTSLUA" / "global.ttslua"
START_MENU_LUA = ROOT / "TTSLUA" / "startMenu.ttslua"
TOOLS_LUA = ROOT / "TTSLUA" / "spawnGameTools.ttslua"


def make_log(**overrides):
    """A minimal but complete two-model, two-snapshot log."""
    log = {
        "v": 1,
        "game": {
            "started": 1788013636,
            "map": "Sweeping Engagement",
            "red": "RedPlayer",
            "blue": "BluePlayer",
            "board": {"w": 60.0, "h": 44.0},
        },
        "board": [
            {"g": "t00001", "n": "", "t": ["Tower"], "x": 0.0, "z": 0.0,
             "ry": 0, "sx": 1, "sz": 1, "bx": 2.0, "bz": 2.0},
            {"g": "t00002", "n": "", "t": ["obj_center1"], "x": 10.0, "z": 4.0,
             "ry": 0, "sx": 1, "sz": 1, "bx": 1.0, "bz": 1.0},
            {"g": "t00003", "n": "", "t": ["battlemaster_battlemat"], "x": 0.0, "z": 0.0,
             "ry": 0, "sx": 1, "sz": 1, "bx": 30.0, "bz": 22.0},
        ],
        "units": {
            "u-red-1": {"n": "Intercessor Squad", "c": "Red",
                        "d": "[00ff16]Intercessor Sergeant[-]\n6\" 4 3+ 2 6 2\n"
                             "[dc61ed]Abilities[-]\nOath of Moment"},
            "u-blue-1": {"n": "Hormagaunts", "c": "Blue", "d": ""},
        },
        "roster": {
            "aaa111": {"c": "Red", "u": "u-red-1", "n": "Intercessor Sergeant",
                       "w": 2, "b": [0.63, 0.63], "tags": ["uuid:u-red-1", "leaderModel"]},
            "bbb222": {"c": "Blue", "u": "u-blue-1", "n": "Hormagaunt",
                       "w": 1, "b": [0.5, 0.5], "tags": ["uuid:u-blue-1"]},
        },
        "dead": {},
        "snaps": [
            {"r": 1, "t": "Red", "p": 1, "lbl": "Round 1, Red Turn 1 - Command Phase",
             "why": "deploy", "ts": 1, "vp": {"r": 0, "b": 0}, "cp": {"r": 1, "b": 1},
             "m": {"aaa111": [-10.0, -5.0, 90, 2], "bbb222": [12.0, 6.0, 270, 1]},
             "dead": []},
            {"r": 1, "t": "Red", "p": 2, "lbl": "Round 1, Red Turn 1 - Movement Phase",
             "why": "phase", "ts": 2, "vp": {"r": 5, "b": 0}, "cp": {"r": 2, "b": 1},
             "m": {"aaa111": [-4.0, -2.0, 90, 1]},
             "dead": ["bbb222"]},
        ],
    }
    log.update(overrides)
    return log


class TestNameParsing(unittest.TestCase):
    def test_forceorg_wound_prefix_is_split_out(self):
        name, cur, mx = BR.parse_model_name("[00ff16]2/3[-] Intercessor Sergeant")
        self.assertEqual(name, "Intercessor Sergeant")
        self.assertEqual((cur, mx), (2, 3))

    def test_damaged_model_keeps_current_and_max_separate(self):
        _, cur, mx = BR.parse_model_name("[ff0000]1/6[-] Redemptor Dreadnought")
        self.assertEqual((cur, mx), (1, 6))

    def test_non_forceorg_model_degrades_to_plain_name(self):
        name, cur, mx = BR.parse_model_name("Some Terrain Piece")
        self.assertEqual(name, "Some Terrain Piece")
        self.assertIsNone(cur)
        self.assertIsNone(mx)

    def test_colour_markup_without_wounds_is_stripped(self):
        name, cur, _ = BR.parse_model_name("[abcdef]Objective Marker[-]")
        self.assertEqual(name, "Objective Marker")
        self.assertIsNone(cur)

    def test_empty_name_is_safe(self):
        self.assertEqual(BR.parse_model_name(""), ("", None, None))
        self.assertEqual(BR.parse_model_name(None), ("", None, None))


class TestDatasheetParsing(unittest.TestCase):
    def test_statline_is_read_from_the_second_line(self):
        profile = BR.parse_datasheet(
            "[00ff16]Intercessor Sergeant[-]\n6\" 4 3+ 2 6 2\n[dc61ed]Abilities[-]\nOath of Moment"
        )
        self.assertEqual(profile["t"], "4")
        self.assertEqual(profile["sv"], "3+")
        self.assertEqual(profile["w"], "2")

    def test_sections_are_captured(self):
        profile = BR.parse_datasheet(
            "[00ff16]Name[-]\n6\" 4 3+ 2 6 2\n[dc61ed]Abilities[-]\nOath of Moment"
        )
        self.assertIn("abilities", profile["sections"])
        self.assertEqual(profile["sections"]["abilities"], ["Oath of Moment"])

    def test_empty_description_returns_empty_profile(self):
        self.assertEqual(BR.parse_datasheet(""), {})
        self.assertEqual(BR.parse_datasheet(None), {})


class TestSchema(unittest.TestCase):
    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(BR.BattleReportError):
            BR.load_log(make_log(v=99))

    def test_log_without_snapshots_is_rejected(self):
        with self.assertRaises(BR.BattleReportError):
            BR.load_log(make_log(snaps=[]))

    def test_non_object_is_rejected(self):
        with self.assertRaises(BR.BattleReportError):
            BR.load_log([1, 2, 3])

    def test_save_without_battle_log_gives_a_clear_error(self):
        save = {"LuaScriptState": json.dumps({"svredPlayerID": "1"})}
        with self.assertRaises(BR.BattleReportError) as ctx:
            BR.extract_log_from_save(save)
        self.assertIn("svBattleLog", str(ctx.exception))

    def test_save_round_trip(self):
        save = {"LuaScriptState": json.dumps({"svBattleLog": make_log()})}
        log = BR.extract_log_from_save(save)
        self.assertEqual(len(log["snaps"]), 2)


class TestFrames(unittest.TestCase):
    def setUp(self):
        self.report = BR.build_report(BR.load_log(make_log()))
        self.frames = self.report["frames"]

    def test_one_frame_per_snapshot(self):
        self.assertEqual(len(self.frames), 2)

    def test_casualties_accumulate_and_persist(self):
        self.assertEqual(self.frames[0]["casualties"], [])
        self.assertEqual([c["guid"] for c in self.frames[1]["casualties"]], ["bbb222"])

    def test_dead_model_is_absent_from_later_model_lists(self):
        guids = {m["guid"] for m in self.frames[1]["models"]}
        self.assertNotIn("bbb222", guids)

    def test_wounds_are_carried_per_frame(self):
        red_first = next(m for m in self.frames[0]["models"] if m["guid"] == "aaa111")
        red_second = next(m for m in self.frames[1]["models"] if m["guid"] == "aaa111")
        self.assertEqual(red_first["wounds"], 2)
        self.assertEqual(red_second["wounds"], 1)
        self.assertEqual(red_second["max_wounds"], 2)

    def test_base_extents_come_from_the_roster(self):
        red = next(m for m in self.frames[0]["models"] if m["guid"] == "aaa111")
        self.assertAlmostEqual(red["bx"], 0.63)

    def test_models_inside_the_board_are_not_in_reserve(self):
        self.assertFalse(any(m["reserve"] for m in self.frames[0]["models"]))

    def test_models_parked_off_board_are_flagged_as_reserve(self):
        log = make_log()
        log["snaps"][0]["m"]["aaa111"] = [0.0, 74.25, 0, 2]
        frames = BR.build_frames(BR.load_log(log))
        red = next(m for m in frames[0]["models"] if m["guid"] == "aaa111")
        self.assertTrue(red["reserve"])

    def test_vp_and_cp_context_is_preserved(self):
        self.assertEqual(self.frames[1]["vp"], {"r": 5, "b": 0})
        self.assertEqual(self.frames[1]["cp"], {"r": 2, "b": 1})


class TestReservesAndSecondaries(unittest.TestCase):
    def test_reserve_board_occupancy_is_reported_per_side(self):
        log = make_log()
        log["snaps"][0]["res"] = {
            "Red":  {"g": "fe7926", "x": 0.7, "z": 84.0, "models": ["aaa111"]},
            "Blue": {"g": "85f0cf", "x": 1.6, "z": -83.3, "models": []},
        }
        frame = BR.build_frames(BR.load_log(log))[0]
        self.assertEqual(frame["reserves"]["Red"]["count"], 1)
        self.assertEqual(frame["reserves"]["Red"]["models"][0]["name"],
                         "Intercessor Sergeant")
        self.assertEqual(frame["reserves"]["Blue"]["count"], 0)

    def test_missing_reserve_data_is_simply_absent(self):
        frame = BR.build_frames(BR.load_log(make_log()))[0]
        self.assertEqual(frame["reserves"], {})

    def test_active_secondaries_are_reported_when_lct_tracks_them(self):
        log = make_log()
        log["snaps"][0]["sec"] = {
            "Red": [{"n": "Behind Enemy Lines", "fd": False},
                    {"n": "Cleanse", "fd": True}],
        }
        frame = BR.build_frames(BR.load_log(log))[0]
        self.assertEqual(len(frame["secondaries"]["Red"]), 2)
        self.assertTrue(frame["secondaries"]["Red"][1]["face_down"])
        self.assertNotIn("Blue", frame["secondaries"])

    def test_untracked_secondaries_yield_nothing_rather_than_a_guess(self):
        frame = BR.build_frames(BR.load_log(make_log()))[0]
        self.assertEqual(frame["secondaries"], {})

    def test_deployment_spec_is_exposed_for_the_overlay(self):
        log = make_log()
        log["game"]["deploy"] = {
            "name": "Hammer and Anvil",
            "draw": [{"type": "line", "color": "Red", "position": "x", "fromSide": 18}],
        }
        report = BR.build_report(BR.load_log(log))
        self.assertEqual(report["deployment"]["name"], "Hammer and Anvil")
        self.assertEqual(report["deployment"]["draw"][0]["fromSide"], 18)

    def test_absent_deployment_spec_is_none(self):
        self.assertIsNone(BR.build_report(BR.load_log(make_log()))["deployment"])


class TestTolerance(unittest.TestCase):
    def test_model_row_without_wounds_is_accepted(self):
        log = make_log()
        log["snaps"][0]["m"]["aaa111"] = [1.0, 2.0, 90]
        frames = BR.build_frames(BR.load_log(log))
        red = next(m for m in frames[0]["models"] if m["guid"] == "aaa111")
        self.assertIsNone(red["wounds"])

    def test_malformed_model_row_is_skipped_not_fatal(self):
        log = make_log()
        log["snaps"][0]["m"]["ccc333"] = [1.0]
        frames = BR.build_frames(BR.load_log(log))
        self.assertNotIn("ccc333", {m["guid"] for m in frames[0]["models"]})

    def test_model_missing_from_the_roster_still_renders(self):
        log = make_log()
        log["snaps"][0]["m"]["zzz999"] = [1.0, 2.0, 0, 1]
        frames = BR.build_frames(BR.load_log(log))
        stray = next(m for m in frames[0]["models"] if m["guid"] == "zzz999")
        self.assertEqual(stray["name"], "zzz999")

    def test_missing_board_size_falls_back_to_strike_force(self):
        log = make_log()
        log["game"].pop("board")
        self.assertEqual(BR.board_size(log), (60.0, 44.0))


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.log = BR.load_log(make_log())
        self.report = BR.build_report(self.log)

    def test_page_shows_one_board_and_carries_every_frame_as_data(self):
        # The viewer renders a single board client-side and switches frames, so
        # there is exactly one <svg> host regardless of how long the game ran.
        page = BR.render_html(self.report)
        self.assertEqual(page.count("<svg"), 1)
        self.assertEqual(len(self._payload(page)["frames"]), 2)

    def test_round_and_phase_navigation_is_present(self):
        page = BR.render_html(self.report)
        for hook in ('id="rounds"', 'id="phases"', 'id="next"', 'id="prev"'):
            self.assertIn(hook, page)

    def test_overlay_toggles_cover_the_in_game_ones(self):
        # Assert on the toggle ids, not their button captions: the captions are
        # cosmetic and get shortened to keep the nav on one line.
        page = BR.render_html(self.report)
        for toggle in ("terrain", "deploy", "objectives",
                       "territory", "quarters", "reserves", "denial", "labels"):
            self.assertIn(f"id:'{toggle}'", page)

    def test_no_objective_range_ring_is_offered(self):
        # 11th edition dropped the 3" objective control range, so the toggle and
        # its ring were removed; nothing should reintroduce them.
        page = BR.render_html(self.report)
        self.assertNotIn("objrange", page)
        self.assertNotIn('Obj 3', page)

    def test_player_tab_sits_between_round_and_phase(self):
        page = BR.render_html(self.report)
        self.assertIn('id="players"', page)
        self.assertLess(page.index('id="rounds"'), page.index('id="players"'))
        self.assertLess(page.index('id="players"'), page.index('id="phases"'))

    def test_deployment_map_is_a_separate_view_that_is_off_by_default(self):
        page = BR.render_html(self.report)
        self.assertIn('id="deployView"', page)
        self.assertIn("deployMap:false", page)
        self.assertIn("renderDeployMap", page)

    def test_board_supports_pan_and_zoom(self):
        page = BR.render_html(self.report)
        for hook in ("wheel", "pointerdown", "pointermove", "resetView", 'id="reset"'):
            self.assertIn(hook, page)

    def test_board_geometry_is_drawn_in_inches_not_pixels(self):
        # The viewBox is the table in inches, so a stroke or font size of 1 is a
        # one-inch mark. Anything >= 2 here would be a leftover pixel value and
        # would swamp a 0.63in base.
        page = BR.render_html(self.report)
        css = page[page.index("<style>"):page.index("</style>")]
        for cls in (".t-area", ".t-light", ".t-dense", ".quarter", ".dz",
                    ".mlabel", ".objective", ".territory"):
            block = css[css.index(cls):css.index(cls) + 200]
            for m in re.finditer(r"(?:stroke-width|font-size):\s*([0-9.]+)", block):
                self.assertLess(float(m.group(1)), 2.0,
                                f"{cls} uses a pixel-scale value: {m.group(0)}")

    @staticmethod
    def _payload(page):
        m = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                      page, re.S)
        assert m, "embedded report payload not found"
        return json.loads(m.group(1))

    def test_a_hostile_model_name_cannot_break_out_of_the_script_block(self):
        # "</script>" ends a script element whatever the JSON quoting, so a nickname
        # from an imported army list must not be able to reach the page as markup.
        log = make_log()
        log["roster"]["aaa111"]["n"] = "</script><img src=x onerror=alert(1)>"
        page = BR.render_html(BR.build_report(BR.load_log(log)))
        self.assertNotIn("</script><img", page)
        self.assertEqual(page.count("<script"), page.count("</script>"))
        names = [m["name"] for m in self._payload(page)["frames"][0]["models"]]
        self.assertIn("</script><img src=x onerror=alert(1)>", names)

    def test_model_names_are_rendered_as_labels(self):
        page = BR.render_html(self.report)
        self.assertIn("Intercessor Sergeant", page)

    def test_the_battlemat_surface_is_not_drawn_as_terrain(self):
        svg = BR.render_board_svg(self.report, self.report["frames"][0])
        self.assertIn("Tower", svg)
        self.assertNotIn("battlemaster_battlemat", svg)

    def test_objective_markers_are_drawn_distinctly(self):
        svg = BR.render_board_svg(self.report, self.report["frames"][0])
        self.assertIn("objective", svg)

    def test_reserve_models_are_not_drawn_on_the_board(self):
        log = make_log()
        log["snaps"][0]["m"]["aaa111"] = [0.0, 74.25, 0, 2]
        report = BR.build_report(BR.load_log(log))
        svg = BR.render_board_svg(report, report["frames"][0])
        self.assertNotIn("Intercessor Sergeant", svg)

    def test_player_names_appear_in_the_header(self):
        page = BR.render_html(self.report)
        self.assertIn("RedPlayer", page)
        self.assertIn("BluePlayer", page)

    def test_write_report_emits_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            html_path = BR.write_report(self.log, out)
            self.assertTrue(html_path.is_file())
            data = json.loads((out / "snapshots.json").read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], BR.SCHEMA_VERSION)
            self.assertEqual(len(data["frames"]), 2)

    def test_html_escapes_model_names(self):
        log = make_log()
        log["roster"]["aaa111"]["n"] = "<script>alert(1)</script>"
        report = BR.build_report(BR.load_log(log))
        page = BR.render_html(report)
        self.assertNotIn("<script>alert(1)</script>", page)


class TestLuaWiring(unittest.TestCase):
    """Lock the Lua-side contracts this pipeline depends on."""

    def test_schema_version_matches_the_lua_side(self):
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        m = re.search(r"BATTLE_LOG_SCHEMA\s*=\s*(\d+)", text)
        self.assertIsNotNone(m, "BATTLE_LOG_SCHEMA missing from global.ttslua")
        self.assertEqual(int(m.group(1)), BR.SCHEMA_VERSION)

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
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        start = text.find("function canRegisterFor(")
        self.assertNotEqual(start, -1)
        body = text[start:text.find("\nend", start)]
        # Solo play and an unoccupied seat must both be allowed, or one player
        # cannot register both armies.
        self.assertIn("battleSoloModeActive()", body)
        self.assertIn("p.seated", body)

    def test_solo_mode_uses_the_singles_flag_not_just_simulation(self):
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        start = text.find("function battleSoloModeActive(")
        self.assertNotEqual(start, -1)
        body = text[start:text.find("\nend", start)]
        # `simulation` is reset on startGame's last line, so it alone cannot carry
        # solo play through a game.
        self.assertIn("singlesMode", body)

    def test_tool_buttons_forward_to_global(self):
        text = TOOLS_LUA.read_text(encoding="utf-8")
        for fn in ("registerArmyR", "registerArmyB", "captureSnapshot", "exportReport"):
            self.assertIn(f"function {fn}(", text)
        self.assertIn("battleRegisterFromSelection", text)
        self.assertIn("battleClearArmy", text)
        self.assertIn("exportBattleReport", text)

    def test_report_url_matches_the_server_default(self):
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        m = re.search(r'BATTLE_REPORT_URL\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m)
        import battle_report_server as SRV
        self.assertEqual(m.group(1), f"http://127.0.0.1:{SRV.DEFAULT_PORT}{SRV.REPORT_PATH}")


class TestViewerScript(unittest.TestCase):
    def test_viewer_javascript_parses(self):
        # The page is useless if this has a syntax error, and nothing else in the
        # pipeline would notice. Optional dependency: pip install esprima.
        try:
            import esprima
        except ImportError:
            self.skipTest("esprima not installed (pip install esprima)")
        esprima.parseScript(BR.VIEWER_JS)


class TestDeploymentShapes(unittest.TestCase):
    """The viewer must cover every draw type the mod's DeployZones tables use, or a
    Combat Patrol report silently loses its zones."""

    DRAW_TYPES = ("line", "stepped", "quarter", "circle", "triangle",
                  "cornerRectangle", "circleDeployment", "cornerPolyline")

    def test_viewer_handles_every_draw_type_the_mod_defines(self):
        # The set of types actually reachable through selectedDeployment().
        source = START_MENU_LUA.read_text(encoding="utf-8", errors="replace")
        in_tables = set()
        for block in re.finditer(r"DeployZones\w+\s*=\s*\{(.*?)\n\}", source, re.S):
            in_tables.update(re.findall(r'type\s*=\s*"(\w+)"', block.group(1)))
        in_tables.discard("none")
        missing = sorted(t for t in in_tables if f"'{t}'" not in BR.VIEWER_JS)
        self.assertEqual(missing, [], f"viewer cannot draw: {missing}")

    def test_the_types_this_test_knows_about_are_all_handled(self):
        for t in self.DRAW_TYPES:
            self.assertIn(f"'{t}'", BR.VIEWER_JS)

    def test_a_draw_that_is_not_a_list_cannot_reach_a_loop(self):
        # "No Deployment Zone" stores draw as {type = "none"}, which JSON-encodes
        # to an object; for..of and .map on it would throw and blank the page.
        self.assertIn("Array.isArray", BR.VIEWER_JS)
        self.assertNotIn("(dep.draw || []).map", BR.VIEWER_JS)
        for bad in ("of spec.draw", "of D.deployment.draw"):
            self.assertNotIn(bad, BR.VIEWER_JS)

    def test_a_none_deployment_renders(self):
        log = make_log()
        log["game"]["deploy"] = {"name": "No Deployment Zone", "draw": {"type": "none"}}
        page = BR.render_html(BR.build_report(BR.load_log(log)))
        self.assertIn("No Deployment Zone", page)


class TestLuaWiringDeployment(unittest.TestCase):
    def test_type_is_compared_against_a_string_not_the_stdlib_table(self):
        # type() returns a string, so comparing it to the bare word `table` (the
        # stdlib table) is always true and would silently drop every deployment.
        src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        body = src[src.index("function battleDeploymentSpec"):]
        body = body[:body.index("\nend")]
        self.assertIn('~= "table"', body)
        self.assertNotIn("~= table\n", body)


class TestReportServer(unittest.TestCase):
    """The server is left running for a whole game, and across edits to the
    renderer while the report is being worked on."""

    def setUp(self):
        self.src = (SCRIPT_DIR / "battle_report_server.py").read_text(
            encoding="utf-8", errors="replace")

    def test_the_renderer_is_reimported_per_request(self):
        # Importing once at startup makes every later edit invisible, which looks
        # exactly like the report refusing to update.
        self.assertIn("importlib.reload", self.src)
        self.assertIn("br = renderer()", self.src)

    def test_the_reloaded_module_is_used_for_its_own_exception_class(self):
        # reload() rebuilds BattleReportError; catching the stale class would
        # turn a clean 422 into an unhandled 500.
        self.assertIn("except br.BattleReportError", self.src)
        self.assertNotIn("except BR.BattleReportError", self.src)

    def test_a_broken_edit_does_not_kill_the_server(self):
        block = self.src[self.src.index("def renderer("):]
        block = block[:block.index("\nROOT")]
        self.assertIn("try:", block)
        self.assertIn("except Exception", block)


class TestGeneratedStamp(unittest.TestCase):
    def test_the_page_says_when_it_was_rendered(self):
        # Rendered through a long-running server, so "is this page stale?" needs
        # an answer visible on the page itself.
        page = BR.render_html(BR.build_report(BR.load_log(make_log())))
        self.assertRegex(page, r"rendered \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


class TestCrossScriptTableSafety(unittest.TestCase):
    """TTS raises "resources owned by different scripts" when one script reads a
    table that belongs to another script's state. Object.call returning a freshly
    built table is fine; returning a long-lived one (DeployZonesData[i]) is not.
    Every such read must therefore sit INSIDE its pcall, not after it."""

    def setUp(self):
        self.src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")

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


class TestDeploymentRound(unittest.TestCase):
    """The Start Game baseline is its own round, not part of Round 1."""

    def _log(self):
        log = make_log()
        log["snaps"][0]["why"] = "deploy"
        log["snaps"][0]["r"] = 1          # the counter already reads 1 by then
        return log

    def test_deploy_snapshot_becomes_round_zero(self):
        frames = BR.build_frames(BR.load_log(self._log()))
        self.assertEqual(frames[0]["round"], 0)
        self.assertEqual(frames[1]["round"], 1)

    def test_deploy_frame_is_labelled_deployment(self):
        frames = BR.build_frames(BR.load_log(self._log()))
        self.assertEqual(frames[0]["label"], "Deployment")

    def test_a_phase_snapshot_in_round_one_keeps_its_round(self):
        # Only "deploy" moves; a manual capture during round 1 stays in round 1.
        log = make_log()
        log["snaps"][0]["why"] = "manual"
        frames = BR.build_frames(BR.load_log(log))
        self.assertEqual([f["round"] for f in frames], [1, 1])

    def test_the_viewer_labels_round_zero_as_deploy(self):
        page = BR.render_html(BR.build_report(BR.load_log(self._log())))
        self.assertIn("'Deploy'", page)

    def test_start_game_says_so_when_no_deployment_snapshot_is_possible(self):
        src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        body = src[src.index("function battleStartGame"):]
        body = body[:body.index("\nend")]
        self.assertIn("battleRosterCount(nil) == 0", body)
        self.assertIn("deployment snapshot", body)


class TestLuaScoreAndContext(unittest.TestCase):
    def test_scores_do_not_go_through_playerSum(self):
        # Object.call passes its params table as the SINGLE argument, so
        # call("playerSum", {1}) hands playerSum the table {1} and it errors
        # indexing scores[pId]. getMatchSummary is the supported entry point.
        src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        # Comments explain the trap by name, so only real code is searched.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("--"))
        self.assertNotIn('call("playerSum"', code)
        self.assertIn('call("getMatchSummary")', code)

    def test_a_snapshot_backfills_missing_game_context(self):
        # battleStartGame only fires when someone presses Start Game with this
        # build loaded; joining a game in progress must still get a map name and
        # deployment, or the report has no header and an empty deployment map.
        src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("function battleEnsureGameContext", src)
        body = src[src.index("function recordBattleSnapshot"):]
        body = body[:body.index("\nend")]
        # Called through pcall so gathering context can never lose a snapshot.
        self.assertIn("pcall(battleEnsureGameContext)", body)

    def test_backfill_never_overwrites_what_start_game_recorded(self):
        src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        body = src[src.index("function battleEnsureGameContext"):]
        body = body[:body.index("\nend\n\nfunction")]
        for field in ("started", "map", "mapGuid", "deploy", "red", "blue"):
            self.assertIn(f"g.{field} == nil", body,
                          f"{field} is assigned without a nil guard")


class TestScorePanel(unittest.TestCase):
    def setUp(self):
        self.report = BR.build_report(BR.load_log(make_log()))

    def test_primary_secondary_split_is_shown_when_present(self):
        log = make_log()
        for snap in log["snaps"]:
            snap["vp"] = {"r": 12, "b": 9, "rp": 8, "rs": 4, "bp": 5, "bs": 4}
        report = BR.build_report(BR.load_log(log))
        self.assertEqual(report["frames"][0]["vp"]["rp"], 8)
        self.assertIn("splitR", BR.render_html(report))

    def test_a_log_without_the_split_still_renders(self):
        # Older logs carry only {r, b}; the split line must stay blank rather
        # than reporting zeroes that were never scored.
        page = BR.render_html(self.report)
        self.assertIn("setSplit", page)
        self.assertIn("!== undefined", page)


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


class TestTerrainClassification(unittest.TestCase):
    """The board is coloured by the map's own scheme, not by guessing from names."""

    @staticmethod
    def piece(**kw):
        base = {"g": "x", "n": "", "t": [], "x": 0.0, "z": 0.0,
                "ry": 0, "sx": 1, "sz": 1, "bx": 2.0, "bz": 2.0}
        base.update(kw)
        return base

    def test_the_material_description_decides_light_or_dense(self):
        self.assertEqual(BR.classify_terrain(self.piece(t=["Tower"], d="Dense")),
                         BR.KIND_DENSE)
        self.assertEqual(BR.classify_terrain(self.piece(t=["Corner"], d="Light")),
                         BR.KIND_LIGHT)

    def test_the_description_beats_the_tag_name(self):
        # The whole point of capturing it: a name that reads "light" is not it.
        self.assertEqual(
            BR.classify_terrain(self.piece(t=["Short Barrier"], d="Dense")),
            BR.KIND_DENSE)

    def test_a_mixed_piece_reads_as_dense(self):
        mixed = self.piece(t=["Short Barrier", "Tower"],
                           d="Tower = Dense\nWalls = Light")
        self.assertEqual(BR.classify_terrain(mixed), BR.KIND_DENSE)

    def test_area_outlines_are_areas_not_terrain_features(self):
        for shape in sorted(BR.AREA_SHAPES):
            self.assertEqual(BR.classify_terrain(self.piece(sh=shape)), BR.KIND_AREA,
                             shape)

    def test_an_obj_tagged_outline_is_an_area_not_a_marker(self):
        # obj_* marks the outline drawn AROUND an objective; the marker itself is a
        # separate object. Drawing the outline as a small circle lost the area and
        # put the objective an inch off at the same time.
        self.assertEqual(
            BR.classify_terrain(self.piece(t=["obj_home_red"], sh="bigrect")),
            BR.KIND_AREA)

    def test_an_untagged_battlemat_is_not_drawn_as_terrain(self):
        # A good number of shipped maps leave the mat untagged. Classified as
        # terrain it becomes a board-sized slab covering the entire report.
        mat = self.piece(bx=29.99, bz=21.97)
        self.assertEqual(BR.classify_terrain(mat, (59.79, 43.71)), BR.KIND_SURFACE)
        # ... but a genuinely large piece of terrain is still terrain.
        big = self.piece(t=["Tower"], d="Dense", bx=6.0, bz=4.0)
        self.assertEqual(BR.classify_terrain(big, (59.79, 43.71)), BR.KIND_DENSE)

    def test_a_physical_marker_is_recognised_by_its_nickname(self):
        for nick in ("Home Objective", "Center Objective", "Expansion Objective"):
            self.assertEqual(BR.classify_terrain(self.piece(n=nick, bx=1.0, bz=1.0)),
                             BR.KIND_OBJECTIVE, nick)

    def test_a_log_without_the_material_falls_back_to_the_tag(self):
        # Logs recorded before captureBoard stored the Description still have to
        # colour correctly, which is what LEGACY_TERRAIN_MATERIAL is for.
        self.assertEqual(BR.classify_terrain(self.piece(t=["Corner"])), BR.KIND_LIGHT)
        self.assertEqual(BR.classify_terrain(self.piece(t=["Pipes"])), BR.KIND_DENSE)

    def test_the_legacy_material_table_matches_every_shipped_map(self):
        """LEGACY_TERRAIN_MATERIAL must stay true to data/maps, or it is a lie."""
        maps = sorted((ROOT / "data" / "maps").glob("*.lua"))
        self.assertTrue(maps, "no map payloads to check against")
        found = {}
        for path in maps:
            text = path.read_text(encoding="utf-8", errors="replace")
            for blob in re.findall(r"\[\[(\{.*?\})\]\]", text, re.S):
                try:
                    obj = json.loads(blob)
                except ValueError:
                    continue
                desc = (obj.get("Description") or "").strip().lower()
                if not desc:
                    continue
                # Combined pieces name both materials; they are covered by the
                # mixed-piece rule, not by a per-tag entry.
                if "dense" in desc and "light" in desc:
                    continue
                if "dense" in desc:
                    kind = BR.KIND_DENSE
                elif "light" in desc:
                    kind = BR.KIND_LIGHT
                else:
                    continue
                for tag in (obj.get("Tags") or []):
                    found.setdefault(tag, set()).add(kind)
        self.assertTrue(found, "no materials found in the map payloads")
        for tag, kinds in sorted(found.items()):
            self.assertEqual(len(kinds), 1, f"{tag} is both {sorted(kinds)}")
            self.assertEqual(BR.LEGACY_TERRAIN_MATERIAL.get(tag), next(iter(kinds)),
                             f"{tag} is {next(iter(kinds))} in data/maps")

    def test_a_combo_piece_is_labelled_with_both_of_its_names(self):
        label = BR.terrain_label(self.piece(t=["Short Barrier", "Tower"], d="Dense"))
        self.assertIn("Short Barrier", label)
        self.assertIn("Tower", label)

    def test_every_piece_carries_a_kind_into_the_report(self):
        report = BR.build_report(make_log())
        for piece in report["board"]["terrain"]:
            self.assertIn(piece["kind"],
                          {BR.KIND_SURFACE, BR.KIND_AREA, BR.KIND_LIGHT,
                           BR.KIND_DENSE, BR.KIND_OBJECTIVE})

    def test_objective_markers_are_preferred_over_the_outlines(self):
        pieces = [
            {"kind": BR.KIND_OBJECTIVE, "x": 1.0, "z": 2.0, "label": "Home Objective",
             "t": []},
            {"kind": BR.KIND_AREA, "x": 1.2, "z": 2.2, "label": "bigrect",
             "t": ["obj_home_red"]},
        ]
        anchors = BR._objective_anchors(pieces)
        self.assertEqual([(a["x"], a["z"]) for a in anchors], [(1.0, 2.0)])

    def test_objectives_fall_back_to_the_outlines_when_no_marker_was_captured(self):
        pieces = [{"kind": BR.KIND_AREA, "x": 1.2, "z": 2.2, "label": "bigrect",
                   "t": ["obj_home_red"]}]
        self.assertEqual(len(BR._objective_anchors(pieces)), 1)


class TestBoardOrientation(unittest.TestCase):
    """TTS +z is the far edge of the table and belongs at the top of the picture."""

    def test_the_viewer_negates_z(self):
        self.assertIn("const sz = z => (H/2 - z) + PAD;", BR.VIEWER_JS)
        self.assertNotIn("const sz = z => (z + H/2) + PAD;", BR.VIEWER_JS)

    def test_the_static_renderer_draws_positive_z_above_the_centre(self):
        report = BR.build_report(make_log())
        frame = report["frames"][0]
        far = dict(frame["models"][0], x=0.0, z=10.0, reserve=False)
        near = dict(far, z=-10.0)
        far_svg = BR.render_board_svg(report, dict(frame, models=[far]))
        near_svg = BR.render_board_svg(report, dict(frame, models=[near]))
        far_y = float(re.search(r'<ellipse cx="[\d.]+" cy="([\d.]+)"', far_svg).group(1))
        near_y = float(re.search(r'<ellipse cx="[\d.]+" cy="([\d.]+)"', near_svg).group(1))
        self.assertLess(far_y, near_y, "+z must render above -z")

    def test_off_centre_rectangles_anchor_on_the_higher_z_edge(self):
        # A rect's y attribute is its TOP, so under a flipped sz it has to be built
        # from the larger z of the pair. Symmetric rects are unaffected; these two
        # are not symmetric about their own centre.
        self.assertIn("y:sz(cz + rh/2)", BR.VIEWER_JS)
        self.assertIn("y:sz(H/2-6)", BR.VIEWER_JS)

    def test_the_deploy_map_caption_sits_above_the_board(self):
        self.assertIn("y:sz(H/2) - 0.9", BR.VIEWER_JS)


class TestTerritoryDivider(unittest.TestCase):
    """The divider is derived from the deployment, exactly as the table derives it."""

    def test_it_is_no_longer_a_bare_centre_line(self):
        self.assertNotIn("line(el('g', {}, svg), -W/2, 0, W/2, 0, 'territory')",
                         BR.VIEWER_JS)
        self.assertIn("drawTerritory(el('g', {}, svg))", BR.VIEWER_JS)

    def test_the_viewer_ports_every_branch_the_lua_derives(self):
        source = START_MENU_LUA.read_text(encoding="utf-8", errors="replace")
        body = source[source.index("function drawTerritoryLine"):]
        body = body[:body.index("\n-- 9")]
        for helper in ("territoryReferenceEntry", "stepBoundaryFromCentre",
                       "territoryChordLength"):
            self.assertIn(helper, BR.VIEWER_JS, f"{helper} was not ported")
        for branch in ('"triangle"', '"quarter"', '"stepped"'):
            self.assertIn(branch, body)
            self.assertIn(branch.replace('"', "'"), BR.VIEWER_JS,
                          f"viewer is missing the {branch} case")

    def test_an_explicit_territory_spec_wins_over_the_derivation(self):
        # Combat Patrol declares its divider outright; its cornerPolyline shapes
        # have no angle to derive one from.
        self.assertIn("Array.isArray(spec.territory)", BR.VIEWER_JS)

    def test_the_mod_hands_the_territory_spec_to_the_log(self):
        source = START_MENU_LUA.read_text(encoding="utf-8", errors="replace")
        body = source[source.index("function selectedDeploymentJSON"):]
        body = body[:body.index("\nend")]
        self.assertIn("territory = zone.territory", body)


class TestSecondaryScan(unittest.TestCase):
    """The secondaries scan reads the slots the way the rest of the mod does."""

    def setUp(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        start = source.index("function battleSecondaryState")
        self.body = source[start:source.index("\nend", start)]

    def test_it_uses_the_mods_own_slot_helper(self):
        self.assertIn("getCardsInSecondarySlot(zone)", self.body)

    def test_it_does_not_read_the_zone_directly(self):
        # zone.getObjects() came back empty with all four slots visibly filled: a
        # card dropped onto a zone is not registered as inside it until it settles.
        code = "\n".join(ln for ln in self.body.splitlines()
                         if not ln.lstrip().startswith("--"))
        self.assertNotIn("zone.getObjects()", code)

    def test_the_helper_it_leans_on_still_exists(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("function getCardsInSecondarySlot(zone)", source)


class TestBoardCapture(unittest.TestCase):
    """captureBoard records what the report needs to colour the board."""

    def setUp(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        start = source.index("function captureBoard")
        self.body = source[start:source.index("\nend", source.index("Wait.frames", start))]

    def test_it_records_the_material_description(self):
        self.assertIn("o.getDescription()", self.body)
        self.assertIn("d  = desc,", self.body)

    def test_the_description_is_capped(self):
        self.assertIn("BATTLE_BOARD_DESC_MAX", self.body)
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertRegex(source, r"BATTLE_BOARD_DESC_MAX = \d+")

    def test_it_records_the_area_outline_shape(self):
        self.assertIn("5mm%-border", self.body)
        self.assertIn("sh = sh,", self.body)

    def test_the_shape_pattern_matches_the_meshes_the_maps_actually_use(self):
        # The Lua pattern and AREA_SHAPES have to agree, or every area is
        # misclassified as a terrain feature.
        pattern = re.compile(r"-([a-z]+)-5mm-border")
        maps = sorted((ROOT / "data" / "maps").glob("*.lua"))
        found = set()
        for path in maps[:40]:
            text = path.read_text(encoding="utf-8", errors="replace")
            found.update(pattern.findall(text))
        self.assertTrue(found, "no area outline meshes found in the map payloads")
        self.assertTrue(found <= BR.AREA_SHAPES,
                        f"unknown area shapes: {sorted(found - BR.AREA_SHAPES)}")


class TestPlateFootprints(unittest.TestCase):
    """Terrain areas draw their real footprint, not the box their bounds imply."""

    # Authored plate sizes, from TERRAIN_ASSETS in battlemaster_reconstruct.py.
    AUTHORED = {
        "shortline": (6.003, 2.003), "smallrect": (6.003, 4.003),
        "longline": (10.003, 2.503), "bigrect": (11.503, 7.003),
        "triangle": (11.503, 8.003),
    }

    def test_an_outline_is_known_for_every_area_shape(self):
        self.assertEqual(set(BR.PLATE_OUTLINES), BR.AREA_SHAPES)
        self.assertEqual(set(BR.PLATE_BOUNDS), BR.AREA_SHAPES)

    def test_each_outline_spans_its_authored_size(self):
        for shape, (width, height) in self.AUTHORED.items():
            pts = BR.PLATE_OUTLINES[shape]
            span_x = max(p[0] for p in pts) - min(p[0] for p in pts)
            span_z = max(p[1] for p in pts) - min(p[1] for p in pts)
            self.assertAlmostEqual(span_x, width, places=2, msg=shape)
            self.assertAlmostEqual(span_z, height, places=2, msg=shape)

    def test_the_authored_sizes_still_match_the_reconstructor(self):
        """If Battlemaster reshapes a plate, these outlines go stale silently."""
        source = (ROOT / "scripts" / "battlemaster_reconstruct.py").read_text(
            encoding="utf-8", errors="replace")
        block = source[source.index("TERRAIN_ASSETS = ("):]
        block = block[:block.index("\n)\n")]
        found = {}
        for entry in re.finditer(
                r'"plate":\s*"\d+-(\w+)".*?"width":\s*([\d.]+),\s*"height":\s*([\d.]+)',
                block, re.S):
            found[entry.group(1)] = (float(entry.group(2)), float(entry.group(3)))
        self.assertEqual(set(found), set(self.AUTHORED),
                         "the reconstructor's plate list changed")
        for shape, size in found.items():
            self.assertEqual(size, self.AUTHORED[shape], f"{shape} was resized")

    def test_the_triangle_plate_is_a_trapezoid(self):
        # It is named "triangle" but has four corners, two of them a short edge.
        # Drawing it as a triangle -- or as a box -- is wrong either way.
        pts = BR.PLATE_OUTLINES["triangle"]
        self.assertEqual(len(pts), 4)
        short_edge = [p for p in pts if abs(p[0] - 5.932) < 0.001]
        self.assertEqual(len(short_edge), 2)
        self.assertAlmostEqual(abs(short_edge[0][1] - short_edge[1][1]), 2.0, places=2)

    def test_no_two_plates_can_be_confused_by_their_bounds(self):
        # The bounds inference below is only sound while the boxes stay apart.
        names = sorted(BR.PLATE_BOUNDS)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                gap = max(abs(BR.PLATE_BOUNDS[a][0] - BR.PLATE_BOUNDS[b][0]),
                          abs(BR.PLATE_BOUNDS[a][1] - BR.PLATE_BOUNDS[b][1]))
                self.assertGreater(gap, 2 * BR.PLATE_BOUNDS_TOLERANCE,
                                   f"{a} and {b} are within tolerance of each other")

    def test_a_captured_box_identifies_its_plate(self):
        for shape, (width, height) in BR.PLATE_BOUNDS.items():
            piece = {"bx": width / 2, "bz": height / 2}
            self.assertEqual(BR.infer_plate(piece), shape)

    def test_something_that_is_not_a_plate_is_not_guessed_at(self):
        self.assertIsNone(BR.infer_plate({"bx": 1.0, "bz": 1.0}))

    def test_an_old_log_still_gets_real_footprints(self):
        # No sh field, only bounds -- which is every log recorded before the plate
        # name was captured.
        log = make_log()
        log["board"] = [{"g": "p1", "n": "", "t": [], "x": 0.0, "z": 0.0, "ry": 0,
                         "sx": 1, "sz": 1, "bx": 5.0, "bz": 1.814}]
        piece = BR.build_report(log)["board"]["terrain"][0]
        self.assertEqual(piece["kind"], BR.KIND_AREA)
        self.assertEqual(piece["sh"], "longline")

    def test_the_outlines_reach_the_page(self):
        page = BR.render_html(BR.build_report(make_log()))
        self.assertIn("platePoints", page)
        self.assertIn('"plates":', page.replace(" ", ""))

    def test_a_plate_renders_as_a_polygon_and_other_terrain_as_a_box(self):
        log = make_log()
        log["board"] = [
            {"g": "p1", "n": "", "t": [], "sh": "bigrect", "x": 0.0, "z": 0.0,
             "ry": 0, "sx": 1, "sz": 1, "bx": 5.75, "bz": 3.771},
            {"g": "p2", "n": "", "t": ["Tower"], "d": "Dense", "x": 4.0, "z": 2.0,
             "ry": 0, "sx": 1, "sz": 1, "bx": 1.2, "bz": 1.2},
        ]
        report = BR.build_report(log)
        svg = BR.render_board_svg(report, report["frames"][0])
        self.assertEqual(svg.count("<polygon"), 1)
        self.assertEqual(len(re.findall(r'<rect[^>]*class="t-', svg)), 1)

    def test_a_mirrored_plate_is_flipped_not_just_rotated(self):
        # Battlemaster mirrors with an extra 180 about x or z; without honouring it
        # the trapezoid faces the wrong way.
        log = make_log()
        base = {"g": "p1", "n": "", "t": [], "sh": "triangle", "x": 0.0, "z": 0.0,
                "ry": 0, "sx": 1, "sz": 1, "bx": 5.931, "bz": 4.0}
        plain = BR.build_report(dict(log, board=[dict(base)]))
        flipped = BR.build_report(dict(log, board=[dict(base, rz=180)]))
        a = BR.render_board_svg(plain, plain["frames"][0])
        b = BR.render_board_svg(flipped, flipped["frames"][0])
        self.assertIn("<polygon", a)
        self.assertNotEqual(re.search(r'<polygon points="([^"]+)"', a).group(1),
                            re.search(r'<polygon points="([^"]+)"', b).group(1))

    def test_the_mod_records_the_mirror_rotations(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        start = source.index("function captureBoard")
        body = source[start:source.index("\nend", source.index("Wait.frames", start))]
        self.assertIn("rx = rx,", body)
        self.assertIn("rz = rz,", body)
        # stored only when set, so a normal piece costs nothing
        self.assertIn("if rx == 0 then rx = nil end", body)


class TestLayoutArt(unittest.TestCase):
    """The mission's layout diagram is shown as the deployment view."""

    def test_the_url_crosses_the_script_boundary_as_a_string(self):
        # Same rule as selectedDeploymentJSON: a card table owned by startMenu must
        # not be read from Global.
        menu = START_MENU_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("function loadedLayoutArtURL()", menu)
        glob_src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        body = glob_src[glob_src.index("function battleLayoutArtURL"):]
        body = body[:body.index("\nend")]
        self.assertIn('sm.call("loadedLayoutArtURL")', body)
        self.assertIn('type(raw) ~= "string"', body)

    def test_it_is_looked_up_once_per_game(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertIn("_battleArtTried", source)
        # and cleared when a new game starts, or the next game inherits the miss
        reset = source[source.index("function resetBattleLog"):]
        self.assertIn("_battleArtTried = false", reset[:reset.index("\nend")])

    def test_the_viewer_only_loads_it_over_http(self):
        self.assertIn("/^https?:", BR.VIEWER_JS)

    def test_the_page_carries_the_art_url_when_the_log_has_one(self):
        log = make_log()
        log["game"]["art"] = "https://example.invalid/layout.jpg"
        page = BR.render_html(BR.build_report(log))
        self.assertIn("example.invalid/layout.jpg", page)
        self.assertIn('id="artImg"', page)

    def test_a_log_without_art_still_renders_the_vector_deployment_map(self):
        page = BR.render_html(BR.build_report(make_log()))
        self.assertIn("renderDeployMap", page)
        self.assertIn('"art":null', page.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
