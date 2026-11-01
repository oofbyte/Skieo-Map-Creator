"""Network recovery preserves whole immutable responses; no synthetic geometry."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit

import pytest
from skieo_creator.osm import automatic_osm as osm
from skieo_creator.osm.io import write_json

SPEC=dict(lat=46.25328,lon=10.53563,size_m=10200,osm_lifts=True,osm_pistes=True)
RAW=b'{"elements":[{"type":"node","id":1,"lat":46.2,"lon":10.5}]}'
TOKEN=SimpleNamespace(check=lambda:None)


def test_failover_keeps_full_original_query_and_bytes(tmp_path):
    calls=[]
    def get(url):
        calls.append(url)
        if len(calls)==1:raise TimeoutError('502 Bad Gateway')
        if len(calls)==2:raise TimeoutError('read timed out')
        return RAW
    path=osm.acquire(SPEC,tmp_path,get,TOKEN,lambda *a:None)
    assert len(calls)==3
    assert all(parse_qs(urlsplit(url).query)['data']==[osm.query_for(SPEC)] for url in calls)
    assert path.read_bytes()==RAW
    assert path.stem==hashlib.sha256(RAW).hexdigest()
    receipt=json.loads(next(tmp_path.glob('*.provenance.json')).read_text())
    assert receipt['endpoint']==osm.ENDPOINTS[2]
    assert receipt['query']==osm.query_for(SPEC)


def test_cache_is_exact_hash_checked_and_does_not_rewrite_receipts(tmp_path):
    path=osm.acquire(SPEC,tmp_path,lambda url:RAW,TOKEN,lambda *a:None)
    original={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    def no_network(url):raise AssertionError('Verified recent response must be reused')
    assert osm.acquire(SPEC,tmp_path,no_network,TOKEN,lambda *a:None)==path
    assert original=={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='original OSM data changed'):
        osm.acquire(SPEC,tmp_path,no_network,TOKEN,lambda *a:None)


@pytest.mark.parametrize('different', ['area','flags','expired'])
def test_unrelated_or_expired_response_never_substitutes_data(tmp_path,different):
    osm.acquire(SPEC,tmp_path,lambda url:RAW,TOKEN,lambda *a:None)
    spec=dict(SPEC)
    if different=='area':spec['lat']+=0.01
    elif different=='flags':spec['osm_pistes']=False
    else:
        receipt=next(tmp_path.glob('*.provenance.json'));info=json.loads(receipt.read_text())
        info['retrieved_utc']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();write_json(receipt,info)
    calls=[]
    def unavailable(url):calls.append(url);raise TimeoutError('unavailable')
    with pytest.raises(ValueError,match='OpenStreetMap servers could not complete'):
        osm.acquire(spec,tmp_path,unavailable,TOKEN,lambda *a:None)
    assert len(calls)==len(osm.ENDPOINTS)


def test_cancellation_during_download_is_not_swallowed(tmp_path):
    class Cancelled(Exception):pass
    cancelled=False
    def check():
        if cancelled:raise Cancelled()
    def get(url):
        nonlocal cancelled
        cancelled=True
        raise TimeoutError()
    with pytest.raises(Cancelled):osm.acquire(SPEC,tmp_path,get,SimpleNamespace(check=check),lambda *a:None)
    assert not list(tmp_path.glob('*.json'))
