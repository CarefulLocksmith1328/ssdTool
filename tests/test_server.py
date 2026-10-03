"""API tests for the local HTTP server (uses an NTFS image, see test_ntfs_image)."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from ssdtool.server import make_server

from .test_ntfs_image import MARKER, Image, needs_ntfs


@pytest.fixture
def server():
    httpd, app = make_server(0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, token=None, body=None, host=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read() or b"{}") if "json" in res.headers.get("Content-Type", "") else res.read()
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def wait(base, token, job):
    for _ in range(200):
        status, j = call(base, "GET", f"/api/jobs/{job['id']}", token)
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise TimeoutError


def test_api_requires_token(server):
    base, app = server
    assert call(base, "GET", "/api/info")[0] == 401
    assert call(base, "GET", "/api/info", "falsch")[0] == 401
    status, info = call(base, "GET", "/api/info", app.token)
    assert status == 200 and info["confirmWord"] == "LÖSCHEN"


def test_foreign_host_is_rejected(server):
    base, app = server
    assert call(base, "GET", "/api/info", app.token, host="evil.example:80")[0] == 403


def test_static_frontend_served_without_traversal(server):
    base, _ = server
    status, body = call(base, "GET", "/")
    assert status == 200 and b"SSD-Datenretter" in body
    assert call(base, "GET", "/../server.py")[0] == 404


def test_destructive_calls_need_confirmation(server):
    base, app = server
    status, err = call(base, "POST", "/api/wipe-free", app.token, {"drive": "C"})
    assert status == 400 and "LÖSCHEN" in err["error"]
    status, err = call(base, "POST", "/api/shred", app.token, {"scan": "x", "ids": [1]})
    assert status == 400


@needs_ntfs
def test_scan_preview_recover_shred_via_api(server, tmp_path):
    base, app = server
    img = Image(tmp_path)
    try:
        img.mount()
        with open(img.p("geheim.txt"), "wb") as fh:
            fh.write(MARKER * 3000)
        with open(img.p("bild.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + os.urandom(20000))
        os.remove(img.p("geheim.txt"))
        os.remove(img.p("bild.png"))
        img.umount()
    finally:
        os.system(f"umount {img.mnt} 2>/dev/null")

    tok = app.token
    _, job = call(base, "POST", "/api/scan", tok, {"source": img.path})
    job = wait(base, tok, job)
    assert job["status"] == "done", job
    scan = job["result"]["scan"]
    _, files = call(base, "GET", f"/api/scans/{scan}/files?sort=name", tok)
    assert [f["name"] for f in files["items"]] == ["bild.png", "geheim.txt"]
    ids = {f["name"]: f["id"] for f in files["items"]}

    _, pv = call(base, "GET", f"/api/scans/{scan}/preview/{ids['bild.png']}", tok)
    assert pv["kind"] == "image" and pv["mime"] == "image/png"
    _, pv = call(base, "GET", f"/api/scans/{scan}/preview/{ids['geheim.txt']}", tok)
    assert pv["kind"] == "text" and pv["text"].startswith(MARKER.decode())

    _, filtered = call(base, "GET", f"/api/scans/{scan}/files?category=image", tok)
    assert filtered["total"] == 1

    out = tmp_path / "rettung"
    _, job = call(base, "POST", "/api/recover", tok, {"scan": scan, "ids": list(ids.values()), "target": str(out)})
    job = wait(base, tok, job)
    assert job["status"] == "done"
    assert (out / "geheim.txt").read_bytes() == MARKER * 3000

    _, job = call(base, "POST", "/api/shred", tok, {"scan": scan, "ids": [ids["geheim.txt"]], "confirm": "LÖSCHEN"})
    job = wait(base, tok, job)
    assert job["status"] == "done" and job["result"]["files"][0]["ok"]
    _, files = call(base, "GET", f"/api/scans/{scan}/files", tok)
    assert [f["name"] for f in files["items"]] == ["bild.png"]
    assert MARKER not in img.raw()
