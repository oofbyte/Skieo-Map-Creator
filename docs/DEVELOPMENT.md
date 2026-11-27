# Developing the Map Creator

How the maps are built, how to run and build the app from source, where things are, and how a release is made.
For players, see the [README](../README.md).

## How it works

1. **Elevation** (`dem.py`, the sources in `dem_sources.py`): the finest open bare-earth model covering the square,
   then the next ones for what it leaves (a border, gaps), each matched to the level of the finer one along the edge
   and blended in over 24 grid steps (`dem.merge`). Order: USGS 3DEP in the US; LidarBC, Ontario's lidar DTM; then
   Mapterhorn (tiles.mapterhorn.com: the open national models in one Terrarium tile set, whose zoom 13 and finer
   tiles exist only where it has national data); then each country's own server (swisstopo, IGN France and Spain,
   BEV, Kartverket, GSI, NRCan HRDEM, CUZK, NSW, Bolzano, Piemonte, Lombardia, INGV TINITALY) for what Mapterhorn
   lacks or when it can't be reached; AWS Terrain Tiles (~30 m) for the rest. A source is a server that resamples
   (WCS, WMS, ArcGIS ImageServer: `dem.Gridded`, `dem.ImageServer`), web Mercator tiles (`dem.XyzTiles`) or
   GeoTIFF tiles read with range requests (`dem.Tiled`, `cog.py`: the overview the grid needs, only the tiles under
   the square; LZW goes to Pillow's libtiff, Deflate and the predictors are decoded with numpy, so no imagecodecs).
   Each raster is sampled from its own projection (`crs.py`: transverse Mercator, Lambert conic and azimuthal, polar
   stereographic, LV95, Helmert datum shifts, forward only) onto the game's 2017 x 2017 landscape grid on a local
   transverse Mercator projection (`geo.py`). A map keeps its heights (`maps/<slug>/dem.npy`), which Rebuild builds
   on again, and the ids of the sources it was made from (`'ca_bc_lidar+mapterhorn'`), which a map made before the
   heights were kept loads again (the old `'3dep'`, `'3dep2'` and `'terrarium'` give the terrain of 1.10 bit for bit);
   `dem_sources.VERSION` goes up when sources change.
2. **The template and the game's data**: every map is the Map Creator's own template (`data/map_template.zip`), made from
   scratch in the mod kit and laid out like the game's own maps: a level (landscape of 32 x 32 components, the
   environment settings, 64 snow shell pieces, the edge wall, the game mode and the shared level), a forest, map
   preview, height texture and starting save asset, each of the game's own classes holding only our values (see
   "The map template" below). `gamedata.py` reads from the player's game, with retoc, into a cache: the stock map
   Mad River Glen for one thing only, the compiled terrain material that every game map carries (made from the
   material's node graph, which only the game's developer has); and that map's layout, which must still be the one the
   template was made for (`engine/stockcheck.py`), plus the tree species and biomes. It dry-runs every writer before
   the first build. Nothing of the game's ships with the Map Creator, and no game map is copied. retoc reads the game's
   `Content/Paks` folder itself (`system.game_containers`): it mounts only the containers right in the folder, not
   `~mods`, so no mod's copy of a package comes out. Only when another container sits next to the game's are the
   game's set apart in a work folder: hard links, or copies on drives without them (exFAT: `[WinError 1]`, and the
   17 GB base container took up to 20 minutes to copy).
3. **Build** (`builder.py`): the template is filled in:
   - **Heights and scale:** landscape heights and all their mip levels (made the way Unreal makes them), edge-normal
     rings and grid scale (`engine/terrain_write.py`, `engine/provider_write.py`); component bounds and collision;
     the map frame (size, height scale); and the edge wall, whose edge the terrain is fitted to (`engine/map_frame.py`).
   - **Forest:** in the game's baked-obstacle format (`trees.py`): stands of the tree species the game's own maps of
     the biome have, mixed and placed by height, slope, sun, wetness and ridges as there (`ecology.py`,
     `data/tree_patterns.json`, see below), conifers only near the summits (`biomes.py`). The game's frame rate goes
     with the tree instances it draws (every tree and each living broadleaf or larch canopy: shadow and wind each frame,
     however far), not with their triangles, so a forest over its budget (`trees.tree_budget`: the Max trees count,
     at most `trees.TREE_BUDGET` instances, as many as the game's own biggest maps, unless the map is made in
     experimental mode) thins out, least around the lifts and trails, the trees it keeps a size larger where that
     keeps the ground covered (`data/tree_meshes.json`: the meshes of each kind of tree, see below).
   - **Snow piles:** written from a cooked template mesh (`shells.py`, `data/shell_template.*`).
   - **Terrain material:** a package of the map's own (`MI_<slug>_Terrain`) holding the game's compiled terrain
     material instance, taken from the stock map in the player's game with the package around it made new
     (`engine/terrain_material.py`: the stock map's name and import tables kept, so the material's data is used as
     it is), with the Map Creator's settings (`engine/material_params.py`).
   - **Everything else:** the height texture, the map preview (with the map's terrain generation), the preview camera
     (`builder.preview_pose`: where the game's own maps put theirs), and the map list entry. Every renamed string
     keeps its length. A map starts empty, like the game's own maps (none of them has a starting save but the
     tutorial map).
   - **Real trails and lifts** (optional, `resort.py`): the ski area's OpenStreetMap lifts and pistes become the
     game's own lift and trail records in a starting save written from scratch (`startsave.py`: the ticket office
     on the base area, its hub, the trail map's bounds; read and written with `engine/gvas_tree.py`, record layouts
     from `data/save_template.bin` and `data/resort_templates.bin`, blank records of the game's save format; weather,
     snow, staff, money and the date are left to the game), each run's curves as wide as its real cut through the forest in the
     satellite canopy heights (`resort._cuts`), the forest leaves their corridors clear, and the map list entry's
     `InitialSave` makes New Game start from that save. Glades are left out: the ones OSM marks, and runs under tree
     canopy with no cleared corridor in the satellite canopy heights (`canopy.py`). A run drawn twice in OSM is built
     once. Within 300 m of the runs, glades and lifts the forest follows the real tree cover from the same canopy
     heights (`resort.tree_cover`), so open bowls and meadows stay open.
4. **Pack and install**: retoc `to-zen` packs a `ZZZ_MC_<name>_P` pak. `install.py` adds the map to the game's
   map list (`ZZZ_Registry_P`) and copies it into `Content/Paks/~mods`.

The window is a local web UI (`web/`: Leaflet map, three.js 3D preview) shown by pywebview: WebView2 on
Windows, WebKit on macOS. `server.py` serves it on 127.0.0.1, with a token-protected tile proxy on its own port.

Mods are not the Map Creator's business any more: they come from Z's Mod Loader. The Mod Library of versions 1.2 to
1.8 (removed in 1.9; its code is in the git history) installed UE4SS, Lua mods and lift and building packs into the
game; `oldmods.py` takes out what it installed, once per game folder, the first time the game is found closed, by its
markers and the hashes of the files it shipped. Installing a map checks that its package holds only a map
(`install.Game.only_a_map`): no asset every mod shares (the build menu, the snow caps, the map list) and none of the
game's own outside the map folders.

## Run from source

Windows 10/11 or macOS on Apple silicon, Python 3.12, Ski-E-O! installed through Steam.

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

On macOS:

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

## Build the exe and the macOS app

```
packaging\build.bat
```

This makes `dist\SkiEO-Map-Creator.exe`, a single file that bundles `web/`, `data/` and `tools/retoc.exe`.

On macOS, `packaging/build_mac.sh` makes `dist/SkiEO Map Creator.app` (arm64, ad-hoc signed by PyInstaller, not
notarized) with `tools/retoc`, and zips it as `dist/SkiEO-Map-Creator-macOS.zip`.

## macOS

What differs between the platforms is in `skieo_creator/system.py`. On macOS:

- The game folder is `<Steam>/steamapps/common/skieogame/Ski-E-O.app/Contents/UE`, so every
  `SkiResortSimulator/...` path is the same as on Windows (the paks are `SkiResortSimulator-Mac.*`). Maps go into
  `Content/Paks/~mods` inside the game's app bundle; the game loads them from there. Work folders go next to the
  bundle, never inside it.
- `tools/retoc` is retoc v0.1.5's `aarch64-apple-darwin` release build, the same version as `tools/retoc.exe`. Its
  Oodle library (`liboo2coremac64.2.9.10.dylib`) downloads next to the copy in the app folder on first use.
- Experimental mode is off and hidden (`install.TREES_UNCAPPED`): Oof's Tree Limit, which lets the game show more
  than 1.5 million trees, is a Windows mod. `restore_tree_limit` has nothing to do (the macOS game was never patched).
- The app is not replaced in place on update: a newer release with `SkiEO-Map-Creator-macOS.zip` is offered and its
  page opens.
- The old Mod Library never shipped for macOS, so `oldmods.py` doesn't run there.

## The map template

`data/map_template.zip` is the map every build fills in: a level, forest, map preview, height texture and starting
save asset, made from scratch in the mod kit (`D:\SkiModKit`, the Unreal editor project our asset packs are made in)
by `mod-loader-upload/kit/maptemplate.py`. Its editor script (`SkiModKit/Scripts/kit/build_map_template.py`, with
`MapKit` in the kit's editor module) makes a new level without World Partition and puts in it what the game's own
maps have, from the game's classes in the kit's stub of the game's module: a landscape of 32 x 32 components of 63
quads (heights from a tilted plane, so every component has a height range of its own), the environment settings, 64
snow shell pieces, the edge wall, the game mode, and the shared level always loaded. Stand-ins take the place of the
game's assets the level refers to (the terrain, rock and snow materials, the game mode, the shared level, the snow
meshes) and are never cooked: the level only names them. The landscape's material instances point at a stand-in for
the terrain material package each map gets from the player's game.

`maptemplate.py` cooks only the template's packages, converts them to the 5.7 layout retoc reads, checks retoc takes
them (to-zen and back), and checks the template against the game's own map (same classes, properties and export
sizes) and against every writer of the Map Creator. It records the stock map's layout (`engine/stockcheck.py`), which
`gamedata.py` compares with the player's game, and the template's landscape and heightfield GUIDs, which every map
gets its own of (`builder.map_guids`: the engine shares terrain collision between components of the same
HeightfieldGuid). Make the template again after a game update that changes the map classes (the Map
Creator then says it needs an update) or the engine:

```
python <mod-loader-upload>\kit\stubgen.py <UHT header dump>\SkiResortSimulator D:\SkiModKit\SkiResortSimulator\Source\SkiResortSimulator
python <mod-loader-upload>\kit\build.py --cpp
python <mod-loader-upload>\kit\maptemplate.py
```

(`build.py --cpp` rebuilds the kit's C++ and the asset packs; see `mod-loader-upload/kit/README.md`.)

## Data learned from the game

`data/tree_patterns.json` holds how each biome's tree species are spread over the game's own maps (measured once,
offline; the Map Creator reads no game forest when it builds a map). To make it again
(after a game update that changes the maps' forests), extract the maps' forests and height textures, then run the
learner on them:

```
tools\retoc.exe to-legacy --no-shaders -f DA_BakedObstacles_Map_ -f Materials/Terrain/Generated/ <game>\SkiResortSimulator\Content\Paks <out>
python packaging/make_tree_patterns.py <out>\SkiResortSimulator\Content --check
```

`data/tree_meshes.json` holds, for every kind of tree (species, size, state, trunk mesh), how many meshes the game
draws for it (its trunk, and its canopy if it has one) and their triangles, measured from the game's tree meshes. To
make it again (after a game update that changes the trees):

```
tools\retoc.exe to-legacy --no-shaders -f Meshes/Mountain/ -f DA_TreeSpecies_ <game>\SkiResortSimulator\Content\Paks <out>
python packaging/make_tree_meshes.py <out>\SkiResortSimulator\Content
```

`data/forest_stats.json` holds what the game's own forests are like, so the Map Creator plants its own without reading
them: trees per m2, and per look (`biomes.py`: Mad River Glen's trees or Sugar Bowl's) the trees' scale range and
their scales by height and size. To make it again:

```
tools\retoc.exe to-legacy --no-shaders -f DA_BakedObstacles_Map_mad_river_glen_real. -f DA_BakedObstacles_Map_sugar_bowl_real. <game>\SkiResortSimulator\Content\Paks <out>
python packaging/make_forest_stats.py <out>\SkiResortSimulator\Content
```

`data/save_template.bin` holds blank records of the game's save format (the ticket office, its hub, the trail map,
cleared ground) that a starting save is written from (`startsave.py`), and `save_template.json` the game's save
version. Make it again after a game update that changes the save format, from any save of the new version with a
ticket office, and the game's own starting save asset for the version numbers:

```
tools\retoc.exe to-legacy --no-shaders -f InitialSaves/ <game>\SkiResortSimulator\Content\Paks <out>
python packaging/make_save_template.py <a .sav with a ticket office> <out>\SkiResortSimulator\Content\InitialSaves\IS_Map_sugar_bowl_real_tutorial.uexp
```

## Layout

| Path | What |
|---|---|
| `skieo_creator/` | app: `main.py` (window), `api.py` (calls from the page), `system.py` (Windows / macOS differences), `builder.py`, `trees.py`, `shells.py`, `dem.py` and `dem_sources.py` (elevation), `crs.py` (map projections), `cog.py` (GeoTIFFs over range requests), `install.py`, `oldmods.py`, `update.py` ... |
| `skieo_creator/engine/` | writers for the cooked map data (heightmaps, providers, bounds, collision, frame, headers, materials), the terrain material package (`terrain_material.py`), the check of the game's map layout (`stockcheck.py`) |
| `web/` | the UI (HTML / CSS / JS) and vendored Leaflet + three.js |
| `data/` | `map_template.zip`, the map template (see above); the cooked snow-shell template; `save_template.bin` and `resort_templates.bin`, blank save records (from `packaging/make_save_template.py` and `make_resort_templates.py`); the tree meshes, patterns and forest statistics |
| `tools/` | retoc.exe (MIT, https://github.com/trumank/retoc) and its license, both shipped in the exe; retoc, the macOS build, shipped in the macOS app. Nothing else goes here: `verify_exe.py` fails the release build on any file in `tools/`, `web/` or `data/` the exe doesn't ship (but `tools/retoc`) |
| `packaging/` | PyInstaller spec, build script, icon, version info, credits (`THIRD-PARTY.txt`), `verify_exe.py`; developer scripts the exe doesn't ship, such as `make_tree_patterns.py` and `make_tree_meshes.py` |
| `docs/` | this file and the README's screenshots |
| `tests/ui_drive.py` | scripted UI sessions with screenshots (`search`, `3d`, `3d-offline`, `numbox`, `osm`, ...) |
| `tests/test_resort.py` | self-check of the trail and lift import rules on made-up data |
| `tests/test_trees.py` | self-check of the forest on a made-up mountain (no game, no downloads) |
| `tests/test_dem.py` | self-check of reading where the USGS elevation image really is (its GeoTIFF tags) |
| `tests/test_elevation.py` | self-check of the map projections against published coordinates, sampling a raster from its projection, merging sources, the COG reader and its decoders |
| `tests/dem_live.py` | live check of each elevation source on a real ski area against Mapterhorn and Terrain Tiles: coverage, offset, and the shift that lines them up (0, 0 when in place); downloads data |
| `tests/test_update.py` | self-check of the self-update (release lookup, checksums, swapping the exe) without the network |
| `tests/test_install.py` | self-check of the map list entries (never a tutorial map, the once-only rebuild of older ones) |
| `tests/test_map_package.py` | self-check that a map package holds only a map, with stand-in containers made from the game's files (skipped without the game) |
| `tests/test_oldmods.py` | self-check of taking out what the old Mod Library installed, in stand-in game folders |
| `tests/test_main.py` | self-check of the empty-window guard (a second try, then a message) with a stand-in window |
| `.github/workflows/release.yml` | builds, checks, hashes and attests the exe and the macOS app and attaches them to each release |

## Releasing

1. Bump `VERSION` in `skieo_creator/__init__.py` and the versions in `packaging/version_info.txt`, and add a
   `## vX.Y.Z` section to `CHANGELOG.md`.
2. On GitHub: **Releases → Draft a new release**, create the tag `vX.Y.Z`, and publish. The description can stay
   empty.
3. The workflow builds the exe from that tag, checks it against the source, and attaches it and `SHA256SUMS.txt` to
   the release, with the notes from `CHANGELOG.md`. It stops if the tag doesn't match the versions in step 1. A
   second job then builds the macOS app on a macOS runner, attaches `SkiEO-Map-Creator-macOS.zip` and adds its line
   to `SHA256SUMS.txt`.
