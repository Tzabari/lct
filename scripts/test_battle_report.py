#!/usr/bin/env python3
"""Behavioural tests for the battle report pipeline.

These lock the runtime contracts the Lua side depends on: the ForceOrg nickname
format, casualty accumulation, reserve detection from the board rectangle, and
tolerance of logs that are partly malformed (a report should still render).
"""

import argparse
import contextlib
import gzip
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import bake_terrain_cache as BTC
import battle_report as BR
import battle_report_server as SRV
import map_payloads as MP
import map_terrain as MT
import report_store as RS

ROOT = SCRIPT_DIR.parent
GLOBAL_LUA = ROOT / "TTSLUA" / "global.ttslua"
START_MENU_LUA = ROOT / "TTSLUA" / "startMenu.ttslua"
TOOLS_LUA = ROOT / "TTSLUA" / "spawnGameTools.ttslua"


def make_log(**overrides):
    """A minimal but complete two-model, two-snapshot log."""
    log = {
        "v": 2,
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
        # Distinct nicknames seen this game, verbatim -- wound prefix and colour
        # markup included, exactly as recordBattleSnapshot interns them. A
        # snapshot's model row carries an index into this pool rather than the
        # string itself (1-based, matching battleAddName).
        "names": [
            "[00ff16]2/2[-] Intercessor Sergeant",   # 1: aaa111 at full health
            "[ffff00]1/1[-] Hormagaunt",              # 2: bbb222 before it dies
            "[ff0000]1/2[-] Intercessor Sergeant",    # 3: aaa111 after a wound
        ],
        "snaps": [
            {"r": 1, "t": "Red", "p": 1, "lbl": "Round 1, Red Turn 1 - Command Phase",
             "why": "deploy", "ts": 1, "vp": {"r": 0, "b": 0}, "cp": {"r": 1, "b": 1},
             "m": {"aaa111": [-10.0, 0.0, -5.0, 90, 1],
                   "bbb222": [12.0, 0.0, 6.0, 270, 2]},
             "dead": []},
            {"r": 1, "t": "Red", "p": 2, "lbl": "Round 1, Red Turn 1 - Movement Phase",
             "why": "phase", "ts": 2, "vp": {"r": 5, "b": 0}, "cp": {"r": 2, "b": 1},
             "m": {"aaa111": [-4.0, 0.0, -2.0, 90, 3]},
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
        log["snaps"][0]["m"]["aaa111"] = [0.0, 0.0, 74.25, 0, 1]
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

    def test_a_full_slate_of_eight_secondaries_reaches_the_report(self):
        """Nothing downstream of the capture caps the list -- the capture did."""
        log = make_log()
        log["snaps"][0]["sec"] = {
            "Red":  [{"n": "Red %d" % i, "fd": False} for i in range(1, 9)],
            "Blue": [{"n": "Blue %d" % i, "fd": False} for i in range(1, 9)],
        }
        frame = BR.build_frames(BR.load_log(log))[0]
        self.assertEqual([s["name"] for s in frame["secondaries"]["Red"]],
                         ["Red %d" % i for i in range(1, 9)])
        self.assertEqual(len(frame["secondaries"]["Blue"]), 8)

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
        # nameIndex 0 is out of range (the pool is 1-based), so the name -- and
        # with it the wounds parsed out of it -- resolves to blank rather than
        # erroring.
        log["snaps"][0]["m"]["aaa111"] = [1.0, 0.0, 2.0, 90, 0]
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
        log["snaps"][0]["m"]["zzz999"] = [1.0, 0.0, 2.0, 0, 0]
        frames = BR.build_frames(BR.load_log(log))
        stray = next(m for m in frames[0]["models"] if m["guid"] == "zzz999")
        self.assertEqual(stray["name"], "zzz999")

    def test_missing_board_size_falls_back_to_strike_force(self):
        log = make_log()
        log["game"].pop("board")
        self.assertEqual(BR.board_size(log), (60.0, 44.0))


class TestSchema2ModelRows(unittest.TestCase):
    """The v2 row shape: [x, y, z, rotY, nameIndex] plus an optional [rx, rz]
    tilt pair, with the model's displayed name (and wounds) resolved through
    the log's names pool rather than carried as a bare number."""

    def test_model_entry_resolves_the_name_pool(self):
        entry = BR._model_entry([1.0, 2.0, 3.0, 90, 2],
                                 ["[00ff16]2/2[-] Alpha", "[ff0000]1/3[-] Bravo"])
        self.assertEqual(entry["name"], "Bravo")
        self.assertEqual((entry["w"], entry["mw"]), (1, 3))

    def test_model_entry_carries_y_through(self):
        # Y is what puts a model back on a ruin's upper floor on rewind; the
        # report draws nothing with it, but it must round-trip all the same.
        entry = BR._model_entry([1.0, 4.5, 3.0, 90, 0], [])
        self.assertEqual(entry["y"], 4.5)

    def test_model_entry_reads_the_optional_tilt_pair(self):
        entry = BR._model_entry([1.0, 0.0, 3.0, 90, 0, 12.0, -6.0], [])
        self.assertEqual((entry["rx"], entry["rz"]), (12.0, -6.0))

    def test_model_entry_defaults_tilt_to_zero(self):
        entry = BR._model_entry([1.0, 0.0, 3.0, 90, 0], [])
        self.assertEqual((entry["rx"], entry["rz"]), (0.0, 0.0))

    def test_model_entry_out_of_range_index_is_a_blank_name(self):
        entry = BR._model_entry([1.0, 0.0, 3.0, 90, 99], ["Only One"])
        self.assertEqual(entry["name"], "")

    def test_a_v1_shaped_row_is_now_too_short_to_read(self):
        # The old [x, z, rotY, w] tuple is one element short of v2's
        # [x, y, z, rotY, nameIndex] and must be skipped, not misread as if its
        # trailing wound count were a name index.
        self.assertIsNone(BR._model_entry([1.0, 2.0, 90, 2], []))

    def test_frame_carries_y_from_the_snapshot(self):
        log = make_log()
        log["snaps"][0]["m"]["aaa111"] = [-10.0, 6.5, -5.0, 90, 1]
        frame = BR.build_frames(BR.load_log(log))[0]
        red = next(m for m in frame["models"] if m["guid"] == "aaa111")
        self.assertEqual(red["y"], 6.5)

    def test_frame_name_prefers_the_live_nickname_over_registration(self):
        # A unit renamed mid-game (or simply wounded, changing its bracket
        # colour) must read back as it was AT THAT MOMENT, not as registered.
        log = make_log()
        frame = BR.build_frames(BR.load_log(log))[1]
        red = next(m for m in frame["models"] if m["guid"] == "aaa111")
        self.assertEqual(red["name"], "Intercessor Sergeant")
        self.assertEqual(red["wounds"], 1)


class TestSchema2Secondaries(unittest.TestCase):
    """table.insert on the Lua side used to lose an empty slot's position;
    schema 2 records the slot index explicitly and the report must sort by it
    rather than trust encounter order."""

    def test_secondaries_render_in_slot_order_not_discovery_order(self):
        log = make_log()
        log["snaps"][0]["sec"] = {
            "Red": [{"i": 3, "n": "Third slot"}, {"i": 1, "n": "First slot"}],
        }
        frame = BR.build_frames(BR.load_log(log))[0]
        self.assertEqual([c["name"] for c in frame["secondaries"]["Red"]],
                         ["First slot", "Third slot"])

    def test_a_card_with_no_recorded_slot_sorts_after_slotted_ones(self):
        log = make_log()
        log["snaps"][0]["sec"] = {
            "Red": [{"n": "No slot"}, {"i": 2, "n": "Second slot"}],
        }
        frame = BR.build_frames(BR.load_log(log))[0]
        self.assertEqual([c["name"] for c in frame["secondaries"]["Red"]],
                         ["Second slot", "No slot"])


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
        for cls in (".t-area", ".quarter", ".dz",
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
        # "</script>" ends a script element whatever the JSON quoting, so a
        # model's live nickname -- fully player-controlled TTS text, pooled and
        # indexed by the snapshot -- must not be able to reach the page as markup.
        log = make_log()
        hostile = "</script><img src=x onerror=alert(1)>"
        log["names"][0] = hostile  # index 1: what aaa111 resolves to in frame 0
        page = BR.render_html(BR.build_report(BR.load_log(log)))
        self.assertNotIn("</script><img", page)
        self.assertEqual(page.count("<script"), page.count("</script>"))
        names = [m["name"] for m in self._payload(page)["frames"][0]["models"]]
        self.assertIn(hostile, names)

    def test_model_names_are_rendered_as_labels(self):
        page = BR.render_html(self.report)
        self.assertIn("Intercessor Sergeant", page)

    def test_the_battlemat_surface_is_not_drawn_as_terrain(self):
        svg = BR.render_board_svg(self.report, self.report["frames"][0])
        self.assertNotIn("battlemaster_battlemat", svg)
        # The mat is a board-sized rectangle; drawn as terrain it would cover the
        # whole picture, so only the one .mat backdrop rect may be that big.
        self.assertEqual(svg.count("class=\"mat\""), 1)

    def test_objective_markers_are_drawn_distinctly(self):
        svg = BR.render_board_svg(self.report, self.report["frames"][0])
        self.assertIn("objective", svg)

    def test_reserve_models_are_not_drawn_on_the_board(self):
        log = make_log()
        log["snaps"][0]["m"]["aaa111"] = [0.0, 0.0, 74.25, 0, 1]
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
        # Covers the fallback path: a model whose own nickname doesn't resolve
        # from the pool falls back to its roster registration name, which must
        # be escaped exactly like the pooled-name path above.
        log = make_log()
        log["roster"]["aaa111"]["n"] = "<script>alert(1)</script>"
        log["snaps"][0]["m"]["aaa111"][4] = 0  # invalid index -> falls back
        report = BR.build_report(BR.load_log(log))
        page = BR.render_html(report)
        self.assertNotIn("<script>alert(1)</script>", page)


class TestRenderNonce(unittest.TestCase):
    """render_html(script_nonce=...) is what makes a real CSP possible hosted --
    see scripts/battle_report_server.py's script-src 'nonce-...' header."""

    def setUp(self):
        self.report = BR.build_report(BR.load_log(make_log()))

    def test_a_nonce_appears_on_exactly_both_script_tags(self):
        page = BR.render_html(self.report, script_nonce="abc123")
        self.assertEqual(page.count('nonce="abc123"'), 2)

    def test_the_default_render_has_no_nonce_attribute(self):
        page = BR.render_html(self.report)
        self.assertNotIn("nonce=", page)

    def test_none_leaves_output_byte_identical_to_omitting_the_argument(self):
        self.assertEqual(BR.render_html(self.report),
                         BR.render_html(self.report, script_nonce=None))

    def test_the_page_loads_nothing_from_the_network(self):
        """Pins the property a `default-src 'none'` CSP depends on.

        Worth having regardless of hosting: a report that ever grew an <img
        src=...>, an external stylesheet, or a CSS @import would silently break
        offline viewing of a downloaded copy, which is the whole point of the
        report being self-contained.
        """
        page = BR.render_html(self.report)
        # The SVG namespace URLs are the only "http" strings this page has.
        page_without_svg_ns = page.replace("http://www.w3.org/2000/svg", "")
        for hook in ("src=", "href=", "@import", "url(http"):
            self.assertNotIn(hook, page_without_svg_ns)


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


class TestLuaWiringRemote(unittest.TestCase):
    """The hosted path wired into global.ttslua: the compile-time marker,
    the probe-then-post ladder, Steam-id headers, and -- the one that must
    never regress -- that none of this ever fires before EXPORT is pressed."""

    @classmethod
    def setUpClass(cls):
        cls.src = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        cls.tools_src = TOOLS_LUA.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _body(src, fn):
        start = src.index(f"function {fn}(")
        body = src[start:]
        return body[:body.index("\nend")]

    def test_report_url_matches_the_server_default_is_unaffected(self):
        # Re-asserted here (it lives on TestLuaWiring) because it is the one
        # invariant this whole feature must never disturb: the local loopback
        # path is unchanged.
        m = re.search(r'BATTLE_REPORT_URL\s*=\s*"([^"]+)"', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), f"http://127.0.0.1:{SRV.DEFAULT_PORT}{SRV.REPORT_PATH}")

    def test_the_remote_base_marker_is_present_for_compile_py_to_bake(self):
        self.assertIn("-- @@BATTLE_REPORT_REMOTE_BASE@@", self.src)
        self.assertIn("-- @@BATTLE_REPORT_TOKEN@@", self.src)

    def test_compile_py_bakes_the_same_markers(self):
        # The drift that would otherwise ship a mod pointing nowhere: the
        # marker exists in the Lua source, but nothing writes to it.
        compile_src = (SCRIPT_DIR / "compile.py").read_text(encoding="utf-8")
        self.assertIn("def bake_battle_report_endpoint(", compile_src)
        self.assertIn("@@BATTLE_REPORT_REMOTE_BASE@@", compile_src)
        self.assertIn("@@BATTLE_REPORT_TOKEN@@", compile_src)
        self.assertIn("bake_battle_report_endpoint(global_text", compile_src,
                     "bake_battle_report_endpoint is defined but never called")

    def test_a_test_build_bakes_an_empty_base_not_the_real_service(self):
        import compile as C
        out = C.bake_battle_report_endpoint(self.src, is_test=True)
        self.assertIn('BATTLE_REPORT_REMOTE_BASE = ""', out)
        self.assertIn('BATTLE_REPORT_TOKEN = ""', out)

    def test_lct_report_bake_in_test_opts_a_test_build_into_a_real_base(self):
        # The escape hatch for rehearsing the hosted path (e.g. a LAN-bound
        # `battle_report_server.py --mode hosted`) without the in-game console.
        import compile as C
        saved = {k: os.environ.get(k) for k in
                 ("LCT_REPORT_BAKE_IN_TEST", "LCT_REPORT_REMOTE_BASE", "LCT_REPORT_TOKEN")}
        try:
            os.environ["LCT_REPORT_BAKE_IN_TEST"] = "1"
            os.environ["LCT_REPORT_REMOTE_BASE"] = "http://192.168.0.217:8799"
            os.environ["LCT_REPORT_TOKEN"] = "testtoken123"
            out = C.bake_battle_report_endpoint(self.src, is_test=True)
            self.assertIn('BATTLE_REPORT_REMOTE_BASE = "http://192.168.0.217:8799"', out)
            self.assertIn('BATTLE_REPORT_TOKEN = "testtoken123"', out)

            # Without LCT_REPORT_BAKE_IN_TEST, the same env vars are ignored --
            # a --test build stays local-only unless explicitly opted out.
            del os.environ["LCT_REPORT_BAKE_IN_TEST"]
            out = C.bake_battle_report_endpoint(self.src, is_test=True)
            self.assertIn('BATTLE_REPORT_REMOTE_BASE = ""', out)
            self.assertIn('BATTLE_REPORT_TOKEN = ""', out)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_the_health_path_matches_the_server(self):
        m = re.search(r'BATTLE_REPORT_HEALTH\s*=\s*"([^"]+)"', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), SRV.HEALTH_PATH)

    def test_export_uses_the_backoff_ladder_and_a_busy_guard(self):
        body = self._body(self.src, "exportBattleReport")
        self.assertIn("battleExportBusy", body)
        export_ladder = self._body(self.src, "battleExportLadder")
        self.assertIn("BATTLE_EXPORT_BACKOFF", export_ladder)
        self.assertIn("Wait.time", export_ladder)

    def test_only_the_hosted_endpoint_is_retried_local_fails_fast(self):
        # A local helper that isn't running fails near-instantly and will
        # never start itself mid-game -- retrying it through the whole ~2 min
        # ladder before ever trying the hosted fallback would make "auto" mode
        # look hung for no reason. Only the endpoint marked retryable (hosted)
        # gets the backoff ladder; local falls through after one probe.
        endpoints_body = self._body(self.src, "battleReportEndpoints")
        self.assertIn("retryable = false", endpoints_body)
        self.assertIn("retryable = true", endpoints_body)
        ladder_body = self._body(self.src, "battleExportLadder")
        self.assertIn("endpoint.retryable", ladder_body)

    def test_each_attempt_has_its_own_watchdog_not_a_shared_one(self):
        # Never depend on TTS's undocumented WebRequest timeout -- each network
        # attempt arms its own Wait.time so a slow/absent callback cannot hang
        # the ladder.
        for fn in ("battleProbeThenPost", "battlePostReport"):
            body = self._body(self.src, fn)
            self.assertIn("Wait.time", body)
            self.assertIn("BATTLE_EXPORT_TIMEOUT", body)

    def test_a_503_is_handled_distinctly_from_an_unreachable_server(self):
        # The server being busy is not the same situation as the server being
        # down; retrying immediately would only add to the load that caused it.
        body = self._body(self.src, "battleHandlePostResult")
        self.assertIn("503", body)
        busy_branch = body[body.index("code == 503"):]
        busy_branch = busy_branch[:busy_branch.index("return") + len("return")]
        self.assertNotIn("battleExportLadder", busy_branch,
                         "a 503 (busy) must not re-enter the retry ladder")

    def test_the_result_reaches_chat_and_a_notebook_tab(self):
        body = self._body(self.src, "battleDeliverReportUrl")
        self.assertIn("printToAll", body)
        self.assertIn("battleWriteNotebookTab", body)
        notebook_body = self._body(self.src, "battleWriteNotebookTab")
        self.assertIn("Notes.addNotebookTab", notebook_body)
        self.assertIn("Notes.editNotebookTab", notebook_body)

    def test_steam_id_is_sent_via_the_host_seat_not_the_clicker(self):
        headers_body = self._body(self.src, "battleReportHeaders")
        self.assertIn("X-LCT-Steam-Id", headers_body)
        self.assertIn("battleHostSteamId", headers_body)
        host_id_body = self._body(self.src, "battleHostSteamId")
        self.assertIn(".host", host_id_body)
        self.assertIn("Player.getPlayers", host_id_body)

    def test_the_export_button_tooltip_mentions_the_hosted_path(self):
        self.assertIn("hosted if configured", self.tools_src)

    def test_the_mod_makes_no_background_requests(self):
        """Pins "contact the server only on EXPORT" so a later edit cannot
        quietly reintroduce a warm-up ping. This is the one that must never be
        weakened -- see the plan's round-1 correction on this exact point."""
        # "WebRequest." (an actual call, e.g. WebRequest.get/.custom) rather
        # than the bare word: a comment is allowed to mention WebRequest while
        # explaining behaviour (see battleReportEndpoints' "local" entry)
        # without that tripping this check.
        for fn in ("recordBattleSnapshot", "battleRegisterFromSelection",
                  "battleClearArmy", "battleStartGame", "battleManualCapture"):
            body = self._body(self.src, fn)
            self.assertNotIn("WebRequest.", body, f"{fn} must not call WebRequest")

        # Every WebRequest call in the BATTLE LOG block lives in a function
        # reachable only from exportBattleReport's own ladder.
        allowed = {"battleProbeThenPost", "battlePostReport"}
        for fn in allowed:
            self.assertIn("WebRequest.", self._body(self.src, fn))
        section_start = self.src.index("function battleWebStatusCode(")
        section_end = self.src.index("\nfunction battleSetReportMode(")
        section = self.src[section_start:section_end]
        # Every function in this section that calls WebRequest must be one of
        # the allowed two -- a stray third call site would be a regression.
        for fn_match in re.finditer(r"function (\w+)\(.*?\n(.*?)\nend", section, re.S):
            name, body = fn_match.group(1), fn_match.group(2)
            if "WebRequest." in body:
                self.assertIn(name, allowed, f"unexpected WebRequest call in {name}")


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


@contextlib.contextmanager
def _live_server(**overrides):
    """A real ThreadingHTTPServer on 127.0.0.1:0, torn down on exit.

    Sets SRV.ReportHandler's class attributes directly rather than going
    through resolve_config()/main() -- this is what main() does at boot, just
    with a Config built by hand so each test controls exactly what it needs
    (a tiny store cap, a short TTL, a low rate limit) without touching argv or
    the environment.
    """
    defaults = dict(
        host="127.0.0.1", port=0, mode="hosted", out_dir=Path("unused"),
        open_browser=False, dev_reload=False, public_base_url="", token="",
        max_body_bytes=SRV.HOSTED_MAX_BODY_BYTES, store_dir="",
        report_max=500, store_max_bytes=32 * 1024 * 1024,
        ttl_seconds=900.0, ttl_under_load=120.0, high_watermark=0.8,
        download_grace=60.0, rate_window=600.0,
        rate_limit_ip=1000, rate_limit_steam=1000,
    )
    defaults.update(overrides)
    config = SRV.Config(**defaults)

    SRV.CONFIG = config
    SRV.ReportHandler.config = config
    SRV.ReportHandler.store = RS.ReportStore(
        directory=config.store_dir or None, max_reports=config.report_max,
        max_bytes=config.store_max_bytes, ttl_seconds=config.ttl_seconds,
        ttl_under_load=config.ttl_under_load, high_watermark=config.high_watermark,
        download_grace=config.download_grace)
    SRV.ReportHandler.ip_limiter = SRV.RateLimiter(config.rate_limit_ip, config.rate_window)
    SRV.ReportHandler.steam_limiter = SRV.RateLimiter(config.rate_limit_steam, config.rate_window)

    server = SRV.ThreadingHTTPServer((config.host, config.port), SRV.ReportHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(base, path, body, headers=None):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers=headers or {})
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read())
        except (ValueError, UnicodeDecodeError):
            data = None
        return exc.code, data, dict(exc.headers)


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class TestReportServerHosted(unittest.TestCase):
    """Live HTTP against a real server, in hosted mode -- this is Step 8's LAN
    rehearsal, minus the LAN: the same code path a Render deploy runs."""

    def _post_log(self, base, **headers):
        body = json.dumps(make_log()).encode("utf-8")
        headers = {"Content-Type": "application/json", **headers}
        return _post(base, SRV.REPORT_PATH, body, headers)

    def test_a_hosted_export_returns_view_and_download_links(self):
        with _live_server() as base:
            code, data, _ = self._post_log(base)
            self.assertEqual(code, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["mode"], "hosted")
            self.assertIsNone(data["path"])
            self.assertTrue(data["url"].startswith(base + SRV.VIEW_PREFIX))
            self.assertTrue(data["download_url"].startswith(base + SRV.DOWNLOAD_PREFIX))
            # Not exactly 900: expires_in is measured a moment after put() set
            # the deadline, so a few milliseconds have already elapsed.
            self.assertAlmostEqual(data["expires_in"], 900, delta=2)

    def test_the_view_url_serves_the_report_with_a_matching_csp_nonce(self):
        with _live_server() as base:
            _, data, _ = self._post_log(base)
            code, body, headers = _get(base, urllib.request.urlparse(data["url"]).path)
            self.assertEqual(code, 200)
            text = body.decode("utf-8")
            self.assertIn("Battle Report", text)
            self.assertIn("temporarily hosted copy", text)   # the injected banner
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            csp = headers.get("Content-Security-Policy", "")
            m = re.search(r"nonce-([A-Za-z0-9_-]+)", csp)
            self.assertIsNotNone(m, "no nonce in CSP header")
            self.assertIn(f'nonce="{m.group(1)}"', text)

    def test_the_download_url_offers_a_sane_filename_and_identical_bytes(self):
        with _live_server() as base:
            _, data, _ = self._post_log(base)
            _, view_body, _ = _get(base, urllib.request.urlparse(data["url"]).path)
            code, dl_body, headers = _get(base, urllib.request.urlparse(data["download_url"]).path)
            self.assertEqual(code, 200)
            self.assertEqual(dl_body, view_body)
            disposition = headers.get("Content-Disposition", "")
            self.assertIn("attachment", disposition)
            self.assertRegex(disposition, r'filename="battle-report-[\w.-]+\.html"')

    def test_a_bad_token_is_rejected(self):
        with _live_server(token="secret123") as base:
            code, data, _ = self._post_log(base, **{"X-LCT-Token": "wrong"})
            self.assertEqual(code, 401)
            code, data, _ = self._post_log(base)   # missing entirely
            self.assertEqual(code, 401)
            code, data, _ = self._post_log(base, **{"X-LCT-Token": "secret123"})
            self.assertEqual(code, 200)

    def test_an_oversized_body_is_rejected(self):
        with _live_server(max_body_bytes=100) as base:
            code, data, _ = self._post_log(base)
            self.assertEqual(code, 413)

    def test_an_unknown_view_id_is_404(self):
        with _live_server() as base:
            code, body, _ = _get(base, SRV.VIEW_PREFIX + "deadbeef")
            self.assertEqual(code, 404)
            self.assertIn("not found", json.loads(body)["error"])

    def test_healthz_is_ok(self):
        with _live_server() as base:
            code, body, _ = _get(base, SRV.HEALTH_PATH)
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["ok"])

    def test_a_full_store_answers_503_with_retry_after(self):
        with _live_server(report_max=1) as base:
            code, _, _ = self._post_log(base)
            self.assertEqual(code, 200)
            code, data, headers = self._post_log(base)
            self.assertEqual(code, 503)
            self.assertIn("busy", data["error"])
            self.assertIn("Retry-After", headers)

    def test_a_download_shortens_the_view_links_remaining_life(self):
        with _live_server(ttl_seconds=900.0, download_grace=0.2) as base:
            _, data, _ = self._post_log(base)
            view_path = urllib.request.urlparse(data["url"]).path
            dl_path = urllib.request.urlparse(data["download_url"]).path
            code, _, _ = _get(base, dl_path)   # triggers mark_downloaded
            self.assertEqual(code, 200)
            time.sleep(0.3)
            code, body, _ = _get(base, view_path)
            self.assertEqual(code, 404)
            self.assertIn("expired", json.loads(body)["error"])

    def test_ip_only_rate_limiting_when_no_steam_header_is_sent(self):
        with _live_server(rate_limit_ip=1, rate_limit_steam=1000) as base:
            code, _, _ = self._post_log(base)
            self.assertEqual(code, 200)
            code, data, headers = self._post_log(base)
            self.assertEqual(code, 429)
            self.assertIn("Retry-After", headers)

    def test_distinct_steam_ids_are_not_throttled_by_a_shared_ip(self):
        with _live_server(rate_limit_ip=1000, rate_limit_steam=1) as base:
            code, _, _ = self._post_log(base, **{"X-LCT-Steam-Id": "111"})
            self.assertEqual(code, 200)
            # Same IP (it's all localhost), different Steam id -- allowed.
            code, _, _ = self._post_log(base, **{"X-LCT-Steam-Id": "222"})
            self.assertEqual(code, 200)
            # Reusing "111" hits its own bucket's limit of 1.
            code, data, _ = self._post_log(base, **{"X-LCT-Steam-Id": "111"})
            self.assertEqual(code, 429)


class TestReportServerLocalMode(unittest.TestCase):
    """Local mode keeps writing report/ + opening a browser, unchanged -- and
    also populates the store, which is what lets hosted mode be rehearsed
    against a plain local run (see the plan's Step 8)."""

    def test_local_mode_still_writes_a_file_and_also_populates_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _live_server(mode="local", out_dir=Path(tmp)) as base:
                code, data, _ = _post(base, SRV.REPORT_PATH,
                                      json.dumps(make_log()).encode("utf-8"),
                                      {"Content-Type": "application/json"})
                self.assertEqual(code, 200)
                self.assertEqual(data["mode"], "local")
                self.assertIsNotNone(data["path"])
                self.assertTrue(Path(data["path"]).exists())
                # The hosted-style links work too, from the very same export.
                code, body, _ = _get(base, urllib.request.urlparse(data["url"]).path)
                self.assertEqual(code, 200)
                self.assertNotIn("temporarily hosted copy", body.decode("utf-8"),
                                 "local mode must not get the hosted banner")

    def test_open_browser_is_gated_by_config(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(SRV.webbrowser, "open") as m_open, \
                 _live_server(mode="local", out_dir=Path(tmp), open_browser=True) as base:
                code, _, _ = _post(base, SRV.REPORT_PATH,
                                   json.dumps(make_log()).encode("utf-8"),
                                   {"Content-Type": "application/json"})
                self.assertEqual(code, 200)
            self.assertEqual(m_open.call_count, 1)

            with mock.patch.object(SRV.webbrowser, "open") as m_open, \
                 _live_server(mode="local", out_dir=Path(tmp), open_browser=False) as base:
                code, _, _ = _post(base, SRV.REPORT_PATH,
                                   json.dumps(make_log()).encode("utf-8"),
                                   {"Content-Type": "application/json"})
                self.assertEqual(code, 200)
            self.assertEqual(m_open.call_count, 0)


class TestRendererGating(unittest.TestCase):
    """dev_reload is what tells renderer() whether it is safe to hot-reload --
    on in local mode (the whole point is editing the renderer while a game is
    running), off in hosted mode (reloading under a live ThreadingHTTPServer is
    a race, and a half-written edit has no business reaching a real player)."""

    def test_renderer_reloads_only_when_dev_reload_is_true(self):
        import unittest.mock as mock
        original_config = SRV.CONFIG
        try:
            with mock.patch.object(SRV.importlib, "reload",
                                   wraps=SRV.importlib.reload) as m_reload:
                SRV.CONFIG = SRV.Config(dev_reload=False)
                SRV.renderer()
                m_reload.assert_not_called()

                SRV.CONFIG = SRV.Config(dev_reload=True)
                SRV.renderer()
                m_reload.assert_called_once_with(SRV.BR)
        finally:
            SRV.CONFIG = original_config


class TestResolveConfig(unittest.TestCase):
    """CLI args win, then the matching env var, then the literal default."""

    def _args(self, **overrides):
        base = dict(host=None, port=None, mode=None, out=None, no_open=False,
                   public_base_url=None, token=None, store_dir=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def setUp(self):
        # Every LCT_REPORT_* / PORT / HOST env var this reads, cleared for the
        # duration of the test so the real environment cannot leak in.
        self._env_keys = ("PORT", "HOST", "LCT_REPORT_MODE", "LCT_PUBLIC_BASE_URL",
                          "LCT_REPORT_TOKEN", "LCT_REPORT_STORE_DIR", "LCT_REPORT_MAX",
                          "LCT_REPORT_TTL", "LCT_MAX_BODY_BYTES", "LCT_REPORT_TTL_UNDER_LOAD",
                          "LCT_REPORT_STORE_HIGH_WATERMARK", "LCT_REPORT_DOWNLOAD_GRACE",
                          "LCT_REPORT_RATE_WINDOW", "LCT_REPORT_RATE_LIMIT",
                          "LCT_REPORT_RATE_STEAM_LIMIT", "LCT_REPORT_STORE_MAX_BYTES")
        self._saved = {k: os.environ.pop(k, None) for k in self._env_keys}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_with_nothing_set(self):
        config = SRV.resolve_config(self._args())
        self.assertEqual(config.mode, "local")
        self.assertEqual(config.host, SRV.DEFAULT_HOST)
        self.assertEqual(config.port, SRV.DEFAULT_PORT)
        self.assertEqual(config.max_body_bytes, SRV.MAX_BODY_BYTES)
        self.assertTrue(config.open_browser)
        self.assertTrue(config.dev_reload)

    def test_env_vars_are_read_when_no_cli_arg_is_given(self):
        os.environ["LCT_REPORT_MODE"] = "hosted"
        os.environ["LCT_REPORT_TTL"] = "42"
        config = SRV.resolve_config(self._args())
        self.assertEqual(config.mode, "hosted")
        self.assertEqual(config.ttl_seconds, 42.0)
        self.assertEqual(config.max_body_bytes, SRV.HOSTED_MAX_BODY_BYTES,
                         "hosted mode's body cap must apply once mode comes from the env")
        self.assertFalse(config.open_browser)
        self.assertFalse(config.dev_reload)

    def test_a_cli_arg_overrides_the_matching_env_var(self):
        os.environ["LCT_REPORT_MODE"] = "hosted"
        config = SRV.resolve_config(self._args(mode="local"))
        self.assertEqual(config.mode, "local")

    def test_hosted_mode_never_opens_a_browser_even_without_no_open(self):
        config = SRV.resolve_config(self._args(mode="hosted", no_open=False))
        self.assertFalse(config.open_browser)

    def test_import_has_no_environment_dependent_side_effect(self):
        """test_report_url_matches_the_server_default imports this module and
        reads its constants directly -- they must not depend on the process
        environment, only resolve_config()'s return value may."""
        os.environ["LCT_REPORT_MODE"] = "hosted"
        os.environ["PORT"] = "1"
        try:
            reloaded = importlib.reload(SRV)
            self.assertEqual(reloaded.DEFAULT_PORT, 8787)
            self.assertEqual(reloaded.CONFIG.mode, "local")
        finally:
            importlib.reload(SRV)   # restore a clean module for later tests


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_the_limit_then_denies(self):
        clock = _FakeClock()
        limiter = SRV.RateLimiter(2, 60, now=clock)
        self.assertEqual(limiter.check("a")[0], True)
        self.assertEqual(limiter.check("a")[0], True)
        allowed, retry_after = limiter.check("a")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_different_keys_have_independent_budgets(self):
        clock = _FakeClock()
        limiter = SRV.RateLimiter(1, 60, now=clock)
        self.assertTrue(limiter.check("a")[0])
        self.assertTrue(limiter.check("b")[0])
        self.assertFalse(limiter.check("a")[0])

    def test_the_window_slides(self):
        clock = _FakeClock()
        limiter = SRV.RateLimiter(1, 60, now=clock)
        self.assertTrue(limiter.check("a")[0])
        self.assertFalse(limiter.check("a")[0])
        clock.advance(61)
        self.assertTrue(limiter.check("a")[0])


class TestRenderDeployment(unittest.TestCase):
    """render.yaml and requirements.txt, regex-parsed (no PyYAML dependency)."""

    @classmethod
    def setUpClass(cls):
        cls.render_yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
        cls.requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    def test_start_command_names_the_script_and_binds_every_interface(self):
        m = re.search(r'startCommand:\s*"([^"]+)"', self.render_yaml)
        self.assertIsNotNone(m, "no startCommand in render.yaml")
        command = m.group(1)
        self.assertIn("battle_report_server.py", command)
        self.assertIn("--host 0.0.0.0", command)
        self.assertIn("$PORT", command)
        self.assertIn("--mode hosted", command)

    def test_health_check_path_matches_the_server(self):
        m = re.search(r"healthCheckPath:\s*(\S+)", self.render_yaml)
        self.assertIsNotNone(m, "no healthCheckPath in render.yaml")
        self.assertEqual(m.group(1), SRV.HEALTH_PATH)

    def test_runtime_is_python_on_the_free_plan(self):
        self.assertIn("runtime: python", self.render_yaml)
        self.assertIn("plan: free", self.render_yaml)

    def test_the_token_is_never_committed(self):
        # sync: false means "set this in the dashboard" -- a literal token
        # value here would ship a secret in the repo.
        block = self.render_yaml[self.render_yaml.index("LCT_REPORT_TOKEN"):]
        block = block[:block.index("\n      - key:") if "\n      - key:" in block else len(block)]
        self.assertIn("sync: false", block)

    def test_requirements_txt_has_no_real_dependencies(self):
        # A comment-only file: Render's Python runtime detection needs the
        # file to exist, but this project is stdlib-only -- a real entry here
        # would be an accidental dependency nobody meant to add.
        real_lines = [line for line in self.requirements.splitlines()
                     if line.strip() and not line.strip().startswith("#")]
        self.assertEqual(real_lines, [])


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
    """What the capture still has to identify now that terrain comes from the map."""

    @staticmethod
    def piece(**kw):
        base = {"g": "x", "n": "", "t": [], "x": 0.0, "z": 0.0,
                "ry": 0, "sx": 1, "sz": 1, "bx": 2.0, "bz": 2.0}
        base.update(kw)
        return base

    def test_an_untagged_battlemat_is_not_drawn_as_terrain(self):
        # A good number of shipped maps leave the mat untagged. Classified as
        # terrain it becomes a board-sized slab covering the entire report.
        mat = self.piece(bx=29.99, bz=21.97)
        self.assertEqual(BR.classify_terrain(mat, (59.79, 43.71)), BR.KIND_SURFACE)

    def test_a_tagged_battlemat_is_a_surface(self):
        mat = self.piece(t=["battlemaster_battlemat"], bx=2.0, bz=2.0)
        self.assertEqual(BR.classify_terrain(mat, (60.0, 44.0)), BR.KIND_SURFACE)

    def test_a_physical_marker_is_recognised_by_its_nickname(self):
        # The objective markers are spawned separately from the map payload, so
        # the capture is the only place they exist.
        for nick in ("Home Objective", "Center Objective", "Expansion Objective"):
            self.assertEqual(BR.classify_terrain(self.piece(n=nick, bx=1.0, bz=1.0)),
                             BR.KIND_OBJECTIVE, nick)

    def test_a_combo_piece_is_labelled_with_both_of_its_names(self):
        label = BR.terrain_label(self.piece(t=["Short Barrier", "Tower"], d="Dense"))
        self.assertIn("Short Barrier", label)
        self.assertIn("Tower", label)

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

    def test_an_objective_area_is_labelled_with_its_objective(self):
        # This label ends up on the marker when no physical one was captured, so
        # "terrain area" on an objective would be actively misleading.
        self.assertEqual(BR.terrain_label(self.piece(t=["obj_home_red"])),
                         "obj_home_red")


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
        self.source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
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

    def test_it_no_longer_sniffs_the_mesh_for_a_plate_shape(self):
        # Terrain geometry comes from the map's shipped payload now, so the
        # capture has no reason to open getCustomObject(). Leaving the sniff in
        # would invite the old bounds-guessing path back.
        self.assertNotIn("5mm%-border", self.body)
        self.assertNotIn("sh = sh,", self.body)

    def test_it_records_the_mirror_rotations(self):
        # Battlemaster mirrors a plate with a 180 on x or z rather than a negative
        # scale, and the payload reader relies on those being real rotations.
        self.assertIn("rx = rx,", self.body)
        self.assertIn("rz = rz,", self.body)


class TestPlateOutlines(unittest.TestCase):
    """Terrain areas draw their real footprint, taken from the map's own payload.

    These are the tests that pin the placement convention. Sixteen combinations of
    axis flips and yaw sign were plausible; exactly one puts every plate of every
    shipped map on the board, and the runner-up misses by 1152 square inches. Any
    of the others reads as "mostly fine" by eye while mirroring the trapezoid, so
    the invariant below is what actually keeps this honest.
    """

    @staticmethod
    def ring_area(points):
        n = len(points)
        return abs(sum(points[i][0] * points[(i + 1) % n][1]
                       - points[(i + 1) % n][0] * points[i][1]
                       for i in range(n))) / 2

    @classmethod
    def clip_to_board(cls, poly, half_w, half_h):
        """Sutherland-Hodgman. Exact, and fast enough to run over every map."""
        edges = (
            (lambda p: p[0] >= -half_w,
             lambda a, b: (-half_w, a[1] + (b[1] - a[1]) * (-half_w - a[0]) / (b[0] - a[0]))),
            (lambda p: p[0] <= half_w,
             lambda a, b: (half_w, a[1] + (b[1] - a[1]) * (half_w - a[0]) / (b[0] - a[0]))),
            (lambda p: p[1] >= -half_h,
             lambda a, b: (a[0] + (b[0] - a[0]) * (-half_h - a[1]) / (b[1] - a[1]), -half_h)),
            (lambda p: p[1] <= half_h,
             lambda a, b: (a[0] + (b[0] - a[0]) * (half_h - a[1]) / (b[1] - a[1]), half_h)),
        )
        for inside, cross in edges:
            out = []
            for i in range(len(poly)):
                a, b = poly[i - 1], poly[i]
                if inside(b):
                    if not inside(a):
                        out.append(cross(a, b))
                    out.append(b)
                elif inside(a):
                    out.append(cross(a, b))
            poly = out
            if not poly:
                return []
        return poly

    def test_every_plate_of_every_shipped_map_has_a_known_outline(self):
        """A gap here means some map silently loses all of its terrain."""
        guids = sorted(MP.manifest_card_guids())
        self.assertTrue(guids, "no map cards in the manifest")
        unresolved = [g for g in guids if MT.map_terrain(g) is None]
        self.assertEqual(unresolved, [],
                         f"{len(unresolved)} manifest maps have an unknown plate mesh")

    def test_no_plate_hangs_off_the_board(self):
        """The invariant that identifies the one correct placement convention.

        A plate is part of a designed layout; none of them overhang the table. So
        any flip or sign error shows up here as area outside the board rectangle,
        and the correct convention scores exactly zero.
        """
        total_outside = 0.0
        hanging = []
        plates = 0
        for guid in sorted(MP.manifest_card_guids()):
            terrain = MT.map_terrain(guid)
            self.assertIsNotNone(terrain, guid)
            width, height = terrain["board"] or (60.0, 44.0)
            for plate in terrain["plates"]:
                points = [tuple(pt) for pt in plate["points"]]
                plates += 1
                inside = self.clip_to_board(points, width / 2, height / 2)
                outside = self.ring_area(points) - (
                    self.ring_area(inside) if len(inside) >= 3 else 0.0)
                if outside > 0.05:
                    hanging.append((guid, plate["shape"], round(outside, 2)))
                total_outside += max(0.0, outside)
        self.assertGreater(plates, 3000, "expected thousands of plates to check")
        self.assertEqual(hanging, [], f"{len(hanging)} plates hang off the board")
        self.assertAlmostEqual(total_outside, 0.0, places=3)

    def test_the_mesh_x_axis_is_negated(self):
        """.obj is right-handed and TTS is left-handed.

        Dropping this flip mirrors every asymmetric plate -- which looks fine for
        the rectangles, and put the trapezoid the wrong way round.
        """
        ring = [(0.0, 0.0), (2.0, 0.0), (2.0, 1.0)]
        placed = MT.place_ring(ring, {"posX": 0, "posZ": 0, "rotY": 0})
        self.assertEqual([p[0] for p in placed], [0.0, -2.0, -2.0])
        self.assertEqual([p[1] for p in placed], [0.0, 0.0, 1.0])

    def test_a_180_about_z_mirrors_the_x_axis_back(self):
        ring = [(0.0, 0.0), (2.0, 0.0)]
        placed = MT.place_ring(ring, {"posX": 0, "posZ": 0, "rotY": 0, "rotZ": 180})
        self.assertEqual([p[0] for p in placed], [0.0, 2.0])

    def test_a_180_about_x_mirrors_the_z_axis(self):
        ring = [(0.0, 0.0), (0.0, 3.0)]
        placed = MT.place_ring(ring, {"posX": 0, "posZ": 0, "rotY": 0, "rotX": 180})
        self.assertEqual([p[1] for p in placed], [0.0, -3.0])

    def test_a_yaw_rotates_the_outline(self):
        # 90 degrees of TTS yaw takes local +x to board -z.
        placed = MT.place_ring([(1.0, 0.0)], {"posX": 0, "posZ": 0, "rotY": 90})
        self.assertAlmostEqual(placed[0][0], 0.0, places=6)
        self.assertAlmostEqual(placed[0][1], 1.0, places=6)

    def test_the_trapezoid_keeps_its_handedness(self):
        """The plate the maps call a triangle is really a right trapezoid.

        Negating one axis flips a ring's signed area, so this is exactly the
        quantity the mirroring bug changed. Pinned so the x flip above cannot be
        quietly dropped again.
        """
        shapes, _ = MT.load_outlines()
        placed = MT.place_ring(shapes["triangle"], {"posX": 0, "posZ": 0, "rotY": 0})
        n = len(placed)
        signed = sum(placed[i][0] * placed[(i + 1) % n][1]
                     - placed[(i + 1) % n][0] * placed[i][1] for i in range(n)) / 2
        self.assertGreater(abs(signed), 40, "not the trapezoid we think it is")
        self.assertLess(signed, 0, "the trapezoid came out mirrored")

    def test_a_symmetric_map_places_its_objective_plates_symmetrically(self):
        """End to end on a real map, with the layout's own symmetry as the check.

        Tipping Point is 180-degree rotationally symmetric, so every objective
        plate must have a partner that is its exact point reflection. A mirror or
        sign error breaks that pairing without moving anything off the board.
        """
        terrain = MT.map_terrain("ff5fec")   # PtF vs Dis 2 - Tipping Point - T5S2
        self.assertIsNotNone(terrain)
        tagged = [p for p in terrain["plates"] if p["tags"]]
        self.assertEqual(len(tagged), 6)
        for plate in tagged:
            reflected = sorted((-x, -z) for x, z in plate["points"])
            self.assertTrue(
                any(self._rings_match(reflected, sorted(map(tuple, other["points"])))
                    for other in tagged),
                f"{plate['tags']} has no symmetric partner")

    @staticmethod
    def _rings_match(a, b, tol=0.01):
        return len(a) == len(b) and all(
            abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol for p, q in zip(a, b))

    def test_a_map_with_no_payload_reports_terrain_as_unknown(self):
        log = make_log()
        log["game"]["mapGuid"] = "zzzzzz"
        report = BR.build_report(log)
        self.assertFalse(report["board"]["terrain_known"])
        self.assertEqual(report["board"]["terrain"], [])

    def test_a_real_map_fills_the_board_terrain(self):
        log = make_log()
        log["game"]["mapGuid"] = "ff5fec"
        report = BR.build_report(log)
        self.assertTrue(report["board"]["terrain_known"])
        self.assertEqual(len(report["board"]["terrain"]), 16)
        for plate in report["board"]["terrain"]:
            self.assertGreaterEqual(len(plate["points"]), 3)

    def test_the_renderer_draws_areas_and_nothing_else(self):
        log = make_log()
        log["game"]["mapGuid"] = "ff5fec"
        report = BR.build_report(log)
        svg = BR.render_board_svg(report, report["frames"][0])
        self.assertEqual(svg.count("t-area"), 16)
        self.assertNotIn("t-light", svg)
        self.assertNotIn("t-dense", svg)


class TestTerrainCache(unittest.TestCase):
    """data/terrain_cache.json is what a hosted report server reads instead of
    the 26 MB of raw data/maps/ payloads -- this class is what keeps it honest.
    """

    def test_the_checked_in_cache_is_not_stale(self):
        """This is bake_terrain_cache.py --check, run as a test.

        A map added or changed by scripts/sync_battlemaster_maps.py without a
        rebaked cache would otherwise ship a hosted server that silently draws
        less terrain than the payloads it was generated from -- this is the one
        thing that catches that before it reaches production.
        """
        fresh, _ = BTC.build()
        self.assertEqual(
            json.loads(BTC.CACHE_PATH.read_text(encoding="utf-8"))["maps"], fresh,
            "data/terrain_cache.json is stale -- rerun scripts/bake_terrain_cache.py")

    def test_every_cached_map_reproduces_map_terrain_computed_from_its_payload(self):
        for guid in sorted(MT.load_cache()):
            cached = MT.map_terrain(guid, use_cache=True)
            fresh = MT.map_terrain(guid, use_cache=False)
            self.assertEqual(cached, fresh, guid)

    def test_map_terrain_still_works_with_the_cache_disabled(self):
        terrain = MT.map_terrain("ff5fec", use_cache=False)
        self.assertIsNotNone(terrain)
        self.assertEqual(len(terrain["plates"]), 16)

    def test_a_guid_absent_from_the_cache_falls_back_to_the_payload(self):
        guid = next(iter(MT.load_cache()))
        trimmed = dict(MT.load_cache())
        del trimmed[guid]
        original = MT._cache
        MT._cache = trimmed
        try:
            self.assertEqual(MT.map_terrain(guid, use_cache=True),
                              MT.map_terrain(guid, use_cache=False))
        finally:
            MT._cache = original

    def test_the_three_unresolvable_maps_are_still_unresolvable(self):
        """Pins the count so a real regression (a newly-broken mesh lookup)
        cannot hide behind "well, some maps are always unresolved"."""
        _, unresolved = BTC.build()
        self.assertEqual(len(unresolved), 3)

    def test_a_missing_cache_file_behaves_like_an_empty_one(self):
        original = MT._cache
        MT._cache = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(MT.load_cache(Path(tmp) / "missing.json"), {})
        finally:
            MT._cache = original


class _FakeClock:
    """An injectable now() for report_store tests -- no real sleeping needed."""

    def __init__(self, start=1_000_000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TestReportStore(unittest.TestCase):
    """report_store.ReportStore, exercised with no HTTP involved."""

    def test_ids_are_short_urlsafe_and_unique(self):
        store = RS.ReportStore(max_reports=50)
        ids = {store.put("<html>x</html>") for _ in range(50)}
        self.assertEqual(len(ids), 50, "collision, or an id was reused")
        for report_id in ids:
            self.assertRegex(report_id, r"\A[A-Za-z0-9_-]{6,64}\Z")

    def test_put_get_round_trip(self):
        store = RS.ReportStore()
        report_id = store.put("<html>hello</html>", nonce="abc123",
                               meta={"map": "Tipping Point"})
        entry = store.get(report_id)
        self.assertIsNotNone(entry)
        self.assertEqual(gzip.decompress(entry.gz).decode("utf-8"),
                         "<html>hello</html>")
        self.assertEqual(entry.nonce, "abc123")
        self.assertEqual(entry.meta, {"map": "Tipping Point"})

    def test_unknown_id_is_a_miss(self):
        store = RS.ReportStore()
        store.put("<html></html>")
        self.assertIsNone(store.get("nosuchid"))

    def test_put_accepts_a_caller_supplied_id(self):
        store = RS.ReportStore()
        report_id = store.put("<html></html>", report_id="my-custom-id")
        self.assertEqual(report_id, "my-custom-id")
        self.assertIsNotNone(store.get("my-custom-id"))

    def test_put_rejects_a_caller_supplied_id_that_collides(self):
        store = RS.ReportStore()
        store.put("<html>1</html>", report_id="dupe-id")
        with self.assertRaises(ValueError):
            store.put("<html>2</html>", report_id="dupe-id")

    def test_put_rejects_an_invalid_caller_supplied_id(self):
        store = RS.ReportStore()
        with self.assertRaises(ValueError):
            store.put("<html></html>", report_id="../escape")

    def test_status_distinguishes_missing_from_expired(self):
        clock = _FakeClock()
        store = RS.ReportStore(ttl_seconds=10, now=clock)
        report_id = store.put("<html></html>")
        self.assertEqual(store.status(report_id), "ok")
        self.assertEqual(store.status("nosuchid"), "missing")
        clock.advance(11)
        self.assertEqual(store.status(report_id), "expired")

    def test_status_does_not_discard_an_expired_entry(self):
        """A peek, not a side effect -- get()/prune() still own eviction."""
        clock = _FakeClock()
        store = RS.ReportStore(ttl_seconds=10, now=clock)
        report_id = store.put("<html></html>")
        clock.advance(11)
        store.status(report_id)
        self.assertEqual(len(store), 1, "status() must not have evicted the entry")

    def test_ids_do_not_escape_the_store_dir(self):
        """The traversal guard. This test must never be skipped or weakened."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RS.ReportStore(directory=tmp)
            store.put("<html></html>")
            before = sorted(Path(tmp).iterdir())
            malformed = ("../../etc/passwd", "a/b", "", "..", ".", "/etc/passwd", "a" * 200)
            for hostile in malformed + ("valid-but-unknown-id",):
                self.assertIsNone(store.get(hostile), repr(hostile))
                self.assertEqual(store.status(hostile), "missing", repr(hostile))
            for hostile in malformed:
                with self.assertRaises(ValueError, msg=repr(hostile)):
                    store.put("<html></html>", report_id=hostile)
            # No file was read or written anywhere as a side effect of the lookups.
            self.assertEqual(sorted(Path(tmp).iterdir()), before)

    def test_put_raises_storefull_at_the_count_cap(self):
        store = RS.ReportStore(max_reports=2)
        store.put("<html>1</html>")
        store.put("<html>2</html>")
        with self.assertRaises(RS.StoreFull):
            store.put("<html>3</html>")
        self.assertEqual(len(store), 2)

    def test_put_raises_storefull_at_the_byte_cap(self):
        store = RS.ReportStore(max_reports=1000, max_bytes=1)
        with self.assertRaises(RS.StoreFull):
            store.put("<html>" + "x" * 1000 + "</html>")

    def test_a_full_store_does_not_evict_a_live_entry_to_make_room(self):
        store = RS.ReportStore(max_reports=1)
        kept_id = store.put("<html>keep me</html>")
        with self.assertRaises(RS.StoreFull):
            store.put("<html>should not fit</html>")
        entry = store.get(kept_id)
        self.assertIsNotNone(entry, "the live entry was evicted instead of refused")
        self.assertEqual(gzip.decompress(entry.gz).decode("utf-8"), "<html>keep me</html>")

    def test_an_expired_entry_reads_as_a_miss_and_prune_frees_its_slot(self):
        clock = _FakeClock()
        store = RS.ReportStore(max_reports=1, ttl_seconds=10, now=clock)
        first_id = store.put("<html>1</html>")
        clock.advance(11)
        self.assertIsNone(store.get(first_id), "an expired entry must read as a miss")
        # The slot was not freed by get() alone -- a fresh put still has to prune.
        second_id = store.put("<html>2</html>")  # would raise StoreFull if still full
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(store), 1)

    def test_prune_returns_the_count_it_removed(self):
        clock = _FakeClock()
        store = RS.ReportStore(max_reports=10, ttl_seconds=10, now=clock)
        store.put("<html>1</html>")
        store.put("<html>2</html>")
        clock.advance(11)
        self.assertEqual(store.prune(), 2)
        self.assertEqual(len(store), 0)

    def test_disk_backed_store_survives_a_rebuild(self):
        """Simulates a process restart: a fresh ReportStore over the same dir."""
        clock = _FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            store_a = RS.ReportStore(directory=tmp, ttl_seconds=900, now=clock)
            report_id = store_a.put("<html>durable</html>", nonce="n1",
                                    meta={"map": "Test Map"})

            store_b = RS.ReportStore(directory=tmp, ttl_seconds=900, now=clock)
            entry = store_b.get(report_id)   # memory miss on store_b -> reads through
            self.assertIsNotNone(entry)
            self.assertEqual(gzip.decompress(entry.gz).decode("utf-8"), "<html>durable</html>")
            self.assertEqual(entry.nonce, "n1")
            self.assertEqual(entry.meta, {"map": "Test Map"})

    def test_disk_backed_store_does_not_resurrect_an_expired_entry(self):
        clock = _FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            store_a = RS.ReportStore(directory=tmp, ttl_seconds=10, now=clock)
            report_id = store_a.put("<html>gone soon</html>")
            clock.advance(11)

            store_b = RS.ReportStore(directory=tmp, ttl_seconds=10, now=clock)
            self.assertIsNone(store_b.get(report_id))


class TestReportStoreTTLPolicy(unittest.TestCase):
    """The three TTL mechanisms: base, load-shortened, and post-download."""

    def test_put_below_the_high_watermark_gets_the_full_ttl(self):
        clock = _FakeClock()
        store = RS.ReportStore(max_reports=5, ttl_seconds=900, ttl_under_load=120,
                                high_watermark=0.8, now=clock)
        report_id = store.put("<html></html>")
        entry = store.get(report_id)
        self.assertEqual(entry.expires - entry.created, 900)

    def test_put_at_or_above_the_high_watermark_gets_the_shortened_ttl(self):
        clock = _FakeClock()
        store = RS.ReportStore(max_reports=5, ttl_seconds=900, ttl_under_load=120,
                                high_watermark=0.8, now=clock)
        for _ in range(4):
            store.put("<html></html>")   # occupancy before each: 0, .2, .4, .6 -> full TTL
        fifth = store.put("<html></html>")   # occupancy before this one: 4/5 = .8 -> shortened
        entry = store.get(fifth)
        self.assertEqual(entry.expires - entry.created, 120)

    def test_an_existing_entrys_ttl_does_not_change_when_later_puts_raise_the_load(self):
        clock = _FakeClock()
        store = RS.ReportStore(max_reports=5, ttl_seconds=900, ttl_under_load=120,
                                high_watermark=0.8, now=clock)
        first = store.put("<html></html>")
        original_expiry = store.get(first).expires
        for _ in range(3):
            store.put("<html></html>")
        self.assertEqual(store.get(first).expires, original_expiry)

    def test_mark_downloaded_shortens_expires_when_sooner_than_the_current_deadline(self):
        clock = _FakeClock()
        store = RS.ReportStore(ttl_seconds=900, download_grace=60, now=clock)
        report_id = store.put("<html></html>")
        store.mark_downloaded(report_id)
        entry = store.get(report_id)
        self.assertEqual(entry.expires, clock() + 60)

    def test_mark_downloaded_never_extends_a_deadline(self):
        clock = _FakeClock()
        store = RS.ReportStore(ttl_seconds=2, download_grace=1000, now=clock)
        report_id = store.put("<html></html>")
        original_expiry = store.get(report_id).expires
        store.mark_downloaded(report_id)
        self.assertEqual(store.get(report_id).expires, original_expiry)

    def test_mark_downloaded_on_an_unknown_id_is_a_silent_no_op(self):
        store = RS.ReportStore()
        store.mark_downloaded("doesnotexist")   # must not raise

    def test_mark_downloaded_on_a_pruned_id_is_a_silent_no_op(self):
        clock = _FakeClock()
        store = RS.ReportStore(ttl_seconds=10, now=clock)
        report_id = store.put("<html></html>")
        clock.advance(11)
        store.prune()
        store.mark_downloaded(report_id)   # must not raise, must not resurrect it
        self.assertIsNone(store.get(report_id))


if __name__ == "__main__":
    unittest.main()
