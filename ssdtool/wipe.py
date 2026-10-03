"""Permanently destroy the remains of deleted files.

Two strategies:

``shred_deleted``
    Targeted: overwrites the still-free data clusters of selected deleted
    files and replaces their MFT records with a blank record (removes name,
    timestamps and small resident file content).  Requires exclusive access
    to the volume, so on Windows it only works on drives that can be locked
    (not the system drive, no open files).

``wipe_free_space``
    Works on every drive, including C:.  Fills all free MFT records with
    dummy entries and all free clusters with random data through normal
    file I/O, then deletes the filler.  Afterwards a TRIM (ReTrim) tells the
    SSD that all those blocks are unused.
"""

from __future__ import annotations

import os
import shutil
import struct
import uuid
from typing import Callable, Iterable

from .device import RawDevice
from .ntfs import FIRST_USER_RECORD, NTFSVolume, protect_fixups
from .scanner import WIPE_DIR_PREFIX, DeletedFile

CHUNK = 4 * 1024 * 1024

Progress = Callable[[str, float], None]


class WipeError(Exception):
    pass


def _random(n: int) -> bytes:
    return os.urandom(n)


def blank_record(old_raw: bytes, record_size: int, number: int) -> bytearray:
    """Build an empty, not-in-use MFT record replacing ``old_raw``."""
    usa_ofs, usa_count = struct.unpack_from("<HH", old_raw, 4)
    seq = struct.unpack_from("<H", old_raw, 0x10)[0]
    attrs_ofs = struct.unpack_from("<H", old_raw, 0x14)[0]
    old_usn = struct.unpack_from("<H", old_raw, usa_ofs)[0]
    if attrs_ofs < usa_ofs + usa_count * 2 or attrs_ofs > 0x100:
        usa_ofs, usa_count, attrs_ofs = 0x30, record_size // 512 + 1, 0x38
    rec = bytearray(record_size)
    rec[0:4] = b"FILE"
    struct.pack_into("<HH", rec, 4, usa_ofs, usa_count)
    struct.pack_into("<Q", rec, 8, 0)                    # $LogFile LSN
    struct.pack_into("<HHHH", rec, 0x10, seq, 0, attrs_ofs, 0)  # seq, links, attrs, flags
    struct.pack_into("<II", rec, 0x18, attrs_ofs + 8, record_size)
    struct.pack_into("<QH", rec, 0x20, 0, 0)             # base ref, next attr id
    if attrs_ofs >= 0x30:
        struct.pack_into("<I", rec, 0x2C, number)
    struct.pack_into("<I", rec, attrs_ofs, 0xFFFFFFFF)
    usn = (old_usn + 1) & 0xFFFF or 1
    protect_fixups(rec, usn)
    return rec


def shred_deleted(
    source: str,
    entries: Iterable[DeletedFile],
    base_offset: int = 0,
    passes: int = 1,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[dict]:
    progress = progress or (lambda m, f: None)
    entries = list(entries)
    results: list[dict] = []
    progress("Laufwerk wird exklusiv gesperrt …", 0.0)
    with RawDevice(source, writable=True, base_offset=base_offset) as dev:
        if not dev.lock():
            raise WipeError(
                "Das Laufwerk konnte nicht exklusiv gesperrt werden. Gezieltes Löschen ist nur auf "
                "Laufwerken möglich, die gerade nicht benutzt werden (nicht auf dem Windows-Systemlaufwerk). "
                "Schließe alle Programme/Explorer-Fenster auf dem Laufwerk oder nutze "
                "„Freien Speicher bereinigen“."
            )
        try:
            vol = NTFSVolume(dev)
            vol.reload_bitmaps()
            total = len(entries) or 1
            for i, e in enumerate(entries):
                if cancelled():
                    raise InterruptedError
                progress(f"Überschreibe: {e.name}", i / total)
                try:
                    wiped = _shred_one(vol, dev, e, passes, cancelled)
                    results.append({"id": e.id, "name": e.name, "ok": True, "message": wiped})
                except InterruptedError:
                    raise
                except Exception as exc:
                    results.append({"id": e.id, "name": e.name, "ok": False, "message": str(exc)})
            dev.flush()
        finally:
            dev.dismount()  # force Windows to re-read the metadata we changed
            dev.unlock()
    progress("Fertig", 1.0)
    return results


def _shred_one(vol: NTFSVolume, dev: RawDevice, e: DeletedFile, passes: int, cancelled) -> str:
    if e.id < FIRST_USER_RECORD:
        raise WipeError("Systemeintrag – wird nicht angefasst.")
    raw = vol.read_record_raw(e.id)
    rec = vol.read_record(e.id)
    if rec is None:
        raise WipeError("MFT-Eintrag nicht lesbar.")
    if rec.in_use or vol.record_allocated(e.id) or rec.seq != e.seq:
        raise WipeError("MFT-Eintrag wird inzwischen von einer anderen Datei benutzt – übersprungen.")
    ds = vol.data_stream(rec)
    wiped_clusters = 0
    skipped_clusters = 0
    if ds is not None and not ds.resident:
        for lcn, n in ds.runs:
            if lcn is None:
                continue
            free = list(vol.free_ranges(lcn, n))
            free_count = sum(c for _, c in free)
            skipped_clusters += n - free_count
            for start, count in free:
                for _ in range(passes):
                    _overwrite(dev, start * vol.cs, count * vol.cs, cancelled)
                wiped_clusters += count
    # blank extension records first, then the base record
    ext_ids = ds.extension_records if ds is not None else []
    for ext in ext_ids:
        ext_rec = vol.read_record(ext)
        if ext_rec is not None and not ext_rec.in_use and not vol.record_allocated(ext):
            dev.write(vol.record_location(ext), blank_record(vol.read_record_raw(ext), vol.rs, ext))
    dev.write(vol.record_location(e.id), blank_record(raw, vol.rs, e.id))
    if ds is not None and ds.resident:
        msg = "Inhalt lag im MFT-Eintrag – Eintrag samt Name und Inhalt geleert."
    else:
        msg = f"{wiped_clusters} Cluster überschrieben, MFT-Eintrag geleert."
    if skipped_clusters:
        msg += f" {skipped_clusters} Cluster gehören bereits anderen Dateien und wurden nicht angetastet."
    return msg


def _overwrite(dev: RawDevice, offset: int, length: int, cancelled) -> None:
    pos = 0
    while pos < length:
        if cancelled():
            raise InterruptedError
        n = min(CHUNK, length - pos)
        dev.write(offset + pos, _random(n))
        pos += n


def wipe_free_space(
    root: str,
    mft_records: int = 0,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    reserve_bytes: int = 0,
) -> dict:
    """Overwrite free space of the file system mounted at ``root``.

    ``mft_records`` is the number of free MFT records to occupy with dummy
    files (0 = skip that step).  ``reserve_bytes`` leaves some space free.
    """
    progress = progress or (lambda m, f: None)
    work = os.path.join(root, WIPE_DIR_PREFIX + uuid.uuid4().hex[:8])
    os.makedirs(work)
    stats = {"mft_records": 0, "bytes": 0}
    try:
        # 1) occupy free MFT records with tiny files so old file names and
        #    resident data in those records get overwritten
        if mft_records:
            payload_name = "x" * 60
            for i in range(mft_records):
                if cancelled():
                    raise InterruptedError
                if i % 500 == 0:
                    progress(f"MFT-Einträge werden überschrieben … {i}/{mft_records}", 0.3 * i / mft_records)
                sub = os.path.join(work, f"m{i // 1000}")
                if i % 1000 == 0:
                    os.makedirs(sub, exist_ok=True)
                try:
                    with open(os.path.join(sub, f"{payload_name}{i:08d}"), "wb") as fh:
                        fh.write(_random(500))
                except OSError:
                    break
                stats["mft_records"] += 1
        # 2) fill all free clusters with random data
        usage = shutil.disk_usage(root)
        free_total = max(1, usage.free - reserve_bytes)
        written = 0
        file_index = 0
        chunk = CHUNK
        done = False
        while not done:
            path = os.path.join(work, f"fill{file_index:04d}.bin")
            file_index += 1
            file_written = 0
            try:
                fh = open(path, "wb", buffering=0)
            except OSError:
                break  # not even room for another file entry
            with fh:
                while True:
                    if cancelled():
                        raise InterruptedError
                    if reserve_bytes and shutil.disk_usage(root).free - chunk < reserve_bytes:
                        done = True
                        break
                    try:
                        fh.write(_random(chunk))
                    except OSError:
                        if chunk > 4096:
                            chunk //= 16  # fill the last gaps with smaller blocks
                            continue
                        done = True
                        break
                    written += chunk
                    file_written += chunk
                    if written % (256 * 1024 * 1024) < chunk:
                        progress(
                            f"Freier Speicher wird überschrieben … {written / 2**30:.1f} GiB",
                            0.3 + 0.65 * min(1.0, written / free_total),
                        )
                    if file_written >= 4 * 2**30:  # max. 4 GiB per filler file
                        break
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        stats["bytes"] = written
    finally:
        progress("Füll-Dateien werden entfernt …", 0.96)
        shutil.rmtree(work, ignore_errors=True)
    progress("Fertig", 1.0)
    return stats
