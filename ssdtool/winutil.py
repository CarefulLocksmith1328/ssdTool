"""Windows specific helpers: admin rights, drive list, SSD/TRIM detection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def relaunch_as_admin() -> bool:
    """Restart the current program elevated (UAC prompt). True if started."""
    if not IS_WINDOWS:
        return False
    import ctypes

    if getattr(sys, "frozen", False):
        exe, params = sys.executable, " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        params = "-m ssdtool " + " ".join(f'"{a}"' for a in sys.argv[1:])
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    return rc > 32


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW, encoding="utf-8", errors="replace",
        )
        return res.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _powershell(script: str, timeout: int = 30) -> str:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def trim_enabled() -> bool | None:
    """Is TRIM (DisableDeleteNotify = 0) enabled for NTFS?"""
    if not IS_WINDOWS:
        return None
    out = _run(["fsutil", "behavior", "query", "DisableDeleteNotify"])
    for line in out.splitlines():
        if "NTFS" in line.upper() and "=" in line:
            value = line.split("=", 1)[1].strip()
            return value.startswith("0")
    return None


def _media_types() -> dict[str, dict]:
    """Map drive letter -> {media, bus, model} via Storage cmdlets."""
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-Partition | Where-Object DriveLetter | ForEach-Object {"
        " $pd = Get-PhysicalDisk | Where-Object DeviceId -eq ([string]$_.DiskNumber);"
        " [pscustomobject]@{Letter=[string]$_.DriveLetter; Media=[string]$pd.MediaType;"
        " Bus=[string]$pd.BusType; Model=[string]$pd.FriendlyName} } | ConvertTo-Json -Compress"
    )
    out = _powershell(script).strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    result = {}
    for item in data:
        letter = (item.get("Letter") or "").upper()
        if letter:
            result[letter] = {
                "media": item.get("Media") or "Unknown",
                "bus": item.get("Bus") or "",
                "model": item.get("Model") or "",
            }
    return result


def list_drives() -> list[dict]:
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    media = _media_types()
    system_drive = os.environ.get("SystemDrive", "C:").upper()[:1]
    drives = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i)
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3):  # removable or fixed
            continue
        label = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        ok = k32.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), None, None, fsname, 261)
        if not ok:
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        m = media.get(letter, {})
        drives.append({
            "letter": letter,
            "label": label.value,
            "filesystem": fsname.value,
            "total": usage.total,
            "free": usage.free,
            "media": m.get("media", "Unknown"),
            "bus": m.get("bus", ""),
            "model": m.get("model", ""),
            "system": letter == system_drive,
            "removable": dtype == 2,
        })
    return drives


def retrim(letter: str) -> str:
    """Ask Windows to send TRIM for all free clusters (defrag /L)."""
    if not IS_WINDOWS:
        return "ReTrim ist nur unter Windows verfügbar."
    out = _run(["defrag", f"{letter.upper()[0]}:", "/L"], timeout=3600)
    return out.strip() or "ReTrim ausgeführt."


def pick_folder(initial: str | None = None) -> str | None:
    """Native folder picker (tkinter).  None if unavailable or cancelled."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(initialdir=initial or None, title="Zielordner wählen")
    finally:
        root.destroy()
    return os.path.normpath(path) if path else None
