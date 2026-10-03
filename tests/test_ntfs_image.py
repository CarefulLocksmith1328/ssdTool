"""End-to-end tests against real NTFS images created with ntfs-3g.

Requires ``mkntfs`` and ``ntfs-3g`` (FUSE) and root permissions to mount;
skipped otherwise.  Run from the repository root:  ``python -m pytest``
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import subprocess

import pytest

from ssdtool.device import RawDevice
from ssdtool.ntfs import NTFSVolume, lznt1_decompress
from ssdtool.recover import recover_files
from ssdtool.scanner import scan_volume
from ssdtool.wipe import shred_deleted, wipe_free_space

HAVE_TOOLS = bool(shutil.which("mkntfs") and shutil.which("ntfs-3g")) and os.path.exists("/dev/fuse") \
    and hasattr(os, "geteuid") and os.geteuid() == 0

needs_ntfs = pytest.mark.skipif(not HAVE_TOOLS, reason="mkntfs/ntfs-3g/root nicht verfügbar")

MARKER = b"GEHEIMER-INHALT-0815-"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Image:
    def __init__(self, tmp_path, size="64M", compression=False):
        self.path = str(tmp_path / "vol.img")
        self.mnt = str(tmp_path / "mnt")
        os.mkdir(self.mnt)
        subprocess.run(["truncate", "-s", size, self.path], check=True)
        subprocess.run(["mkntfs", "-F", "-Q", "-q", "-L", "TEST", self.path], check=True, capture_output=True)
        self.opts = ["-o", "compression"] if compression else []

    def mount(self):
        subprocess.run(["ntfs-3g", *self.opts, self.path, self.mnt], check=True)

    def umount(self):
        subprocess.run(["umount", self.mnt], check=True)

    def p(self, *parts):
        return os.path.join(self.mnt, *parts)

    def scan(self):
        with RawDevice(self.path) as dev:
            return scan_volume(NTFSVolume(dev))

    def raw(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()


@pytest.fixture
def img(tmp_path):
    image = Image(tmp_path)
    yield image
    subprocess.run(["umount", image.mnt], capture_output=True)


def _write(path, data: bytes):
    with open(path, "wb") as fh:
        fh.write(data)


@needs_ntfs
def test_scan_and_recover_deleted_files(img, tmp_path):
    rnd = random.Random(1)
    img.mount()
    os.makedirs(img.p("Docs", "Sub"))
    files = {
        img.p("Docs", "klein.txt"): b"Hallo Welt\n",  # resident in the MFT record
        img.p("Docs", "Sub", "text.txt"): "".join(f"Zeile {i}\n" for i in range(9000)).encode(),
        img.p("foto.jpg"): b"\xff\xd8\xff\xe0" + rnd.randbytes(300_000),
    }
    for path, data in files.items():
        _write(path, data)
    # fragmented file: interleave with another file
    with open(img.p("Docs", "frag.bin"), "wb") as f1, open(img.p("Docs", "keep.bin"), "wb") as f2:
        for i in range(30):
            f1.write(bytes([i]) * 8192); f1.flush(); os.fsync(f1.fileno())
            f2.write(bytes([255 - i]) * 8192); f2.flush(); os.fsync(f2.fileno())
    files[img.p("Docs", "frag.bin")] = b"".join(bytes([i]) * 8192 for i in range(30))
    for path in files:
        os.remove(path)
    os.rmdir(img.p("Docs", "Sub"))
    img.umount()

    entries, summary = img.scan()
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"klein.txt", "text.txt", "foto.jpg", "frag.bin"}
    assert by_name["text.txt"].path == "\\Docs\\Sub"  # parent dir was deleted too
    assert by_name["klein.txt"].resident
    assert by_name["foto.jpg"].category == "image"
    assert all(e.status == "good" for e in entries)
    assert summary.deleted_files == 4

    out = tmp_path / "out"
    with RawDevice(img.path) as dev:
        results = recover_files(NTFSVolume(dev), entries, str(out))
    assert all(r.ok for r in results)
    expected = {os.path.basename(p): md5(d) for p, d in files.items()}
    for r in results:
        assert md5(open(r.target, "rb").read()) == expected[r.name]
    assert os.path.exists(out / "Docs" / "Sub" / "text.txt")


@needs_ntfs
def test_overwritten_clusters_are_detected(img):
    img.mount()
    _write(img.p("alt.bin"), os.urandom(2 * 1024 * 1024))
    os.remove(img.p("alt.bin"))
    # fill the volume so the freed clusters get reused
    free = shutil.disk_usage(img.mnt).free
    _write(img.p("neu.bin"), os.urandom(free - 2 * 1024 * 1024))
    img.umount()
    entries, _ = img.scan()
    alt = next(e for e in entries if e.name == "alt.bin")
    assert alt.status in ("partial", "overwritten")
    assert alt.overwritten_pct > 0


@needs_ntfs
def test_trimmed_blocks_are_reported(img):
    img.mount()
    _write(img.p("weg.bin"), os.urandom(200_000))
    os.remove(img.p("weg.bin"))
    img.umount()
    entries, _ = img.scan()
    e = next(e for e in entries if e.name == "weg.bin")
    assert e.status == "good"
    # simulate what an SSD returns after TRIM: zeros
    with RawDevice(img.path) as dev:
        vol = NTFSVolume(dev)
        ds = vol.data_stream(vol.read_record(e.id))
    with RawDevice(img.path, writable=True) as dev:
        for lcn, n in ds.runs:
            dev.write(lcn * vol.cs, b"\x00" * (n * vol.cs))
    entries, _ = img.scan()
    assert next(e for e in entries if e.name == "weg.bin").status == "trimmed"


@needs_ntfs
def test_compressed_file_recovery(tmp_path):
    image = Image(tmp_path, compression=True)
    try:
        image.mount()
        os.mkdir(image.p("Comp"))
        os.setxattr(image.p("Comp"), "system.ntfs_attrib_be", (0x800).to_bytes(4, "big"))
        rnd = random.Random(7)
        data = "".join(f"Text {i} {rnd.random()}\n" for i in range(20000)).encode()
        mixed = rnd.randbytes(100_000) + b"A" * 200_000
        _write(image.p("Comp", "a.txt"), data)
        _write(image.p("Comp", "b.bin"), mixed)
        os.remove(image.p("Comp", "a.txt"))
        os.remove(image.p("Comp", "b.bin"))
        image.umount()
        entries, _ = image.scan()
        assert all(e.compressed for e in entries)
        out = tmp_path / "out"
        with RawDevice(image.path) as dev:
            results = recover_files(NTFSVolume(dev), entries, str(out), keep_paths=False)
        got = {r.name: md5(open(r.target, "rb").read()) for r in results}
        assert got == {"a.txt": md5(data), "b.bin": md5(mixed)}
    finally:
        subprocess.run(["umount", image.mnt], capture_output=True)


@needs_ntfs
def test_shred_deleted_removes_content_and_metadata(img):
    img.mount()
    secret = MARKER * 20000
    _write(img.p("geheim.txt"), secret)
    _write(img.p("winzig.txt"), MARKER + b"klein")
    _write(img.p("bleibt.txt"), b"Diese Datei bleibt erhalten")
    os.remove(img.p("geheim.txt"))
    os.remove(img.p("winzig.txt"))
    img.umount()
    assert MARKER in img.raw()

    entries, _ = img.scan()
    targets = [e for e in entries if e.name in ("geheim.txt", "winzig.txt")]
    assert len(targets) == 2
    results = shred_deleted(img.path, targets)
    assert all(r["ok"] for r in results), results

    raw = img.raw()
    assert MARKER not in raw
    entries, _ = img.scan()
    assert not [e for e in entries if e.name in ("geheim.txt", "winzig.txt")]

    # file system must still be healthy
    check = subprocess.run(["ntfsfix", "-n", img.path], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    img.mount()
    assert open(img.p("bleibt.txt"), "rb").read() == b"Diese Datei bleibt erhalten"
    _write(img.p("neu.txt"), b"x" * 50_000)  # allocating new records/clusters still works
    img.umount()


@needs_ntfs
def test_wipe_free_space(img):
    img.mount()
    _write(img.p("geheim.bin"), MARKER * 50000)
    _write(img.p("mini.txt"), MARKER + b"resident")
    _write(img.p("bleibt.txt"), b"bleibt")
    os.remove(img.p("geheim.bin"))
    os.remove(img.p("mini.txt"))
    img.umount()
    with RawDevice(img.path) as dev:
        free_records = NTFSVolume(dev).free_record_count()
    img.mount()
    stats = wipe_free_space(img.mnt, free_records)
    assert stats["bytes"] > 0 and stats["mft_records"] > 0
    assert sorted(os.listdir(img.mnt)) == ["bleibt.txt"]
    img.umount()
    assert MARKER not in img.raw()
    entries, _ = img.scan()
    assert not [e for e in entries if e.name in ("geheim.bin", "mini.txt")]


def test_lznt1_uncompressed_chunk():
    raw = b"Z" * 4096
    header = (len(raw) - 1) | 0x3000  # bit 15 clear = stored uncompressed
    assert lznt1_decompress(header.to_bytes(2, "little") + raw) == raw


def test_lznt1_back_reference():
    # flags 0b1000: three literals "abc", then token 0x2006 = back 3, length 9
    chunk = bytes([0x08]) + b"abc" + (0x2006).to_bytes(2, "little")
    header = (len(chunk) - 1) | 0xB000
    assert lznt1_decompress(header.to_bytes(2, "little") + chunk, 12) == b"abc" * 4
