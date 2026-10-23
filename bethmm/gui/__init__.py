"""The desktop GUI (PySide6). Start it with ``bethmm-gui`` or ``bethmm gui``;
archives named on the command line go through the install dialog once the
window is open."""

from __future__ import annotations

import os
import subprocess
import sys

# Run in a child process: a leaking PySide crashes the interpreter it ran in.
_PROBE = """
import sys
from PySide6.QtCore import QObject, Signal
class Probe(QObject):
    ping = Signal()
probe = Probe()
before = sys.getrefcount(True)
for _ in range(50):
    probe.ping.emit()
print(before - sys.getrefcount(True), flush=True)
import os
os._exit(0)  # a leaking PySide would crash the interpreter's shutdown
"""


def pyside_problem() -> str | None:
    """Why the installed PySide6 can't run the app, or None. PySide6 6.12.0
    drops a reference to True on every signal emitted from Python; before
    Python 3.12 (where True is immortal) that crashes the app sooner or later."""
    if sys.version_info >= (3, 12):
        return None
    try:
        # -P, not -I: the same PySide as here, wherever it comes from (PYTHONPATH, user site).
        out = subprocess.run([sys.executable, "-P", "-c", _PROBE], capture_output=True, text=True, timeout=120)
        lost = int(out.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None  # can't tell: don't stand in the way
    if lost > 25:
        import PySide6
        return (f"PySide6 {PySide6.__version__} has a bug that crashes this app on Python "
                f"{sys.version_info[0]}.{sys.version_info[1]}. Run ./install.sh again to install a working version.")
    return None


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
    # The X11 window class the menu entry's StartupWMClass names, however the
    # app was started (taken back once the window exists: programs the app
    # starts mustn't inherit it).
    set_resource_name = "RESOURCE_NAME" not in os.environ
    os.environ.setdefault("RESOURCE_NAME", DESKTOP_ID)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
        archives = app.arguments()[1:]  # without the options Qt took for itself
    else:
        archives = sys.argv[1:] if argv is None else list(argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    problem = pyside_problem()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        QMessageBox.critical(None, APP_NAME, problem)
        return 1
    try:
        settings = load_settings()
    except SettingsError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    from bethmm.gui.main_window import MainWindow

    window = MainWindow(settings)
    window.show()
    if set_resource_name:
        os.environ.pop("RESOURCE_NAME", None)
    if archives:
        QTimer.singleShot(0, lambda: window.install_paths([os.path.abspath(a) for a in archives]))
    return app.exec()
