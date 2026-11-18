"""Self-update from the GitHub releases (the exe the release workflow builds, checks against the source and attaches).

Before the window opens (main.py) the newest release is looked up. When it is newer than this exe and the player says
yes, the window shows a progress page while the new exe is downloaded next to this one and checked against the
release's SHA256SUMS.txt; then it takes this exe's name (a running exe can be renamed, not overwritten: this one
becomes <name>.old.exe, removed on the next start) and is started once this window has closed. Any failure leaves this
version as it was and the window goes on to the app. Only for the packaged exe, never when run from the source.

On macOS the app is not replaced in place (an app bundle is a folder, and macOS checks a downloaded one before it first
runs): when a newer release has the macOS app attached, the player is asked and the release page opens.
"""
import glob
import hashlib
import html
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import APP_NAME, VERSION, net, system

log = logging.getLogger(__name__)
REPO = 'oofbyte/Skieo-Map-Creator'
API = 'https://api.github.com/repos/%s/releases/' % REPO
DOWNLOADS = 'https://github.com/%s/releases/download/' % REPO
PAGE_URL = 'https://github.com/%s/releases/latest' % REPO
HOSTS = ('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')   # where they redirect
EXE, SUMS = 'SkiEO-Map-Creator.exe', 'SHA256SUMS.txt'
MAC_ZIP = 'SkiEO-Map-Creator-macOS.zip'
IN_PLACE = not system.MAC                     # the new version replaces this one by itself (else: the release page)
ASSET = EXE if IN_PLACE else MAC_ZIP          # what a release must have attached for this platform
MIN_MB = 20                                   # anything smaller is not the app


class UpdateError(Exception):
    pass


def _version(s):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(s or '').strip())
    return tuple(int(x) for x in m.groups()) if m else None


def release(tag=None):
    """the latest release (or the one tagged `tag`) as dict(version, notes, exe, size, sums, page), or None when it
    has no exe and checksums attached (yet: the workflow attaches them a few minutes after publishing)"""
    r = json.loads(net.get(API + ('tags/' + urllib.parse.quote(tag) if tag else 'latest'), timeout=5, retries=0,
                           accept='application/vnd.github+json'))
    v = _version(r.get('tag_name'))
    files = {a.get('name'): a for a in r.get('assets') or ()}
    exe, sums = files.get(ASSET), files.get(SUMS)
    if not (v and exe and sums) or r.get('draft') or r.get('prerelease'):
        return None
    for a in (exe, sums):                     # only this repository's own release files
        if not str(a.get('browser_download_url', '')).startswith(DOWNLOADS):
            return None
    return dict(version='%d.%d.%d' % v, notes=str(r.get('body') or ''), exe=exe['browser_download_url'],
                size=int(exe.get('size') or 0), sums=sums['browser_download_url'], page=str(r.get('html_url') or ''))


def is_newer(rel):
    """rel (from release()) is a newer version than this one"""
    cur = _version(VERSION)
    return bool(rel and cur and _version(rel['version']) > cur)


def newer():
    """the latest release when it is newer than this exe; None otherwise, when run from the source, with
    SKIEO_NO_UPDATE set, or when GitHub can't be reached (never holds up the start for more than a few seconds)"""
    if not getattr(sys, 'frozen', False) or os.environ.get('SKIEO_NO_UPDATE') or not _version(VERSION):
        return None
    try:
        rel = release()
    except (net.NetError, ValueError, KeyError, TypeError) as ex:
        log.info('update check skipped: %s', ex)
        return None
    if is_newer(rel):
        log.info('update available: %s (this is %s)', rel['version'], VERSION)
        return rel
    return None


def notes(text, limit=700):
    """the release's changelog part as plain text for a message box"""
    text = text.split('### Download')[0]
    lines = []
    for line in text.splitlines():               # the changelog wraps its lines: join them up again
        s = line.strip()
        if lines and lines[-1].strip() and line[:1].isspace() and s and not re.match(r'[-*] ', s):
            lines[-1] += ' ' + s
        else:
            lines.append(line)
    out = []
    for line in lines:
        line = re.sub(r'\*\*|__|`', '', line).rstrip()
        line = re.sub(r'^(\s*)[-*] ', r'\1• ', re.sub(r'^#+\s*', '', line))
        if line.strip() or (out and out[-1]):
            out.append(line)
    s = '\n'.join(out).strip()
    if len(s) > limit:
        s = s[:limit].rsplit('\n', 1)[0].rstrip() + '\n…'
    return s


def prompt(rel):
    n = notes(rel['notes'])
    if not IN_PLACE:
        return ('%s %s is out (you have %s).%s\n\nOpen the download page now? Download %s there, unzip it and put the '
                'new app in place of this one.' % (APP_NAME, rel['version'], VERSION, '\n\n' + n if n else '', MAC_ZIP))
    return ('%s %s is out (you have %s).%s\n\nUpdate now? The new version (%d MB) is downloaded from GitHub, checked, '
            'and started in place of this one.' % (APP_NAME, rel['version'], VERSION, '\n\n' + n if n else '',
                                                   max(1, rel['size'] >> 20)))


class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    """follows GitHub's redirects to its download servers, nowhere else"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or (p.hostname or '') not in HOSTS:
        raise UpdateError('refused to download from %s' % (p.hostname or url))


def _open(url, timeout):
    _check_url(url)
    req = urllib.request.Request(url, headers={'User-Agent': net.USER_AGENT, 'Accept': 'application/octet-stream'})
    return urllib.request.build_opener(_GitHubOnly).open(req, timeout=timeout)


def _why(ex):
    return str(getattr(ex, 'reason', None) or ex) or type(ex).__name__


def _unlink(p):
    try:
        Path(p).unlink(missing_ok=True)
    except OSError:
        pass


def _retry(fn, *args):
    """a rename that an antivirus or the search indexer may block for a moment, right after a file was written"""
    for i in range(15):
        try:
            return fn(*args)
        except PermissionError:
            if i == 14:
                raise
            time.sleep(0.2)


def download(rel, folder, progress=lambda done, total: None):
    """the release's exe, downloaded into `folder` (next to this exe: same drive, so it can be renamed into place) and
    checked against the release's SHA256SUMS.txt -> its path"""
    try:
        with _open(rel['sums'], 30) as r:
            sums = r.read(1 << 16).decode('utf-8', 'replace')
    except UpdateError:
        raise
    except Exception as ex:                   # noqa: BLE001  (URLError, OSError, http.client errors ...)
        raise UpdateError('could not download the checksums (%s)' % _why(ex))
    m = re.search(r'^([0-9a-fA-F]{64}) [ *]?%s\s*$' % re.escape(EXE), sums, re.M)
    if not m:
        raise UpdateError('the release has no checksum for %s' % EXE)
    want = m.group(1).lower()
    tmp = Path(folder) / (EXE + '.download')
    try:
        f = open(tmp, 'wb')
    except PermissionError:
        raise UpdateError('Windows doesn\'t let the Map Creator write to its folder (%s). Move it to a folder of your '
                          'own (Desktop, Documents or Downloads) and start it from there' % folder)
    except OSError as ex:
        raise UpdateError('could not save the download (%s)' % _why(ex))
    h, n = hashlib.sha256(), 0
    try:
        with f, _open(rel['exe'], 60) as r:
            try:
                total = int(r.headers.get('Content-Length') or 0) or rel['size']
            except ValueError:
                total = rel['size']
            while True:
                b = r.read(1 << 16)
                if not b:
                    break
                f.write(b)
                h.update(b)
                n += len(b)
                progress(n, total)
        if h.hexdigest() != want:
            raise UpdateError('the download is damaged (its SHA-256 is not the release\'s)')
        with open(tmp, 'rb') as f:
            head = f.read(2)
        if n < MIN_MB << 20 or head != b'MZ':
            raise UpdateError('the download is not the Map Creator')
    except Exception as ex:                   # noqa: BLE001  (never leave a half download behind)
        _unlink(tmp)
        if isinstance(ex, (UpdateError, net.Cancelled)):
            raise
        raise UpdateError('the download failed (%s)' % _why(ex))
    return tmp


def swap(new, exe):
    """put the checked download `new` in place of `exe` (running: it is renamed out of the way, not overwritten) ->
    (exe, where the replaced one went). On failure exe is left as it was."""
    exe = Path(exe)
    for i in range(10):                       # an earlier update's copy, if it is still there and not running
        old = exe.with_name('%s.old%s.exe' % (exe.stem, i or ''))
        try:
            old.unlink(missing_ok=True)
            break
        except OSError:
            continue
    else:
        raise UpdateError('could not make room next to %s' % exe.name)
    try:
        _retry(os.replace, exe, old)
    except OSError as ex:
        raise UpdateError('could not replace %s (%s)' % (exe.name, _why(ex)))
    try:
        _retry(os.replace, new, exe)
    except OSError as ex:
        _retry(os.replace, old, exe)
        raise UpdateError('could not put the new version in place (%s)' % _why(ex))
    log.info('updated %s (the old one is %s)', exe, old.name)
    return exe, old


def start(exe, *args):
    """start the new exe as a fresh program (a packaged exe started from another one would otherwise take over that
    one's unpacked files, which are deleted as it exits)"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('_MEI', '_PYI'))}
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    system.start_detached([str(exe), *args], str(Path(exe).parent), env)


def undo(exe, old, why):
    """the new exe would not start (blocked by an antivirus, say): put the replaced one back -> what to tell the player"""
    try:
        _retry(os.replace, old, exe)
        return ('The new version was downloaded, but Windows would not start it (%s), so the previous version is back. '
                'Start the Map Creator again.' % _why(why))
    except OSError:
        return ('The new version was downloaded, but Windows would not start it (%s). The previous version is kept '
                'next to it as %s: rename it to %s to use it again.' % (_why(why), Path(old).name, Path(exe).name))


def cleanup():
    """remove what an update left next to this exe: the replaced copy (once it has exited) and an unfinished
    download (in the background: the replaced copy takes a few seconds to exit)"""
    if not getattr(sys, 'frozen', False) or not IN_PLACE:
        return
    exe = Path(sys.executable)
    left = [exe.with_name(EXE + '.download')]           # first: nothing is downloading yet
    left += [Path(p) for p in glob.glob(glob.escape(str(exe.with_name(exe.stem))) + '.old*.exe')]

    def work():
        for p in left:
            for _ in range(40):
                try:
                    p.unlink(missing_ok=True)
                    break
                except OSError:
                    time.sleep(0.5)
    threading.Thread(target=work, daemon=True).start()


def page(rel):
    """the window's progress page while updating"""
    return PAGE % dict(v=html.escape(rel['version']))


def run(win, rel, app_url, done, message):
    """(started with the window) download, check and swap in the new exe with progress in the window; on success
    done['exe'] / done['old'] are set and the window closes (main() then starts it), on failure the window goes on to
    the app. Closing the window while it downloads drops the update at once: the download runs in a daemon thread, so
    the program never waits for the network to end (the swap itself, quick, is never cut off halfway)."""
    closed, finished = threading.Event(), threading.Event()
    win.events.closed += closed.set
    now = [0, 0]                              # bytes downloaded, of
    got = {}
    exe = Path(sys.executable)

    def paint():                              # its own thread: the download never waits for the window
        shown = None
        while not (finished.is_set() or closed.is_set()):
            if tuple(now) != shown:
                shown = tuple(now)
                try:
                    win.evaluate_js('show(%d, %d)' % shown)
                except Exception:             # noqa: BLE001  (only the progress display)
                    pass
            time.sleep(0.25)

    def progress(n, total):
        if closed.is_set():
            raise UpdateError('the window was closed')
        now[:] = n, total

    def fetch():
        try:
            got['file'] = download(rel, exe.parent, progress)
        except Exception as ex:               # noqa: BLE001
            got['error'] = ex
    threading.Thread(target=paint, daemon=True).start()
    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    while worker.is_alive() and not closed.is_set():
        worker.join(0.2)
    if closed.is_set():
        return                                # cleanup() removes what the download left on the next start
    ex = got.get('error')
    if ex is None:
        try:
            done['exe'], done['old'] = swap(got['file'], exe)
        except Exception as e:                # noqa: BLE001
            _unlink(got['file'])
            ex = e
    finished.set()
    if ex is not None:                        # carry on with this version
        log.warning('update to %s failed: %s', rel['version'], ex)
        if closed.is_set():
            return
        win.load_url(app_url)
        message('The update to %s could not be installed: %s.\n\nThis version opens as it is. The new one is also on '
                '%s' % (rel['version'], ex, rel['page'] or 'GitHub'))
        return
    try:
        win.destroy()
    except Exception:                         # noqa: BLE001  (closed already)
        pass


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%%;background:#0b1118;color:#f6efe0;font:15px 'Segoe UI',system-ui,sans-serif}
body{display:flex;align-items:center;justify-content:center}
.box{width:440px}
h1{font-size:19px;font-weight:600;margin:0 0 16px}
.bar{height:10px;background:#1d2a37;border-radius:5px;overflow:hidden}
#b{height:100%%;width:0;background:#6cbf6c;transition:width .2s}
#t{margin-top:10px;color:#a9b4c2;font-size:13px}
</style></head><body><div class="box"><h1>Updating the Map Creator to %(v)s</h1>
<div class="bar"><div id="b"></div></div><div id="t">Starting the download from GitHub...</div></div>
<script>function show(n, t) {
  document.getElementById('b').style.width = (t ? Math.min(100, 100 * n / t) : 0) + '%%';
  document.getElementById('t').textContent = n >= t && t ? 'Checking and installing...'
    : 'Downloading from GitHub: ' + Math.round(n / 1048576) + ' of ' + Math.round(t / 1048576) + ' MB';
}</script></body></html>"""
