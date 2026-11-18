"""Platform independent parts of the whole-disk erase (run against image files)."""

from __future__ import annotations

import os

import pytest

from ssdtool import diskerase


def make_disk(tmp_path, size=16 * 1024 * 1024):
    path = tmp_path / "disk.img"
    with open(path, "wb") as fh:
        for i in range(size // 4096):
            fh.write((b"ALTE-DATEN-%08d-" % i).ljust(4096, b"#"))
    return str(path), size


def test_overwrite_changes_every_sample(tmp_path):
    path, size = make_disk(tmp_path)
    samples = diskerase.take_samples(path, size, 512, seed=1)
    assert len(samples) > 100 and all(h is not None for _, h in samples)
    before = os.path.getsize(path)
    written = diskerase.overwrite_device(path, size, 512)
    assert written == size and os.path.getsize(path) == before
    result = diskerase.verify_samples(path, samples, 512)
    assert result["checked"] == len(samples) and result["unchanged"] == 0
    assert b"ALTE-DATEN" not in open(path, "rb").read()


def test_verify_detects_untouched_data(tmp_path):
    path, size = make_disk(tmp_path)
    samples = diskerase.take_samples(path, size, 512, seed=2)
    result = diskerase.verify_samples(path, samples, 512)
    assert result["changed"] == 0 and result["unchanged"] == result["checked"] > 0


def test_zero_sectors_are_not_counted_as_proof(tmp_path):
    path = tmp_path / "empty.img"
    path.write_bytes(b"\0" * 8 * 1024 * 1024)
    samples = diskerase.take_samples(str(path), 8 * 1024 * 1024, 512, seed=3)
    result = diskerase.verify_samples(str(path), samples, 512)
    assert result["checked"] == 0 and result["was_empty"] == len(samples)


def test_overwrite_can_be_cancelled(tmp_path):
    path, size = make_disk(tmp_path)
    with pytest.raises(InterruptedError):
        diskerase.overwrite_device(path, size, 512, cancelled=lambda: True)


@pytest.mark.parametrize("disk, blocked", [
    ({"system": True, "letters": [], "size": 1}, True),
    ({"boot": True, "letters": [], "size": 1}, True),
    ({"letters": ["C"], "size": 1}, True),  # SystemDrive
    ({"letters": ["E"], "pagefile": True, "size": 1}, True),
    ({"letters": ["E"], "readonly": True, "size": 1}, True),
    ({"letters": ["E"], "size": 0}, True),
    ({"letters": ["E"], "size": 1}, False),
    ({"letters": [], "size": 1}, False),
])
def test_blocked_reason(disk, blocked, monkeypatch):
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(diskerase, "_own_drive_letters", lambda: {"Z"})
    assert (diskerase.blocked_reason(disk) is not None) == blocked


def test_own_drive_is_blocked(monkeypatch):
    monkeypatch.setattr(diskerase, "_own_drive_letters", lambda: {"F"})
    assert "Programm" in diskerase.blocked_reason({"letters": ["F"], "size": 1})


def test_erase_refused_outside_windows(monkeypatch):
    monkeypatch.setattr(diskerase, "IS_WINDOWS", False)
    with pytest.raises(diskerase.EraseError):
        diskerase.erase_disk(1, "x")


def test_erase_refuses_changed_serial(monkeypatch):
    monkeypatch.setattr(diskerase, "IS_WINDOWS", True)
    monkeypatch.setattr(diskerase, "get_disk", lambda n: {"number": n, "serial": "ANDERE", "letters": [], "size": 1})
    with pytest.raises(diskerase.EraseError, match="geändert"):
        diskerase.erase_disk(1, "ERWARTET")


def test_erase_refuses_system_disk(monkeypatch):
    monkeypatch.setattr(diskerase, "IS_WINDOWS", True)
    monkeypatch.setattr(diskerase, "get_disk", lambda n: {"number": n, "serial": "S", "boot": True, "letters": ["C"], "size": 1})
    called = []
    monkeypatch.setattr(diskerase, "diskpart", lambda *a, **k: called.append(a))
    with pytest.raises(diskerase.EraseError, match="Windows"):
        diskerase.erase_disk(0, "S")
    assert not called


def test_erase_refuses_changed_size(monkeypatch):
    monkeypatch.setattr(diskerase, "IS_WINDOWS", True)
    monkeypatch.setattr(diskerase, "get_disk", lambda n: {"number": n, "serial": "", "letters": [], "size": 64})
    with pytest.raises(diskerase.EraseError, match="geändert"):
        diskerase.erase_disk(3, "", expected_size=128)
