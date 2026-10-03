"""Copy deleted files from the volume into a target folder."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from .ntfs import NTFSVolume
from .scanner import DeletedFile

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class RecoveryError(Exception):
    pass


@dataclass
class RecoveryResult:
    id: int
    name: str
    ok: bool
    target: str | None = None
    message: str = ""

    def to_dict(self):
        return self.__dict__.copy()


def safe_name(name: str) -> str:
    cleaned = _INVALID.sub("_", name).strip(" .")
    return cleaned or "unbenannt"


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def same_volume(source_drive: str | None, target_dir: str) -> bool:
    """True if ``target_dir`` lives on the scanned drive letter."""
    if not source_drive:
        return False
    drive = os.path.splitdrive(os.path.abspath(target_dir))[0].rstrip(":").upper()
    return drive == source_drive.rstrip(":\\").upper()[-1:]


def recover_files(
    vol: NTFSVolume,
    entries: Iterable[DeletedFile],
    target_dir: str,
    keep_paths: bool = True,
    progress: Callable[[str, float], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[RecoveryResult]:
    progress = progress or (lambda m, f: None)
    entries = list(entries)
    os.makedirs(target_dir, exist_ok=True)
    total_bytes = sum(max(e.size, 1) for e in entries) or 1
    done_bytes = 0
    results = []
    for e in entries:
        if cancelled():
            raise InterruptedError
        progress(f"Stelle wieder her: {e.name}", done_bytes / total_bytes)
        try:
            target = _recover_one(vol, e, target_dir, keep_paths)
            msg = ""
            if e.status in ("partial", "overwritten"):
                msg = "Teile der Datei wurden bereits überschrieben – Inhalt ist vermutlich beschädigt."
            elif e.status == "trimmed":
                msg = "Datenblöcke wurden per TRIM gelöscht – Datei enthält nur Nullen."
            elif e.status == "encrypted":
                msg = "EFS-verschlüsselt – nur Rohdaten wiederhergestellt."
            results.append(RecoveryResult(e.id, e.name, True, target, msg))
        except Exception as exc:  # report per file, continue with the rest
            results.append(RecoveryResult(e.id, e.name, False, None, str(exc)))
        done_bytes += max(e.size, 1)
    progress("Fertig", 1.0)
    return results


def _recover_one(vol: NTFSVolume, e: DeletedFile, target_dir: str, keep_paths: bool) -> str:
    rec = vol.read_record(e.id)
    if rec is None:
        raise RecoveryError("MFT-Eintrag ist nicht mehr lesbar.")
    if rec.in_use or rec.seq != e.seq:
        raise RecoveryError("MFT-Eintrag wurde inzwischen für eine andere Datei verwendet.")
    ds = vol.data_stream(rec)
    out_dir = target_dir
    if keep_paths:
        parts = [safe_name(p) for p in e.path.split("\\") if p]
        out_dir = os.path.join(target_dir, *parts)
    os.makedirs(out_dir, exist_ok=True)
    target = _unique(os.path.join(out_dir, safe_name(e.name)))
    with open(target, "wb") as fh:
        if ds is not None:
            for chunk in vol.iter_stream(ds):
                fh.write(chunk)
    if e.modified_ts:
        try:
            os.utime(target, (e.accessed_ts or e.modified_ts, e.modified_ts))
        except (OSError, OverflowError, ValueError):
            pass
    return target


def read_preview(vol: NTFSVolume, e: DeletedFile, limit: int) -> bytes:
    rec = vol.read_record(e.id)
    if rec is None or rec.in_use or rec.seq != e.seq:
        raise RecoveryError("MFT-Eintrag wurde inzwischen wiederverwendet.")
    ds = vol.data_stream(rec)
    if ds is None:
        return b""
    out = bytearray()
    for chunk in vol.iter_stream(ds, chunk=min(limit, 4 * 1024 * 1024)):
        out += chunk
        if len(out) >= limit:
            break
    return bytes(out[:limit])
