"""Offline checks of the game tree limit handling (skieo_creator/install.py: restore_tree_limit, tree_mod_on) on a
tiny made-up exe laid out like the game's: usage: python tests/test_tree_limit.py"""
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import install  # noqa: E402

MAC = install.system.MAC
install.system.MAC = False                # the Windows exe handling first; macOS at the end


def fake_exe(limit):
    """a PE with .text (raw 0x400, RVA 0x1000) holding "cmp ecx, limit ; jbe ... lea rdx, [message]" and .rdata (raw
    0x600, RVA 0x2000) holding the message, as the game's baked-tree check is laid out; returns (bytes, imm offset)"""
    d = bytearray(0x800)
    d[0:2] = b'MZ'
    struct.pack_into('<I', d, 0x3C, 0x80)
    d[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', d, 0x84, 0x8664, 2, 0, 0, 0, 0xF0, 0x22)
    struct.pack_into('<H', d, 0x98, 0x20B)
    for i, (name, va, raw) in enumerate(((b'.text', 0x1000, 0x400), (b'.rdata', 0x2000, 0x600))):
        o = 0x80 + 24 + 0xF0 + 40 * i
        d[o:o + 8] = name.ljust(8, b'\0')
        struct.pack_into('<IIII', d, o + 8, 0x200, va, 0x200, raw)
    text = 'RecordCount 2000000 is over the bake cap'.encode('utf-16-le')
    d[0x610:0x610 + len(text)] = text                               # the message starts at RVA 0x2010
    d[0x420:0x426] = b'\x81\xF9' + struct.pack('<I', limit)         # cmp ecx, limit
    d[0x426:0x42C] = b'\x0F\x86' + struct.pack('<i', 0x40)          # jbe (near)
    d[0x430:0x437] = b'\x48\x8D\x15' + struct.pack('<i', 0x2010 - 0x1037)   # lea rdx, [RVA 0x2010]
    return bytes(d), 0x422


def run(limit, running=False):
    install._tree_limit_seen.clear()
    install._tree_check_seen.clear()
    with tempfile.TemporaryDirectory() as t:
        exe = Path(t) / 'SkiResortSimulator' / 'Binaries' / 'Win64' / install.GAME_EXE
        exe.parent.mkdir(parents=True)
        data, at = fake_exe(limit)
        exe.write_bytes(data)
        real = install.game_running
        install.game_running = lambda: running
        try:
            got = install.restore_tree_limit(t)
        finally:
            install.game_running = real
        after = exe.read_bytes()
        return got, struct.unpack_from('<I', after, at)[0], after == data


assert install._tree_check_at(fake_exe(1500000)[0]) == 0x422
# our old patch is undone, back to the game's own value; nothing else in the file changes
got, value, same = run(install.OLD_TREE_PATCH)
assert (got, value, same) == (install.GAME_TREE_LIMIT, install.GAME_TREE_LIMIT, False)
assert fake_exe(install.GAME_TREE_LIMIT)[0] == fake_exe(install.GAME_TREE_LIMIT)[0]
# the game's own value, or any value we didn't write, is left as it is
assert run(install.GAME_TREE_LIMIT) == (install.GAME_TREE_LIMIT, install.GAME_TREE_LIMIT, True)
assert run(3000000) == (3000000, 3000000, True)
# while the game runs the exe is locked: nothing is written, the next look tries again
assert run(install.OLD_TREE_PATCH, running=True) == (install.OLD_TREE_PATCH, install.OLD_TREE_PATCH, True)

# the mod counts as on only with its DLL and enabled.txt in the game's mods folder
with tempfile.TemporaryDirectory() as t:
    mod = Path(t) / 'mods' / install.TREE_MOD
    assert not install.tree_mod_on(t)
    (mod / 'Native').mkdir(parents=True)
    (mod / 'Native' / 'TreeLimit.dll').write_bytes(b'MZ')
    assert not install.tree_mod_on(t)
    (mod / 'enabled.txt').write_bytes(b'')
    assert install.tree_mod_on(t)

# macOS: the game there was never patched, so its files are never touched (and Experimental mode is off)
install.system.MAC = True
assert run(install.OLD_TREE_PATCH) == (None, install.OLD_TREE_PATCH, True)
install.system.MAC = MAC

print('tree limit: OK')
