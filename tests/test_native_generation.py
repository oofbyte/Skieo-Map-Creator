import hashlib
import pytest
from skieo_creator.osm import native_generation


def test_native_contract_requires_matching_build_and_binary(tmp_path, monkeypatch):
    original = native_generation.contract()
    binary = tmp_path/'SkiResortSimulator/Binaries/Win64/skieogame.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'TEST ONLY - no executable code')
    fixture = dict(original, executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
    monkeypatch.setattr(native_generation, 'contract', lambda: fixture)
    assert native_generation.verify(original['game_build'], tmp_path)['local_executable_verified'] is True
    with pytest.raises(ValueError, match='not verified for game build'):
        native_generation.verify('unverified-test-build', tmp_path)
    binary.write_bytes(b'changed')
    with pytest.raises(ValueError, match='executable differs'):
        native_generation.verify(original['game_build'], tmp_path)


def test_missing_binary_does_not_assume_native_fallback(tmp_path):
    with pytest.raises(ValueError, match='executable differs'):
        native_generation.verify(native_generation.contract()['game_build'], tmp_path)


def test_contract_is_a_native_load_behavior_without_runtime_hooks():
    value = native_generation.contract()
    assert value['waypoints_minimum'] == 2
    assert value['serialized_tower_count'] == 0
    assert value['runtime_offsets_or_hooks'] is False
    assert value['gameplay_verified'] is False


def test_approved_variant_does_not_accept_other_builds_or_other_changes(tmp_path, monkeypatch):
    original = native_generation.contract()
    binary = tmp_path/'SkiResortSimulator/Binaries/Win64/skieogame.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'test approved variant; not executable')
    observed = hashlib.sha256(binary.read_bytes()).hexdigest()
    fixture = dict(original, approved_executable_variants={observed: {'name': 'test-only approved variant'}})
    monkeypatch.setattr(native_generation, 'contract', lambda: fixture)
    assert native_generation.verify(original['game_build'], tmp_path)['observed_executable_sha256'] == observed
    with pytest.raises(ValueError, match='not verified for game build'):
        native_generation.verify('another-build', tmp_path)
    binary.write_bytes(binary.read_bytes()+b'additional modification')
    with pytest.raises(ValueError, match='executable differs'):
        native_generation.verify(original['game_build'], tmp_path)


def test_upstream_tree_patch_on_private_copy_matches_approved_binary(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from skieo_creator import install
    source = os.environ.get('SKIEO_CREATOR_GAME_EXE')
    if not source:
        pytest.skip('Supply a private local executable; no game code is bundled')
    game = tmp_path/'game'
    binary = game/'SkiResortSimulator/Binaries/Win64/skieogame.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(Path(source).read_bytes())
    monkeypatch.setattr(install, 'game_running', lambda: False)
    assert install.raise_tree_limit(game) == 20000000  # Only the disposable private copy.
    proof = native_generation.verify(native_generation.contract()['game_build'], game)
    assert proof['observed_executable_variant'] == 'upstream-1.3.0-tree-limit-20000000'
    assert proof['observed_executable_sha256'] == '08b7cadbc8c5358b65b536a56e24ba4193609dcb3bce5c9445695c01adc1a660'
    assert install._tree_check_at(binary.read_bytes()) is not None
