# Changelog

Maps made with an older version show **Rebuild recommended** in My Maps: one click on Rebuild gives them the fixes.

## v1.5.0
- **Oof's Alpine Buildings** (Mod Library): 50 new buildings in the build menu (under Comfort), all made from scratch
  for the pack out of the game's own materials (wood, stone, metal roofs, windows that light up at night):
  - **Two giants**: the Grand Summit Lodge (a glazed great hall between two stone chimneys, cross-gabled timber wings,
    a trussed porte-cochere) and the Grand Alpine Palace Hotel (green copper roofs, turrets, a grand portico).
  - **Timber-frame mountain homes**: walkout homes with covered decks and stone chimneys through glazed gables, homes
    with heavy-truss porticos on river-rock columns and rusty shed awnings.
  - **Log cabins, A-frames, Swiss chalets, a guesthouse and townhouses.**
  - **Modern**: villas, and hotels and apartment houses with larch balconies and dark metal top floors.
  - **Inns, grand lodges, hotels and three lakeside estates.**
  Homes work like the Cabin, the big ones like the Grand Lodge or the Hotel: same unlock and upkeep, more to build the
  bigger they are, and room for more guests.
- The R2 lift packs and the buildings pack share the build menu: switch on any of them and you get all of their
  content.

## v1.4.0
- **Full game**: works with **Ski-E-O! Ski Resort Tycoon** on Steam, finds it by itself, and Play starts it. The demo
  is no longer supported (its game files are encrypted since its last update). Maps made with an older version
  show **Rebuild recommended**: rebuild them once for the full game.
- **Custom maps are real size only**: the Map size switch (Real Size / Diorama) in Advanced Options is hidden for them.
- **Oof's Simple Cheat Menu** (Mod Library, **F6** in game) replaces Weather Controls and keeps all of its weather
  controls. Four tabs:
  - **Money**: add $100K to $100M, pay off all loans, infinite money (never below $100M), income multiplier up to 10x.
  - **Time**: speeds 1x to 100x, pause, skip ahead an hour, 6 hours, a day or a month, jump to morning, noon, sunset
    or night, freeze the clock.
  - **Weather**: force sunny, cloudy, snowing or blizzard; turn snowfall, storms and temperature up or down
    (buried in snow? lower the snowfall); re-roll the winter.
  - **Resort**: level up or jump to the top level (unlocks everything), add XP, add guests, and god mode switches:
    no accidents, no breakdowns, staff never get tired, build anywhere (slopes, trees, overlapping).
  If you had Weather Controls on, the app swaps it for the cheat menu by itself. F9 is now the full game's Report a Bug.
- **R2 lifts cost what the game's own lift of the same kind costs**, to build and to run, and unlock at the same
  level: the R2 Six like the High-Speed Six ($721,200+, Elite Resort), the R2 Gondola like the Gondola ($2,414,400+,
  World-Class). Before, both cost what the double chair does and were available almost from the start.
- **The R2 lifts install only through the Mod Library** (no more copying files into `~mods` by hand). The app keeps
  them up to date: a pack from an older version, or one copied in by hand, is replaced with the app's own.
- **The R2 lifts no longer hide new buildings**: with an R2 lift switched on, the full game's Base Lodge and autumn
  decorations were missing from the build menu. The app now copies the game's building list from your own game
  files, and copies it again after a game update the next time the app starts.

## v1.3.0
- **Millions of trees**: the game normally shows no trees at all on a map with more than 1.5 million. The creator now
  raises that limit for you: it changes that one number in `skieogame.exe` when it starts and before it installs a
  map, so it comes back by itself after a game update ("Verify integrity of game files" in Steam undoes it).
- **The tree slider follows the map size**: picking a size sets it to the game's own forest density (about 19,000
  trees per km²: 2.7 million on a 12 km map, 7.5 million on 20 km), which is also the most. You can still lower it.
- **Snowfall** shows the yearly snowfall the game will show for the map next to the multiplier, e.g. 1.10 × (385″/yr).
- **ft | m switch** next to "Your resort" (the same as Units at the bottom).
- Larger maps with many trees can take **up to 10 minutes** to fully load in game: the forest fills in square by
  square and the game runs slowly until it is done (the app says so under the tree slider).
- **Trees up to** starts at **42°** (was 33°), about where the game's own forests stop: steeper cliffs stay bare rock.
- A map never gets more trees than you set (it could go a few hundred over, and past the game's limit that wipes
  every tree).

## v1.2.0
- **Mod Library** (the brown button at the bottom): flip a switch to install a mod, flip it back to remove it.
  - **Weather Controls** (F9 in game): force sunny / cloudy / snowing / blizzard, and turn snowfall, storms and
    temperature up or down live. Fixes resorts getting buried in snow. Installs the UE4SS mod loader too if you
    don't have it, and removes it again when switched off.
  - **Doppelmayr D-Line R2 Six** and **R2 Gondola** lifts: switch them on and they appear in the build menu.
- **Satellite** map view next to Topo and Streets.
- **Treeline fade**: the forest thins out gradually below the treeline instead of stopping in a straight line
  (default 200 m; 0 = the old sharp edge).
- **Max trees** goes up to 1,490,000 (was 1,000,000). The game shows no trees at all from 1.5 million.
- Maps up to **20 km** wide (was 12). The game's terrain grid stays the same size, so bigger maps are coarser
  (about 10 m between height points at 20 km).

## v1.1.10
- Number boxes accept European decimals: `0,93` works like `0.93`, and `3.500 m` counts as thousands.

## v1.1.9
- Fixed resort names with accents (like ü or é) breaking the game's map list.
- Names use letters, numbers, spaces and `- ' . & ( )` only. Accents become plain letters automatically
  (Zürs → Zurs).

## v1.1.8
- Fixed installing, rebuilding and the game status failing on non-English Windows (German and others).

## v1.1.7
- Max trees goes up to 1,000,000 (higher counts made the game drop all trees).

## v1.1.6
- Fixed the thin lines across the terrain for good, and the terrain lighting looking too steep.

## v1.1.5
- Full forest right up to the "Trees up to" angle, no thinning before it.

## v1.1.4
- New **Trees up to** slider: the steepest slope that still gets trees (default 33°).
- Removed the Share buttons.

## v1.1.3
- No more repeating stripes on big rock faces (larger rock texture).

## v1.1.2
- Trees no longer grow on bare rock (33° and steeper).
- First fix for the lines along terrain tile borders.

## v1.1.1
- No trees on cliffs.

## v1.1.0
- Fixed cracks in far-away terrain.
- Fixed see-through gaps between the terrain and the map's edge walls.
- New **Max trees** slider for better frame rates on slower PCs.
- Type exact values next to every slider.
- Notice after installing: the first game start after a new map usually crashes; just start it again.
- The 3D preview no longer gets stuck loading on slow or no internet.

## v1.0.0
- First release.
