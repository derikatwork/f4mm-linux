"""The desktop GUI (PySide6). Start it with ``bethmm-gui`` or ``bethmm gui``;
archives named on the command line go through the install dialog once the
window is open."""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        print(f"error: the GUI needs PySide6 ({exc}). Run ./install.sh again, or "
              "'pip install beth-mod-manager[gui]' in the app's environment.", file=sys.stderr)
        return 1
    from bethmm import APP_NAME
    from bethmm.gui.desktop import DESKTOP_ID
    from bethmm.log import setup_logging
    from bethmm.settings import SettingsError, load_settings

    try:
        setup_logging()
    except OSError as exc:
        print(f"warning: logging disabled: {exc}", file=sys.stderr)
    # The X11 window class the menu entry's StartupWMClass names, however the app was started.
    os.environ.setdefault("RESOURCE_NAME", DESKTOP_ID)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
        archives = app.arguments()[1:]  # without the options Qt took for itself
    else:
        archives = sys.argv[1:] if argv is None else list(argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    try:
        settings = load_settings()
    except SettingsError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    from bethmm.gui.main_window import MainWindow

    window = MainWindow(settings)
    window.show()
    if archives:
        QTimer.singleShot(0, lambda: window.install_paths([os.path.abspath(a) for a in archives]))
    return app.exec()
