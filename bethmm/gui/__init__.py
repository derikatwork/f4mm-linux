"""The desktop GUI (PySide6). Start it with ``bethmm-gui`` or ``bethmm gui``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        print(f"error: the GUI needs PySide6 ({exc}). Run ./install.sh again, or "
              "'pip install beth-mod-manager[gui]' in the app's environment.", file=sys.stderr)
        return 1
    from bethmm import APP_NAME
    from bethmm.log import setup_logging
    from bethmm.settings import SettingsError, load_settings

    try:
        setup_logging()
    except OSError as exc:
        print(f"warning: logging disabled: {exc}", file=sys.stderr)
    app = QApplication.instance() or QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("beth-mod-manager")
    try:
        settings = load_settings()
    except SettingsError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    from bethmm.gui.main_window import MainWindow

    window = MainWindow(settings)
    window.show()
    return app.exec()
