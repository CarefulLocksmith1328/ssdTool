"""History cleaner against a fake Windows user profile (works on any OS)."""

from __future__ import annotations

import os
import sqlite3

import pytest

from ssdtool import privacy


@pytest.fixture
def profile(tmp_path, monkeypatch):
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    temp = tmp_path / "Temp"
    sysroot = tmp_path / "Windows"
    for d in (roaming, local, temp, sysroot / "Prefetch", sysroot / "Temp"):
        d.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("SystemRoot", str(sysroot))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    return tmp_path


def write(path, data=b"x" * 100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_chrome(profile):
    prof = profile / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    write(prof / "Preferences", b"{}")
    keep = [write(prof / "Bookmarks"), write(prof / "Login Data"), write(prof / "Web Data")]
    history = [write(prof / "History", b"https://geheim.example/" * 50), write(prof / "Visited Links"),
               write(prof / "Sessions" / "Session_1")]
    cookies = [write(prof / "Network" / "Cookies"), write(prof / "Local Storage" / "leveldb" / "000.log")]
    cache = [write(prof / "Cache" / "Cache_Data" / "f_000001", b"c" * 5000), write(profile / "Local" / "Google" / "Chrome" / "User Data" / "ShaderCache" / "x")]
    return keep, history, cookies, cache


def make_firefox(profile):
    prof = profile / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release"
    prof.mkdir(parents=True)
    db = prof / "places.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, visit_count INTEGER, last_visit_date INTEGER, origin_id INTEGER);
        CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER);
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, fk INTEGER, title TEXT);
        CREATE TABLE moz_origins (id INTEGER PRIMARY KEY, host TEXT);
        INSERT INTO moz_origins VALUES (1, 'lesezeichen.example'), (2, 'geheim-firefox.example');
        INSERT INTO moz_places VALUES (1, 'https://lesezeichen.example/', 3, 123, 1);
        INSERT INTO moz_places VALUES (2, 'https://geheim-firefox.example/seite', 9, 456, 2);
        INSERT INTO moz_historyvisits VALUES (1, 1, 1), (2, 2, 2), (3, 2, 3);
        INSERT INTO moz_bookmarks VALUES (1, 1, 'Lesezeichen');
    """)
    con.commit()
    con.close()
    write(prof / "formhistory.sqlite")
    write(prof / "cookies.sqlite")
    write(prof / "storage" / "default" / "https+++site" / "ls" / "data.sqlite")
    ext = write(prof / "storage" / "permanent" / "chrome" / "idb" / "ext.sqlite")
    cache = write(profile / "Local" / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release" / "cache2" / "entries" / "AB")
    return db, ext, cache


def test_categories_and_analysis(profile):
    make_chrome(profile)
    make_firefox(profile)
    write(profile / "Temp" / "a.tmp", b"t" * 1000)
    write(profile / "Windows" / "Prefetch" / "CHROME.EXE-1234.pf")
    res = privacy.analyze()
    cats = {c["id"]: c for c in res["categories"]}
    assert cats["browser_history"]["files"] >= 4
    assert cats["browser_cache"]["bytes"] >= 5000
    assert cats["browser_cookies"]["default"] is False
    assert cats["temp"]["files"] == 1 and cats["temp"]["bytes"] == 1000
    assert cats["prefetch"]["files"] == 1


def test_clean_browsers_keeps_bookmarks_and_passwords(profile):
    keep, history, cookies, cache = make_chrome(profile)
    db, ext, ff_cache = make_firefox(profile)
    results = privacy.clean(["browser_history", "browser_cache", "browser_cookies"], restart_explorer=False)
    assert all(r["ok"] for r in results), results
    for p in history + cookies + cache + [ff_cache]:
        assert not p.exists(), p
    for p in keep + [ext]:
        assert p.exists(), p
    con = sqlite3.connect(db)
    assert con.execute("SELECT url FROM moz_places").fetchall() == [("https://lesezeichen.example/",)]
    assert con.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0] == 0
    assert con.execute("SELECT host FROM moz_origins").fetchall() == [("lesezeichen.example",)]
    assert con.execute("SELECT visit_count, last_visit_date FROM moz_places").fetchone() == (0, None)
    con.close()
    # VACUUM + secure_delete: the deleted URL must not survive in the file
    assert b"geheim-firefox.example" not in db.read_bytes()
    assert not (db.parent / "formhistory.sqlite").exists()
    assert not (db.parent / "cookies.sqlite").exists()


def test_clean_only_selected_categories(profile):
    keep, history, cookies, cache = make_chrome(profile)
    privacy.clean(["browser_history"], restart_explorer=False)
    assert not history[0].exists()
    assert all(p.exists() for p in cookies + cache)


def test_temp_cleanup_never_follows_links(profile, tmp_path):
    outside = write(tmp_path / "Dokumente" / "wichtig.txt", b"nicht loeschen")
    temp = profile / "Temp"
    write(temp / "sub" / "x.tmp")
    write(temp / "y.tmp")
    os.symlink(tmp_path / "Dokumente", temp / "link-auf-dokumente", target_is_directory=True)
    privacy.clean(["temp"], restart_explorer=False)
    assert outside.read_bytes() == b"nicht loeschen"
    assert temp.is_dir()
    assert list(temp.iterdir()) == []


def test_running_program_folder_is_excluded(profile, monkeypatch):
    own = write(profile / "Temp" / "_MEI12345" / "python311.dll")
    write(profile / "Temp" / "other.tmp")
    monkeypatch.setattr(privacy.sys, "_MEIPASS", str(own.parent), raising=False)
    privacy.clean(["temp"], restart_explorer=False)
    assert own.exists()
    assert not (profile / "Temp" / "other.tmp").exists()


def test_recent_keeps_folder_structure(profile):
    recent = profile / "Roaming" / "Microsoft" / "Windows" / "Recent"
    write(recent / "Steuer.pdf.lnk")
    write(recent / "AutomaticDestinations" / "5f7b5f1e01b83767.automaticDestinations-ms")
    res = privacy.clean(["recent"], restart_explorer=False)
    assert res[0]["files"] == 2
    assert (recent / "AutomaticDestinations").is_dir()
    assert not any(p.is_file() for p in recent.rglob("*"))


def test_files_are_overwritten_before_deletion(profile, monkeypatch):
    target = write(profile / "Temp" / "geheim.tmp", b"GEHEIM" * 1000)
    seen = {}
    real_remove = os.remove

    def spy(path):
        if path.endswith("geheim.tmp"):
            seen["content"] = open(path, "rb").read()
        real_remove(path)

    monkeypatch.setattr(privacy.os, "remove", spy)
    privacy.clean(["temp"], restart_explorer=False)
    assert not target.exists()
    assert len(seen["content"]) == 6000 and b"GEHEIM" not in seen["content"]


def test_unknown_selection_is_rejected(profile):
    with pytest.raises(ValueError):
        privacy.clean([], restart_explorer=False)


@pytest.mark.parametrize("bad", ["root", "profile", "notemp"])
def test_misconfigured_temp_is_ignored(profile, monkeypatch, tmp_path, bad):
    home = tmp_path / "home"
    write(home / "Dokumente" / "wichtig.txt")
    monkeypatch.setenv("USERPROFILE", str(home))
    target = {"root": "/", "profile": str(home), "notemp": str(home / "Dokumente")}[bad]
    monkeypatch.setenv("TEMP", target)
    cats = {c.id: c for c in privacy.build_categories()}
    assert all(os.path.abspath(t.value) != os.path.abspath(target) for t in cats["temp"].targets)
    privacy.clean(["temp"], restart_explorer=False)
    assert (home / "Dokumente" / "wichtig.txt").exists()


def test_rdp_style_temp_subfolder_is_accepted(profile, monkeypatch):
    sub = profile / "Temp" / "2"
    write(sub / "x.tmp")
    monkeypatch.setenv("TEMP", str(sub))
    privacy.clean(["temp"], restart_explorer=False)
    assert not (sub / "x.tmp").exists()
