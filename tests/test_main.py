"""Offline checks of the blank-window guard (skieo_creator/main.watch_load) with a stand-in window.
usage: python tests/test_main.py"""
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import main as M  # noqa: E402

M.LOAD_S = 0.6
M.system.MAC = False                                 # the Windows behavior first (WebView2); macOS at the end
M._probe = lambda url: 'answers (200)'
shown, opened = [], []
M.message = lambda text, flags=0: shown.append(text) or M.IDYES
M.webbrowser.open = opened.append
FAIL = ('WebView2 initialization failed with exception:\n System.Runtime.InteropServices.COMException (0x8007139F): '
        'The group or resource is not in the correct state.\n   at Microsoft.Web.WebView2.Core...')


class Events:
    def __init__(self):
        self.loaded, self.closed = threading.Event(), threading.Event()


class Win:
    def __init__(self):
        self.events, self.destroyed = Events(), False

    def destroy(self):
        self.destroyed = True


class Settings:
    def __init__(self):
        self.data = {}

    def set(self, **kw):
        self.data.update(kw)


def run(retry=False, safe=False, loaded=False, closed=False, fail=None):
    win, failure, done, settings = Win(), M.InitFailure(), {}, Settings()
    pv = logging.getLogger('pywebview')
    pv.addHandler(failure)
    try:
        if fail:
            pv.error(fail)                             # what pywebview logs when WebView2 does not start
        if loaded:
            win.events.loaded.set()
        if closed:
            win.events.closed.set()
        del shown[:], opened[:]
        M.watch_load(win, 'http://127.0.0.1:1/', failure, retry, safe, done, settings)
    finally:
        pv.removeHandler(failure)
    return win, done, settings


# the page loads: nothing happens; on the second try, safe starts are kept from then on
win, done, st = run(loaded=True)
assert not win.destroyed and not done and not st.data and not shown
win, done, st = run(retry=True, safe=True, loaded=True)
assert not win.destroyed and st.data == {'safe_start': True} and not shown

# WebView2 does not start: the first time, a second try (no message); after that, the reason and the repair
win, done, st = run(fail=FAIL)
assert win.destroyed and done == {'retry': True} and not shown
win, done, st = run(retry=True, safe=True, fail=FAIL)
assert win.destroyed and not done and not st.data and len(shown) == 1 and opened == [M.WEBVIEW2_URL]
assert '0x8007139F' in shown[0] and 'did not start' in shown[0] and 'Modify' in shown[0], shown[0]

# the page never loads (no error): the same, after the wait
win, done, st = run(safe=True)
assert win.destroyed and 'did not show its page within' in shown[0]

# the player closed the window before it loaded: leave it
win, done, st = run(closed=True)
assert not win.destroyed and not done and not shown

# only pywebview's start failure counts
f = M.InitFailure()
f.emit(logging.LogRecord('pywebview', logging.ERROR, '', 0, 'some other error', None, None))
assert f.text is None

# macOS: WebKit is part of the system, so no second try and no repair: the reason, then the window closes
M.system.MAC = True
win, done, st = run(fail=FAIL)
assert win.destroyed and not done and len(shown) == 1 and not opened and 'stays empty' in shown[0], shown
print('ok')
