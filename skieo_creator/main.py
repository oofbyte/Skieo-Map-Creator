"""Start the Map Creator window."""
import ctypes
import locale
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

from . import APP_NAME, VERSION, paths, system, update

log = logging.getLogger('skieo_creator')
WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
MB_OK, MB_YESNO, MB_ICONERROR, MB_ICONWARNING, MB_ICONINFORMATION, IDYES = 0x0, 0x4, 0x10, 0x30, 0x40, 6
MB_SETFOREGROUND = 0x10000
SAFE_START = '--safe-start'      # the second try after the window stayed blank (watch_load)
LOAD_S = 25                      # the page normally loads in 1-3 s
NO_GPU = '--disable-gpu'         # WebGL then draws with Windows' software renderer: the 3D view still works
BLANK = ('The Map Creator window stays empty: Microsoft Edge WebView2, which draws it, %s.\n\n'
         'Repairing WebView2 usually fixes this: Windows Settings > Apps > Microsoft Edge WebView2 Runtime > Modify. '
         'If it is not listed there, install it from Microsoft. Open the download page now?\n\n'
         'Details were written to:\n%s')
BLANK_MAC = ('The Map Creator window stays empty: its page %s. Close the Map Creator and start it again; a security '
             'program that blocks local connections (127.0.0.1) can cause this.\n\nDetails were written to:\n%s')

MAC_DIALOG = '''on run argv
  set {t, kind, yesno} to argv
  if yesno is "1" then
    set r to display dialog t with title "%s" buttons {"No", "Yes"} default button "Yes" with icon (kind as integer)
  else
    set r to display dialog t with title "%s" buttons {"OK"} default button "OK" with icon (kind as integer)
  end if
  return button returned of r
end run''' % (APP_NAME, APP_NAME)


def message(text, flags=MB_OK | MB_ICONERROR):
    if system.MAC:
        return _mac_message(text, flags)
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)
    except Exception:
        return 0


def _mac_message(text, flags):
    """the same message box on macOS (osascript; the text is an argument, never part of the script)"""
    icon = 0 if flags & 0x30 == MB_ICONERROR else (2 if flags & 0x30 == MB_ICONWARNING else 1)  # stop / caution / note
    try:
        r = subprocess.run(['osascript', '-e', MAC_DIALOG, text, str(icon), '1' if flags & MB_YESNO else '0'],
                           capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError):
        return 0
    return IDYES if r.stdout.strip() == 'Yes' else 0


def single_instance():
    """True if this is the only running copy; otherwise brings the other window to the front"""
    if system.MAC:                                         # macOS brings an app that is open already to the front
        import fcntl                                       # itself; this is for the copy run from the source
        paths.ensure_dirs()
        f = open(paths.APP_DIR / '.instance.lock', 'w')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        single_instance.handle = f
        return True
    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, 'Local\\SkiEOMapCreator')
    if k32.GetLastError() == 183:                          # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return False
    single_instance.handle = handle                        # keep the mutex for the process lifetime
    return True


def release_instance():
    """before starting the next copy (update, second try): it must not find this one still running"""
    h = getattr(single_instance, 'handle', None)
    if h is None:
        return
    if system.MAC:
        h.close()
    else:
        ctypes.windll.kernel32.CloseHandle(h)
    single_instance.handle = None


def webview2_version():
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\' + WEBVIEW2_GUID),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\EdgeUpdate\Clients\\' + WEBVIEW2_GUID),
            (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\EdgeUpdate\Clients\\' + WEBVIEW2_GUID)]
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                v = winreg.QueryValueEx(k, 'pv')[0]
                if v and v != '0.0.0.0':
                    return v
        except OSError:
            continue
    return None


def style_titlebar():
    """dark title bar (Windows 10 2004+), wood-brown with cream text on Windows 11"""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        for attr in (20, 19):                               # DWMWA_USE_IMMERSIVE_DARK_MODE (19 before 20H1)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
        for attr, rgb in ((35, 0x5C331D), (36, 0xF6EFE0)):   # DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR (COLORREF = BGR)
            c = ctypes.c_int(((rgb & 0xFF) << 16) | (rgb & 0xFF00) | (rgb >> 16))
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), 4)
    except Exception:
        pass


def release_webview_profile(profile):
    """WebView2 processes of a crashed earlier run can keep the browser profile locked, and the window would then stay
    blank ('resource in use'). Only one creator runs at a time (single_instance), so any such process is an orphan."""
    lock = profile / 'EBWebView' / 'lockfile'
    if not lock.exists():
        return
    try:
        lock.unlink()
        return
    except PermissionError:
        pass
    except OSError:
        return
    log.warning('browser profile is locked by an earlier run, stopping its WebView2 processes')
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
          "Where-Object { $_.CommandLine -like '*SkiEO Map Creator*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], timeout=30,
                       creationflags=0x08000000, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return
    for _ in range(40):
        try:
            lock.unlink()
            return
        except PermissionError:
            time.sleep(0.25)
        except OSError:
            return


class InitFailure(logging.Handler):
    """pywebview only logs it when WebView2 fails to start, and the window then stays blank"""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.text = None

    def emit(self, record):
        if 'WebView2 initialization failed' in record.getMessage():
            self.text = record.getMessage()


def _why(failure):
    if failure.text:
        lines = [l.strip() for l in failure.text.splitlines()[1:] if l.strip()]
        return 'did not start (%s)' % (lines[0][:240] if lines else 'no reason given')
    return 'did not show its page within %d seconds' % LOAD_S


def _probe(url):
    """for the log: does the page's own little web server answer (a security program blocking it looks the same)"""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r:
            return 'answers (%d)' % r.status
    except Exception as e:
        return 'does not answer (%s)' % e


def watch_load(win, url, failure, retry, safe, done, settings):
    """In a thread once the window is up. If WebView2 failed to start or the page never loaded, the window would stay
    blank for good. The first time, the Map Creator starts again with its own fresh browser profile and graphics
    acceleration off (and starts that way from then on if it helped); blank after that, it says why and how to fix it."""
    t0 = time.monotonic()
    while not failure.text and time.monotonic() - t0 < LOAD_S:
        if win.events.loaded.wait(0.25):
            log.info('page loaded in %.1f s', time.monotonic() - t0)
            if retry:
                settings.set(safe_start=True)
            return
        if win.events.closed.is_set():
            return
    why = _why(failure)
    log.error('the window stayed blank: %s %s; the page server %s', 'WebKit' if system.MAC else 'WebView2', why,
              _probe(url))
    if system.MAC:                                         # WebKit is part of macOS: nothing to repair or retry
        message(BLANK_MAC % (why, paths.LOGS / 'creator.log'), MB_OK | MB_ICONWARNING)
    elif not safe:
        done['retry'] = True
    elif message(BLANK % (why, paths.LOGS / 'creator.log'), MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND) == IDYES:
        webbrowser.open(WEBVIEW2_URL)
    if not win.events.closed.is_set():
        win.destroy()


def restart(*args):
    """start this program again as a new process: the packaged exe, or run.py from source"""
    if getattr(sys, 'frozen', False):
        update.start(sys.executable, *args)
    else:
        update.start(sys.executable, os.path.abspath(sys.argv[0]), *args)


def _language():
    try:
        if system.MAC:                                     # an app started from Finder has no LANG
            from Foundation import NSLocale
            return str(NSLocale.currentLocale().localeIdentifier())
        if not system.WINDOWS:
            return locale.getlocale()[0] or os.environ.get('LANG') or '?'
        return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), '?')
    except Exception:
        return '?'


def _excepthook(kind, value, tb):
    log.critical('unhandled error:\n%s', ''.join(traceback.format_exception(kind, value, tb)))


def main():
    paths.setup_logging()
    sys.excepthook = _excepthook
    threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)
    retry = SAFE_START in sys.argv
    log.info('%s %s starting (frozen=%s%s) on %s, language %s', APP_NAME, VERSION, getattr(sys, 'frozen', False),
             ', second try after a blank window' if retry else '', system.os_version(), _language())
    if not (system.WINDOWS or system.MAC):
        message('The Map Creator runs on Windows and macOS only.')
        return 1
    if not single_instance():
        return 0
    safe = retry or bool(paths.Settings().get('safe_start'))
    profile = paths.WEBVIEW_SAFE if safe else paths.WEBVIEW
    if system.WINDOWS:
        wv2 = webview2_version()
        log.info('WebView2 runtime: %s', wv2)
        if not wv2:
            if message('The Map Creator needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.\n\n'
                       'It is a free, small download from Microsoft. Open the download page now?',
                       MB_YESNO | MB_ICONWARNING) == IDYES:
                webbrowser.open(WEBVIEW2_URL)
            return 1
        release_webview_profile(profile)
        if retry:
            shutil.rmtree(profile, ignore_errors=True)     # a fresh browser profile
        if safe:
            os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = NO_GPU     # added to pywebview's own arguments
            log.info('safe start: own browser profile, graphics acceleration off')
    update.cleanup()                                       # what an earlier update left next to the exe
    rel = None if retry else update.newer()                # a newer release on GitHub: ask before the window opens
    if rel and message(update.prompt(rel), MB_YESNO | MB_ICONINFORMATION) != IDYES:
        log.info('update to %s: not now', rel['version'])
        rel = None
    if rel and not update.IN_PLACE:                        # macOS: the player downloads the new app from the release
        webbrowser.open(rel['page'] or update.PAGE_URL)
        rel = None
    done = {}
    try:
        import webview
        from . import api, server
        url, _ = server.start()
        bridge = api.Api()
        bridge._done = done                                # the footer's Check for updates restarts through it too
        failure = InitFailure()
        logging.getLogger('pywebview').addHandler(failure)
        win = webview.create_window(APP_NAME, None if rel else url, html=update.page(rel) if rel else None,
                                    js_api=bridge, width=1440, height=900, min_size=(1100, 680),
                                    background_color='#0b1118', text_select=False)
        bridge._attach(win)
        if system.WINDOWS:
            win.events.shown += style_titlebar
        if rel:
            task, args = update.run, (win, rel, url, done, message)
        else:
            task, args = watch_load, (win, url, failure, retry, safe, done, bridge._settings)
        webview.start(task, args, gui='cocoa' if system.MAC else 'edgechromium', private_mode=False, storage_path=str(profile),
                      debug='--debug' in sys.argv)
    except Exception:
        log.critical('window failed:\n%s', traceback.format_exc())
        message('The Map Creator window could not be opened.\n\nDetails were written to:\n%s'
                % (paths.LOGS / 'creator.log'))
        return 1
    if done.get('exe'):                                    # updated: the new copy takes over from here
        release_instance()
        try:
            update.start(done['exe'])
            log.info('started %s', done['exe'])
        except OSError as ex:
            log.error('the new version would not start: %s', ex)
            message(update.undo(done['exe'], done['old'], ex))
    elif done.get('retry'):                                # blank window: a second try (watch_load)
        release_instance()
        try:
            restart(SAFE_START)
            log.info('started again for a second try')
        except OSError as ex:
            log.error('could not start again: %s', ex)
            message('The Map Creator window stayed empty, and the Map Creator could not start again by itself (%s). '
                    'Please start it again.' % ex)
    log.info('closed')
    return 0
