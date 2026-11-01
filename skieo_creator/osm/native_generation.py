"""Guard native lift-generation methods for the selected local game.

This module does not call game code, patch a binary or install a runtime hook.
The early empty-Towers load fallback was observed to produce zero-height bases.
Explicitly pending baked actors require a separate terrain-ready initializer.
"""
import json
from pathlib import Path
from .. import paths
from .io import sha256


def contract() -> dict:
    return json.loads(paths.resource('data', 'osm', 'native-tower-load-contract.json').read_text(encoding='utf-8'))


def verify(game_build: str, game_folder: Path) -> dict:
    expected = contract()
    if str(game_build) != expected['game_build']:
        raise ValueError('Native tower generation is not verified for game build '+str(game_build))
    executable = Path(game_folder)/'SkiResortSimulator/Binaries/Win64/skieogame.exe'
    if not executable.is_file():
        raise ValueError('Game executable differs from the verified native tower-generation contract')
    observed = sha256(executable)
    if observed == expected['executable_sha256']:
        variant = 'stock'
    else:
        approved = expected.get('approved_executable_variants', {}).get(observed)
        if approved is None:
            raise ValueError('Game executable differs from the verified native tower-generation contract')
        variant = approved['name']
    return {**expected, 'local_executable_verified': True,
            'observed_executable_sha256': observed, 'observed_executable_variant': variant}
