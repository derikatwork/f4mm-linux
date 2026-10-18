"""The applications-menu entry that install.sh writes to
~/.local/share/applications/beth-mod-manager.desktop:

    python -m bethmm.gui.desktop EXECUTABLE ICON > beth-mod-manager.desktop
"""

from __future__ import annotations

import sys

from bethmm import APP_NAME

DESKTOP_ID = "beth-mod-manager"
# install.sh only replaces or removes a menu entry that has this line.
MARKER = "X-BethMM-Installed-By=install.sh"


def _string(value: str) -> str:
    """A Desktop Entry string value (the format's own backslash escapes)."""
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\t", "\\t").replace("\r", "\\r"))


def _exec_arg(value: str) -> str:
    """One argument of Exec: quoted with its reserved characters escaped, then
    escaped again as a string, with literal % doubled."""
    quoted = '"' + "".join("\\" + c if c in '"`$\\' else c for c in value) + '"'
    return _string(quoted).replace("%", "%%")


def desktop_entry(executable: str, icon: str) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "GenericName=Mod Manager",
        "Comment=Install and manage mods for Bethesda games running under Wine and Lutris",
        f"Exec={_exec_arg(executable)} %F",
        f"Icon={_string(icon)}",
        "Terminal=false",
        "Categories=Game;",
        "Keywords=mods;Fallout;Skyrim;Oblivion;Starfield;Bethesda;Lutris;",
        "StartupNotify=true",
        f"StartupWMClass={DESKTOP_ID}",
        MARKER,
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m bethmm.gui.desktop EXECUTABLE ICON", file=sys.stderr)
        return 2
    sys.stdout.write(desktop_entry(*args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
