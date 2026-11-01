"""HTTPS contract checks with mocks; no Windows or live TLS certification."""
import io
import ssl
import sys
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from skieo_creator import net
from skieo_creator.osm import automatic_osm as osm


def test_windows_context_matches_official_sdk_without_global_injection(monkeypatch):
    context=ssl.create_default_context()
    calls=[]
    def factory(protocol):calls.append(protocol);return context
    monkeypatch.setitem(sys.modules,'truststore',SimpleNamespace(SSLContext=factory))
    monkeypatch.setattr(net.sys,'platform','win32')
    assert net._ssl_context() is context
    assert calls==[ssl.PROTOCOL_TLS_CLIENT]
    assert context.check_hostname is True and context.verify_mode==ssl.CERT_REQUIRED


def test_proxy_download_receives_verified_context(monkeypatch):
    seen={}
    def urlopen(request,**kwargs):
        seen.update(kwargs)
        return io.BytesIO(b'response')
    monkeypatch.setattr(net.urllib.request,'getproxies',lambda:{'https':'http://example.invalid:8080'})
    monkeypatch.setattr(net.urllib.request,'urlopen',urlopen)
    assert net.get('https://example.invalid/data',retries=0)==b'response'
    assert seen['context'] is net._ssl
    assert net._ssl.check_hostname and net._ssl.verify_mode==ssl.CERT_REQUIRED


def test_direct_download_receives_same_verified_context(monkeypatch):
    seen={}
    class Connection:
        def __init__(self,host,port,**kwargs):seen.update(kwargs)
        def request(self,*a,**kw):pass
        def getresponse(self):
            response=io.BytesIO(b'response');response.status=200;response.will_close=True
            return response
        def close(self):pass
    monkeypatch.setattr(net.urllib.request,'getproxies',lambda:{})
    monkeypatch.setattr(net.http.client,'HTTPSConnection',Connection)
    monkeypatch.setattr(net,'_pools',{})
    assert net.get('https://direct.invalid/data',retries=0)==b'response'
    assert seen['context'] is net._ssl
    assert net._ssl.check_hostname and net._ssl.verify_mode==ssl.CERT_REQUIRED


@pytest.mark.parametrize('proxy',[False,True])
def test_certificate_rejection_is_never_retried_without_validation(monkeypatch,proxy):
    calls=[]
    failure=ssl.SSLCertVerificationError(1,'unable to get local issuer certificate')
    def reject(*a,**kw):
        calls.append(kw)
        raise URLError(failure) if proxy else failure
    monkeypatch.setattr(net.urllib.request,'getproxies',lambda:{'https':'proxy'} if proxy else {})
    if proxy:monkeypatch.setattr(net.urllib.request,'urlopen',reject)
    else:monkeypatch.setattr(net,'_pooled',reject)
    with pytest.raises(net.NetError,match='HTTPS certificate verification failed') as error:
        net.get('https://invalid.invalid/data',retries=4)
    assert error.value.category=='tls' and len(calls)==1


def test_http_gateway_failure_is_not_presented_as_local_connection_failure(monkeypatch):
    monkeypatch.setattr(net.urllib.request,'getproxies',lambda:{})
    def unavailable(*a):raise net._HTTPStatus(502,'Bad Gateway')
    monkeypatch.setattr(net,'_pooled',unavailable)
    with pytest.raises(net.NetError,match='remote service') as error:
        net.get('https://unavailable.invalid/data',retries=0)
    assert error.value.category=='server' and error.value.status==502


def test_osm_failure_keeps_certificate_diagnosis(tmp_path):
    def unavailable(url):raise net.NetError('untrusted issuer',category='tls')
    with pytest.raises(ValueError,match='HTTPS certificate verification failed'):
        osm.acquire(dict(lat=46.2,lon=10.5,size_m=10200,osm_lifts=True),tmp_path,unavailable,
                    SimpleNamespace(check=lambda:None),lambda *a:None)
