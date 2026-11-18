"""Remove usage history and traces of the current Windows user.

Covers browsers (Chromium family and Firefox) and Windows' own history
stores.  Files are overwritten with random data before they are deleted;
because SSDs remap writes, the caller should additionally clean the free
space afterwards (``wipe.wipe_free_space`` + ReTrim) - that step makes the
removal secure.

Deliberately NOT touched: saved passwords, bookmarks, autofill data,
browser settings and Windows event logs.
"""

from __future__ import annotations

import csv
import glob
import io
import os
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import winutil

IS_WINDOWS = sys.platform == "win32"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

Progress = Callable[[str, float], None]


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------
@dataclass
class Target:
    kind: str     # path | contents | files_in | reg_tree | reg_values | firefox_places | special
    value: str
    browser: str | None = None  # process name that locks this target


@dataclass
class Category:
    id: str
    group: str            # "browser" | "windows"
    label: str
    description: str
    default: bool = True
    targets: list[Target] = field(default_factory=list)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value and os.path.isdir(value) else None


def _safe_temp(path: str | None) -> str | None:
    """Accept a temp directory only if it really looks like one.

    Guards against a misconfigured TEMP variable (e.g. ``C:\\`` or the user
    profile) turning "delete temp files" into "delete everything".
    """
    if not path:
        return None
    norm = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    parts = [p.lower() for p in norm.replace("\\", "/").split("/") if p]
    names = {"temp", "tmp"}
    if len(parts) < 2:
        return None
    # ...\Temp or a per-session subfolder such as ...\Temp\2 (Remote Desktop)
    if parts[-1] not in names and not (parts[-1].isdigit() and parts[-2] in names):
        return None
    for name in ("USERPROFILE", "HOME", "SystemRoot", "ProgramFiles", "ProgramData", "APPDATA", "LOCALAPPDATA"):
        other = os.environ.get(name)
        if other and os.path.normcase(os.path.abspath(other)).rstrip("\\/") == norm:
            return None
    return path


CHROMIUM_BROWSERS = [
    # (key, name, env var, relative user data dir, process, single profile)
    ("chrome", "Google Chrome", "LOCALAPPDATA", r"Google\Chrome\User Data", "chrome.exe", False),
    ("edge", "Microsoft Edge", "LOCALAPPDATA", r"Microsoft\Edge\User Data", "msedge.exe", False),
    ("brave", "Brave", "LOCALAPPDATA", r"BraveSoftware\Brave-Browser\User Data", "brave.exe", False),
    ("vivaldi", "Vivaldi", "LOCALAPPDATA", r"Vivaldi\User Data", "vivaldi.exe", False),
    ("opera", "Opera", "APPDATA", r"Opera Software\Opera Stable", "opera.exe", True),
    ("operagx", "Opera GX", "APPDATA", r"Opera Software\Opera GX Stable", "opera.exe", True),
]

CHROMIUM_HISTORY = [
    "History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal", "Shortcuts",
    "Shortcuts-journal", "Network Action Predictor", "Network Action Predictor-journal", "Favicons",
    "Favicons-journal", "History Provider Cache", "Sessions", "Current Session", "Current Tabs",
    "Last Session", "Last Tabs", "DownloadMetadata", "Media History", "Media History-journal",
]
CHROMIUM_COOKIES = [
    "Cookies", "Cookies-journal", r"Network\Cookies", r"Network\Cookies-journal", "Extension Cookies",
    "Extension Cookies-journal", "Local Storage", "Session Storage", "IndexedDB", "Service Worker",
    r"Network\Trust Tokens", r"Network\Trust Tokens-journal", "databases", "File System",
]
CHROMIUM_CACHE = [
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "Media Cache", "Application Cache",
]
CHROMIUM_ROOT_CACHE = ["ShaderCache", "GrShaderCache", "GraphiteDawnCache"]

FIREFOX_HISTORY = ["formhistory.sqlite", "sessionstore.jsonlz4", "sessionstore-backups", "favicons.sqlite",
                   "favicons.sqlite-wal"]
FIREFOX_COOKIES = ["cookies.sqlite", "cookies.sqlite-wal", "webappsstore.sqlite", "webappsstore.sqlite-wal",
                   r"storage\default"]
FIREFOX_CACHE = ["cache2", "startupCache", "thumbnails", "jumpListCache"]

EXPLORER = r"Software\Microsoft\Windows\CurrentVersion\Explorer"


def _chromium_profiles(root: str, single: bool) -> list[str]:
    if single:
        return [root] if os.path.isdir(root) else []
    out = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and os.path.exists(os.path.join(entry.path, "Preferences")):
                out.append(entry.path)
    except OSError:
        pass
    return out


def _firefox_profiles(env: str) -> list[str]:
    base = _env(env)
    if not base:
        return []
    return sorted(p for p in glob.glob(os.path.join(base, "Mozilla", "Firefox", "Profiles", "*")) if os.path.isdir(p))


def _join_existing(base: str, names: list[str], browser: str) -> list[Target]:
    out = []
    for name in names:
        p = os.path.join(base, *name.split("\\"))
        if os.path.lexists(p):
            out.append(Target("path", p, browser))
    return out


def _browser_targets() -> dict[str, list[Target]]:
    t: dict[str, list[Target]] = {"history": [], "cookies": [], "cache": []}
    for _key, _name, env, rel, proc, single in CHROMIUM_BROWSERS:
        base = _env(env)
        if not base:
            continue
        roots = [os.path.join(base, *rel.split("\\"))]
        if single:  # Opera keeps its cache under LOCALAPPDATA
            local = _env("LOCALAPPDATA")
            if local:
                roots.append(os.path.join(local, *rel.split("\\")))
        for root in roots:
            for prof in _chromium_profiles(root, single):
                t["history"] += _join_existing(prof, CHROMIUM_HISTORY, proc)
                t["cookies"] += _join_existing(prof, CHROMIUM_COOKIES, proc)
                t["cache"] += _join_existing(prof, CHROMIUM_CACHE, proc)
            if not single:
                t["cache"] += _join_existing(root, CHROMIUM_ROOT_CACHE, proc)
    for prof in _firefox_profiles("APPDATA"):
        places = os.path.join(prof, "places.sqlite")
        if os.path.exists(places):
            t["history"].append(Target("firefox_places", places, "firefox.exe"))
        t["history"] += _join_existing(prof, FIREFOX_HISTORY, "firefox.exe")
        t["cookies"] += _join_existing(prof, FIREFOX_COOKIES, "firefox.exe")
    for prof in _firefox_profiles("LOCALAPPDATA"):
        t["cache"] += _join_existing(prof, FIREFOX_CACHE, "firefox.exe")
    return t


def build_categories() -> list[Category]:
    b = _browser_targets()
    appdata, local = _env("APPDATA"), _env("LOCALAPPDATA")
    temp, sysroot, progdata = _safe_temp(_env("TEMP")), _env("SystemRoot"), _env("ProgramData")

    def files(pattern_base: str | None, *parts: str, kind: str = "path") -> list[Target]:
        if not pattern_base:
            return []
        return [Target(kind, p) for p in sorted(glob.glob(os.path.join(pattern_base, *parts)))]

    def dir_target(base: str | None, *parts: str, kind: str = "contents") -> list[Target]:
        if not base:
            return []
        p = os.path.join(base, *parts)
        return [Target(kind, p)] if os.path.isdir(p) else []

    cats = [
        Category("browser_history", "browser", "Browserverlauf",
                 "Besuchte Seiten, Downloadliste, Sitzungen/offene Tabs, Suchvorschläge und Formulareingaben "
                 "(Chrome, Edge, Brave, Vivaldi, Opera, Firefox). Lesezeichen und Passwörter bleiben erhalten.",
                 True, b["history"]),
        Category("browser_cache", "browser", "Browser-Cache",
                 "Zwischengespeicherte Seiten, Bilder und Skripte.", True, b["cache"]),
        Category("browser_cookies", "browser", "Cookies & Website-Daten",
                 "Cookies, lokaler Speicher und Website-Datenbanken. Du wirst dadurch auf allen Webseiten abgemeldet.",
                 False, b["cookies"]),
        Category("recent", "windows", "Zuletzt verwendete Dateien",
                 "„Zuletzt verwendet“-Liste, Sprunglisten der Taskleiste und die Verläufe der Öffnen-/Speichern-Dialoge.",
                 True,
                 dir_target(appdata, "Microsoft", "Windows", "Recent", kind="files_in")
                 + [Target("reg_tree", EXPLORER + r"\RecentDocs")]
                 + [Target("reg_tree", EXPLORER + "\\ComDlg32\\" + k) for k in
                    ("OpenSavePidlMRU", "LastVisitedPidlMRU", "LastVisitedPidlMRULegacy", "OpenSaveMRU",
                     "LastVisitedMRU", "CIDSizeMRU")]),
        Category("explorer", "windows", "Explorer- & Ausführen-Verlauf",
                 "Eingaben im Ausführen-Dialog, in der Adress- und Suchleiste des Explorers, Programmnutzung "
                 "(UserAssist) und geöffnete Ordner (ShellBags).",
                 True,
                 [Target("reg_values", EXPLORER + r"\RunMRU"),
                  Target("reg_values", EXPLORER + r"\TypedPaths"),
                  Target("reg_tree", EXPLORER + r"\WordWheelQuery"),
                  Target("reg_values", EXPLORER + r"\Map Network Drive MRU"),
                  Target("reg_values", r"Software\Microsoft\Internet Explorer\TypedURLs"),
                  Target("reg_values", r"Software\Microsoft\Internet Explorer\TypedURLsTime"),
                  Target("reg_userassist", EXPLORER + r"\UserAssist"),
                  Target("reg_tree", r"Software\Microsoft\Windows\Shell\BagMRU"),
                  Target("reg_tree", r"Software\Microsoft\Windows\Shell\Bags"),
                  Target("reg_tree", r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"),
                  Target("reg_tree", r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags")]),
        Category("thumbnails", "windows", "Miniaturansichten-Cache",
                 "Vorschaubilder von Fotos und Videos, die der Explorer zwischenspeichert – auch von längst gelöschten Dateien.",
                 True,
                 files(local, "Microsoft", "Windows", "Explorer", "thumbcache_*.db")
                 + files(local, "Microsoft", "Windows", "Explorer", "iconcache_*.db")),
        Category("timeline", "windows", "Aktivitätsverlauf (Timeline)",
                 "Von Windows protokollierte Aktivitäten: geöffnete Dokumente, Webseiten und Apps.", True,
                 files(local, "ConnectedDevicesPlatform", "*", "ActivitiesCache.db*")),
        Category("temp", "windows", "Temporäre Dateien",
                 "Inhalt der Temp-Ordner des Benutzers und von Windows.", True,
                 dir_target(temp, "") + (dir_target(sysroot, "Temp") if sysroot and len(sysroot) > 3 else [])),
        Category("prefetch", "windows", "Prefetch",
                 "Protokoll, welche Programme wann gestartet wurden.", True,
                 files(sysroot, "Prefetch", "*.pf")),
        Category("powershell", "windows", "Befehlsverlauf",
                 "Verlauf der in PowerShell eingegebenen Befehle.", True,
                 files(appdata, "Microsoft", "Windows", "PowerShell", "PSReadLine", "*_history.txt")),
        Category("crashdumps", "windows", "Fehlerberichte & Absturzabbilder",
                 "Windows-Fehlerberichte und Speicherabbilder abgestürzter Programme (können Dokumentinhalte enthalten).",
                 True,
                 dir_target(local, "CrashDumps")
                 + dir_target(progdata, "Microsoft", "Windows", "WER", "ReportArchive")
                 + dir_target(progdata, "Microsoft", "Windows", "WER", "ReportQueue")),
        Category("recyclebin", "windows", "Papierkorb", "Leert den Papierkorb auf allen Laufwerken.", True,
                 [Target("special", "recyclebin")]),
        Category("clipboard", "windows", "Zwischenablage", "Aktueller Inhalt und Verlauf der Zwischenablage (Win+V).",
                 True, [Target("special", "clipboard")]),
        Category("dns", "windows", "DNS-Cache", "Liste der zuletzt aufgerufenen Domains im Netzwerk-Cache.", True,
                 [Target("special", "dns")]),
    ]
    return cats


# --------------------------------------------------------------------------
# helpers: file system
# --------------------------------------------------------------------------
def _excluded_paths() -> list[str]:
    out = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.normcase(os.path.abspath(meipass)))
    out.append(os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return out


def _is_excluded(path: str, excluded: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return any(p == e or e.startswith(p + os.sep) or p.startswith(e + os.sep) for e in excluded)


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _measure(path: str) -> tuple[int, int]:
    """(file count, bytes) below ``path`` without following links."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        return 1, st.st_size
    files = size = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode) and not _is_link(st):
                    stack.append(e.path)
                else:
                    files += 1
                    size += st.st_size
    return files, size


class _Stats:
    def __init__(self, overwrite: bool, excluded: list[str], cancelled):
        self.overwrite = overwrite
        self.excluded = excluded
        self.cancelled = cancelled
        self.files = 0
        self.bytes = 0
        self.failed: list[str] = []

    def fail(self, path: str):
        self.failed.append(path)


def _overwrite_file(path: str, size: int) -> None:
    with open(path, "r+b", buffering=0) as fh:
        pos = 0
        while pos < size:
            n = min(1024 * 1024, size - pos)
            fh.write(os.urandom(n))
            pos += n
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _remove_file(path: str, st: os.stat_result, s: _Stats) -> None:
    try:
        if not st.st_mode & stat.S_IWRITE:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        if s.overwrite and st.st_size:
            _overwrite_file(path, st.st_size)
        os.remove(path)
        s.files += 1
        s.bytes += st.st_size
    except OSError:
        s.fail(path)


def _delete(path: str, s: _Stats, keep_self: bool = False, files_only: bool = False) -> None:
    if s.cancelled():
        raise InterruptedError
    if _is_excluded(path, s.excluded) and not keep_self:
        return
    try:
        st = os.lstat(path)
    except OSError:
        return
    if _is_link(st):
        if keep_self:
            return
        try:  # remove the link itself, never what it points to
            os.rmdir(path) if stat.S_ISDIR(st.st_mode) else os.unlink(path)
        except OSError:
            s.fail(path)
        return
    if not stat.S_ISDIR(st.st_mode):
        _remove_file(path, st, s)
        return
    try:
        children = [e.path for e in os.scandir(path)]
    except OSError:
        s.fail(path)
        return
    for child in children:
        if _is_excluded(child, s.excluded):
            continue
        _delete(child, s, files_only=files_only)
    if not keep_self and not files_only:
        try:
            os.rmdir(path)
        except OSError:
            pass  # still contains locked/excluded entries


def clean_firefox_places(path: str) -> int:
    """Remove history from places.sqlite but keep bookmarks. Returns rows removed."""
    con = sqlite3.connect(path, timeout=2)
    removed = 0
    try:
        con.execute("PRAGMA secure_delete = ON")
        statements = [
            "DELETE FROM moz_historyvisits",
            "DELETE FROM moz_inputhistory",
            "DELETE FROM moz_places_metadata",
            "DELETE FROM moz_places_metadata_search_queries",
            "DELETE FROM moz_places WHERE id NOT IN (SELECT fk FROM moz_bookmarks WHERE fk IS NOT NULL)",
            "UPDATE moz_places SET visit_count = 0, last_visit_date = NULL",
            "DELETE FROM moz_annos WHERE place_id NOT IN (SELECT id FROM moz_places)",
            "DELETE FROM moz_origins WHERE id NOT IN (SELECT origin_id FROM moz_places)",
        ]
        for sql in statements:
            try:
                cur = con.execute(sql)
                if sql.startswith("DELETE"):
                    removed += max(cur.rowcount, 0)
            except sqlite3.Error:
                pass  # table not present in this Firefox version
        con.commit()
        con.execute("VACUUM")  # rebuild the file so deleted rows leave no residue
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return removed


# --------------------------------------------------------------------------
# helpers: registry and special actions (Windows only)
# --------------------------------------------------------------------------
def _reg_open(sub: str, write: bool = False):
    import winreg

    access = winreg.KEY_ALL_ACCESS if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, access)


def _reg_count(sub: str, recursive: bool) -> int:
    import winreg

    try:
        key = _reg_open(sub)
    except OSError:
        return 0
    with key:
        _subkeys, values, _ = winreg.QueryInfoKey(key)
        total = values
        if recursive:
            names = [winreg.EnumKey(key, i) for i in range(_subkeys)]
            total += sum(_reg_count(sub + "\\" + n, True) + 1 for n in names)
    return total


def _reg_delete_tree(sub: str) -> int:
    import winreg

    count = _reg_count(sub, True)
    try:
        key = _reg_open(sub, write=True)
    except OSError:
        return 0
    with key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _reg_delete_tree(sub + "\\" + child)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
    except OSError:
        pass
    return count


def _reg_clear_values(sub: str) -> int:
    import winreg

    try:
        key = _reg_open(sub, write=True)
    except OSError:
        return 0
    removed = 0
    with key:
        while True:
            try:
                name = winreg.EnumValue(key, 0)[0]
                winreg.DeleteValue(key, name)
                removed += 1
            except OSError:
                break
    return removed


def _userassist_count_keys(sub: str) -> list[str]:
    import winreg

    try:
        key = _reg_open(sub)
    except OSError:
        return []
    with key:
        n = winreg.QueryInfoKey(key)[0]
        return [sub + "\\" + winreg.EnumKey(key, i) + "\\Count" for i in range(n)]


def _recycle_bin_info() -> tuple[int, int]:
    import ctypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return int(info.i64NumItems), int(info.i64Size)


def _special(name: str) -> str:
    if not IS_WINDOWS:
        return "nur unter Windows"
    if name == "recyclebin":
        import ctypes

        count, size = _recycle_bin_info()
        hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
        if hr not in (0, -2147418113):  # S_OK / E_UNEXPECTED (already empty)
            return f"Papierkorb konnte nicht geleert werden (Fehler {hr})"
        return f"{count} Elemente ({size / 2**20:.1f} MB) entfernt"
    if name == "clipboard":
        winutil._powershell(
            "try { $null=[Windows.ApplicationModel.DataTransfer.Clipboard,Windows.ApplicationModel.DataTransfer,"
            "ContentType=WindowsRuntime]; [Windows.ApplicationModel.DataTransfer.Clipboard]::ClearHistory() | Out-Null;"
            " [Windows.ApplicationModel.DataTransfer.Clipboard]::Clear() } catch {};"
            " try { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::Clear() } catch {}"
        )
        return "Zwischenablage und Verlauf geleert"
    if name == "dns":
        winutil._run(["ipconfig", "/flushdns"])
        return "DNS-Cache geleert"
    return ""


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------
BROWSER_PROCESSES = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi", "opera.exe": "Opera", "firefox.exe": "Firefox",
}


def running_processes() -> set[str]:
    if not IS_WINDOWS:
        return set()
    out = winutil._run(["tasklist", "/FO", "CSV", "/NH"])
    names = set()
    for row in csv.reader(io.StringIO(out)):
        if row:
            names.add(row[0].lower())
    return names


def close_processes(names: set[str], wait: float = 6.0) -> None:
    if not IS_WINDOWS or not names:
        return
    for n in names:
        winutil._run(["taskkill", "/IM", n, "/T"])
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and running_processes() & names:
        time.sleep(0.5)
    for n in running_processes() & names:
        winutil._run(["taskkill", "/F", "/IM", n, "/T"])
    time.sleep(1.0)


def restart_explorer_unelevated() -> None:
    """Start explorer.exe again with the user's normal (non-admin) rights."""
    # A scheduled task with /RL LIMITED runs in the user's session without the
    # admin token - starting explorer.exe directly would give an elevated shell.
    task = "SSDTool-Explorer-Restart"
    try:
        created = subprocess.run(
            ["schtasks", "/Create", "/TN", task, "/TR", "explorer.exe", "/SC", "ONCE", "/ST", "00:00",
             "/RL", "LIMITED", "/F"], capture_output=True, creationflags=0x08000000, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        created = False
    if created:
        winutil._run(["schtasks", "/Run", "/TN", task])
        time.sleep(2)
        winutil._run(["schtasks", "/Delete", "/TN", task, "/F"])
    else:
        subprocess.Popen(["explorer.exe"], creationflags=0x08000000)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def analyze(progress: Progress | None = None, cancelled: Callable[[], bool] = lambda: False) -> dict:
    progress = progress or (lambda m, f: None)
    cats = build_categories()
    running = running_processes()
    excluded = _excluded_paths()
    result = []
    for i, c in enumerate(cats):
        if cancelled():
            raise InterruptedError
        progress(f"Analysiere: {c.label} …", i / len(cats))
        files = size = entries = 0
        for t in c.targets:
            if t.kind in ("path", "firefox_places"):
                if not _is_excluded(t.value, excluded):
                    f, b = _measure(t.value)
                    files, size = files + f, size + b
            elif t.kind in ("contents", "files_in"):
                try:
                    for e in os.scandir(t.value):
                        if not _is_excluded(e.path, excluded):
                            f, b = _measure(e.path)
                            files, size = files + f, size + b
                except OSError:
                    pass
            elif IS_WINDOWS and t.kind == "reg_tree":
                entries += _reg_count(t.value, True)
            elif IS_WINDOWS and t.kind == "reg_values":
                entries += _reg_count(t.value, False)
            elif IS_WINDOWS and t.kind == "reg_userassist":
                entries += sum(_reg_count(k, False) for k in _userassist_count_keys(t.value))
            elif t.kind == "special" and t.value == "recyclebin" and IS_WINDOWS:
                n, b = _recycle_bin_info()
                files, size = files + n, size + b
        browsers = sorted({BROWSER_PROCESSES[t.browser] for t in c.targets
                           if t.browser and t.browser in running})
        result.append({
            "id": c.id, "group": c.group, "label": c.label, "description": c.description,
            "default": c.default, "files": files, "bytes": size, "entries": entries,
            "special": any(t.kind == "special" for t in c.targets),
            "running": browsers, "available": bool(c.targets),
        })
    progress("Fertig", 1.0)
    running_browsers = sorted({v for k, v in BROWSER_PROCESSES.items() if k in running})
    return {"categories": result, "running_browsers": running_browsers, "windows": IS_WINDOWS}


def clean(category_ids: list[str], overwrite: bool = True, close_browsers: bool = True,
          restart_explorer: bool = True, progress: Progress | None = None,
          cancelled: Callable[[], bool] = lambda: False) -> list[dict]:
    progress = progress or (lambda m, f: None)
    wanted = set(category_ids)
    cats = [c for c in build_categories() if c.id in wanted]
    if not cats:
        raise ValueError("Keine Kategorie ausgewählt.")
    excluded = _excluded_paths()

    running = running_processes()
    used_browsers = {t.browser for c in cats for t in c.targets if t.browser}
    to_close = running & used_browsers
    skip_browsers: set[str] = set()
    if to_close:
        if close_browsers:
            progress("Browser werden geschlossen …", 0.0)
            close_processes(to_close)
            skip_browsers = running_processes() & to_close
        else:
            skip_browsers = to_close

    stop_explorer = restart_explorer and IS_WINDOWS and any(c.id in ("thumbnails", "explorer", "recent") for c in cats)
    if stop_explorer:
        progress("Explorer wird kurz beendet …", 0.02)
        winutil._run(["taskkill", "/F", "/IM", "explorer.exe"])
        time.sleep(1.5)

    results = []
    try:
        for i, c in enumerate(cats):
            progress(f"Entferne: {c.label} …", 0.05 + 0.9 * i / len(cats))
            s = _Stats(overwrite, excluded, cancelled)
            entries = 0
            notes = []
            skipped = set()
            for t in c.targets:
                if t.browser and t.browser in skip_browsers:
                    skipped.add(BROWSER_PROCESSES[t.browser])
                    continue
                if t.kind == "path":
                    _delete(t.value, s)
                elif t.kind == "contents":
                    _delete(t.value, s, keep_self=True)
                elif t.kind == "files_in":
                    _delete(t.value, s, keep_self=True, files_only=True)
                elif t.kind == "firefox_places":
                    try:
                        entries += clean_firefox_places(t.value)
                    except sqlite3.Error as exc:
                        notes.append(f"Firefox-Verlauf: {exc}")
                        s.fail(t.value)
                elif IS_WINDOWS and t.kind == "reg_tree":
                    entries += _reg_delete_tree(t.value)
                elif IS_WINDOWS and t.kind == "reg_values":
                    entries += _reg_clear_values(t.value)
                elif IS_WINDOWS and t.kind == "reg_userassist":
                    entries += sum(_reg_clear_values(k) for k in _userassist_count_keys(t.value))
                elif t.kind == "special":
                    msg = _special(t.value)
                    if msg:
                        notes.append(msg)
            if skipped:
                notes.append("Übersprungen, weil noch geöffnet: " + ", ".join(sorted(skipped)))
            if s.failed:
                notes.append(f"{len(s.failed)} Dateien sind in Benutzung und wurden nicht gelöscht")
            results.append({
                "id": c.id, "label": c.label, "files": s.files, "bytes": s.bytes,
                "entries": entries, "failed": len(s.failed), "failed_examples": s.failed[:5],
                "notes": notes, "ok": not s.failed and not skipped,
            })
    finally:
        if stop_explorer:
            restart_explorer_unelevated()
    progress("Fertig", 1.0)
    return results
