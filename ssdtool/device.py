"""Low-level, sector-aligned access to a raw Windows volume or a disk image.

Windows only allows reads/writes on raw volume handles (``\\\\.\\C:``) at
offsets and lengths that are multiples of the sector size.  ``RawDevice``
hides that behind a byte-addressed ``read``/``write`` API by always working
on 4096-byte aligned blocks (which satisfies 512e and 4Kn drives alike).
"""

from __future__ import annotations

import os
import re
import struct
import sys
import threading

ALIGN = 4096

IS_WINDOWS = sys.platform == "win32"

# Windows FSCTL codes for volume locking (winioctl.h)
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020

_VOLUME_RE = re.compile(r"^[A-Za-z]:?\\?$")


class DeviceError(Exception):
    pass


def volume_path(source: str) -> str:
    """Turn ``C``, ``C:`` or ``C:\\`` into ``\\\\.\\C:``; leave other paths alone."""
    s = source.strip()
    if _VOLUME_RE.match(s):
        return "\\\\.\\" + s[0].upper() + ":"
    return s


def is_volume_path(path: str) -> bool:
    return path.startswith("\\\\.\\") and len(path) == 6 and path.endswith(":")


class RawDevice:
    """Byte-addressed access to a block device, volume handle or image file.

    ``base_offset`` lets the device start inside a larger image (e.g. a
    partition inside a whole-disk image).
    """

    def __init__(self, path: str, writable: bool = False, base_offset: int = 0):
        self.path = path
        self.writable = writable
        self.base_offset = base_offset
        self._lock = threading.Lock()
        flags = os.O_RDWR if writable else os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        try:
            self.fd = os.open(path, flags)
        except PermissionError as exc:
            raise DeviceError(
                f"Zugriff auf {path} verweigert. Das Tool muss als Administrator laufen."
            ) from exc
        except OSError as exc:
            raise DeviceError(f"{path} kann nicht geöffnet werden: {exc}") from exc
        self._locked = False

    # -- context manager -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.fd is not None:
            try:
                if self._locked:
                    self.unlock()
            finally:
                os.close(self.fd)
                self.fd = None

    # -- I/O -------------------------------------------------------------
    def _read_exact(self, start: int, length: int) -> bytes:
        os.lseek(self.fd, start, os.SEEK_SET)
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = os.read(self.fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) < length:
            data += b"\x00" * (length - len(data))
        return data

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        offset += self.base_offset
        start = offset - (offset % ALIGN)
        end = offset + length
        end += (-end) % ALIGN
        with self._lock:
            data = self._read_exact(start, end - start)
        rel = offset - start
        return data[rel : rel + length]

    def write(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise DeviceError("Gerät ist nur zum Lesen geöffnet")
        if not data:
            return
        offset += self.base_offset
        start = offset - (offset % ALIGN)
        end = offset + len(data)
        end += (-end) % ALIGN
        with self._lock:
            if start != offset or end != offset + len(data):
                # read-modify-write for unaligned ranges
                block = bytearray(self._read_exact(start, end - start))
                rel = offset - start
                block[rel : rel + len(data)] = data
                payload = bytes(block)
            else:
                payload = bytes(data)
            os.lseek(self.fd, start, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(self.fd, view)
                if written <= 0:
                    raise DeviceError("Schreiben auf das Gerät fehlgeschlagen")
                view = view[written:]

    def flush(self):
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    # -- Windows volume locking -----------------------------------------
    def _ioctl(self, code: int) -> bool:
        if not IS_WINDOWS:
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(self.fd)
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(handle), wintypes.DWORD(code), None, 0, None, 0,
            ctypes.byref(returned), None,
        )
        return bool(ok)

    def lock(self) -> bool:
        """Lock the volume (exclusive access).  No-op for image files."""
        if not is_volume_path(self.path):
            self._locked = True
            return True
        ok = self._ioctl(FSCTL_LOCK_VOLUME)
        self._locked = ok
        return ok

    def dismount(self) -> bool:
        if not is_volume_path(self.path):
            return True
        return self._ioctl(FSCTL_DISMOUNT_VOLUME)

    def unlock(self) -> bool:
        if not self._locked:
            return True
        self._locked = False
        if not is_volume_path(self.path):
            return True
        return self._ioctl(FSCTL_UNLOCK_VOLUME)


def find_ntfs_offset(path: str) -> int:
    """Return the byte offset of the first NTFS file system in ``path``.

    Handles bare volumes/partition images (offset 0) as well as whole-disk
    images with an MBR or GPT partition table.
    """
    with RawDevice(path) as dev:
        sector0 = dev.read(0, 512)
        if sector0[3:11] == b"NTFS    ":
            return 0
        if sector0[510:512] != b"\x55\xaa":
            raise DeviceError("Kein NTFS-Dateisystem und keine Partitionstabelle gefunden.")
        candidates = []
        entries = [sector0[446 + i * 16 : 462 + i * 16] for i in range(4)]
        if any(e[4] == 0xEE for e in entries):
            hdr = dev.read(512, 512)
            if hdr[0:8] == b"EFI PART":
                entries_lba, count, size = struct.unpack_from("<QII", hdr, 72)
                table = dev.read(entries_lba * 512, count * size)
                for i in range(count):
                    e = table[i * size : (i + 1) * size]
                    if e[0:16] == b"\x00" * 16:
                        continue
                    first_lba = struct.unpack_from("<Q", e, 32)[0]
                    candidates.append(first_lba * 512)
        else:
            for e in entries:
                ptype = e[4]
                lba = struct.unpack_from("<I", e, 8)[0]
                if ptype and lba:
                    candidates.append(lba * 512)
        for off in candidates:
            if dev.read(off, 512)[3:11] == b"NTFS    ":
                return off
    raise DeviceError("Keine NTFS-Partition im Abbild gefunden.")
