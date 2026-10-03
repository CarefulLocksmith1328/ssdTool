"""Local HTTP server that exposes the tool to the browser frontend.

The server only listens on 127.0.0.1 and every API call must carry a
random per-start token (``X-Token`` header) so that other websites open in
the browser cannot trigger scans or wipes (CSRF / DNS rebinding).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, winutil
from .device import DeviceError, RawDevice, find_ntfs_offset, is_volume_path, volume_path
from .ntfs import NTFSError, NTFSVolume
from .recover import read_preview, recover_files, same_volume
from .scanner import scan_volume
from .wipe import WipeError, shred_deleted, wipe_free_space

CONFIRM_WORD = "LÖSCHEN"


def resource_dir() -> str:
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle
    if meipass:
        return os.path.join(meipass, "ssdtool", "web")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"
        self.message = "Wird gestartet …"
        self.progress = 0.0
        self.result = None
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.started = time.time()
        self.finished: float | None = None

    def update(self, message: str, fraction: float):
        self.message = message
        self.progress = max(0.0, min(1.0, fraction))

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "message": self.message,
            "progress": round(self.progress, 4), "result": self.result, "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class Scan:
    def __init__(self, source: str, base_offset: int, drive_letter: str | None):
        self.source = source
        self.base_offset = base_offset
        self.drive_letter = drive_letter
        self.entries = []
        self.by_id = {}
        self.summary = None

    def open_volume(self, writable: bool = False):
        dev = RawDevice(self.source, writable=writable, base_offset=self.base_offset)
        try:
            return dev, NTFSVolume(dev)
        except Exception:
            dev.close()
            raise


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.jobs: dict[str, Job] = {}
        self.scans: dict[str, Scan] = {}
        self.lock = threading.Lock()

    def run_job(self, kind: str, fn) -> Job:
        job = Job(kind)
        self.jobs[job.id] = job

        def runner():
            try:
                job.result = fn(job)
                job.status = "done"
                job.progress = 1.0
                if job.message.startswith("Wird"):
                    job.message = "Fertig"
            except InterruptedError:
                job.status = "cancelled"
                job.message = "Abgebrochen"
            except (DeviceError, NTFSError, WipeError, ValueError, OSError) as exc:
                job.status = "error"
                job.error = str(exc)
            except Exception as exc:  # pragma: no cover - unexpected
                traceback.print_exc()
                job.status = "error"
                job.error = f"Unerwarteter Fehler: {exc}"
            finally:
                job.finished = time.time()

        threading.Thread(target=runner, daemon=True).start()
        return job

    # -- actions ----------------------------------------------------------
    def start_scan(self, source: str, check_trim: bool) -> Job:
        if not source:
            raise ValueError("Bitte ein Laufwerk oder eine Abbild-Datei angeben.")
        path = volume_path(source)
        letter = path[4] if is_volume_path(path) else None

        def work(job: Job):
            job.update("Dateisystem wird geöffnet …", 0)
            offset = 0 if letter else find_ntfs_offset(path)
            scan = Scan(path, offset, letter)
            dev, vol = scan.open_volume()
            with dev:
                entries, summary = scan_volume(vol, job.update, job.cancel_event.is_set, check_trim)
            entries.sort(key=lambda e: (e.path.lower(), e.name.lower()))
            scan.entries = entries
            scan.by_id = {e.id: e for e in entries}
            scan.summary = summary
            self.scans[job.id] = scan
            job.message = f"{len(entries):,} gelöschte Dateien gefunden".replace(",", ".")
            return {"scan": job.id, "summary": summary.__dict__, "source": source}

        return self.run_job("scan", work)

    def _selected(self, scan_id: str, ids) -> tuple[Scan, list]:
        scan = self.scans.get(scan_id)
        if scan is None:
            raise ValueError("Scan nicht gefunden – bitte erneut scannen.")
        entries = [scan.by_id[i] for i in map(int, ids or []) if i in scan.by_id]
        if not entries:
            raise ValueError("Keine Dateien ausgewählt.")
        return scan, entries

    def start_recover(self, body: dict) -> Job:
        scan, entries = self._selected(body.get("scan"), body.get("ids"))
        target = (body.get("target") or "").strip()
        if not target:
            raise ValueError("Bitte einen Zielordner angeben.")
        target = os.path.abspath(os.path.expanduser(target))
        if same_volume(scan.drive_letter, target) and not body.get("allow_same_drive"):
            raise ValueError(
                "Der Zielordner liegt auf demselben Laufwerk, das durchsucht wurde. Dadurch würden "
                "gelöschte Daten überschrieben. Bitte ein anderes Laufwerk (z. B. USB-Stick) wählen."
            )
        keep_paths = bool(body.get("keep_paths", True))

        def work(job: Job):
            dev, vol = scan.open_volume()
            with dev:
                res = recover_files(vol, entries, target, keep_paths, job.update, job.cancel_event.is_set)
            ok = sum(1 for r in res if r.ok)
            job.message = f"{ok} von {len(res)} Dateien wiederhergestellt"
            return {"target": target, "files": [r.to_dict() for r in res]}

        return self.run_job("recover", work)

    def start_shred(self, body: dict) -> Job:
        if body.get("confirm") != CONFIRM_WORD:
            raise ValueError(f"Bitte zur Bestätigung „{CONFIRM_WORD}“ eingeben.")
        scan, entries = self._selected(body.get("scan"), body.get("ids"))
        passes = max(1, min(3, int(body.get("passes", 1))))
        do_trim = bool(body.get("retrim", True))

        def work(job: Job):
            res = shred_deleted(scan.source, entries, scan.base_offset, passes,
                                job.update, job.cancel_event.is_set)
            for r in res:
                if r["ok"]:
                    e = scan.by_id.pop(r["id"], None)
                    if e is not None:
                        scan.entries.remove(e)
            trim_msg = None
            if do_trim and scan.drive_letter:
                job.update("TRIM wird an die SSD gesendet …", 0.99)
                trim_msg = winutil.retrim(scan.drive_letter)
            ok = sum(1 for r in res if r["ok"])
            job.message = f"{ok} von {len(res)} Dateien endgültig gelöscht"
            return {"files": res, "retrim": trim_msg}

        return self.run_job("shred", work)

    def start_wipe_free(self, body: dict) -> Job:
        if body.get("confirm") != CONFIRM_WORD:
            raise ValueError(f"Bitte zur Bestätigung „{CONFIRM_WORD}“ eingeben.")
        drive = (body.get("drive") or "").strip()
        if not drive:
            raise ValueError("Kein Laufwerk angegeben.")
        if len(drive) <= 3 and drive[0].isalpha():
            letter = drive[0].upper()
            root = f"{letter}:\\"
            source = volume_path(letter)
        else:  # a mounted directory (mainly for testing on other systems)
            letter, root = None, drive
            source = body.get("source")
        scrub_mft = bool(body.get("mft", True))
        do_trim = bool(body.get("retrim", True))

        def work(job: Job):
            mft_records = 0
            note = None
            if scrub_mft and source:
                try:
                    job.update("Freie MFT-Einträge werden gezählt …", 0)
                    with RawDevice(source) as dev:
                        mft_records = NTFSVolume(dev).free_record_count()
                except (DeviceError, NTFSError, OSError) as exc:
                    note = f"MFT-Bereinigung übersprungen: {exc}"
            stats = wipe_free_space(root, mft_records, job.update, job.cancel_event.is_set)
            if do_trim and letter:
                job.update("TRIM wird an die SSD gesendet …", 0.98)
                stats["retrim"] = winutil.retrim(letter)
            stats["note"] = note
            job.message = (
                f"{stats['bytes'] / 2**30:.2f} GiB freier Speicher und "
                f"{stats['mft_records']} MFT-Einträge überschrieben"
            )
            # previous scan results of that drive are now stale
            for sid, sc in list(self.scans.items()):
                if letter and sc.drive_letter == letter:
                    del self.scans[sid]
            return stats

        return self.run_job("wipe_free", work)

    def preview(self, scan_id: str, file_id: int) -> dict:
        scan = self.scans.get(scan_id)
        if scan is None or file_id not in scan.by_id:
            raise ValueError("Datei nicht gefunden.")
        e = scan.by_id[file_id]
        dev, vol = scan.open_volume()
        with dev:
            data = read_preview(vol, e, 8 * 1024 * 1024)
        mime = _sniff(data)
        if mime:
            return {"kind": "image", "mime": mime, "data": base64.b64encode(data).decode()}
        head = data[:4096]
        if head and _looks_text(head):
            return {"kind": "text", "text": head.decode("utf-8", errors="replace")}
        return {"kind": "hex", "hex": _hexdump(data[:1024]), "zero": not data.strip(b"\x00")}


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM" and len(data) > 54:
        return "image/bmp"
    return None


def _looks_text(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    return printable / max(len(text), 1) > 0.95


def _hexdump(data: bytes) -> str:
    lines = []
    for i in range(0, len(data), 16):
        row = data[i : i + 16]
        hexpart = " ".join(f"{b:02x}" for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{i:08x}  {hexpart:<47}  {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
SORT_KEYS = {
    "name": lambda e: e.name.lower(),
    "path": lambda e: (e.path.lower(), e.name.lower()),
    "size": lambda e: e.size,
    "modified": lambda e: e.modified or "",
    "status": lambda e: e.status,
}


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by make_server
    server_version = "SSDTool/" + __version__

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, msg: str, status: int = 400):
        self._json({"error": msg}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _auth_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.app.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Ungültige Anfrage")

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        if not self._host_ok():
            return self._error("Ungültiger Host", 403)
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if not parts or parts[0] != "api":
            if method == "GET":
                return self._static(url.path)
            return self._error("Nicht gefunden", 404)
        if not self._auth_ok():
            return self._error("Nicht autorisiert – bitte die Seite über den Link aus dem Programmfenster öffnen.", 401)
        try:
            self._api(method, parts[1:], parse_qs(url.query))
        except ValueError as exc:
            self._error(str(exc))
        except (DeviceError, NTFSError, WipeError, OSError) as exc:
            self._error(str(exc), 500)

    def _static(self, path: str):
        name = path.lstrip("/") or "index.html"
        base = resource_dir()
        full = os.path.normpath(os.path.join(base, name))
        if not full.startswith(os.path.normpath(base)) or not os.path.isfile(full):
            return self._error("Nicht gefunden", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _api(self, method: str, parts: list[str], query: dict):
        app = self.app
        q = lambda k, d=None: query.get(k, [d])[0]  # noqa: E731
        route = "/".join(parts)

        if method == "GET" and route == "info":
            return self._json({
                "version": __version__,
                "windows": winutil.IS_WINDOWS,
                "admin": winutil.is_admin(),
                "trim": winutil.trim_enabled(),
                "drives": winutil.list_drives(),
                "confirmWord": CONFIRM_WORD,
            })
        if method == "POST" and route == "scan":
            body = self._body()
            job = app.start_scan(body.get("source", ""), bool(body.get("check_trim", True)))
            return self._json(job.to_dict())
        if parts[:1] == ["jobs"] and len(parts) >= 2:
            job = app.jobs.get(parts[1])
            if job is None:
                return self._error("Auftrag nicht gefunden", 404)
            if method == "POST" and parts[2:] == ["cancel"]:
                job.cancel_event.set()
            return self._json(job.to_dict())
        if method == "GET" and parts[:1] == ["scans"] and len(parts) >= 3:
            scan = app.scans.get(parts[1])
            if scan is None:
                return self._error("Scan nicht gefunden", 404)
            if parts[2] == "files":
                return self._json(_list_files(scan, q))
            if parts[2] == "preview" and len(parts) == 4:
                return self._json(app.preview(parts[1], int(parts[3])))
        if method == "POST" and route == "recover":
            return self._json(app.start_recover(self._body()).to_dict())
        if method == "POST" and route == "shred":
            return self._json(app.start_shred(self._body()).to_dict())
        if method == "POST" and route == "wipe-free":
            return self._json(app.start_wipe_free(self._body()).to_dict())
        if method == "POST" and route == "pick-folder":
            return self._json({"path": winutil.pick_folder(self._body().get("initial"))})
        if method == "POST" and route == "open-folder":
            path = self._body().get("path") or ""
            if winutil.IS_WINDOWS and os.path.isdir(path):
                os.startfile(path)  # type: ignore[attr-defined]
            return self._json({"ok": True})
        if method == "POST" and route == "elevate":
            ok = winutil.relaunch_as_admin()
            if ok:
                threading.Timer(1.0, lambda: os._exit(0)).start()
            return self._json({"ok": ok})
        return self._error("Nicht gefunden", 404)


def _list_files(scan: Scan, q) -> dict:
    items = scan.entries
    text = (q("q") or "").strip().lower()
    status = q("status") or ""
    category = q("category") or ""
    if text:
        items = [e for e in items if text in e.name.lower() or text in e.path.lower()]
    if status == "recoverable":
        items = [e for e in items if e.status == "good"]
    elif status:
        items = [e for e in items if e.status == status]
    if category:
        items = [e for e in items if e.category == category]
    sort = q("sort") or "path"
    if sort in SORT_KEYS:
        items = sorted(items, key=SORT_KEYS[sort], reverse=q("dir") == "desc")
    offset = max(0, int(q("offset") or 0))
    limit = max(1, min(1000, int(q("limit") or 200)))
    if q("all_ids") == "1":
        return {"ids": [e.id for e in items], "total": len(items)}
    return {
        "total": len(items),
        "offset": offset,
        "items": [e.to_dict() for e in items[offset : offset + limit]],
        "summary": scan.summary.__dict__ if scan.summary else None,
        "remaining": len(scan.entries),
    }


def make_server(port: int = 0) -> tuple[ThreadingHTTPServer, App]:
    app = App()
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd, app
