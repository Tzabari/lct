# Changelog

Player-facing changes to the LCT 40k TTS table.

How this file is used by the compiler (`compile.py --release`):
- The **topmost** `## vX.Y.Z` heading is the current version. It is stamped into
  the TTS save name (e.g. `LCT - v1.0.0`) and into the
  in-game `GAME_VERSION`.
- The `-` bullet points directly under that heading are the patch notes shown
  to players in-game the first time they open a map built from a newer version
  than their saved game.

To cut a release: add a new `## vX.Y.Z` section at the top with its bullets,
then run `python3 compile.py --release`.

## v1.12.0
- Feat: Battle Log. Each player selects their models and presses REGISTER ARMY on the game-tools object; from then on the table records where every registered model stands at each phase change, plus the terrain layout, VP/CP and per-model wounds. Press EXPORT REPORT (or run the exporter on a saved game) to get a browsable battle report with a top-down board per phase.
- Feat: EXPORT REPORT can also reach a hosted renderer instead of the local helper, so anyone at the table gets a shareable link with nothing installed. The mod never contacts it except when EXPORT is pressed, probing first so a sleeping free-tier instance's ~60s wake shows a progress message instead of failing. A view link and a permanent-download link are printed to chat and written to a "Battle Report" notebook tab; the view link expires (15 minutes by default, sooner if the server is busy or the report has been downloaded) but the downloaded copy is self-contained and keeps working offline forever.
- Feat: Battle Log works with only one player seated, from either seat, so solo games and testing record properly. A present player can register an empty seat's army.
- Feat: CAPTURE button records an extra snapshot mid-phase, for moments a phase boundary would miss.
- Feat: The battle report is now a viewer: one board fills the window, with Round tabs, a player tab, per-round phase tabs and Next/Prev phase buttons (arrow keys work too).
- Feat: Report overlay toggles mirror the in-game ones - deployment zones, objectives, territory line, table quarters, 6" strategic reserves and the 3"/6" centre rings.
- Feat: The report tracks both Reinforcements and Reserves boards and each side's currently active secondaries.
- Feat: The report board zooms and pans (mouse wheel to zoom, drag to pan, double-click or 0 to reset), and the terrain, deployment and name markings are drawn at true table scale so they no longer swamp the models.
- Feat: A Deployment map view shows the mission setup on its own - zones, quarters and objectives - instead of a moment in the game.
- Feat: The board as it stood when Start Game was pressed is its own **Deploy** round in the report, ahead of Round 1. Register your army before starting the game to get it; the table says so if nothing was registered in time.
- Feat: The score panel shows each side's primary/secondary split alongside the VP total.
- Bug fix: VP always read 0 in the battle log. The score sheet's playerSum takes a plain number, but Object.call hands it the parameter table, so every snapshot threw "attempt to index a nil value" - scores now come from getMatchSummary.
- Bug fix: The battle log recorded no map name, players or deployment when the game was already in progress (only pressing Start Game filled them in). Any snapshot now backfills whatever context is missing, without overwriting what Start Game recorded.
- Bug fix: Recording a snapshot raised "Attempt to perform operations with resources owned by different scripts" on every phase, which aborted the snapshot and left the whole game unlogged. The chosen deployment now crosses between scripts as JSON instead of as a table owned by the start menu.
- Bug fix: The report server rendered with whatever version of the renderer was on disk when it started, so a server left running kept serving the old report. It now re-reads the renderer per export, and the page header stamps the time it was rendered.
- Feat: Report terrain is now exact rather than inferred. The report looks up the map card that was loaded and reads the terrain straight out of that card's own spawn data, so every terrain area is drawn at its true position and true outline - including the wedge-shaped plate, which is a trapezoid rather than the triangle its name suggests, and which used to come out mirrored.
- Feat: Terrain areas are drawn from the real plate models, traced once and shipped with the mod. Eleven shapes cover every terrain area on all 225 maps, so no map falls back to a rough box.
- Change: The ruins standing on the terrain areas are no longer drawn, and neither is the mission's layout-art diagram in the Deployment map view. The ruin shapes cluttered the board under the models, and one layout-art card covers three different terrain layouts, so it was wrong for two maps in three.
- Bug fix: The report board was mirrored top-to-bottom. It was drawn as if seen from under the table, which also reversed every model's facing.
- Bug fix: Report terrain was unreadable. The battlemat is untagged on many maps, so it was drawn as a board-sized slab of terrain covering everything; the outlines that mark terrain areas were drawn as if they were the terrain; and objective markers were drawn as terrain boxes while the outlines around them were drawn as the objectives.
- Bug fix: The report's territory line was always the centre line. It is now derived from the deployment that was played, as the table derives it, so it tilts correctly for stepped and diagonal zones - and Combat Patrol's own declared divider is used where a mission has one.
- Bug fix: Active secondaries never appeared in the report. The scan read each slot zone directly, and a card dropped into a slot is not registered as inside it until it settles; it now goes through the same slot helper the scoreboard uses.
- Bug fix: The report listed only the first two of a player's secondaries. The battle log kept its own two-entry list of slot zones rather than using the mod's, so a card drawn into slots 3 to 8 was recorded nowhere; all eight slots a side are now read.
- Bug fix: The report drew only five of the mod's eight deployment shapes, so Combat Patrol zones went missing, and a "No Deployment Zone" mission could blank the page.
- Feat: Battle Rewind. Press END GAME (next to EXPORT REPORT) to record the final state, close the battle log to further recording, and open a Round / Player / Phase selector - greyed out wherever no snapshot was recorded. Pick a moment and press REWIND to put every model, CP, VP and secondary back exactly as they were then; press RETURN TO END to put the table back to the end of the game. Rewinding is a review, not an undo - the log is never truncated, so you can scrub back and forth as much as you like without losing anything.
- Feat: A model destroyed during the game comes back under its original GUID when a rewind puts it back on the board, at the position, wounds and health-bracket colour it had at that moment; a model still alive when you rewind past its death goes back to the graveyard until you return to a moment after it fell.

## v1.11.3
- UI Scoreboard change for better visibility and new button underneath scoring overlay.

## v1.11.2
- Maps: Updated T5S2 map pack thanks to NConroy! Now all Map options are fully compatible with the latest GW layout update!
- Bug fix: Now changing table mat should not have any objects floating. 

## v1.11.1
- Bug fixes: Fixed Lava theme footprint border, map picker UI arrow bugs and also updated Mothmy Titanias token bag!
- Maps: Updated all Layouts (art and maps) based on GW August update. All battlemaster maps should be up and working. 
- Maps: Added 3 new themes (Lava Temple - Imperium Ice Colony - Mars Base) as parts of LCT Pack 1.
- Feat: Added an advanced features panel at the top of the panel, near the LCT logo that currently has a button that disables and re-enables terrain colliders. Atm only Battlemaster maps are supported.
- Feat: Added a new and improved Statshelper based on the one made by Beowulf78.
- Feat: Add a better animation effect for when Quick roll is used (Right click +xd6 buttons)
- Bug fix: Clear Mat now removes Lethal Hits too.
- Improvement: additional dice roller resiliency changes 
- UI: Improved initial intro screen for better patch notes visibility 


## v1.10.7 
- Removed competitive/thematic map filter for now. New thematic/narrative map system will come soon! 
- Added Armaggedon Ruins map set from Battlemaster that uses the official GW Armageddon terrain ruins.
- Fixed some minor map mistakes with ruins placement.

## v1.10.6
- Battlemaster,LCT Map, Cra5hNatural and T5S2 packs are updated based on July Dataslate.
- Updated Event companion pdf 

## v1.10.3
- Fix load-order race on Global GUIDs and secondary button desync for joining players 
- Changed Lightning settings from background to Gradient. 
- Refreshed Battlemaster maps.  

## v1.10.2
- Reverted vortex to it's previous state where it would destroy objects instead of store them, also now the lid closes again on click! 
- Removed Select all dice button from roll dice
-- New Highlight button on the dice tray (next to Coherency/Engagement): select models and press it to paint them in the colour shown on the swatch next to it - click the swatch to cycle colours. The paint sticks until you RIGHT-CLICK Highlight, which clears all highlights from the table. Coherency Check and Engagement Range were renamed to Coherency and Engagement to make room.
- Added Tacoma Open FAQ since it's written by GW Rules team 
- 2nd performance pass 
- Changed a bit some icons in the overlay

## v1.10.0
- Added the first pack of LCT maps, 15 are now in (1 for each matchup) that aim to be both thematic and close to GW terrains that they showed us. Also they will have a lot of texture randomization. The next packs will come soon!
- Improved clock in the overlay. There is a Pass/Pause button in the overlay. Also I 've added shortcuts (that work now) that allow each player to Pass the clock and Pause/Resume the active clock. Also now the clock (if active) will pass to the other player when passing the turn.
- Updated the cards from Shinobau, also reduced drastically their image size. This is the first optimization pass that I am working on.
- Additional overlay fixes, now it should be in a less intrusive/annoying position. It will still "reset" due to how TTS works so keep that in mind.


## v1.9.3 
- Fixed bug where End of Battle scoring didn't count towards 45 pts primary limit. 
- Some UI cleanups and buttons changing positions.
- Refreshed Battlemaster maps
- Added an endless 6-dice bag next to each players tray just in case something bugs out. 
- Chess clock rework: new "Pass Chess Clock" and "Pause Chess Clock" game keys (bind them under Options -> Game Keys). Anyone can use them - passing stops the running clock and starts the opponent's, and works even while paused. The old numpad 7/8/9 keys now do the same instead of the confusing per-seat behaviour. 
- Added play/pause buttons next to each clock in the score overlay. Play starts that clock; pause stops the running clock and, pressed again, resumes whoever was on the clock. 
- Fixed the chess clock ignoring the next click after a spectator pressed it. 
- The chess clock now follows the turn: passing the turn while the clock is running automatically switches it to the new active player (a paused or not-yet-started clock is left alone). 

## v1.9.2 
- Added set mission button that sets mission to the board state. This is more of a QoL for people who play non-comp modes. 
- Additional default tokens for basic things to the left of each players board 
- Back-end clean up for better map management. 

## v1.9.1
- Added links to other workshop items. 
- Added a small introduction in the notebook about how a game starts. 
- Some more token additions & changes

## v1.9.0 
- Reverted pdfs, edited workshop tags, trying to clean up things 
- UI: Added dedicated modes buttons, so now you can choose map size from initial start menu
- Onslaught mode now is handled appropriately, menus are moved to provide enough space. 
- Added Dice+ button that provides more dice colours 
- Added another token bag that has primary/secondary missions tokens and some additional generic ones. 
- Cool new dice tray
- Bug fixes: Territory lines, GUID spam on map load.


## v1.8.4b
- Refreshed Battlemaster cards
- Cleaning up
- Quick fixes

## v1.8.2c
- Added more Table themes 
- Updated Primary & Secondary mission decks based on this Changelog -> https://github.com/game-datacards/missioncards/blob/main/CHANGELOG.md
- Added more space to the board when Onslaught mode is set
- Fixed minor overlay bugs
- Added tags to T5S2 maps for objective tokens
- Added Dominatus items on main page (1v1 and Solo). 
- Added T5S2 maps on the list.
- Minor bug fixes (Removed Mission gen when map loads,quick roll custom dice bug)

## v1.8.1
- Added a lof of new maps from creators. But most of them came from Battlemaster, made by Superwutz.
- Combat Patrol & Narrattive/Support.
- Added an Advanced control menu on top
- Map/Mod customization: In the menu there is an option to changes loaded maps mat or to change the table theme. 
- Added an End of Battle Scoreboard button that doesnt have the primary/secondary points limitations.
- New Hotkey additions but if you encounter some weird shortcut behaviour you might need to reset your hotkeys.
- New and cleaner UI, things should look cleaner and more symmetrical. Also replaced legacy widgets with counters. 
- Multiple minor bug fixes

## v1.7.0e
- Added back Activation and Wound tokens! 
- Small ui changes (eg. Map filter going away when game starts)

## v1.7.0d
- Deployed test version with Convex fixes.
- Fixed most of the objective based bugs for maps
- Now Back to Selection undo button returns all cards appropriately
- Quick fix with some ui errors and primaries not moving on board 
- Added map filter
- All maps should now be available. If you notice any map errors, let me know!
- Reworked ui to to make it look more intuitive.


## v1.6.5c
- More maps, currently we have all Take and Hold Matchups, Priority Assets vs Priority Assets, Priority Assets vs Recon , Purge the Foe vs Purge the Foe, Purge the Foe vs Recon, Purge the Foe vs Priority Assets. 
- Added a "Back to Menu" button that appears beneath dispotition text after a map is loaded, in case people want to back to the original selection. 
- Revamped Score board overlay so that it looks way better. 
- Added the Tournament Companion PDF

## v1.6.5b
- Map Generation from multiple creators. Now the system of loading layouts, will also pull maps from different creators, so if you press Generate mission again, eg for TnH vs TnH you ll always get appropriate Layouts 1,2 and 3 but you might get Layout 1 from creater A and Layout 2,3 from creator B. Hopefully this will allow for map variety and I will eventually add a tool that will allow for map filtering. 
- Greatly improved performance for Coherency tool 
- Removed/Refactored old legacy code.
- UI: Improved boards contrast 
- Added clear mat button for people who want to use maps with additive loading.  

## v1.6.5 
- UI: New board art 
- UI: Added Gain CP boards 
- UI: There is now an "X" button in the overlay to hide it. You can show it again if you press "Show/Hide" button.
- Code: Improved dice roller performance and trimmed legacy code yet again. 

## v1.6.4b
- Fixed Dice Roller issue on red side. 
- Added updated primary/secondary cards (improved wording)
- UI/UX changes 
- Added Sort Secondary buttons to place secondary cards on empty spots 
- Additional game keys and sorted them alphabetically.

## v1.6.3b
- Improved Map Loading (this wont work with maps outside the mod)
- Proper Objective marker graphics! Try it!
- Added legacy quick roll feature if you right click dice spawn buttons in ordered manner. (This still uses the new dice rolling algorith).
- Added engange on all fronts fixed

## v16.2c
- Edited the new dice roller yet again. Removed the instant dice rolling buttons but there should be better stability
- Automatically set Deployment Zone on Map Load.
- Additional dev tools on backend
- Removed old pdfs and added the new Core Rules with bookmarks (thanks to Bookmarkable PDF by CaptironJack) for easier navigation in game. 
- Added an image with the new strategems 
- All primary & secondary cards are now using the amazing card design by Shinobau https://github.com/game-datacards/missioncards 
- Added new extra tokens (inside a memory bag) by MothmyTitania have a look! 
- Added a new coherency button that dynamically calculates the distance between selected units. Keep in mind while it's on you can't really draw lines until you toggle it off (right click on button) or 15 seconds pass
- FIX: fixed a small issue with coherency being wonky with oval based models.


## v1.6.1
- FIX: Fixed a dice rolling bug and weird cloning issues if both SuS & Lethals occured. 
- FEATURE: Dynamic Objective/Ruin Markers that calculates dynamically where it should be placed, cause with so many layouts and different DZones it would be a hell to manually place these onto every map and even harder to change it later. 
- FEATURE: Mission Dispotition Take and hold Maps are in! Keep in mind other matchups remain random. 
- UI/UX: Moved Mission Generation near the centre for better visibility. 
- UI/UX: Added a 15'' bubble button too.


## v1.5.2
- QOL: Deployment zones are now disabled when game starts. 
- Fix: Territory button wasn't synced before.
- Fix: Reverted to old stats helper from Ricu
- Fix: Fixed a bug with LOS markers that weren't able to separate markers from different players.

## v1.5.1 
- TOOL ADDITION :Added LOS markers by the amazing Kvothe! 
- FEATURE: There is a button next to area denial that now handles the new show territory areas, it's based on current Deployment zone so it wont work before choosing one 
- UI/UX: Added more options to the overlay for better readability. Now there are Pass Turn, Gain CP and Use CP buttons. There is also a new side button "-" pressing that will toggle to a more mininalistic UI. 
- UI/UX: Added banners above map decks. This is still placeholder art until we get all the layout info.
- QUICK FIX: Clicking the Start Button 3 times allows to "Force Start" the game and ignore tactical/fixed step. 

## v1.5.0 
- TOOL ADDITION :Added LOS markers by the amazing Kvothe! 
- FEATURE: There is a button next to area denial that now handles the new show territory areas, it's based on current Deployment zone so it wont work before choosing one 
- UI/UX: Added more options to the overlay for better readability. Now there are Pass Turn, Gain CP and Use CP buttons. There is also a new side button "-" pressing that will toggle to a more mininalistic UI. 
- Added banners above map decks. This is still placeholder art until we get all the layout info.

## v1.4.0
- Backend compiler improvements 
- Added dynamic custom overlay bubble, set a hotkey and try it! 
- Minor UI fixes

## v1.3.3
- Dropped support for 10th edition 
- Gaining or decreasting CP via widget and/or overlay now is logged in chat for tracking.
- Added deployment zone picker based on dispotition.
- Added VP limitations for primary, secondary missions and totla scores. 
- Changed scoreboard buttons 
- Added the option to change table mat 
- Multiple new tokens 
- Major code cleanup

## v1.0.0
- CP tracking added to the VP/CP overlay
- Table texture editing and new control buttons
- Option to draw a single secondary only
- Renamed legacy bags for clarity
