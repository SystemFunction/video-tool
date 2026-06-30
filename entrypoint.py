#!/usr/bin/env python3
"""Docker entrypoint: startet Video Tool v0.0.1 als Flet-Web-App.

Das Originalskript ist eine Desktop-GUI (ft.run(main)). In einem
headless Container gibt es keinen Window-Server, also starten wir
denselben main() ueber Flet's Web-View. Erreichbar im Browser unter
http://<host>:8550/ .
"""

from __future__ import annotations

import os

import flet as ft

# Originalskript unveraendert importieren. _install_deps() wird
# beim Import ausgefuehrt, ist aber dank Image-Vorinstallation ein
# No-Op.
from video_tool import main

PORT = int(os.environ.get("FLET_SERVER_PORT", "8550"))
HOST = os.environ.get("FLET_SERVER_HOST", "0.0.0.0")

if __name__ == "__main__":
    ft.run(
        main,
        view=ft.AppView.WEB_BROWSER,
        host=HOST,
        port=PORT,
    )
