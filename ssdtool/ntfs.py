"""Minimal read-mostly NTFS parser focused on deleted MFT records.

Only what is needed to find deleted files, read their content and wipe
their remains is implemented: boot sector, MFT records (incl. fixups),
attribute parsing, data runs, $ATTRIBUTE_LIST, cluster bitmap and LZNT1
decompression.
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from typing import Iterator

from .device import RawDevice

# attribute types
AT_STANDARD_INFORMATION = 0x10
AT_ATTRIBUTE_LIST = 0x20
AT_FILE_NAME = 0x30
AT_DATA = 0x80
AT_BITMAP = 0xB0
AT_END = 0xFFFFFFFF

# record header flags
MFT_RECORD_IN_USE = 0x01
MFT_RECORD_IS_DIRECTORY = 0x02

# attribute flags
ATTR_COMPRESSED = 0x0001
ATTR_ENCRYPTED = 0x4000
ATTR_SPARSE = 0x8000

ROOT_RECORD = 5
FIRST_USER_RECORD = 24  # records 0..23 are reserved for NTFS metadata

FIXUP_STRIDE = 512
_FILETIME_EPOCH = _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)


class NTFSError(Exception):
    pass


def filetime_to_iso(ft: int) -> str | None:
    if not ft:
        return None
    try:
        return (_FILETIME_EPOCH + _dt.timedelta(microseconds=ft // 10)).isoformat(timespec="seconds")
    except OverflowError:
        return None


def filetime_to_unix(ft: int) -> float | None:
    if not ft:
        return None
    return (ft - 116444736000000000) / 10_000_000


# --------------------------------------------------------------------------
# boot sector
# --------------------------------------------------------------------------
@dataclass
class BootSector:
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_lcn: int
    mftmirr_lcn: int
    mft_record_size: int
    serial: int

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def total_clusters(self) -> int:
        return self.total_sectors // self.sectors_per_cluster

    @property
    def volume_size(self) -> int:
        return self.total_sectors * self.bytes_per_sector


def parse_boot_sector(data: bytes) -> BootSector:
    if data[3:11] != b"NTFS    ":
        raise NTFSError("Kein NTFS-Dateisystem (nur NTFS wird unterstützt).")
    bps = struct.unpack_from("<H", data, 0x0B)[0]
    spc = data[0x0D]
    if spc > 0x80:  # 2^(256-x) encoding for clusters > 64 KiB
        spc = 1 << (256 - spc)
    total = struct.unpack_from("<Q", data, 0x28)[0]
    mft_lcn, mirr_lcn = struct.unpack_from("<QQ", data, 0x30)
    cpr = struct.unpack_from("<b", data, 0x40)[0]
    rec_size = (1 << -cpr) if cpr < 0 else cpr * bps * spc
    serial = struct.unpack_from("<Q", data, 0x48)[0]
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or rec_size < 512:
        raise NTFSError("Ungültiger NTFS-Bootsektor.")
    return BootSector(bps, spc, total, mft_lcn, mirr_lcn, rec_size, serial)


# --------------------------------------------------------------------------
# fixups / update sequence array
# --------------------------------------------------------------------------
def apply_fixups(buf: bytearray) -> bool:
    """Undo the multi-sector protection in place. Returns False if torn."""
    usa_ofs, usa_count = struct.unpack_from("<HH", buf, 4)
    if usa_count < 2 or usa_ofs + usa_count * 2 > len(buf):
        return False
    usn = bytes(buf[usa_ofs : usa_ofs + 2])
    for i in range(1, usa_count):
        end = i * FIXUP_STRIDE
        if end > len(buf):
            break
        if buf[end - 2 : end] != usn:
            return False
        buf[end - 2 : end] = buf[usa_ofs + 2 * i : usa_ofs + 2 * i + 2]
    return True


def protect_fixups(buf: bytearray, usn: int) -> None:
    """Apply multi-sector protection in place (inverse of ``apply_fixups``)."""
    usa_ofs, usa_count = struct.unpack_from("<HH", buf, 4)
    usn_bytes = struct.pack("<H", usn)
    buf[usa_ofs : usa_ofs + 2] = usn_bytes
    for i in range(1, usa_count):
        end = i * FIXUP_STRIDE
        if end > len(buf):
            break
        buf[usa_ofs + 2 * i : usa_ofs + 2 * i + 2] = buf[end - 2 : end]
        buf[end - 2 : end] = usn_bytes


# --------------------------------------------------------------------------
# data runs
# --------------------------------------------------------------------------
Run = tuple  # (lcn | None, length_in_clusters)


def decode_runlist(data: bytes, pos: int = 0) -> list[Run]:
    runs: list[Run] = []
    lcn = 0
    while pos < len(data):
        header = data[pos]
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = header >> 4
        pos += 1
        if len_size == 0 or len_size > 8 or off_size > 8 or pos + len_size + off_size > len(data):
            raise NTFSError("Beschädigte Datenlauf-Liste")
        length = int.from_bytes(data[pos : pos + len_size], "little")
        pos += len_size
        if off_size == 0:
            runs.append((None, length))
        else:
            lcn += int.from_bytes(data[pos : pos + off_size], "little", signed=True)
            pos += off_size
            if lcn < 0:
                raise NTFSError("Beschädigte Datenlauf-Liste")
            runs.append((lcn, length))
    return runs


def runs_clusters(runs: list[Run]) -> int:
    return sum(length for _, length in runs)


# --------------------------------------------------------------------------
# records and attributes
# --------------------------------------------------------------------------
@dataclass
class Attribute:
    type: int
    name: str
    non_resident: bool
    flags: int
    attr_id: int
    value: bytes | None = None
    start_vcn: int = 0
    last_vcn: int = 0
    runs: list = field(default_factory=list)
    alloc_size: int = 0
    data_size: int = 0
    init_size: int = 0
    compression_unit: int = 0

    @property
    def size(self) -> int:
        return self.data_size if self.non_resident else len(self.value or b"")


@dataclass
class FileName:
    parent_record: int
    parent_seq: int
    name: str
    namespace: int
    created: int
    modified: int
    mft_changed: int
    accessed: int
    real_size: int
    flags: int


@dataclass
class StandardInfo:
    created: int
    modified: int
    mft_changed: int
    accessed: int
    file_attributes: int


@dataclass
class MFTRecord:
    number: int
    seq: int
    flags: int
    base_ref: int
    link_count: int
    attrs_offset: int
    attributes: list[Attribute]

    @property
    def in_use(self) -> bool:
        return bool(self.flags & MFT_RECORD_IN_USE)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & MFT_RECORD_IS_DIRECTORY)

    @property
    def base_record(self) -> int:
        return self.base_ref & 0xFFFFFFFFFFFF

    def attrs(self, atype: int) -> list[Attribute]:
        return [a for a in self.attributes if a.type == atype]

    def file_names(self) -> list[FileName]:
        out = []
        for a in self.attrs(AT_FILE_NAME):
            if a.value and len(a.value) >= 0x42:
                fn = parse_file_name(a.value)
                if fn:
                    out.append(fn)
        return out

    def best_name(self) -> FileName | None:
        names = self.file_names()
        if not names:
            return None
        # prefer Win32 / POSIX names over the 8.3 DOS alias
        names.sort(key=lambda f: {1: 0, 3: 0, 0: 1, 2: 2}.get(f.namespace, 3))
        return names[0]

    def std_info(self) -> StandardInfo | None:
        for a in self.attrs(AT_STANDARD_INFORMATION):
            if a.value and len(a.value) >= 36:
                c, m, mc, ac = struct.unpack_from("<QQQQ", a.value, 0)
                fa = struct.unpack_from("<I", a.value, 32)[0]
                return StandardInfo(c, m, mc, ac, fa)
        return None


def parse_file_name(v: bytes) -> FileName | None:
    try:
        parent = struct.unpack_from("<Q", v, 0)[0]
        c, m, mc, ac = struct.unpack_from("<QQQQ", v, 8)
        real = struct.unpack_from("<Q", v, 0x30)[0]
        flags = struct.unpack_from("<I", v, 0x38)[0]
        nlen = v[0x40]
        ns = v[0x41]
        name = v[0x42 : 0x42 + nlen * 2].decode("utf-16-le", errors="replace")
    except (struct.error, IndexError):
        return None
    return FileName(parent & 0xFFFFFFFFFFFF, parent >> 48, name, ns, c, m, mc, ac, real, flags)


def parse_record(buf: bytes | bytearray, number: int) -> MFTRecord | None:
    """Parse a raw MFT record (fixups not yet applied).  None if invalid."""
    if buf[0:4] != b"FILE":
        return None
    b = bytearray(buf)
    if not apply_fixups(b):
        return None
    seq, link_count, attrs_ofs, flags = struct.unpack_from("<HHHH", b, 0x10)
    bytes_used = struct.unpack_from("<I", b, 0x18)[0]
    base_ref = struct.unpack_from("<Q", b, 0x20)[0]
    limit = min(len(b), bytes_used if 0 < bytes_used <= len(b) else len(b))
    attributes: list[Attribute] = []
    pos = attrs_ofs
    while pos + 16 <= limit:
        atype, alen = struct.unpack_from("<II", b, pos)
        if atype == AT_END or alen < 16 or pos + alen > len(b):
            break
        non_res = b[pos + 8]
        nlen = b[pos + 9]
        nofs, aflags, aid = struct.unpack_from("<HHH", b, pos + 10)
        name = ""
        if nlen:
            name = b[pos + nofs : pos + nofs + nlen * 2].decode("utf-16-le", errors="replace")
        try:
            if not non_res:
                vlen = struct.unpack_from("<I", b, pos + 0x10)[0]
                vofs = struct.unpack_from("<H", b, pos + 0x14)[0]
                value = bytes(b[pos + vofs : pos + vofs + vlen])
                attributes.append(Attribute(atype, name, False, aflags, aid, value=value))
            else:
                svcn, lvcn = struct.unpack_from("<qq", b, pos + 0x10)
                rofs, cu = struct.unpack_from("<HH", b, pos + 0x20)
                asz, dsz, isz = struct.unpack_from("<qqq", b, pos + 0x28)
                runs = decode_runlist(bytes(b[pos : pos + alen]), rofs)
                attributes.append(
                    Attribute(atype, name, True, aflags, aid, start_vcn=svcn, last_vcn=lvcn,
                              runs=runs, alloc_size=asz, data_size=dsz, init_size=isz,
                              compression_unit=cu)
                )
        except (struct.error, NTFSError):
            pass
        pos += alen
    return MFTRecord(number, seq, flags, base_ref, link_count, attrs_ofs, attributes)


@dataclass
class DataStream:
    """The (unnamed) $DATA stream of a file, merged over extension records."""

    resident: bool
    value: bytes = b""
    runs: list = field(default_factory=list)
    data_size: int = 0
    init_size: int = 0
    flags: int = 0
    compression_unit: int = 0
    complete: bool = True
    extension_records: list = field(default_factory=list)

    @property
    def compressed(self) -> bool:
        return bool(self.flags & ATTR_COMPRESSED)

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & ATTR_ENCRYPTED)


# --------------------------------------------------------------------------
# volume
# --------------------------------------------------------------------------
class NTFSVolume:
    def __init__(self, device: RawDevice):
        self.dev = device
        self.boot = parse_boot_sector(device.read(0, 512))
        self.cs = self.boot.cluster_size
        self.rs = self.boot.mft_record_size
        raw0 = device.read(self.boot.mft_lcn * self.cs, self.rs)
        rec0 = parse_record(raw0, 0)
        if rec0 is None:
            raise NTFSError("$MFT-Eintrag konnte nicht gelesen werden.")
        data = [a for a in rec0.attrs(AT_DATA) if not a.name and a.non_resident]
        if not data:
            raise NTFSError("$MFT hat keinen Datenstrom.")
        data.sort(key=lambda a: a.start_vcn)
        self.mft_runs = [r for a in data for r in a.runs]
        self.mft_size = data[0].data_size
        self.record_count = self.mft_size // self.rs
        self._mft_bitmap: bytes | None = None
        self._rec0 = rec0
        self._cluster_bitmap: bytes | None = None

    # -- generic virtual reads ------------------------------------------
    def read_runs(self, runs: list[Run], offset: int, length: int) -> bytes:
        """Read ``length`` bytes at virtual ``offset`` of a run list."""
        out = bytearray()
        vpos = 0
        end = offset + length
        for lcn, clen in runs:
            rstart = vpos
            rend = vpos + clen * self.cs
            vpos = rend
            if rend <= offset:
                continue
            if rstart >= end:
                break
            a = max(offset, rstart)
            b = min(end, rend)
            if lcn is None:
                out += b"\x00" * (b - a)
            else:
                out += self.dev.read(lcn * self.cs + (a - rstart), b - a)
        if len(out) < length:
            out += b"\x00" * (length - len(out))
        return bytes(out)

    def attr_content(self, attr: Attribute) -> bytes:
        if not attr.non_resident:
            return attr.value or b""
        return self.read_runs(attr.runs, 0, attr.data_size)

    # -- MFT -------------------------------------------------------------
    def read_record_raw(self, number: int) -> bytes:
        return self.read_runs(self.mft_runs, number * self.rs, self.rs)

    def read_record(self, number: int) -> MFTRecord | None:
        return parse_record(self.read_record_raw(number), number)

    def record_location(self, number: int) -> int:
        """Byte offset of an MFT record on the volume (must be contiguous)."""
        offset = number * self.rs
        vpos = 0
        for lcn, clen in self.mft_runs:
            size = clen * self.cs
            if vpos <= offset < vpos + size:
                if lcn is None or offset + self.rs > vpos + size:
                    raise NTFSError("MFT-Eintrag liegt nicht zusammenhängend auf dem Datenträger.")
                return lcn * self.cs + (offset - vpos)
            vpos += size
        raise NTFSError("MFT-Eintrag außerhalb der MFT")

    def iter_records_raw(self, chunk_records: int = 1024) -> Iterator[tuple[int, bytes]]:
        n = 0
        while n < self.record_count:
            count = min(chunk_records, self.record_count - n)
            blob = self.read_runs(self.mft_runs, n * self.rs, count * self.rs)
            for i in range(count):
                yield n + i, blob[i * self.rs : (i + 1) * self.rs]
            n += count

    @property
    def mft_bitmap(self) -> bytes:
        if self._mft_bitmap is None:
            attrs = self._rec0.attrs(AT_BITMAP)
            self._mft_bitmap = self.attr_content(attrs[0]) if attrs else b""
        return self._mft_bitmap

    def record_allocated(self, number: int) -> bool | None:
        bm = self.mft_bitmap
        if not bm or (number >> 3) >= len(bm):
            return None
        return bool(bm[number >> 3] & (1 << (number & 7)))

    def free_record_count(self) -> int:
        bm = self.mft_bitmap
        used = sum(bin(b).count("1") for b in bm[: (self.record_count + 7) // 8])
        return max(0, self.record_count - used)

    # -- cluster bitmap -------------------------------------------------
    @property
    def cluster_bitmap(self) -> bytes:
        if self._cluster_bitmap is None:
            rec = self.read_record(6)  # $Bitmap
            if rec is None:
                raise NTFSError("$Bitmap konnte nicht gelesen werden.")
            attrs = [a for a in rec.attrs(AT_DATA) if not a.name]
            if not attrs:
                raise NTFSError("$Bitmap hat keinen Datenstrom.")
            self._cluster_bitmap = self.attr_content(attrs[0])
        return self._cluster_bitmap

    def reload_bitmaps(self):
        self._cluster_bitmap = None
        rec0 = parse_record(self.dev.read(self.boot.mft_lcn * self.cs, self.rs), 0)
        if rec0 is not None:
            self._rec0 = rec0
        self._mft_bitmap = None

    def count_allocated(self, lcn: int, count: int) -> int:
        """Number of clusters in [lcn, lcn+count) currently allocated.

        Clusters outside the volume are counted as allocated (= unusable).
        """
        if count <= 0:
            return 0
        total = self.boot.total_clusters
        outside = max(0, lcn + count - total) if lcn < total else count
        count -= outside
        if count <= 0:
            return outside
        bm = self.cluster_bitmap
        first = lcn >> 3
        last = (lcn + count + 7) >> 3
        val = int.from_bytes(bm[first:last], "little") >> (lcn - first * 8)
        val &= (1 << count) - 1
        return val.bit_count() + outside

    def free_ranges(self, lcn: int, count: int) -> Iterator[tuple[int, int]]:
        """Yield (lcn, length) sub-ranges of [lcn, lcn+count) that are free."""
        total = self.boot.total_clusters
        end = min(lcn + count, total)
        bm = self.cluster_bitmap
        cur = lcn
        start = None
        while cur < end:
            byte = bm[cur >> 3] if (cur >> 3) < len(bm) else 0xFF
            # fast paths for whole bytes
            if cur & 7 == 0 and cur + 8 <= end and byte in (0, 0xFF):
                if byte == 0:
                    if start is None:
                        start = cur
                else:
                    if start is not None:
                        yield start, cur - start
                        start = None
                cur += 8
                continue
            used = byte & (1 << (cur & 7))
            if not used:
                if start is None:
                    start = cur
            elif start is not None:
                yield start, cur - start
                start = None
            cur += 1
        if start is not None:
            yield start, end - start

    # -- file content -----------------------------------------------------
    def data_stream(self, rec: MFTRecord, deleted: bool = True) -> DataStream | None:
        """Return the unnamed $DATA stream of a base record.

        If the record uses an $ATTRIBUTE_LIST, extension records are loaded
        as long as they still belong to this file.
        """
        attrs = [a for a in rec.attrs(AT_DATA) if not a.name]
        ext_records: list[int] = []
        complete = True
        alist = rec.attrs(AT_ATTRIBUTE_LIST)
        if alist:
            try:
                content = self.attr_content(alist[0])
            except Exception:
                content = b""
            pos = 0
            wanted: set[int] = set()
            while pos + 26 <= len(content):
                atype, elen = struct.unpack_from("<IH", content, pos)
                if elen < 26:
                    break
                nlen = content[pos + 6]
                ref = struct.unpack_from("<Q", content, pos + 16)[0] & 0xFFFFFFFFFFFF
                if atype == AT_DATA and nlen == 0 and ref != rec.number:
                    wanted.add(ref)
                pos += elen
            for ref in sorted(wanted):
                ext = self.read_record(ref)
                if (
                    ext is None
                    or ext.base_record != rec.number
                    or (deleted and ext.in_use)
                ):
                    complete = False
                    continue
                ext_records.append(ref)
                attrs.extend(a for a in ext.attrs(AT_DATA) if not a.name)
        if not attrs:
            return None
        attrs.sort(key=lambda a: a.start_vcn)
        first = attrs[0]
        if not first.non_resident:
            return DataStream(True, value=first.value or b"", data_size=len(first.value or b""),
                              init_size=len(first.value or b""), flags=first.flags)
        runs: list[Run] = []
        expected_vcn = 0
        for a in attrs:
            if not a.non_resident:
                continue
            if a.start_vcn != expected_vcn:
                complete = False
                break
            runs.extend(a.runs)
            expected_vcn = a.start_vcn + runs_clusters(a.runs)
        needed = (first.data_size + self.cs - 1) // self.cs
        if runs_clusters(runs) < needed and not (first.flags & ATTR_COMPRESSED):
            complete = False
        return DataStream(False, runs=runs, data_size=first.data_size, init_size=first.init_size,
                          flags=first.flags, compression_unit=first.compression_unit,
                          complete=complete, extension_records=ext_records)

    def iter_stream(self, ds: DataStream, chunk: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        """Yield the content of a data stream in chunks (handles LZNT1)."""
        if ds.resident:
            yield ds.value[: ds.data_size]
            return
        size = ds.data_size
        if ds.compressed and ds.compression_unit:
            unit_clusters = 1 << ds.compression_unit
            unit_bytes = unit_clusters * self.cs
            vmap = _VcnMap(ds.runs)
            produced = 0
            vcn = 0
            while produced < size:
                segs = vmap.segments(vcn, unit_clusters)
                real = [(l, n) for l, n in segs if l is not None]
                real_count = sum(n for _, n in real)
                if real_count == 0:
                    data = b"\x00" * unit_bytes
                elif real_count >= unit_clusters:
                    data = b"".join(self.dev.read(l * self.cs, n * self.cs) for l, n in real)
                else:
                    raw = b"".join(self.dev.read(l * self.cs, n * self.cs) for l, n in real)
                    data = lznt1_decompress(raw, unit_bytes)
                take = min(unit_bytes, size - produced)
                yield data[:take]
                produced += take
                vcn += unit_clusters
            return
        offset = 0
        while offset < size:
            n = min(chunk, size - offset)
            data = self.read_runs(ds.runs, offset, n)
            if ds.init_size < offset + n:
                keep = max(0, ds.init_size - offset)
                data = data[:keep] + b"\x00" * (n - keep)
            yield data
            offset += n


class _VcnMap:
    def __init__(self, runs: list[Run]):
        self.items = []
        vcn = 0
        for lcn, n in runs:
            self.items.append((vcn, lcn, n))
            vcn += n

    def segments(self, vcn: int, count: int) -> list[tuple[int | None, int]]:
        out = []
        end = vcn + count
        for start, lcn, n in self.items:
            if start + n <= vcn:
                continue
            if start >= end:
                break
            a = max(vcn, start)
            b = min(end, start + n)
            out.append((None if lcn is None else lcn + (a - start), b - a))
        covered = sum(n for _, n in out)
        if covered < count:
            out.append((None, count - covered))
        return out


# --------------------------------------------------------------------------
# LZNT1
# --------------------------------------------------------------------------
def lznt1_decompress(data: bytes, max_out: int | None = None) -> bytes:
    out = bytearray()
    pos = 0
    while pos + 2 <= len(data):
        header = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if header == 0:
            break
        size = (header & 0x0FFF) + 1
        chunk = data[pos : pos + size]
        pos += size
        if not header & 0x8000:
            out += chunk
            out += b"\x00" * (4096 - len(chunk)) if len(chunk) < 4096 else b""
        else:
            dec = bytearray()
            cpos = 0
            while cpos < len(chunk):
                flags = chunk[cpos]
                cpos += 1
                for bit in range(8):
                    if cpos >= len(chunk):
                        break
                    if not (flags >> bit) & 1:
                        dec.append(chunk[cpos])
                        cpos += 1
                        continue
                    if cpos + 1 >= len(chunk):
                        cpos = len(chunk)
                        break
                    token = chunk[cpos] | (chunk[cpos + 1] << 8)
                    cpos += 2
                    lg = 0
                    i = len(dec) - 1
                    while i >= 0x10:
                        lg += 1
                        i >>= 1
                    length = (token & (0xFFF >> lg)) + 3
                    back = (token >> (12 - lg)) + 1
                    if back > len(dec):
                        raise NTFSError("Beschädigte LZNT1-Daten")
                    for _ in range(length):
                        dec.append(dec[-back])
            if len(dec) < 4096:
                dec += b"\x00" * (4096 - len(dec))
            out += dec[:4096]
        if max_out is not None and len(out) >= max_out:
            break
    if max_out is not None:
        out = out[:max_out] + b"\x00" * max(0, max_out - len(out))
    return bytes(out)
