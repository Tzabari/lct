#!/usr/bin/env python3
"""Tests for the Lua-side battle log: recording, not rendering.

The renderer that turns a recorded log into a report -- battle_report.py and
everything it needs (map_terrain.py, the terrain cache, the HTML/JS viewer) --
moved to the separate lct-report-server repo, along with its own tests. What
stays here is everything that only source-greps TTSLUA/*.ttslua: the recording
API, the cross-script safety pattern the Lua side depends on, and the hosted
export wiring. None of it needs battle_report.py to exist.
"""

import os
import re
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))  # allow sibling imports (compile, lua_strings)

ROOT = SCRIPT_DIR.parent
GLOBAL_LUA = ROOT / "TTSLUA" / "global.ttslua"
START_MENU_LUA = ROOT / "TTSLUA" / "startMenu.ttslua"
TOOLS_LUA = ROOT / "TTSLUA" / "spawnGameTools.ttslua"
PROJECT_CONTEXT_MD = ROOT / "PROJECT_CONTEXT.md"


class TestLuaWiring(unittest.TestCase):
    """Lock the Lua-side contracts the (now separate) renderer depends on.

    There used to be a test here, test_schema_version_matches_the_lua_side,
    asserting BATTLE_LOG_SCHEMA against battle_report.BR.SCHEMA_VERSION. It
    could only ever compare this repo's own copy of the renderer, not the one
    actually deployed -- it passed the whole time lct-report-server's copy sat
    a schema behind this repo's after the split. The real guarantee now is the
    runtime handshake: battleProbeThenPost checks the server's /healthz schema
    field against BATTLE_LOG_SCHEMA before ever uploading. See this repo's
    README and lct-report-server's test suite.
    """

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
        # The local helper (battle_report_server.py) lives in the separate
        # lct-report-server repo now -- 8787/"/report" is its contract, held
        # here as a literal so this test needs nothing from that repo.
        text = GLOBAL_LUA.read_text(encoding="utf-8")
        m = re.search(r'BATTLE_REPORT_URL\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "http://127.0.0.1:8787/report")


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
        self.assertEqual(m.group(1), "http://127.0.0.1:8787/report")

    def test_the_remote_base_marker_is_present_for_compile_py_to_bake(self):
        self.assertIn("-- @@BATTLE_REPORT_REMOTE_BASE@@", self.src)
        self.assertIn("-- @@BATTLE_REPORT_TOKEN@@", self.src)

    def test_the_markers_are_on_project_contexts_preserved_list(self):
        # PROJECT_CONTEXT.md exists specifically to stop a marker from being
        # deleted as apparent litter; this branch added two markers to
        # global.ttslua without adding them to that list.
        context = PROJECT_CONTEXT_MD.read_text(encoding="utf-8", errors="replace")
        self.assertIn("@@BATTLE_REPORT_REMOTE_BASE@@", context)
        self.assertIn("@@BATTLE_REPORT_TOKEN@@", context)

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

    def test_a_missing_marker_fails_a_release_build_but_only_warns_a_test_build(self):
        # The marker is a removable-looking trailing comment on an otherwise
        # complete assignment (see PROJECT_CONTEXT.md's preserved-markers
        # list), so a --release build must not silently ship with the hosted
        # export path disabled because someone stripped it in a tidy-up pass.
        import compile as C
        stripped = self.src.replace(
            'BATTLE_REPORT_REMOTE_BASE = "" -- @@BATTLE_REPORT_REMOTE_BASE@@',
            'BATTLE_REPORT_REMOTE_BASE = ""',
        )
        self.assertNotEqual(stripped, self.src, "fixture did not find the marker line to strip")
        with self.assertRaises(SystemExit):
            C.bake_battle_report_endpoint(stripped, is_test=False, is_release=True)
        out = C.bake_battle_report_endpoint(stripped, is_test=False, is_release=False)
        self.assertIn("marker @@BATTLE_REPORT_REMOTE_BASE@@ not found", "\n".join(C.WARNINGS))
        self.assertNotIn("@@BATTLE_REPORT_REMOTE_BASE@@", out)

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
        # "/healthz" is lct-report-server's contract (battle_report_server.py's
        # HEALTH_PATH), held here as a literal now that repo is separate.
        m = re.search(r'BATTLE_REPORT_HEALTH\s*=\s*"([^"]+)"', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/healthz")

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

    def test_each_attempt_has_its_own_watchdog(self):
        # Never depend on TTS's undocumented WebRequest timeout -- each network
        # attempt arms its own Wait.time so a slow/absent callback cannot hang
        # the ladder. The probe and the POST use separate constants (not a
        # shared one) because they need opposite tuning: /healthz is tiny and
        # should fail fast, while a several-MB POST must not be mistaken for
        # hung just because it is still uploading.
        probe_body = self._body(self.src, "battleProbeThenPost")
        self.assertIn("Wait.time", probe_body)
        self.assertIn("BATTLE_EXPORT_PROBE_TIMEOUT", probe_body)
        post_body = self._body(self.src, "battlePostReport")
        self.assertIn("Wait.time", post_body)
        self.assertIn("BATTLE_EXPORT_POST_TIMEOUT", post_body)

    def test_a_post_timeout_does_not_resend_the_body(self):
        # WebRequests can't be cancelled, so once the body has actually been
        # handed to WebRequest.custom, a watchdog firing must not re-enter the
        # retry ladder -- that would resend the whole report on top of the copy
        # already in flight. Only battleHandlePostResult (which runs only once
        # the request has genuinely finished, with an error or a definite HTTP
        # status) is allowed to do that.
        post_body = self._body(self.src, "battlePostReport")
        watchdog = post_body[post_body.index("Wait.time"):]
        self.assertNotIn("battleExportLadder", watchdog,
                         "a POST watchdog must not resend the body it already sent")

    def test_the_probe_checks_the_servers_schema_before_posting(self):
        # The handshake that replaces the old cross-repo schema-lockstep test
        # (see TestLuaWiring's docstring): refuse to upload rather than send a
        # log the server has already told us it can't read.
        body = self._body(self.src, "battleProbeThenPost")
        self.assertIn("health.schema", body)
        self.assertIn("BATTLE_LOG_SCHEMA", body)

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
    """captureBoard records only what the report still reads: the fixed board
    size, plus the physical objective markers (terrain comes from the map's own
    shipped payload -- see map_terrain.py -- and nothing has read the rest of a
    full-zone capture since). A measured real game had 47 captured entries of
    which only the markers were ever used; this is what keeps the other ~41 from
    coming back."""

    def setUp(self):
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
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
        source = GLOBAL_LUA.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("BATTLE_BOARD_DESC_MAX", source)


if __name__ == "__main__":
    unittest.main()
