"""Live check of the elevation sources (dev tool, downloads data): each source on a real ski area, against Mapterhorn
(an independent copy of most national models) and the ~30 m Terrain Tiles: coverage, heights, the vertical offset and
the horizontal shift that lines the two up best (0, 0 when the source is in the right place).
usage: python tests/dem_live.py [source ids...] [--km 3]
  Set LOCALAPPDATA to a scratch folder to keep its downloads out of a real creator's cache."""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import dem, dem_sources, net, paths  # noqa: E402,F401

SITES = {                                 # source id: (ski area, latitude, longitude)
    'mapterhorn': ('Kitzbuehel', 47.4476, 12.3926),
    'cz_dmr5g': ('Spindleruv Mlyn', 50.7262, 15.6094),
    'ca_on_dtm': ('Blue Mountain ON', 44.5009, -80.3097),
    'ca_bc_lidar': ('Whistler village', 50.1163, -122.9574),
    'ch_swissalti3d': ('Zermatt', 46.0207, 7.7491),
    'at_bev_als': ('Kitzbuehel', 47.4476, 12.3926),
    'fr_ign_lidarhd': ('Chamonix', 45.9237, 6.8694),
    'es_ign_mdt05': ('Sierra Nevada', 37.0955, -3.3986),
    'no_kartverket': ('Hemsedal', 60.8650, 8.5900),
    'ca_hrdem': ('Mont-Tremblant', 46.2100, -74.5850),
    'jp_gsi_dem5': ('Hakuba', 36.7000, 137.8300),
    'jp_gsi_dem10': ('Niseko', 42.8600, 140.7000),
    'it_bz_dtm': ('Ortisei', 46.5745, 11.6717),
    'it_piemonte': ('Sestriere', 44.9586, 6.8789),
    'it_lombardia': ('Livigno', 46.5380, 10.1357),
    'it_tinitaly': ('Courmayeur', 45.7969, 6.9689),
    'au_nsw_5m': ('Thredbo', -36.5040, 148.3060),
}


def best_shift(a, b, r=6):
    """(dy, dx) in grid steps that best lines up b's slopes with a's, and that correlation"""
    ga = np.gradient(a[::2, ::2])
    gb = np.gradient(b[::2, ::2])
    best = (-2.0, 0, 0)
    h, w = ga[0].shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            sa = (slice(max(0, dy), h + min(0, dy)), slice(max(0, dx), w + min(0, dx)))
            sb = (slice(max(0, -dy), h + min(0, -dy)), slice(max(0, -dx), w + min(0, -dx)))
            x = np.concatenate([ga[0][sa].ravel(), ga[1][sa].ravel()])
            y = np.concatenate([gb[0][sb].ravel(), gb[1][sb].ravel()])
            ok = np.isfinite(x) & np.isfinite(y)
            if ok.sum() < 1000:
                continue
            c = float(np.corrcoef(x[ok], y[ok])[0, 1])
            if c > best[0]:
                best = (c, dy * 2, dx * 2)
    return best[1], best[2], best[0]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    km = float(sys.argv[sys.argv.index('--km') + 1]) if '--km' in sys.argv else 3.0
    ids = [a for a in args if a in dem.SOURCES] or list(SITES)
    for sid in ids:
        name, lat, lon = SITES.get(sid, ('?', 0, 0))
        win = dem.Window(lat, lon, km * 1000)
        src = dem.SOURCES[sid]
        t = time.time()
        try:
            d = src.fetch(win, net.NEVER, lambda f, s: None)
        except Exception as e:                                    # noqa: BLE001
            print('%-15s %-17s FAILED: %r' % (sid, name, e))
            continue
        took = time.time() - t
        if d is None or not np.isfinite(d).any():
            print('%-15s %-17s no data (%.1f s)' % (sid, name, took))
            continue
        cov = float(np.isfinite(d).mean())
        line = '%-15s %-17s %5.1f s  %3.0f%% data  %6.0f..%-6.0f m' % (sid, name, took, 100 * cov, np.nanmin(d), np.nanmax(d))
        refs = [('mapterhorn', dem.SOURCES['mapterhorn'])] if sid != 'mapterhorn' else []
        refs.append(('terrarium', dem.SOURCES['terrarium']))
        for rid, ref in refs:
            try:
                r = ref.fetch(win, net.NEVER, lambda f, s: None)
            except Exception as e:                                # noqa: BLE001
                line += ' | %s failed (%s)' % (rid, e)
                continue
            if r is None or not np.isfinite(r).any():
                line += ' | %s: none' % rid
                continue
            both = np.isfinite(d) & np.isfinite(r)
            off = float(np.median((d - r)[both]))
            mad = float(np.median(np.abs(d - r - off)[both]))
            dy, dx, c = best_shift(d, r, 6 if rid == 'mapterhorn' else 4)
            line += ' | vs %s: offset %+.1f m, spread %.1f m, best shift (%+d, %+d) r=%.2f' % (rid, off, mad, dy, dx, c)
        print(line)
        sys.stdout.flush()


if __name__ == '__main__':
    main()
