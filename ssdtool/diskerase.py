"""Securely erase a complete disk (SSD, HDD, USB drive).

Order of preference:

1. **NVMe Sanitize** (``IOCTL_STORAGE_REINITIALIZE_MEDIA``, Windows 10 1903+):
   the drive firmware erases every flash cell, including over-provisioned
   and remapped areas.  Crypto Erase is tried first, then Block Erase.
   (NIST 800-88 "Purge")
2. **Overwrite + TRIM**: every LBA is overwritten once with random data, then
   the whole LBA range is TRIMmed.  (NIST 800-88 "Clear")

Before erasing, random sample blocks are fingerprinted; afterwards they are
read again to verify that their content really changed.

The disk holding Windows, the boot partition, a page file or this program
itself is never erased.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable

from . import winutil

IS_WINDOWS = sys.platform == "win32"

CHUNK = 8 * 1024 * 1024
SAMPLE_COUNT = 256
EDGE_SKIP = 2 * 1024 * 1024  # partition tables live here; changed by "clean" anyway

# winioctl.h
IOCTL_STORAGE_REINITIALIZE_MEDIA = 0x002D9640
IOCTL_STORAGE_MANAGE_DATA_SET_ATTRIBUTES = 0x002D9404
DEVICE_DSM_ACTION_TRIM = 1
SANITIZE_BLOCK_ERASE = 1
SANITIZE_CRYPTO_ERASE = 2

METHODS = ("auto", "sanitize", "overwrite")

Progress = Callable[[str, float], None]


class EraseError(Exception):
    pass


# --------------------------------------------------------------------------
# disk inventory (Windows)
# --------------------------------------------------------------------------
_LIST_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
$pf = @(Get-CimInstance Win32_PageFileUsage | ForEach-Object { ([string]$_.Name).Substring(0,1).ToUpper() })
@(Get-Disk | ForEach-Object {
  $d = $_
  $pd = Get-PhysicalDisk | Where-Object DeviceId -eq ([string]$d.Number) | Select-Object -First 1
  $letters = @(Get-Partition -DiskNumber $d.Number | Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter })
  [pscustomobject]@{
    number = [int]$d.Number; model = [string]$d.FriendlyName; serial = ([string]$d.SerialNumber).Trim()
    size = [uint64]$d.Size; bus = [string]$d.BusType; media = [string]$pd.MediaType
    sector = [int]$d.LogicalSectorSize; system = [bool]$d.IsSystem; boot = [bool]$d.IsBoot
    offline = [bool]$d.IsOffline; readonly = [bool]$d.IsReadOnly; style = [string]$d.PartitionStyle
    letters = $letters; pagefile = [bool](@($letters | Where-Object { $pf -contains $_ }).Count -gt 0)
  }
}) | ConvertTo-Json -Compress -Depth 3
"""


def _own_drive_letters() -> set[str]:
    letters = set()
    for p in (sys.executable, os.path.abspath(__file__), os.getcwd()):
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if len(drive) == 2 and drive[1] == ":":
            letters.add(drive[0].upper())
    return letters


def blocked_reason(disk: dict) -> str | None:
    """Why this disk must not be erased (None = may be erased)."""
    letters = {str(l).upper() for l in disk.get("letters") or []}
    system_drive = os.environ.get("SystemDrive", "C:")[:1].upper()
    if disk.get("system") or disk.get("boot") or system_drive in letters:
        return "Auf diesem Datenträger läuft Windows."
    if disk.get("pagefile"):
        return "Auf diesem Datenträger liegt die Windows-Auslagerungsdatei."
    own = _own_drive_letters() & letters
    if own:
        return f"Dieses Programm läuft von Laufwerk {sorted(own)[0]}:."
    if disk.get("readonly"):
        return "Der Datenträger ist schreibgeschützt."
    if not disk.get("size"):
        return "Größe des Datenträgers unbekannt."
    return None


def list_disks() -> list[dict]:
    if not IS_WINDOWS:
        return []
    out = winutil._powershell(_LIST_SCRIPT, timeout=60).strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    disks = []
    for d in data:
        letters = d.get("letters") or []
        if isinstance(letters, str):
            letters = [letters]
        d["letters"] = sorted(str(l).upper() for l in letters if l)
        d["sector"] = d.get("sector") or 512
        d["blocked"] = blocked_reason(d)
        disks.append(d)
    disks.sort(key=lambda d: d["number"])
    return disks


def get_disk(number: int) -> dict | None:
    return next((d for d in list_disks() if d["number"] == number), None)


def physical_path(number: int) -> str:
    return f"\\\\.\\PhysicalDrive{int(number)}"


# --------------------------------------------------------------------------
# generic, platform independent building blocks (tested on image files)
# --------------------------------------------------------------------------
def _open(path: str, writable: bool) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    try:
        return os.open(path, flags)
    except PermissionError as exc:
        raise EraseError(f"Kein Zugriff auf {path} – Administratorrechte nötig.") from exc


def _pread(fd: int, offset: int, length: int) -> bytes:
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def take_samples(path: str, size: int, sector: int, count: int = SAMPLE_COUNT,
                 seed: int | None = None) -> list[tuple[int, str | None]]:
    """Fingerprint ``count`` random sectors.  hash None = sector was all zero."""
    rnd = random.Random(seed)
    lo = min(EDGE_SKIP, size // 4) // sector
    hi = (size - min(EDGE_SKIP, size // 4)) // sector - 1
    if hi <= lo:
        lo, hi = 0, max(0, size // sector - 1)
    offsets = sorted({rnd.randint(lo, hi) * sector for _ in range(count)})
    fd = _open(path, False)
    try:
        samples = []
        for off in offsets:
            data = _pread(fd, off, sector)
            samples.append((off, None if not data.strip(b"\x00") else hashlib.sha256(data).hexdigest()))
        return samples
    finally:
        os.close(fd)


def verify_samples(path: str, samples, sector: int) -> dict:
    """Compare sample sectors with their fingerprints from before the erase."""
    fd = _open(path, False)
    changed = unchanged = was_zero = 0
    try:
        for off, digest in samples:
            if digest is None:
                was_zero += 1
                continue
            data = _pread(fd, off, sector)
            if hashlib.sha256(data).hexdigest() == digest:
                unchanged += 1
            else:
                changed += 1
    finally:
        os.close(fd)
    return {"checked": changed + unchanged, "changed": changed, "unchanged": unchanged, "was_empty": was_zero}


def overwrite_device(path: str, size: int, sector: int, progress: Progress | None = None,
                     cancelled: Callable[[], bool] = lambda: False, start: float = 0.0,
                     end: float = 1.0) -> int:
    """Overwrite the whole device with random data. Returns bytes written."""
    progress = progress or (lambda m, f: None)
    size -= size % sector
    fd = _open(path, True)
    written = 0
    last = time.monotonic()
    started = last
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while written < size:
            if cancelled():
                raise InterruptedError
            n = min(CHUNK, size - written)
            data = os.urandom(n)
            view = memoryview(data)
            while view:
                k = os.write(fd, view)
                if k <= 0:
                    raise EraseError(f"Schreibfehler bei Byte {written}")
                view = view[k:]
            written += n
            now = time.monotonic()
            if now - last > 0.5 or written >= size:
                last = now
                speed = written / max(now - started, 1e-6)
                eta = (size - written) / max(speed, 1)
                progress(
                    f"Überschreibe mit Zufallsdaten … {written / 2**30:.1f} von {size / 2**30:.1f} GiB "
                    f"({speed / 2**20:.0f} MB/s, noch ca. {_fmt_eta(eta)})",
                    start + (end - start) * written / size,
                )
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    return written


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


# --------------------------------------------------------------------------
# Windows specific operations
# --------------------------------------------------------------------------
def _ioctl(path: str, code: int, inbuf: bytes) -> tuple[bool, int]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    fd = _open(path, True)
    try:
        handle = msvcrt.get_osfhandle(fd)
        buf = ctypes.create_string_buffer(inbuf, len(inbuf))
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(handle), wintypes.DWORD(code), buf, len(inbuf), None, 0,
            ctypes.byref(returned), None,
        )
        return bool(ok), (0 if ok else ctypes.windll.kernel32.GetLastError())
    finally:
        os.close(fd)


def nvme_sanitize(path: str, method: int, timeout: int = 7200) -> tuple[bool, int]:
    """Send NVMe Sanitize via IOCTL_STORAGE_REINITIALIZE_MEDIA."""
    # STORAGE_REINITIALIZE_MEDIA {Version, Size, TimeoutInSeconds, SanitizeOption bitfield}
    payload = struct.pack("<IIII", 16, 16, timeout, method & 0xF)
    return _ioctl(path, IOCTL_STORAGE_REINITIALIZE_MEDIA, payload)


def trim_device(path: str, size: int, sector: int, progress: Progress | None = None,
                start: float = 0.0, end: float = 1.0) -> bool:
    """TRIM the complete LBA range. False if the driver does not support it."""
    progress = progress or (lambda m, f: None)
    step = 1 << 30  # 1 GiB per range
    per_call = 16
    size -= size % sector
    offset = 0
    while offset < size:
        ranges = []
        while offset < size and len(ranges) < per_call:
            n = min(step, size - offset)
            ranges.append(struct.pack("<qQ", offset, n))
            offset += n
        header = struct.pack("<IIIIIII", 28, DEVICE_DSM_ACTION_TRIM, 0, 0, 0, 32, 16 * len(ranges))
        ok, _err = _ioctl(path, IOCTL_STORAGE_MANAGE_DATA_SET_ATTRIBUTES, header + b"\0" * 4 + b"".join(ranges))
        if not ok:
            return False
        progress(f"TRIM … {offset / 2**30:.0f} von {size / 2**30:.0f} GiB", start + (end - start) * offset / size)
    return True


def diskpart(script: str, timeout: int = 600) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii") as fh:
        fh.write(script)
        path = fh.name
    try:
        res = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True, timeout=timeout,
                             creationflags=0x08000000, encoding="oem", errors="replace")
    finally:
        os.remove(path)
    if res.returncode != 0:
        tail = "\n".join(l for l in res.stdout.strip().splitlines()[-6:] if l.strip())
        raise EraseError(f"diskpart ist fehlgeschlagen:\n{tail}")
    return res.stdout


def _safe_label(label: str) -> str:
    cleaned = "".join(c for c in (label or "") if c.isalnum() or c in " -_")[:32].strip()
    return cleaned or "Datentraeger"


# --------------------------------------------------------------------------
# the complete erase procedure
# --------------------------------------------------------------------------
def erase_disk(number: int, serial: str, method: str = "auto", reformat: bool = True,
               label: str = "", progress: Progress | None = None,
               cancelled: Callable[[], bool] = lambda: False, expected_size: int | None = None) -> dict:
    progress = progress or (lambda m, f: None)
    if not IS_WINDOWS:
        raise EraseError("Das Löschen ganzer Datenträger ist nur unter Windows möglich.")
    if method not in METHODS:
        raise EraseError("Unbekannte Löschmethode.")
    progress("Datenträger wird geprüft …", 0.0)
    disk = get_disk(number)
    if disk is None:
        raise EraseError(f"Datenträger {number} wurde nicht gefunden.")
    if (disk.get("serial") or "") != (serial or "") or (expected_size and int(disk.get("size") or 0) != int(expected_size)):
        raise EraseError("Die Datenträger-Nummern haben sich geändert (Gerät an-/abgesteckt?). "
                         "Bitte die Liste neu laden und erneut auswählen.")
    reason = blocked_reason(disk)
    if reason:
        raise EraseError(f"Dieser Datenträger darf nicht gelöscht werden: {reason}")

    path = physical_path(number)
    size, sector = int(disk["size"]), int(disk.get("sector") or 512)
    report = {"disk": disk["model"], "size": size, "method": None, "steps": []}

    progress("Stichproben werden gespeichert …", 0.01)
    samples = take_samples(path, size, sector)

    if cancelled():
        raise InterruptedError
    progress("Partitionen werden entfernt …", 0.02)
    script = f"select disk {number}\n"
    if disk.get("offline"):
        script += "online disk noerr\n"
    diskpart(script + "attributes disk clear readonly noerr\nclean\n")
    report["steps"].append("Partitionstabelle entfernt")

    sanitized = False
    if method in ("auto", "sanitize") and (disk.get("bus") or "").upper() == "NVME":
        for code, name in ((SANITIZE_CRYPTO_ERASE, "Crypto Erase"), (SANITIZE_BLOCK_ERASE, "Block Erase")):
            progress(f"NVMe Sanitize ({name}) läuft – die SSD löscht sich selbst …", 0.05)
            ok, err = nvme_sanitize(path, code)
            if ok:
                sanitized = True
                report["method"] = f"NVMe Sanitize ({name})"
                report["steps"].append(f"NVMe Sanitize ({name}) von der SSD-Firmware ausgeführt")
                break
            report["steps"].append(f"NVMe Sanitize ({name}) nicht unterstützt (Fehler {err})")
    if method == "sanitize" and not sanitized:
        raise EraseError("Die SSD bzw. ihr Treiber unterstützt NVMe Sanitize nicht. "
                         "Die Partitionen wurden bereits entfernt – wähle „Überschreiben + TRIM“.")

    if not sanitized:
        report["method"] = "Überschreiben mit Zufallsdaten + TRIM"
        written = overwrite_device(path, size, sector, progress, cancelled, 0.05, 0.9)
        report["steps"].append(f"{written / 2**30:.1f} GiB mit Zufallsdaten überschrieben")
        progress("TRIM für den ganzen Datenträger …", 0.9)
        if trim_device(path, size, sector, progress, 0.9, 0.95):
            report["steps"].append("TRIM für den gesamten Datenträger gesendet")
        else:
            report["steps"].append("TRIM vom Treiber nicht unterstützt (bei HDDs/USB normal)")

    progress("Ergebnis wird überprüft …", 0.95)
    report["verify"] = verify_samples(path, samples, sector)

    if reformat:
        progress("Datenträger wird neu formatiert …", 0.97)
        out = diskpart(
            f"select disk {number}\nclean\nconvert gpt\ncreate partition primary\n"
            f'format fs=ntfs quick label="{_safe_label(label)}"\nassign\n'
        )
        report["steps"].append("Neu partitioniert und als NTFS formatiert (Formatieren sendet zusätzlich TRIM)")
        report["format_output"] = out[-400:]
    progress("Fertig", 1.0)
    return report
