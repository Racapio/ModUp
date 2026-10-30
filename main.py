"""ModUp — обновление модов Minecraft (Modrinth + GitHub). Запуск: python main.py (или собранный ModUp.exe)."""
import os
import sys

import webview

from modup import updater
from modup.api import Api


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def main():
    updater.cleanup_old()   # ModUp.old.exe от прошлого самообновления
    api = Api()
    window = webview.create_window(
        f"ModUp {updater.VERSION} — обновление модов", resource("ui", "index.html"), js_api=api,
        width=1280, height=820, min_size=(900, 600), background_color="#0e1116",
    )
    api.attach(window)
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
