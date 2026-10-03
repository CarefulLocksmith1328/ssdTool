"""Start the local server and open the frontend in the browser.

    python -m ssdtool [--port 8765] [--no-browser] [--no-elevate]
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from . import __version__, winutil
from .server import make_server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ssdtool", description="SSD-Datenretter")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    parser.add_argument("--no-elevate", action="store_true", help="Nicht nach Administratorrechten fragen")
    args = parser.parse_args(argv)

    if winutil.IS_WINDOWS and not winutil.is_admin() and not args.no_elevate:
        print("Für den Zugriff auf Laufwerke werden Administratorrechte benötigt – starte neu …")
        if winutil.relaunch_as_admin():
            return 0
        print("Ohne Administratorrechte können keine Laufwerke gelesen werden (nur Abbild-Dateien).")

    try:
        httpd, app = make_server(args.port)
    except OSError:
        httpd, app = make_server(0)  # port in use -> pick a free one
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#token={app.token}"
    print(f"SSD-Datenretter {__version__}")
    print(f"Oberfläche: {url}")
    print("Dieses Fenster offen lassen. Beenden mit Strg+C.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
