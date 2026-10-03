"""Find deleted files in the MFT and estimate how recoverable they are."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Callable

from .ntfs import (
    FIRST_USER_RECORD,
    ROOT_RECORD,
    NTFSVolume,
    filetime_to_iso,
    parse_record,
)

# recoverability states
GOOD = "good"            # data clusters still free and contain data
TRIMMED = "trimmed"      # clusters free but read back as zeros (SSD TRIM)
PARTIAL = "partial"      # some clusters already reused by other files
OVERWRITTEN = "overwritten"  # all clusters reused
ENCRYPTED = "encrypted"  # EFS - content cannot be decrypted
DAMAGED = "damaged"      # incomplete metadata
EMPTY = "empty"          # 0-byte file

WIPE_DIR_PREFIX = "~ssdtool-wipe-"

CATEGORIES = {
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "raw", "cr2", "nef", "arw", "dng", "svg", "ico"},
    "video": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "mpg", "mpeg", "3gp", "webm", "flv"},
    "audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "rtf", "csv", "md", "pages", "numbers", "key", "msg", "eml"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab"},
    "program": {"exe", "dll", "msi", "sys", "bat", "cmd", "ps1", "jar"},
}


def category_for(name: str) -> str:
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


@dataclass(slots=True)
class DeletedFile:
    id: int               # MFT record number
    seq: int              # sequence number at scan time (used to detect reuse)
    name: str
    path: str             # reconstructed parent path
    size: int
    created: str | None
    modified: str | None
    accessed: str | None
    modified_ts: float | None
    accessed_ts: float | None
    status: str
    overwritten_pct: float
    resident: bool
    compressed: bool
    category: str
    orphan: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanSummary:
    total_records: int
    deleted_files: int
    by_status: dict
    cluster_size: int
    record_size: int
    volume_size: int
    free_mft_records: int


class _Dir:
    __slots__ = ("name", "parent", "parent_seq", "seq", "in_use")

    def __init__(self, name, parent, parent_seq, seq, in_use):
        self.name = name
        self.parent = parent
        self.parent_seq = parent_seq
        self.seq = seq
        self.in_use = in_use


ProgressCb = Callable[[str, float], None]


def _seq_matches(d: _Dir, ref_seq: int) -> bool:
    # NTFS bumps the sequence number when a record is freed, so a deleted
    # parent directory carries ref_seq + 1.
    return d.seq == ref_seq or (not d.in_use and (d.seq - 1) & 0xFFFF == ref_seq) or ref_seq == 0


def scan_volume(
    vol: NTFSVolume,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    check_trim: bool = True,
) -> tuple[list[DeletedFile], ScanSummary]:
    progress = progress or (lambda msg, frac: None)
    dirs: dict[int, _Dir] = {}
    candidates = []
    total = vol.record_count

    progress("MFT wird gelesen …", 0.0)
    for n, raw in vol.iter_records_raw():
        if cancelled():
            raise InterruptedError
        if n % 4096 == 0:
            progress(f"MFT wird gelesen … {n:,}/{total:,} Einträge".replace(",", "."), 0.7 * n / max(total, 1))
        if raw[0:4] != b"FILE":
            continue
        # cheap pre-checks on the raw header before full parsing
        flags = raw[0x16]
        base_ref = int.from_bytes(raw[0x20:0x26], "little")
        if base_ref:
            continue
        is_dir = bool(flags & 0x02)
        in_use = bool(flags & 0x01)
        if in_use and not is_dir:
            continue
        rec = parse_record(raw, n)
        if rec is None:
            continue
        fn = rec.best_name()
        if fn is None:
            continue
        if is_dir:
            dirs[n] = _Dir(fn.name, fn.parent_record, fn.parent_seq, rec.seq, in_use)
            continue
        if n < FIRST_USER_RECORD:
            continue
        # double check with the MFT bitmap: allocated means the record lives
        if vol.record_allocated(n):
            continue
        candidates.append((rec, fn))

    path_cache: dict[int, tuple[str, bool]] = {ROOT_RECORD: ("", False)}

    def dir_path(num: int, depth: int = 0) -> tuple[str, bool]:
        if num in path_cache:
            return path_cache[num]
        d = dirs.get(num)
        if d is None or depth > 128:
            return ("\\[unbekannt]", True)
        if d.parent == num:
            res = ("", False)
        elif d.parent in dirs and _seq_matches(dirs[d.parent], d.parent_seq):
            ppath, orphan = dir_path(d.parent, depth + 1)
            res = (ppath + "\\" + d.name, orphan)
        else:
            res = ("\\[verwaist]\\" + d.name, True)
        path_cache[num] = res
        return res

    results: list[DeletedFile] = []
    counts: dict[str, int] = {}
    nc = len(candidates)
    for i, (rec, fn) in enumerate(candidates):
        if cancelled():
            raise InterruptedError
        if i % 256 == 0:
            progress(f"Wiederherstellbarkeit wird geprüft … {i:,}/{nc:,}".replace(",", "."), 0.7 + 0.3 * i / max(nc, 1))
        parent = dirs.get(fn.parent_record)
        if parent is not None and _seq_matches(parent, fn.parent_seq):
            path, orphan = dir_path(fn.parent_record)
        else:
            path, orphan = "\\[verwaist]", True
        if WIPE_DIR_PREFIX in path:
            continue  # filler files of our own free-space wipe
        si = rec.std_info()
        ds = vol.data_stream(rec)
        status, pct, size, resident, compressed = _assess(vol, ds, check_trim)
        if size == 0 and fn.real_size and ds is None:
            size = fn.real_size
        times = si or fn
        df = DeletedFile(
            id=rec.number,
            seq=rec.seq,
            name=fn.name,
            path=path or "\\",
            size=size,
            created=filetime_to_iso(times.created),
            modified=filetime_to_iso(times.modified),
            accessed=filetime_to_iso(times.accessed),
            modified_ts=_unix(times.modified),
            accessed_ts=_unix(times.accessed),
            status=status,
            overwritten_pct=round(pct * 100, 1),
            resident=resident,
            compressed=compressed,
            category=category_for(fn.name),
            orphan=orphan,
        )
        counts[status] = counts.get(status, 0) + 1
        results.append(df)

    progress("Fertig", 1.0)
    summary = ScanSummary(
        total_records=total,
        deleted_files=len(results),
        by_status=counts,
        cluster_size=vol.cs,
        record_size=vol.rs,
        volume_size=vol.boot.volume_size,
        free_mft_records=vol.free_record_count(),
    )
    return results, summary


def _unix(ft):
    from .ntfs import filetime_to_unix

    return filetime_to_unix(ft)


def _is_zero(data: bytes) -> bool:
    return not data.strip(b"\x00")


def _assess(vol: NTFSVolume, ds, check_trim: bool):
    """Return (status, overwritten_fraction, size, resident, compressed)."""
    if ds is None:
        return EMPTY, 0.0, 0, False, False
    if ds.encrypted:
        return ENCRYPTED, 0.0, ds.data_size, ds.resident, False
    if ds.resident:
        return (GOOD if ds.data_size else EMPTY), 0.0, ds.data_size, True, False
    if ds.data_size == 0:
        return EMPTY, 0.0, 0, False, False
    real_runs = [(l, n) for l, n in ds.runs if l is not None]
    total = sum(n for _, n in real_runs)
    if total == 0:
        return (GOOD if ds.complete else DAMAGED), 0.0, ds.data_size, False, ds.compressed
    used = sum(vol.count_allocated(l, n) for l, n in real_runs)
    frac = used / total
    if used >= total:
        return OVERWRITTEN, 1.0, ds.data_size, False, ds.compressed
    if not ds.complete:
        return DAMAGED, frac, ds.data_size, False, ds.compressed
    if used:
        return PARTIAL, frac, ds.data_size, False, ds.compressed
    if check_trim:
        # Sample the first and the last cluster: TRIMmed SSD blocks read
        # back as zeros (deterministic read-zero after TRIM).
        first_lcn = real_runs[0][0]
        last_lcn = real_runs[-1][0] + real_runs[-1][1] - 1
        sample = vol.dev.read(first_lcn * vol.cs, vol.cs)
        if _is_zero(sample) and (last_lcn == first_lcn or _is_zero(vol.dev.read(last_lcn * vol.cs, vol.cs))):
            return TRIMMED, 0.0, ds.data_size, False, ds.compressed
    return GOOD, 0.0, ds.data_size, False, ds.compressed
