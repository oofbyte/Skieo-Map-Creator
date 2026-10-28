"""HTTP downloads with an identifying User-Agent, retries, per-host connection limits and cancellation."""
import http.client
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION

log = logging.getLogger(__name__)
USER_AGENT = 'SkiEO-Map-Creator/%s (community mod tool for the game Ski-E-O!; Windows)' % VERSION
_host_slots = {}
_host_lock = threading.Lock()


class Cancelled(Exception):
    pass


class NetError(Exception):
    pass


class CancelToken:
    def __init__(self):
        self._ev = threading.Event()

    def cancel(self):
        self._ev.set()

    @property
    def cancelled(self):
        return self._ev.is_set()

    def check(self):
        if self._ev.is_set():
            raise Cancelled()

    def sleep(self, seconds):
        if self._ev.wait(seconds):
            raise Cancelled()


NEVER = CancelToken()


def _slot(host, limit):
    with _host_lock:
        s = _host_slots.get(host)
        if s is None:
            s = _host_slots[host] = threading.BoundedSemaphore(limit)
        return s


class _HTTPStatus(Exception):
    def __init__(self, code, reason):
        super().__init__('%d %s' % (code, reason))
        self.code, self.reason = code, reason


_pools = {}
_pool_lock = threading.Lock()
_ssl = ssl.create_default_context()


def _pooled(parts, headers, timeout, cancel):
    """GET over a kept-alive HTTPS connection (map tiles: one TLS handshake per server instead of per tile)"""
    host = parts.hostname
    with _pool_lock:
        pool = _pools.setdefault(host, [])
        conn = pool.pop() if pool else None
    if conn is None:
        conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout, context=_ssl)
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    try:
        try:
            conn.request('GET', path, headers=headers)
            r = conn.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, http.client.CannotSendRequest):
            conn.close()                                           # the server dropped the idle connection
            conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout, context=_ssl)
            conn.request('GET', path, headers=headers)
            r = conn.getresponse()
        chunks = []
        while True:
            cancel.check()
            b = r.read(1 << 16)
            if not b:
                break
            chunks.append(b)
        if r.status != 200:
            raise _HTTPStatus(r.status, r.reason)
        if r.will_close:
            conn.close()
        else:
            with _pool_lock:
                _pools[host].append(conn)
            conn = None
        return b''.join(chunks)
    finally:
        if conn is not None:
            conn.close()


def get(url, timeout=60, retries=4, cancel=NEVER, accept=None, per_host=4, not_found_ok=False):
    """GET url -> bytes. 404 returns None when not_found_ok. Raises NetError with a readable message."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ''
    headers = {'User-Agent': USER_AGENT, 'Accept': accept or '*/*', 'Accept-Encoding': 'identity'}
    direct = parts.scheme == 'https' and not urllib.request.getproxies()
    last = None
    for attempt in range(retries + 1):
        cancel.check()
        try:
            with _slot(host, per_host):
                cancel.check()                    # may have waited for the slot a while
                if direct:
                    return _pooled(parts, headers, timeout, cancel)
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                    chunks = []
                    while True:
                        cancel.check()
                        b = r.read(1 << 16)
                        if not b:
                            break
                        chunks.append(b)
                    return b''.join(chunks)
        except Cancelled:
            raise
        except (urllib.error.HTTPError, _HTTPStatus) as e:
            if e.code == 404 and not_found_ok:
                return None
            last = 'the server answered %d %s' % (e.code, e.reason)
            if e.code in (400, 401, 403, 404, 410):
                break
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, http.client.HTTPException) as e:
            last = str(getattr(e, 'reason', e) or type(e).__name__)
        log.warning('download failed (%s), attempt %d: %s', host, attempt + 1, last)
        if attempt < retries:
            cancel.sleep(min(2 ** attempt, 20))
    raise NetError('Could not download from %s (%s). Check your internet connection and try again.' % (host, last))


class RateLimit:
    """At most one call per `interval` seconds (Nominatim's usage policy asks for 1 request per second)."""

    def __init__(self, interval):
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval
