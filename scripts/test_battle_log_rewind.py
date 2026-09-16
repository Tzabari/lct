"""Battle Log tests: dev branch only (see docs/battle-log-rewind-architecture.md
Part C). Two kinds:

- Static source checks (always run): the save/load/destroy hooks are wired
  into global.ttslua and startMenu.ttslua, onSave does no encoding, there's
  no polling, and the companion files use ~= rather than !=.
- Pure-logic tests of the real battleLog.ttslua source, run against lupa's
  bundled Lua 5.2 (lupa.lua52 -- the closest match to TTS's MoonSharp
  dialect). These stub just enough of the TTS API (JSON, getObjectFromGUID,
  Wait, ...) to exercise diffing, backward resolve and branching without a
  live TTS table. Skipped entirely if lupa isn't installed.
"""

import re
import unittest
from pathlib import Path

try:
    import lupa.lua52 as lua52
except ImportError:
    lua52 = None

ROOT = Path(__file__).resolve().parent.parent
LUA_DIR = ROOT / "TTSLUA"


class StaticChecksTest(unittest.TestCase):
    def test_global_hooks_are_wired(self):
        text = (LUA_DIR / "global.ttslua").read_text(encoding="utf-8")
        self.assertIn("battleLogOnDestroy(obj)", text)
        self.assertIn("battleLogOnLoad(", text)
        self.assertIn("battleLogOnSave()", text)

    def test_start_menu_requests_a_capture_at_every_hook(self):
        text = (LUA_DIR / "startMenu.ttslua").read_text(encoding="utf-8")
        count = text.count('Global.call("battleLogRequestCapture"')
        self.assertEqual(count, 4, "expected one battleLogRequestCapture call "
            "each in startGame, jumpToPhase, nextPhase and passTurn, found " + str(count))

    def test_on_save_does_no_encoding(self):
        text = (LUA_DIR / "battleLog.ttslua").read_text(encoding="utf-8")
        m = re.search(r"function battleLogOnSave\(\)(.*?)\nend", text, re.S)
        self.assertIsNotNone(m, "battleLogOnSave() not found")
        self.assertNotIn("JSON.encode", m.group(1),
            "onSave must return the cache, never encode")

    def test_companion_files_have_no_polling(self):
        for name in ("battleLog.ttslua", "battleRewind.ttslua", "battleDebug.ttslua"):
            text = (LUA_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("Wait.repeat", text,
                f"{name}: no polling allowed (principle #2 - button presses only)")

    def test_companion_files_use_tilde_not_bang_equal(self):
        for name in ("battleLog.ttslua", "battleRewind.ttslua", "battleDebug.ttslua"):
            text = (LUA_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("!=", text, f"{name} uses != instead of ~=")

    def test_rewind_hooks_are_wired(self):
        text = (LUA_DIR / "global.ttslua").read_text(encoding="utf-8")
        self.assertIn("battleRewindOnDestroy(obj)", text)
        self.assertIn("battleRewindOnLoad(", text)
        self.assertIn("battleRewindOnSave()", text)


# A minimal TTS-API stand-in: just enough for battleLog.ttslua's functions to
# run outside TTS. TTS's own JSON is a C# bridge; this is a small pure-Lua
# encoder/decoder good enough for the plain tables battleLog builds (it only
# has to round-trip its own output, not parse arbitrary JSON).
STUB_PRELUDE = r"""
JSON = {}
local function isArray(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n == #t
end
local encodeValue
encodeValue = function(v)
    local tv = type(v)
    if v == nil then return "null" end
    if tv == "boolean" then return v and "true" or "false" end
    if tv == "number" then return tostring(v) end
    if tv == "string" then return string.format("%q", v) end
    if tv == "table" then
        if isArray(v) then
            local parts = {}
            for i, x in ipairs(v) do parts[i] = encodeValue(x) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local parts = {}
        for k, x in pairs(v) do
            parts[#parts + 1] = string.format("%q", tostring(k)) .. ":" .. encodeValue(x)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    error("cannot encode a " .. tv)
end
function JSON.encode(v) return encodeValue(v) end

local decodeValue
local function skipSpace(s, i)
    while i <= #s and s:sub(i, i):match("%s") do i = i + 1 end
    return i
end
local function decodeString(s, i)
    local out, j = {}, i + 1
    while s:sub(j, j) ~= '"' do
        if s:sub(j, j) == "\\" then j = j + 1 end
        out[#out + 1] = s:sub(j, j)
        j = j + 1
    end
    return table.concat(out), j + 1
end
decodeValue = function(s, i)
    i = skipSpace(s, i)
    local c = s:sub(i, i)
    if c == '"' then return decodeString(s, i) end
    if c == "{" or c == "[" then
        local isObj = c == "{"
        local t, j, idx = {}, i + 1, 1
        j = skipSpace(s, j)
        if s:sub(j, j) == (isObj and "}" or "]") then return t, j + 1 end
        while true do
            local k, v
            if isObj then
                k, j = decodeString(s, j)
                j = skipSpace(s, j) + 1  -- skip ':'
            end
            v, j = decodeValue(s, j)
            if isObj then t[k] = v else t[idx] = v; idx = idx + 1 end
            j = skipSpace(s, j)
            local closer = isObj and "}" or "]"
            if s:sub(j, j) == closer then return t, j + 1 end
            j = skipSpace(s, j + 1)  -- skip ',' and any following space
        end
    end
    if c == "t" then return true, i + 4 end
    if c == "f" then return false, i + 5 end
    if c == "n" then return nil, i + 4 end
    local j = i
    while j <= #s and s:sub(j, j):match("[%d%.%-eE]") do j = j + 1 end
    return tonumber(s:sub(i, j - 1)), j
end
function JSON.decode(s)
    return (decodeValue(s, 1))
end

FAKE_OBJECTS = {}
function getObjectFromGUID(guid) return FAKE_OBJECTS[guid] end
function getAllObjects() return {} end
function broadcastToColor(msg, color, tint) end
function broadcastToAll(msg, tint) end
Player = setmetatable({}, {__index = function() return {getSelectedObjects = function() return {} end} end})
Wait = {frames = function(fn, n) fn() end, time = function(fn, n) end}

startMenu_GUID = "startMenu"
gameTurnCounter_GUID = "roundCounter"
redTurnCounter_GUID = "redTurnCounter"
blueTurnCounter_GUID = "blueTurnCounter"
scoresheet_GUID = "scoresheet"
redSecondaryCardZone_GUIDs = {}
blueSecondaryCardZone_GUIDs = {}
DISCARD_NAME_IGNORE = {}
discardedCardTurns = {Red = {}, Blue = {}}
cpGainRoundUsed = {}

function counterValue(counter)
    if counter and counter.value ~= nil then return counter.value end
    return 0
end
function cpCounterForColor(color) return FAKE_OBJECTS["cp" .. color] end
function getSecondaryDiscardPosition(color) return {x = 0, y = 0, z = 0} end
"""


def make_runtime():
    rt = lua52.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUB_PRELUDE)
    rt.execute((LUA_DIR / "battleLog.ttslua").read_text(encoding="utf-8"))
    return rt


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class StateEqualsTest(unittest.TestCase):
    def setUp(self):
        self.rt = make_runtime()

    def test_within_tolerance_is_equal(self):
        result = self.rt.execute('''
            local a = State.new({x=1, y=2, z=3, rx=0, ry=90, rz=0, name="Model"})
            local b = State.new({x=1.005, y=2, z=3, rx=0, ry=90.3, rz=0, name="Model"})
            return State.equals(a, b)
        ''')
        self.assertTrue(result)

    def test_a_real_move_is_not_equal(self):
        result = self.rt.execute('''
            local a = State.new({x=1, y=2, z=3, rx=0, ry=0, rz=0, name="Model"})
            local b = State.new({x=1.5, y=2, z=3, rx=0, ry=0, rz=0, name="Model"})
            return State.equals(a, b)
        ''')
        self.assertFalse(result)

    def test_off_field_ignores_stale_position(self):
        result = self.rt.execute('''
            local a = State.new({x=1, y=2, z=3, rx=0, ry=0, rz=0, name="Model", off=true})
            local b = State.new({x=99, y=99, z=99, rx=45, ry=45, rz=45, name="Model", off=true})
            return State.equals(a, b)
        ''')
        self.assertTrue(result)

    def test_on_field_vs_off_field_is_never_equal(self):
        result = self.rt.execute('''
            local a = State.new({x=1, y=2, z=3, rx=0, ry=0, rz=0, name="Model", off=false})
            local b = State.new({x=1, y=2, z=3, rx=0, ry=0, rz=0, name="Model", off=true})
            return State.equals(a, b)
        ''')
        self.assertFalse(result)


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class CaptureDiffingTest(unittest.TestCase):
    """A snapshot stores only what changed since its base."""

    def setUp(self):
        self.rt = make_runtime()
        self.rt.execute('''
            FAKE_OBJECTS["m1"] = {
                getPosition = function() return {x=0,y=0,z=0} end,
                getRotation = function() return {x=0,y=0,z=0} end,
                getName = function() return "TestModel" end,
                getGUID = function() return "m1" end,
            }
            battleLogRegistry = {m1 = RegisteredObject.new("m1", "Red")}
            battleLog.army = {Red = {"m1"}, Blue = {}}
        ''')

    def test_first_capture_records_everything(self):
        i1 = self.rt.execute('return battleLogCapture("start")')
        snap = self.rt.execute('return battleLog.snaps[%d].parts.objs.m1 ~= nil' % i1)
        self.assertTrue(snap)

    def test_an_unmoved_model_is_omitted_from_the_next_snapshot(self):
        i1 = self.rt.execute('return battleLogCapture("start")')
        i2 = self.rt.execute('return battleLogCapture("phase")')
        self.assertEqual(i1, i2, "nothing changed, so no new snapshot should be appended")

    def test_a_moved_model_is_the_only_thing_in_the_next_diff(self):
        i1 = self.rt.execute('return battleLogCapture("start")')
        self.rt.execute('FAKE_OBJECTS["m1"].getPosition = function() return {x=5,y=0,z=0} end')
        i2 = self.rt.execute('return battleLogCapture("phase")')
        self.assertNotEqual(i1, i2, "the model moved, so a new snapshot must be appended")
        has_objs, has_turn = self.rt.execute('''
            local p = battleLog.snaps[%d].parts
            return p.objs ~= nil and p.objs.m1 ~= nil, p.turn ~= nil
        ''' % i2)
        self.assertTrue(has_objs)
        self.assertFalse(has_turn, "turn/vp/cp/sec didn't change and must stay omitted")


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class ResolveAndBranchingTest(unittest.TestCase):
    """battleLogResolve walks backward along base links; a rewind lets new
    captures branch off an earlier snapshot without breaking either chain."""

    def setUp(self):
        self.rt = make_runtime()
        self.rt.execute('''
            battleLogRegistry = {a = RegisteredObject.new("a", "Red"), c = RegisteredObject.new("c", "Red")}
            battleLog.snaps = {
                {k={1,"Red",1}, tag="start", b=nil, parts={turn={r=1,t="Red",p=1}, cp={r=0,b=0}, objs={a={0,0,0,0,0,0,"A",0}, c={0,0,0,0,0,0,"C",0}}}},
                {k={1,"Red",2}, tag="phase", b=1,   parts={cp={r=1,b=0}, objs={a={1,0,0,0,0,0,"A",0}, c={9,0,0,0,0,0,"C",0}}}},
                {k={1,"Red",3}, tag="phase", b=2,   parts={objs={a={2,0,0,0,0,0,"A",0}}}},
            }
            battleLog.base = 3
        ''')

    def test_resolve_takes_the_most_recent_value_of_each_thing(self):
        turn_r, cp_r, a_x, c_x = self.rt.execute('''
            local r = battleLogResolve(3)
            return r.turn.r, r.cp.r, r.objs.a[1], r.objs.c[1]
        ''')
        self.assertEqual(turn_r, 1, "turn comes from snapshot 1, the only one that has it")
        self.assertEqual(cp_r, 1, "cp comes from snapshot 2, more recent than snapshot 1's")
        self.assertEqual(a_x, 2, "a's position comes from snapshot 3, the most recent")
        self.assertEqual(c_x, 9, "c was last touched in snapshot 2 and never moved again")

    def test_resolve_stops_once_everything_registered_has_a_value(self):
        # Only a and c are registered; snapshot 1 has both, so resolve(2)
        # shouldn't need to walk past snapshot 1 even though nothing prevents it.
        a_x, c_x = self.rt.execute('''
            local r = battleLogResolve(2)
            return r.objs.a[1], r.objs.c[1]
        ''')
        self.assertEqual(a_x, 1)
        self.assertEqual(c_x, 9)

    def test_a_rewind_can_branch_a_new_capture_off_an_earlier_snapshot(self):
        # Rewind to snapshot 2, then play on: a new capture must base off 2,
        # not off 3 -- and resolving 3 (the abandoned future) must still work.
        self.rt.execute('''
            battleLog.base = 2
            battleLogPrevParts = nil
            FAKE_OBJECTS["a"] = {
                getPosition = function() return {x=7,y=0,z=0} end,
                getRotation = function() return {x=0,y=0,z=0} end,
                getName = function() return "A" end,
                getGUID = function() return "a" end,
            }
        ''')
        i4 = self.rt.execute('return battleLogCapture("phase")')
        base_of_4, a_x_on_4, a_x_on_old_3 = self.rt.execute('''
            return battleLog.snaps[%d].b, battleLogResolve(%d).objs.a[1], battleLogResolve(3).objs.a[1]
        ''' % (i4, i4))
        self.assertEqual(base_of_4, 2, "the new snapshot must branch off the rewound-to snapshot")
        self.assertEqual(a_x_on_4, 7, "the new branch sees the post-rewind move")
        self.assertEqual(a_x_on_old_3, 2, "the old, abandoned snapshot 3 must still resolve correctly")


# ---------------------------------------------------------------------------
# Phase 2 (battleRewind.ttslua): extends the stub with just what that file
# needs beyond battleLog.ttslua's own prelude -- HUD calls are recorded into
# HUD_LOG instead of touching real UI, and spawnObjectJSON is a synchronous
# stand-in (SPAWN_FORCE_RENAME lets a test force the "GUID already taken"
# branch without needing a second live object at the same GUID).
# ---------------------------------------------------------------------------
STUB_PRELUDE_REWIND_EXTRA = r"""
HUD_LOG = {}
playerHudSettings = {}
function ensureHudSettings(color)
    if color ~= "Red" and color ~= "Blue" and color ~= "Grey" then color = "Blue" end
    playerHudSettings[color] = playerHudSettings[color] or {}
    return playerHudSettings[color]
end
function getHudColorFromCallback(player, id)
    if id then
        local fromId = string.match(id, "^(Red)Hud_") or string.match(id, "^(Blue)Hud_") or string.match(id, "^(Grey)Hud_")
        if fromId then return fromId end
    end
    if player == "Red" or player == "Blue" or player == "Grey" then return player end
    return "Blue"
end
function setHudAttribute(params) HUD_LOG[#HUD_LOG + 1] = {kind = "attr", id = params.id} end
function setHudValue(params) HUD_LOG[#HUD_LOG + 1] = {kind = "value", id = params.id} end
function applyHudPreferencesForColor(color) HUD_LOG[#HUD_LOG + 1] = {kind = "apply", color = color} end
function refreshScoringOverlay() HUD_LOG[#HUD_LOG + 1] = {kind = "scoring"} end

function counterSetValue(counter, value)
    if not counter then return 0 end
    counter.value = value
    return value
end

SPAWN_FORCE_RENAME = {}
function spawnObjectJSON(params)
    local decoded = JSON.decode(params.json)
    local guid = decoded.GUID
    if SPAWN_FORCE_RENAME[guid] then
        SPAWN_FORCE_RENAME[guid] = nil
        guid = guid .. "_dup"
    end
    local obj = {
        getGUID = function() return guid end,
        getName = function() return decoded.Nickname or "" end,
        setName = function() end,
        getPosition = function() return {x = decoded.Transform.posX, y = decoded.Transform.posY, z = decoded.Transform.posZ} end,
        getRotation = function() return {x = decoded.Transform.rotX, y = decoded.Transform.rotY, z = decoded.Transform.rotZ} end,
        setLock = function() end,
        setPosition = function() end,
        setRotation = function() end,
        setVelocity = function() end,
        setAngularVelocity = function() end,
    }
    FAKE_OBJECTS[guid] = obj
    if params.callback_function then params.callback_function(obj) end
end
"""


def make_rewind_runtime():
    rt = lua52.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUB_PRELUDE)
    rt.execute(STUB_PRELUDE_REWIND_EXTRA)
    rt.execute((LUA_DIR / "battleLog.ttslua").read_text(encoding="utf-8"))
    rt.execute((LUA_DIR / "battleRewind.ttslua").read_text(encoding="utf-8"))
    return rt


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class RegisteredObjectSetStateTest(unittest.TestCase):
    """RegisteredObject:setState, added to battleLog's class by battleRewind.ttslua."""

    def setUp(self):
        self.rt = make_rewind_runtime()
        self.rt.execute('''
            local function makeModel(name0)
                local t = {_pos = {x=0,y=0,z=0}, _rot = {x=0,y=0,z=0}, _locked = false,
                           _name = name0, interactable = true}
                t.getPosition = function() return t._pos end
                t.getRotation = function() return t._rot end
                t.getName = function() return t._name end
                t.getGUID = function() return "m1" end
                t.setLock = function(v) t._locked = v end
                t.setPosition = function(p) t._pos = p end
                t.setRotation = function(r) t._rot = r end
                t.setVelocity = function() end
                t.setAngularVelocity = function() end
                t.setName = function(n) t._name = n end
                return t
            end
            FAKE_OBJECTS["m1"] = makeModel("Model")
            battleLogRegistry = {m1 = RegisteredObject.new("m1", "Red")}
        ''')

    def test_move_teleports_and_queues_for_unlock(self):
        x, ry, locked_during_move, interactable, off, queued = self.rt.execute('''
            local ro = battleLogRegistry.m1
            ro:setState(State.new({x=5, y=0, z=1, rx=0, ry=90, rz=0, name="Model"}))
            local obj = FAKE_OBJECTS["m1"]
            return obj._pos.x, obj._rot.y, obj._locked, obj.interactable, ro.off, #battleRewindUnlockQueue
        ''')
        self.assertEqual(x, 5)
        self.assertEqual(ry, 90)
        self.assertTrue(locked_during_move, "locked for the move itself, so it can't push/topple anything "
            "mid-restore; only setRegisteredObjectsState's batch-end pass unlocks it")
        self.assertTrue(interactable)
        self.assertFalse(off)
        self.assertEqual(queued, 1, "must be queued so the batch-end pass can unlock it")

    def test_unlock_queue_is_drained_once_the_whole_batch_settles(self):
        locked_after_drain = self.rt.execute('''
            local ro = battleLogRegistry.m1
            setRegisteredObjectsState({m1 = State.new({x=5, y=0, z=1, rx=0, ry=90, rz=0, name="Model"}):encode()})
            return FAKE_OBJECTS["m1"]._locked
        ''')
        self.assertFalse(locked_after_drain)

    def test_park_locks_and_hides_the_model_under_the_table(self):
        park_y = self.rt.execute('return BATTLE_REWIND_PARK_Y')
        y, locked, interactable, off = self.rt.execute('''
            local ro = battleLogRegistry.m1
            ro:setState(State.new({x=0, y=0, z=0, rx=0, ry=0, rz=0, name="Model", off=true}))
            local obj = FAKE_OBJECTS["m1"]
            return obj._pos.y, obj._locked, obj.interactable, ro.off
        ''')
        self.assertEqual(y, park_y)
        self.assertTrue(locked)
        self.assertFalse(interactable)
        self.assertTrue(off)


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class GraveyardRoundTripTest(unittest.TestCase):
    """Delete stores a slim, pooled entry; a rewind respawns from it on demand."""

    def setUp(self):
        self.rt = make_rewind_runtime()

    def _register_and_destroy(self, guid="m1"):
        self.rt.execute('''
            FAKE_OBJECTS["%(guid)s"] = {
                getGUID = function() return "%(guid)s" end,
                getJSON = function()
                    return JSON.encode({
                        GUID = "%(guid)s", Nickname = "Bob",
                        LuaScript = "print(1)", LuaScriptState = "{}", XmlUI = "", Description = "d",
                        Transform = {posX=1,posY=2,posZ=3,rotX=0,rotY=90,rotZ=0,scaleX=1,scaleY=1,scaleZ=1},
                        Locked = false,
                    })
                end,
            }
            battleLogRegistry = battleLogRegistry or {}
            battleLogRegistry["%(guid)s"] = RegisteredObject.new("%(guid)s", "Red")
            battleRewindOnDestroy(FAKE_OBJECTS["%(guid)s"])
            FAKE_OBJECTS["%(guid)s"] = nil
        ''' % {"guid": guid})

    def test_destroy_stores_a_slim_pooled_entry(self):
        self._register_and_destroy()
        is_pooled, pooled_script = self.rt.execute('''
            local id = battleRewindGrave["m1"].LuaScript
            return type(id) == "number", battleRewindPoolList[id]
        ''')
        self.assertTrue(is_pooled, "long fields must be replaced with a pool id, not stored inline")
        self.assertEqual(pooled_script, "print(1)")

    def test_save_and_load_round_trip_preserves_the_graveyard(self):
        self._register_and_destroy()
        saved = self.rt.execute('return battleRewindOnSave()')
        self.rt.globals()["_SAVED_BLOB"] = saved
        self.rt.execute('''
            battleRewindOnLoad(JSON.decode(_SAVED_BLOB))
        ''')
        entry_present, script = self.rt.execute('''
            local e = battleRewindGrave["m1"]
            return e ~= nil, e and battleRewindPoolList[e.LuaScript]
        ''')
        self.assertTrue(entry_present)
        self.assertEqual(script, "print(1)")

    def test_a_rewind_to_alive_respawns_the_model_at_the_target(self):
        self._register_and_destroy()
        self.rt.execute('''
            local ro = battleLogRegistry["m1"]
            ro:setState(State.new({x=9, y=1, z=2, rx=0, ry=0, rz=0, name="Bob"}))
        ''')
        x, off, grave_gone, nickname = self.rt.execute('''
            local ro = battleLogRegistry["m1"]
            local obj = FAKE_OBJECTS["m1"]
            return obj.getPosition().x, ro.off, battleRewindGrave["m1"] == nil, obj.getName()
        ''')
        self.assertEqual(x, 9)
        self.assertFalse(off)
        self.assertTrue(grave_gone, "the graveyard entry must be consumed once the model is back")
        self.assertEqual(nickname, "Bob")

    def test_a_guid_already_taken_at_respawn_rekeys_the_registry_and_army_list(self):
        self._register_and_destroy()
        self.rt.execute('''
            battleLog.army = {Red = {"m1"}, Blue = {}}
            SPAWN_FORCE_RENAME["m1"] = true
            local ro = battleLogRegistry["m1"]
            ro:setState(State.new({x=0, y=0, z=0, rx=0, ry=0, rz=0, name="Bob"}))
        ''')
        new_ro_present, old_removed, guid_field, army_red0, grave_moved = self.rt.execute('''
            return battleLogRegistry["m1_dup"] ~= nil, battleLogRegistry["m1"] == nil,
                   battleLogRegistry["m1_dup"] and battleLogRegistry["m1_dup"].guid,
                   battleLog.army.Red[1], battleRewindGrave["m1_dup"] ~= nil
        ''')
        self.assertTrue(new_ro_present, "the registry must gain an entry at the new GUID")
        self.assertTrue(old_removed, "the old GUID must be dropped from the registry")
        self.assertEqual(guid_field, "m1_dup")
        self.assertEqual(army_red0, "m1_dup", "the army list must be re-keyed too")
        self.assertTrue(grave_moved, "the graveyard entry must follow the GUID, not be lost")


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class BattleRewindCheckMapTest(unittest.TestCase):
    def setUp(self):
        self.rt = make_rewind_runtime()

    def test_no_recorded_map_always_passes(self):
        self.assertTrue(self.rt.execute('battleLog.map = nil; return battleRewindCheckMap()'))

    def test_matching_map_passes(self):
        result = self.rt.execute('''
            battleLog.map = {guid = "abc"}
            FAKE_OBJECTS["startMenu"] = {getVar = function(k)
                if k == "debugCurrentMapGuid" then return "abc" end
            end}
            return battleRewindCheckMap()
        ''')
        self.assertTrue(result)

    def test_mismatched_map_is_refused(self):
        result = self.rt.execute('''
            battleLog.map = {guid = "abc"}
            FAKE_OBJECTS["startMenu"] = {getVar = function(k)
                if k == "debugCurrentMapGuid" then return "xyz" end
            end}
            return battleRewindCheckMap()
        ''')
        self.assertFalse(result)


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class BattleRewindToIntegrationTest(unittest.TestCase):
    """battleRewindTo(i): map check -> battleLogResolve(i) -> every setter -> finish."""

    def setUp(self):
        self.rt = make_rewind_runtime()
        self.rt.execute('''
            FAKE_OBJECTS["startMenu"] = {
                getVar = function(k) if k == "debugCurrentMapGuid" then return "map1" end end,
                call = function(name, params) end,
            }
            FAKE_OBJECTS["roundCounter"] = {value = 0}
            FAKE_OBJECTS["redTurnCounter"] = {value = 0}
            FAKE_OBJECTS["blueTurnCounter"] = {value = 0}
            FAKE_OBJECTS["cpRed"] = {value = 0}
            FAKE_OBJECTS["cpBlue"] = {value = 0}

            local t = {_pos = {x=0,y=0,z=0}, _rot = {x=0,y=0,z=0}, _name = "A"}
            t.getPosition = function() return t._pos end
            t.getRotation = function() return t._rot end
            t.getName = function() return t._name end
            t.getGUID = function() return "a" end
            t.setLock = function() end
            t.setPosition = function(p) t._pos = p end
            t.setRotation = function(r) t._rot = r end
            t.setVelocity = function() end
            t.setAngularVelocity = function() end
            t.setName = function(n) t._name = n end
            FAKE_OBJECTS["a"] = t

            battleLog.map = {guid = "map1"}
            battleLogRegistry = {a = RegisteredObject.new("a", "Red")}
            battleLog.snaps = {
                {k={1,"Red",1}, tag="start", b=nil, parts={
                    turn={r=1,t="Red",p=1,rt=0,bt=0}, cp={r=0,b=0},
                    objs={a={0,0,0,0,0,0,"A",0}},
                }},
                {k={1,"Red",2}, tag="phase", b=1, parts={
                    cp={r=2,b=1},
                    objs={a={5,0,1,0,90,0,"A",0}},
                }},
            }
            battleLog.base = 1
        ''')

    def test_rewinding_forward_applies_turn_cp_and_position(self):
        ok = self.rt.execute('return battleRewindTo(2)')
        self.assertTrue(ok)
        x, ry, cp_r, cp_b, base, busy = self.rt.execute('''
            return FAKE_OBJECTS["a"]._pos.x, FAKE_OBJECTS["a"]._rot.y,
                   FAKE_OBJECTS["cpRed"].value, FAKE_OBJECTS["cpBlue"].value,
                   battleLog.base, battleRewindBusy
        ''')
        self.assertEqual(x, 5)
        self.assertEqual(ry, 90)
        self.assertEqual(cp_r, 2, "cp comes from snapshot 2, resolved forward from the base")
        self.assertEqual(cp_b, 1)
        self.assertEqual(base, 2, "battleLog.base must move to the rewound-to index")
        self.assertFalse(busy, "the busy flag must clear once the batch settles")

    def test_a_map_mismatch_refuses_and_changes_nothing(self):
        self.rt.execute('''
            FAKE_OBJECTS["startMenu"].getVar = function(k)
                if k == "debugCurrentMapGuid" then return "otherMap" end
            end
        ''')
        ok = self.rt.execute('return battleRewindTo(2, "Red")')
        self.assertFalse(ok)
        x, base = self.rt.execute('return FAKE_OBJECTS["a"]._pos.x, battleLog.base')
        self.assertEqual(x, 0, "nothing should have moved")
        self.assertEqual(base, 1, "base must stay put on a refused rewind")


@unittest.skipUnless(lua52, "lupa not installed (pip install lupa) -- skipping Lua-logic tests")
class BattleRewindNextTest(unittest.TestCase):
    """NEXT prefers a direct child: apply only that snapshot's diff, not a full resolve."""

    def setUp(self):
        self.rt = make_rewind_runtime()
        self.rt.execute('''
            FAKE_OBJECTS["startMenu"] = {
                getVar = function(k) if k == "debugCurrentMapGuid" then return "map1" end end,
                call = function(name, params) MENU_CALL_COUNT = (MENU_CALL_COUNT or 0) + 1 end,
            }
            FAKE_OBJECTS["roundCounter"] = {value = 0}
            FAKE_OBJECTS["redTurnCounter"] = {value = 0}
            FAKE_OBJECTS["blueTurnCounter"] = {value = 0}

            local t = {_pos = {x=0,y=0,z=0}, _rot = {x=0,y=0,z=0}, _name = "A"}
            t.getPosition = function() return t._pos end
            t.getRotation = function() return t._rot end
            t.getName = function() return t._name end
            t.getGUID = function() return "a" end
            t.setLock = function() end
            t.setPosition = function(p) t._pos = p end
            t.setRotation = function(r) t._rot = r end
            t.setVelocity = function() end
            t.setAngularVelocity = function() end
            t.setName = function(n) t._name = n end
            FAKE_OBJECTS["a"] = t

            battleLog.map = {guid = "map1"}
            battleLogRegistry = {a = RegisteredObject.new("a", "Red")}
            battleLog.snaps = {
                {k={1,"Red",1}, tag="start", b=nil, parts={
                    turn={r=1,t="Red",p=1,rt=0,bt=0}, objs={a={0,0,0,0,0,0,"A",0}},
                }},
                {k={1,"Red",2}, tag="phase", b=1, parts={objs={a={3,0,0,0,0,0,"A",0}}}},
            }
            battleLog.base = 1
            battleRewindSel = {r=1, t="Red", p=1}
        ''')

    def test_next_applies_only_the_childs_diff_not_a_full_resolve(self):
        self.rt.execute('battleRewindNext()')
        x, base, sel_p, menu_calls = self.rt.execute('''
            return FAKE_OBJECTS["a"]._pos.x, battleLog.base, battleRewindSel.p, MENU_CALL_COUNT
        ''')
        self.assertEqual(x, 3)
        self.assertEqual(base, 2)
        self.assertEqual(sel_p, 2, "the selection must follow the applied snapshot's key")
        self.assertIsNone(menu_calls, "snapshot 2's diff has no turn part, so setTurnState (and its "
            "startMenu.call) must never run -- proves this took the diff-only path, not a full resolve")


if __name__ == "__main__":
    unittest.main()
