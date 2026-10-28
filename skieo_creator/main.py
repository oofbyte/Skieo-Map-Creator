"""Start the Map Creator window."""
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from . import APP_NAME, VERSION, paths

log = logging.getLogger('skieo_creator')
WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
MB_OK, MB_YESNO, MB_ICONERROR, MB_ICONWARNING, IDYES = 0x0, 0x4, 0x10, 0x30, 6


def message(text, flags=MB_OK | MB_ICONERROR):
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)
    except Exception:
        return 0


def single_instance():
    """True if this is the only running copy; otherwise brings the other window to the front"""
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


def release_webview_profile():
    """WebView2 processes of a crashed earlier run can keep the browser profile locked, and the window would then stay
    blank ('resource in use'). Only one creator runs at a time (single_instance), so any such process is an orphan."""
    lock = paths.WEBVIEW / 'EBWebView' / 'lockfile'
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


def _excepthook(kind, value, tb):
    log.critical('unhandled error:\n%s', ''.join(traceback.format_exception(kind, value, tb)))


def main():
    paths.setup_logging()
    sys.excepthook = _excepthook
    threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)
    log.info('%s %s starting (frozen=%s)', APP_NAME, VERSION, getattr(sys, 'frozen', False))
    if os.name != 'nt':
        message('The Map Creator runs on Windows only.')
        return 1
    if not single_instance():
        return 0
    wv2 = webview2_version()
    log.info('WebView2 runtime: %s', wv2)
    if not wv2:
        if message('The Map Creator needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.\n\n'
                   'It is a free, small download from Microsoft. Open the download page now?',
                   MB_YESNO | MB_ICONWARNING) == IDYES:
            webbrowser.open(WEBVIEW2_URL)
        return 1
    release_webview_profile()
    try:
        import webview
        from . import api, server
        url, _ = server.start()
        bridge = api.Api()
        win = webview.create_window(APP_NAME, url, js_api=bridge, width=1440, height=900, min_size=(1100, 680),
                                    background_color='#0b1118', text_select=False)
        bridge._attach(win)
        win.events.shown += style_titlebar
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(paths.WEBVIEW),
                      debug='--debug' in sys.argv)
    except Exception:
        log.critical('window failed:\n%s', traceback.format_exc())
        message('The Map Creator window could not be opened.\n\nDetails were written to:\n%s'
                % (paths.LOGS / 'creator.log'))
        return 1
    log.info('closed')
    return 0
