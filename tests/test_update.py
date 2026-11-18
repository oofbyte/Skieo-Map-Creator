"""Offline checks of the self-update (skieo_creator/update.py): no network, no real exe. usage: python tests/test_update.py"""
import hashlib
import http.client
import io
import threading
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import net, update  # noqa: E402

U = update
DL = U.DOWNLOADS + 'v9.9.9/'

# ---- versions
assert U._version('v1.7.3') == (1, 7, 3) and U._version('1.10.0') == (1, 10, 0) and U._version('v2.0.0') > (1, 99, 99)
assert U._version('v1.8.0-beta') is None and U._version('') is None and U._version(None) is None


# ---- the release as GitHub's API describes it
def api(tag='v9.9.9', **kw):
    r = dict(tag_name=tag, draft=False, prerelease=False, html_url='https://github.com/x/y/releases/tag/' + tag,
             body='- **Trails**: no loops\n- `Forest` real\n\n### Download\n**SkiEO-Map-Creator.exe**: one file',
             assets=[dict(name=U.EXE, size=180 << 20, browser_download_url=DL + U.EXE),
                     dict(name=U.SUMS, size=88, browser_download_url=DL + U.SUMS)])
    r.update(kw)
    return json.dumps(r).encode()



if not U.IN_PLACE:
    # ---- macOS: no exe swap; a newer release with the macOS app attached is offered, and its page opens
    real_get = net.get
    try:
        zip_ = dict(name=U.MAC_ZIP, size=90 << 20, browser_download_url=DL + U.MAC_ZIP)
        sums = dict(name=U.SUMS, size=88, browser_download_url=DL + U.SUMS)
        net.get = lambda url, **kw: api(assets=[zip_, sums])
        rel = U.release()
        assert rel['version'] == '9.9.9' and rel['exe'] == DL + U.MAC_ZIP and rel['page'].endswith('v9.9.9'), rel
        net.get = lambda url, **kw: api()
        assert U.release() is None, 'a release with only the Windows exe: nothing to offer on macOS'
        net.get = lambda url, **kw: api(assets=[zip_, dict(sums, browser_download_url='https://evil.example/s')])
        assert U.release() is None, 'only this repository\'s own release files'
    finally:
        net.get = real_get
    text = U.prompt(dict(version='9.9.9', notes='- **Trails**: no loops', size=90 << 20))
    assert 'download page' in text and U.MAC_ZIP in text and 'Trails: no loops' in text, text
    sys.frozen = True
    try:
        U.cleanup()                           # nothing next to the app to clean up
    finally:
        del sys.frozen
    print('update (macOS): all checks passed')
    sys.exit(0)

real_get = net.get
try:
    net.get = lambda url, **kw: api()
    rel = U.release()
    assert rel['version'] == '9.9.9' and rel['exe'] == DL + U.EXE and rel['sums'] == DL + U.SUMS and rel['size'] == 180 << 20
    net.get = lambda url, **kw: api(assets=[dict(name=U.EXE, size=1, browser_download_url=DL + U.EXE)])
    assert U.release() is None, 'no checksums attached yet: nothing to offer'
    net.get = lambda url, **kw: api(prerelease=True)
    assert U.release() is None
    net.get = lambda url, **kw: api(assets=[dict(name=U.EXE, size=1, browser_download_url='https://evil.example/x.exe'),
                                            dict(name=U.SUMS, size=1, browser_download_url=DL + U.SUMS)])
    assert U.release() is None, 'only this repository\'s release files'
    net.get = lambda url, **kw: api(tag='v1.8.0-rc1')
    assert U.release() is None

    # newer(): only for the packaged exe, only when newer, quiet when offline
    net.get = lambda url, **kw: api()
    assert U.newer() is None, 'never when run from the source'
    sys.frozen = True
    real_version = U.VERSION
    for have, expect in (('1.7.3', True), ('9.9.9', False), ('10.0.0', False), ('1.8.0-dev', False)):
        U.VERSION = have
        assert bool(U.newer()) is expect, have
    U.VERSION = '1.7.3'

    def offline(url, **kw):
        raise net.NetError('Could not download from api.github.com (timed out).')
    net.get = offline
    assert U.newer() is None
    net.get = lambda url, **kw: b'<html>not json</html>'
    assert U.newer() is None
    os.environ['SKIEO_NO_UPDATE'] = '1'
    net.get = lambda url, **kw: api()
    assert U.newer() is None
    del os.environ['SKIEO_NO_UPDATE']
    U.VERSION = real_version
finally:
    net.get = real_get
    if hasattr(sys, 'frozen'):
        del sys.frozen

# ---- the message box text: the changelog part only, plain
text = U.prompt(json.loads(api()) | dict(version='9.9.9', notes=json.loads(api())['body'], size=180 << 20))
assert '9.9.9' in text and '180 MB' in text and '• Trails: no loops' in text and '• Forest real' in text, text
assert 'Download' not in text and '**' not in text and '`' not in text
assert U.notes('x\n' * 1000).endswith('…') and len(U.notes('x\n' * 1000)) <= 702

# ---- downloads only from GitHub, over https
for ok in (DL + U.EXE, 'https://objects.githubusercontent.com/a', 'https://release-assets.githubusercontent.com/b'):
    U._check_url(ok)
for bad in ('http://github.com/x', 'https://github.com.evil.example/x', 'https://evil.example/x', 'file:///c:/x.exe'):
    try:
        U._check_url(bad)
        raise AssertionError('accepted ' + bad)
    except U.UpdateError:
        pass


# ---- download(): checked against SHA256SUMS.txt, must be a Windows program of a real size
class Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


def fake_release(exe_bytes, sums_line=None):
    sums = sums_line if sums_line is not None else '%s  %s\n' % (hashlib.sha256(exe_bytes).hexdigest(), U.EXE)
    files = {DL + U.SUMS: sums.encode(), DL + U.EXE: exe_bytes}
    U._open = lambda url, timeout: Resp(files[url])
    return dict(version='9.9.9', exe=DL + U.EXE, sums=DL + U.SUMS, size=len(exe_bytes), page='', notes='')


real_open, real_min = U._open, U.MIN_MB
U.MIN_MB = 0
tmpdir = Path(tempfile.mkdtemp(prefix='upd_'))
try:
    good = b'MZ' + os.urandom(4096)
    seen = []
    path = U.download(fake_release(good), tmpdir, lambda n, t: seen.append((n, t)))
    assert path.read_bytes() == good and seen[-1] == (len(good), len(good))
    for data, sums, why in ((good, '%s  %s\n' % ('0' * 64, U.EXE), 'damaged'),
                            (b'PK' + os.urandom(64), None, 'not the Map Creator'),
                            (good, 'nothing here\n', 'no checksum')):
        try:
            U.download(fake_release(data, sums), tmpdir)
            raise AssertionError('accepted: ' + why)
        except U.UpdateError as ex:
            assert why in str(ex), ex
        assert not (tmpdir / (U.EXE + '.download')).exists() or why == 'no checksum'
    U.MIN_MB = 1
    try:
        U.download(fake_release(good), tmpdir)
        raise AssertionError('accepted a 4 KB exe')
    except U.UpdateError as ex:
        assert 'not the Map Creator' in str(ex)
    U.MIN_MB = 0

    # a connection dropped half way (found in review): a plain failure, no half download left behind
    class Dropped(Resp):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b'MZ', 4096)
    rel = fake_release(good)
    files_open = U._open
    U._open = lambda url, timeout: Dropped(b'') if url.endswith('.exe') else files_open(url, timeout)
    try:
        U.download(rel, tmpdir)
        raise AssertionError('accepted a dropped download')
    except U.UpdateError as ex:
        assert 'the download failed' in str(ex), ex
    assert not (tmpdir / (U.EXE + '.download')).exists()
    # a folder it may not write to: says so (here the download's name is taken by a folder)
    (tmpdir / (U.EXE + '.download')).mkdir()
    try:
        U.download(fake_release(good), tmpdir)
        raise AssertionError('wrote into a folder')
    except U.UpdateError as ex:
        assert 'write to its folder' in str(ex), ex
    (tmpdir / (U.EXE + '.download')).rmdir()

    # ---- swap(): the new exe takes the old one's name, the old one is kept beside it; put back if that fails
    exe = tmpdir / 'SkiEO-Map-Creator.exe'
    exe.write_bytes(b'MZ old')
    (tmpdir / 'SkiEO-Map-Creator.old.exe').write_bytes(b'MZ older')             # an earlier update's copy
    new = tmpdir / (U.EXE + '.download')
    new.write_bytes(b'MZ new')
    assert U.swap(new, exe) == (exe, tmpdir / 'SkiEO-Map-Creator.old.exe') and exe.read_bytes() == b'MZ new'
    assert (tmpdir / 'SkiEO-Map-Creator.old.exe').read_bytes() == b'MZ old' and not new.exists()
    new.write_bytes(b'MZ newer')
    real_replace, calls = os.replace, []

    def flaky(a, b):                          # the new file stays locked (an antivirus scanning it, say)
        calls.append((a, b))
        if Path(a) == new:
            raise PermissionError('locked by an antivirus')
        real_replace(a, b)
    os.replace = flaky
    try:
        U.swap(new, exe)
        raise AssertionError('swap went on after a failure')
    except U.UpdateError as ex:
        assert 'could not put the new version in place' in str(ex)
    finally:
        os.replace = real_replace
    assert exe.read_bytes() == b'MZ new', 'the running exe is back under its name'
    assert sum(Path(a) == new for a, _ in calls) == 15, 'a short lock is waited out'
    calls.clear()
    tries = iter([PermissionError('busy a moment')])

    def busy_once(a, b):
        e = next(tries, None)
        if e:
            raise e
        real_replace(a, b)
    os.replace = busy_once
    try:
        assert U.swap(new, exe)[0] == exe and exe.read_bytes() == b'MZ newer'
    finally:
        os.replace = real_replace
    # undo(): the new one would not start, the old one goes back under the exe's name
    msg = U.undo(exe, tmpdir / 'SkiEO-Map-Creator.old.exe', OSError('blocked'))
    assert 'previous version is back' in msg and exe.read_bytes() == b'MZ new', msg

    # ---- cleanup(): the replaced copy and an unfinished download go on the next start
    (tmpdir / (U.EXE + '.download')).write_bytes(b'partial')
    sys.frozen, real_exe = True, sys.executable
    sys.executable = str(exe)
    try:
        U.cleanup()
        for _ in range(50):
            if not list(tmpdir.glob('*.old*.exe')) and not (tmpdir / (U.EXE + '.download')).exists():
                break
            time.sleep(0.1)
        assert sorted(p.name for p in tmpdir.iterdir()) == ['SkiEO-Map-Creator.exe'], list(tmpdir.iterdir())
    finally:
        sys.executable = real_exe
        del sys.frozen
finally:
    U._open, U.MIN_MB = real_open, real_min
    for p in tmpdir.glob('*'):
        p.unlink()
    tmpdir.rmdir()

# ---- the progress page
pg = U.page(dict(version='9.9.9'))
assert 'Updating the Map Creator to 9.9.9' in pg and '%(' not in pg and 'function show' in pg


# ---- run(): the window's side of it, with a stand-in window
class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for h in self.handlers:
            h()


class Win:
    def __init__(self):
        self.events = type('E', (), {})()
        self.events.closed = Event()
        self.js, self.urls, self.destroyed = [], [], False

    def evaluate_js(self, s):
        self.js.append(s)

    def load_url(self, u):
        self.urls.append(u)

    def destroy(self):
        self.destroyed = True


REL = dict(version='9.9.9', page='https://github.com/x', exe='', sums='', size=1, notes='')
real_download, real_swap = U.download, U.swap
try:
    # it worked: the window closes, main() gets the new exe
    U.download = lambda rel, folder, progress: (progress(5, 10), progress(10, 10), Path('new.download'))[-1]
    U.swap = lambda new, exe: (Path('exe.exe'), Path('exe.old.exe'))
    w, done, said = Win(), {}, []
    U.run(w, REL, 'http://app', done, said.append)
    assert done == {'exe': Path('exe.exe'), 'old': Path('exe.old.exe')} and w.destroyed and not w.urls and not said
    # it failed: the window goes on to the app and says why
    def broken(rel, folder, progress):
        raise U.UpdateError('the download failed (timed out)')
    U.download = broken
    w, done, said = Win(), {}, []
    U.run(w, REL, 'http://app', done, said.append)
    assert not done and not w.destroyed and w.urls == ['http://app'] and 'timed out' in said[0], said
    # the window closed during a slow download (found in review): run() returns at once, quietly
    stuck = threading.Event()
    U.download = lambda rel, folder, progress: stuck.wait(30)
    w, done, said = Win(), {}, []
    t = threading.Thread(target=U.run, args=(w, REL, 'http://app', done, said.append))
    t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    w.events.closed.fire()
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 1.0, 'run() waited for the network'
    assert not done and not said and not w.urls
    stuck.set()
finally:
    U.download, U.swap = real_download, real_swap

# ---- the footer's Check for updates (api.check_update): from the source, newest, newer, offline
from skieo_creator import api as A  # noqa: E402

app = A.Api()
assert app.check_update()['state'] == 'source'
real_release = U.release
sys.frozen = True
try:
    U.release = lambda tag=None: dict(REL, version='9.9.9', notes='- **Trails** fixed', size=180 << 20)
    r = app.check_update()
    assert r['ok'] and r['state'] == 'newer' and r['version'] == '9.9.9' and r['size_mb'] == 180, r
    assert '• Trails fixed' in r['notes'] and app._update['version'] == '9.9.9'
    U.release = lambda tag=None: dict(REL, version=U.VERSION)
    assert app.check_update()['state'] == 'latest'

    def unreachable(tag=None):
        raise net.NetError('Could not download from api.github.com (timed out).')
    U.release = unreachable
    r = app.check_update()
    assert not r['ok'] and 'GitHub could not be reached' in r['error'], r
finally:
    U.release = real_release
    del sys.frozen

# ---- a cancelled download (the job's Cancel) is a cancel, not "the download failed", and leaves nothing behind
tmpdir = Path(tempfile.mkdtemp(prefix='upd_'))
real_min = U.MIN_MB
U.MIN_MB = 0
try:
    def cancel_now(n, t):
        raise net.Cancelled()
    try:
        U.download(fake_release(b'MZ' + os.urandom(1 << 17)), tmpdir, cancel_now)
        raise AssertionError('not cancelled')
    except net.Cancelled:
        pass
    assert not list(tmpdir.iterdir())
finally:
    U._open, U.MIN_MB = real_open, real_min
    tmpdir.rmdir()
print('update: all checks passed')
