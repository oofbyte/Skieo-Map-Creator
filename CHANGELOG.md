# Changelog

Maps made with an older version show **Rebuild recommended** in My Maps: one click on Rebuild gives them the fixes.

## v1.2.0
- **Mod Library** tab: switch in-game mods on and off. The first one is **Weather Controls** (F9 in game): force
  sunny / cloudy / snowing / blizzard, and turn snowfall, storms and temperature up or down live. Installs the UE4SS
  mod loader too if you don't have it, and removes it again when switched off.
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
