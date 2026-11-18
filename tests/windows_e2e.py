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

from ssdtool import diskerase, privacy, winutil  # noqa: E402
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
        print("Windows-E2E (Wiederherstellung/Wipe) erfolgreich.")
        return 0
    finally:
        diskpart(f'select vdisk file="{vhd}"\ndetach vdisk\n', check=False)  # may already be detached
        shutil.rmtree(work, ignore_errors=True)


def test_disk_erase() -> None:
    """Erase a complete (virtual) disk and verify nothing of it survives."""
    disks = diskerase.list_disks()
    print("Datenträger:", [(d["number"], d["model"], d["bus"], d["letters"], d["blocked"]) for d in disks])
    system = [d for d in disks if os.environ.get("SystemDrive", "C:")[0] in d["letters"]]
    assert system and all(d["blocked"] for d in system), "Systemdatenträger muss gesperrt sein"
    try:
        diskerase.erase_disk(system[0]["number"], system[0]["serial"])
        raise AssertionError("Systemdatenträger durfte nicht gelöscht werden")
    except diskerase.EraseError as exc:
        print("Systemdatenträger korrekt abgelehnt:", exc)

    work = tempfile.mkdtemp(prefix="ssdtool-erase-")
    vhd = os.path.join(work, "erase.vhd")
    letter = free_letter()
    diskpart(f'create vdisk file="{vhd}" maximum=64 type=fixed\nselect vdisk file="{vhd}"\nattach vdisk\n'
             f'create partition primary\nformat fs=ntfs quick label=ALT\nassign letter={letter}\n')
    try:
        for _ in range(50):
            if os.path.exists(f"{letter}:\\"):
                break
            time.sleep(0.2)
        with open(f"{letter}:\\geheim.bin", "wb") as fh:
            fh.write(MARKER * 100_000)
        disk = next(d for d in diskerase.list_disks() if letter in d["letters"])
        print("Zu löschender Datenträger:", disk)
        assert disk["blocked"] is None, disk["blocked"]
        report = diskerase.erase_disk(disk["number"], disk["serial"], "auto", True, "Neu",
                                      lambda m, f: print(f"  {f:4.0%} {m}") if f in (0.0, 1.0) else None)
        print("Bericht:", {k: v for k, v in report.items() if k != "format_output"})
        assert report["verify"]["checked"] > 0 and report["verify"]["unchanged"] == 0, report["verify"]
        # the new, empty volume must exist and be usable
        after = next(d for d in diskerase.list_disks() if d["number"] == disk["number"])
        assert after["letters"], "neu formatiertes Laufwerk hat keinen Buchstaben"
        new_root = after["letters"][0] + ":\\"
        assert not os.path.exists(new_root + "geheim.bin")
        with open(new_root + "test.txt", "w") as fh:
            fh.write("ok")
        fd = os.open(diskerase.physical_path(disk["number"]), os.O_RDONLY | os.O_BINARY)
        try:
            raw = b""
            while len(raw) < disk["size"]:
                chunk = os.read(fd, 4 * 1024 * 1024)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
        assert MARKER not in raw, "alte Daten noch auf dem Datenträger"
        print("Datenträger-Löschung erfolgreich.")
    finally:
        diskpart(f'select vdisk file="{vhd}"\ndetach vdisk\n', check=False)
        shutil.rmtree(work, ignore_errors=True)


def test_privacy() -> None:
    """History cleaner: registry MRU lists, junctions in Temp, analysis of the real profile."""
    import winreg

    res = privacy.analyze()
    print("Analyse:", [(c["id"], c["files"], c["bytes"], c["entries"]) for c in res["categories"]])
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, privacy.EXPLORER + r"\RunMRU")
    winreg.SetValueEx(key, "a", 0, winreg.REG_SZ, "geheimes-programm.exe\\1")
    winreg.SetValueEx(key, "MRUList", 0, winreg.REG_SZ, "a")
    key.Close()
    rd = winreg.CreateKey(winreg.HKEY_CURRENT_USER, privacy.EXPLORER + r"\RecentDocs\.pdf")
    winreg.SetValueEx(rd, "0", 0, winreg.REG_BINARY, "Steuer.pdf".encode("utf-16-le"))
    rd.Close()

    work = tempfile.mkdtemp(prefix="ssdtool-privacy-")
    outside = os.path.join(work, "Dokumente")
    os.makedirs(outside)
    with open(os.path.join(outside, "wichtig.txt"), "w") as fh:
        fh.write("bleibt")
    fake_temp = os.path.join(work, "Temp")
    os.makedirs(os.path.join(fake_temp, "sub"))
    with open(os.path.join(fake_temp, "sub", "spur.tmp"), "w") as fh:
        fh.write("weg")
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(fake_temp, "junction"), outside], check=True)
    old_temp = os.environ["TEMP"]
    os.environ["TEMP"] = fake_temp
    try:
        results = privacy.clean(["explorer", "recent", "temp"], overwrite=True, close_browsers=False,
                                restart_explorer=False)
    finally:
        os.environ["TEMP"] = old_temp
    print("Bereinigung:", results)
    assert open(os.path.join(outside, "wichtig.txt")).read() == "bleibt", "Junction wurde verfolgt!"
    assert os.listdir(fake_temp) == [], os.listdir(fake_temp)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, privacy.EXPLORER + r"\RunMRU")
        values = winreg.QueryInfoKey(key)[1]
        key.Close()
    except OSError:
        values = 0
    assert values == 0, "RunMRU nicht geleert"
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, privacy.EXPLORER + r"\RecentDocs").Close()
        raise AssertionError("RecentDocs nicht gelöscht")
    except OSError:
        pass
    shutil.rmtree(work, ignore_errors=True)
    print("Verlauf-Bereinigung erfolgreich.")


if __name__ == "__main__":
    rc = main()
    test_privacy()
    test_disk_erase()
    print("Alle Windows-E2E-Tests erfolgreich.")
    sys.exit(rc)
