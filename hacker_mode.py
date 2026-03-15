"""
hacker_mode.py — HackerMode entry point
Run this file to start the app.
"""

import sys
import os
import threading

from PyQt6.QtWidgets import QApplication

from core import (
    register_startup, ensure_azkar_image,
    install_hook, remove_hook, pump_messages,
)
from ui import LockScreen


def main():
    register_startup()
    ensure_azkar_image()

    install_hook()
    threading.Thread(target=pump_messages, daemon=True).start()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    window = LockScreen()
    window.show()

    app.exec()
    remove_hook()
    os._exit(0)


if __name__ == "__main__":
    main()
