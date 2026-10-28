"""Map frame (horizontal / vertical scale) shared by the build tools. Defaults = Stratton / Sugar Bowl scale."""
G = 2017

SP = 183.44075359445 / 100.0            # metres per vertex
SC = 119.84375 / 128 / 100.0            # metres per height unit
HALF = 1849.08                          # half extent in metres used for initial-save world coordinates
NAME = 'stratton'


def _update():
    global HALF
    HALF = (G - 1) * SP / 2.0


def root():
    """Landscape RootComponent transform: loc xyz (cm), scale xyz (cm/vertex, cm per 128 units), rot."""
    e = (G - 1) * SP * 100.0 / 2.0
    return (-e, -e, 32768 * SC * 100.0, SP * 100.0, SP * 100.0, SC * 128 * 100.0, 0.0)


def set_frame(sp_m, sc_m, name):
    global SP, SC, NAME
    SP, SC, NAME = sp_m, sc_m, name
    _update()


def top_z_cm():
    return 65535 * SC * 100.0
