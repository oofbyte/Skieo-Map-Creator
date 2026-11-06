"""Regression cases for combining upstream 1.2.0 with native OSM preparation."""
from types import SimpleNamespace

import pytest
from skieo_creator import api, modlib
from skieo_creator.osm.automatic_osm import query_for


@pytest.mark.parametrize('folder', ['SkiEO-CreatorSupports-osmbranch_0001', 'OtherDisabledMod'])
def test_weather_off_preserves_loader_needed_by_other_mods(tmp_path,folder):
    b=modlib._bin(tmp_path)
    other=b/'ue4ss/Mods'/folder/'Scripts/main.lua'
    other.parent.mkdir(parents=True);other.write_bytes(b'unchanged')
    (b/'dwmapi.dll').write_bytes(b'proxy')
    (b/'ue4ss/UE4SS.dll').write_bytes(b'loader')
    (b/'ue4ss'/modlib.MARKER).touch()
    own=b/'ue4ss/Mods/SkiWeather/Scripts/main.lua'
    own.parent.mkdir(parents=True);own.touch()
    modlib._set_lua(tmp_path,modlib.MODS['weather'],False)
    assert not own.exists()
    assert other.read_bytes()==b'unchanged'
    assert (b/'dwmapi.dll').read_bytes()==b'proxy'
    assert (b/'ue4ss/UE4SS.dll').read_bytes()==b'loader'


def test_weather_off_removes_unused_owned_loader(tmp_path):
    b=modlib._bin(tmp_path)
    (b/'ue4ss').mkdir(parents=True)
    (b/'ue4ss'/modlib.MARKER).touch()
    (b/'dwmapi.dll').touch()
    modlib._set_lua(tmp_path,modlib.MODS['weather'],False)
    assert not (b/'dwmapi.dll').exists()
    assert not (b/'ue4ss').exists()


def test_weather_does_not_overwrite_existing_runtime_without_proxy(tmp_path):
    b=modlib._bin(tmp_path);loader=b/'ue4ss/UE4SS.dll'
    loader.parent.mkdir(parents=True);loader.write_bytes(b'existing runtime')
    with pytest.raises(ValueError,match='Existing files were preserved'):
        modlib._set_lua(tmp_path,modlib.MODS['weather'],True)
    assert loader.read_bytes()==b'existing runtime'
    assert not (b/'dwmapi.dll').exists()


def test_osm_window_accepts_new_upstream_twenty_km_limit():
    assert 'out' in query_for({'lat':46.3,'lon':10.6,'size_m':20000,'osm_lifts':True,'osm_pistes':True})
    with pytest.raises(ValueError,match='supported map domain'):
        query_for({'lat':46.3,'lon':10.6,'size_m':20001})


def test_creation_retains_fade_and_independent_import_flags():
    creator=api.Api.__new__(api.Api)
    creator._prepare=lambda *a:None
    creator._records=lambda:[]
    creator._game=SimpleNamespace(installed=lambda:{})
    creator._preview={'dem':object()}
    creator._next_order=lambda:1041
    creator._build_and_package=lambda job,spec,*a,**kw:spec
    creator._install_when_closed=lambda *a:None
    creator._map_view=lambda rec:rec
    names=('name','lat','lon','size_m','biome','treeline','valley','snow','max_trees','tree_slope',
           'treeline_fade','acres','base_rc','base_latlon','seed','tg','source')
    params=dict.fromkeys(names,0)
    params.update(name='Compatibility',treeline_fade=200,osm_lifts=True,osm_pistes=False)
    result=creator._do_create(None,params)
    assert result['treeline_fade']==200
    assert result['osm_lifts'] is True and result['osm_pistes'] is False
