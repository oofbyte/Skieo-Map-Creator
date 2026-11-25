"""Offline checks of the USGS elevation georeferencing (dem._georef): usage: python tests/test_dem.py"""
import io
import sys
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import dem  # noqa: E402


def geotiff(west, north, dx, dy, shape=(4, 6), tie=(0, 0)):
    buf = io.BytesIO()
    i, j = tie
    tifffile.imwrite(buf, np.zeros(shape, np.float32), extratags=[
        (33550, 'd', 3, (dx, dy, 0.0), False),                                   # ModelPixelScale
        (33922, 'd', 6, (i, j, 0.0, west + i * dx, north - j * dy, 0.0), False)])  # ModelTiepoint
    return buf.getvalue()


# the box the server really returned, not the one asked for (it widens the latitude span by default)
w, n, dx, dy = dem._georef(geotiff(-72.84, 43.625, 0.0002, 0.0002))
assert (round(w, 6), round(n, 6), dx, dy) == (-72.84, 43.625, 0.0002, 0.0002), (w, n, dx, dy)
# a tie point that is not the top-left pixel still gives the top-left corner
w, n, dx, dy = dem._georef(geotiff(-72.84, 43.625, 0.0002, 0.0002, tie=(3, 2)))
assert (round(w, 6), round(n, 6)) == (-72.84, 43.625), (w, n)
# no georeferencing: None (the caller falls back to the requested box)
buf = io.BytesIO()
tifffile.imwrite(buf, np.zeros((2, 2), np.float32))
assert dem._georef(buf.getvalue()) is None
assert dem.describe('3dep2') == dem.describe('3dep')
print('dem: all checks passed')
