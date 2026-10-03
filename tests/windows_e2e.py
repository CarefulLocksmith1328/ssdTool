"""End-to-end test on a real Windows volume (run as Administrator).

Creates a small NTFS VHD with diskpart, mounts it as a drive letter, deletes
files on it and then exercises scan, recovery, targeted shredding and the
free-space wipe against ``\\\\.\\X:``.  Used by the GitHub Actions workflow;
can also be run manually:  ``python tests\\windows_e2e.py``
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssdtool import winutil  # noqa: E402
from ssdtool.device import RawDevice, volume_path  # noqa: E402
from ssdtool.ntfs import NTFSVolume  # noqa: E402
from ssdtool.recover import recover_files  # noqa: E402
from ssdtool.scanner import scan_volume  # noqa: E402
from ssdtool.server import make_server  # noqa: E402
from ssdtool.wipe import shred_deleted, wipe_free_space  # noqa: E402

MARKER = b"WINDOWS-E2E-GEHEIM-4711-"


def diskpart(script: str, check: bool = True) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        res = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True)
        print(res.stdout)
        if check and res.returncode != 0:
            raise RuntimeError(f"diskpart fehlgeschlagen: {res.stdout} {res.stderr}")
        return res.stdout
    finally:
        os.remove(path)


def free_letter() -> str:
    for c in "RSTUVWXYZ":
        if not os.path.exists(f"{c}:\\"):
            return c
    raise RuntimeError("Kein freier Laufwerksbuchstabe")


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def scan(letter: str):
    with RawDevice(volume_path(letter)) as dev:
        return scan_volume(NTFSVolume(dev))


def main() -> int:
    assert winutil.IS_WINDOWS, "nur unter Windows"
    assert winutil.is_admin(), "bitte als Administrator ausführen"
    work = tempfile.mkdtemp(prefix="ssdtool-e2e-")
    vhd = os.path.join(work, "test.vhd")
    letter = free_letter()
    root = f"{letter}:\\"
    print("TRIM aktiviert:", winutil.trim_enabled())
    diskpart(
        f'create vdisk file="{vhd}" maximum=96 type=fixed\n'
        f'select vdisk file="{vhd}"\nattach vdisk\ncreate partition primary\n'
        f'format fs=ntfs quick label=E2E\nassign letter={letter}\n'
    )
    try:
        for _ in range(50):
            if os.path.exists(root):
                break
            time.sleep(0.2)
        os.makedirs(os.path.join(root, "Daten", "Unterordner"))
        files = {
            "klein.txt": b"Hallo Windows",
            "bild.jpg": b"\xff\xd8\xff\xe0" + os.urandom(400_000),
            "geheim.txt": MARKER * 20_000,
        }
        for name, data in files.items():
            with open(os.path.join(root, "Daten", "Unterordner", name), "wb") as fh:
                fh.write(data)
        with open(os.path.join(root, "bleibt.txt"), "wb") as fh:
            fh.write(b"bleibt")
        for name in files:
            os.remove(os.path.join(root, "Daten", "Unterordner", name))
        os.rmdir(os.path.join(root, "Daten", "Unterordner"))
        subprocess.run(["fsutil", "volume", "flush", f"{letter}:"], capture_output=True)
        time.sleep(1)

        # ---- scan
        entries, summary = scan(letter)
        found = {e.name: e for e in entries}
        print("Gefunden:", {n: (e.status, e.path, e.size) for n, e in found.items()})
        print("Zusammenfassung:", summary)
        for name in files:
            assert name in found, f"{name} nicht gefunden"
        assert found["geheim.txt"].path == "\\Daten\\Unterordner", found["geheim.txt"].path

        # ---- recover (to the system drive, i.e. a different volume)
        out = os.path.join(work, "gerettet")
        with RawDevice(volume_path(letter)) as dev:
            res = recover_files(NTFSVolume(dev), [found[n] for n in files], out)
        for r in res:
            assert r.ok, r
            got = open(r.target, "rb").read()
            if found[r.name].status == "good":
                assert md5(got) == md5(files[r.name]), f"Inhalt von {r.name} falsch"
            print("wiederhergestellt:", r.name, found[r.name].status, r.target)

        # ---- API smoke test
        httpd, app = make_server(0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/info",
                                     headers={"X-Token": app.token})
        info = json.loads(urllib.request.urlopen(req).read())
        print("API-Laufwerke:", [(d["letter"], d["filesystem"], d["media"]) for d in info["drives"]])
        assert any(d["letter"] == letter for d in info["drives"])
        httpd.shutdown()

        # ---- targeted shred (needs a volume lock)
        results = shred_deleted(volume_path(letter), [found["geheim.txt"], found["klein.txt"]])
        print("Shred:", results)
        assert all(r["ok"] for r in results), results
        time.sleep(1)
        entries, _ = scan(letter)
        names = {e.name for e in entries}
        assert "geheim.txt" not in names and "klein.txt" not in names, names
        assert "bild.jpg" in names
        assert open(os.path.join(root, "bleibt.txt"), "rb").read() == b"bleibt"
        chk = subprocess.run(["chkdsk", f"{letter}:", "/scan"], capture_output=True, text=True,
                             encoding="oem", errors="replace")
        print(chk.stdout[-1500:])
        assert chk.returncode == 0, "chkdsk meldet Fehler"

        # ---- free space wipe
        with RawDevice(volume_path(letter)) as dev:
            free_records = NTFSVolume(dev).free_record_count()
        stats = wipe_free_space(root, free_records)
        print("Wipe:", stats)
        assert not [x for x in os.listdir(root) if x.startswith("~ssdtool")], os.listdir(root)
        entries, _ = scan(letter)
        assert "bild.jpg" not in {e.name for e in entries}, "bild.jpg noch auffindbar"
        with RawDevice(volume_path(letter)) as dev:
            vol = NTFSVolume(dev)
            raw = vol.dev.read(0, vol.boot.volume_size - (vol.boot.volume_size % 4096))
        assert MARKER not in raw, "Geheimer Inhalt noch auf dem Volume"
        chk = subprocess.run(["chkdsk", f"{letter}:", "/scan"], capture_output=True, text=True,
                             encoding="oem", errors="replace")
        assert chk.returncode == 0, chk.stdout[-1500:]
        print("Windows-E2E erfolgreich.")
        return 0
    finally:
        diskpart(f'select vdisk file="{vhd}"\ndetach vdisk\n', check=False)  # may already be detached
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
