"""Check that Wine follows the app's links for a game ("bethmm check-links").

Wine reads symlinked files like any other file, but a Flatpak sandbox or an
unusual runner could still get in the way, so this checks it for real:

1. write a small file with random content into
   ``paths.mods_dir(game_id)/.linktest/`` (inside the mods folder on purpose:
   if the app is killed mid-check, ``deploy.verify`` finds the stray link as
   an orphan pointing into the mods folder and ``repair`` removes it);
2. link it into the game's Data folder as ``bethmm-linktest-<random>.txt``
   (symlink, or hardlink when *link_mode* is "hardlink");
3. run ``<wine> cmd /c type <windows path of the link>`` with
   ``WINEPREFIX=<prefix>``, ``WINEDEBUG=-all`` and a timeout, and check that the
   output contains the random content;
4. always (``finally``) remove the link and the test file, and log every
   file operation.

Finding Wine (first hit wins): the *wine* argument; the runner Lutris uses for
this game -- read ``wine.version`` from the game's Lutris yml (matched by
``game.prefix`` == the game's prefix, or by slug) and look for
``<lutris data>/runners/wine/<version>/bin/wine``, and for Proton-style
runners ``<lutris data>/runners/proton/<version>/files/bin/wine`` (lutris data =
``~/.local/share/lutris`` or the Flatpak
``~/.var/app/net.lutris.Lutris/data/lutris``); any ``runners/wine/*/bin/wine``
(newest by name); ``wine`` on PATH. When none is found the result has
``ok=None`` and a message explaining how to test by hand (install a small mod,
start the game, see if it shows up).

Windows paths: a path inside ``<prefix>/drive_c`` becomes ``C:\\...``; any other
absolute path becomes ``Z:\\...`` (Wine maps Z: to /).

Never touches anything else in the game folder, and refuses to run if the
game's Data folder doesn't exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bethmm.discovery import DiscoveredGame


@dataclass
class LinkCheck:
    #: True = Wine read the file through the link; False = it could not;
    #: None = the check could not run (no Wine, no prefix ...).
    ok: bool | None
    message: str
    wine: str | None = None
    windows_path: str | None = None
    #: Combined stdout/stderr of the Wine command (trimmed), for troubleshooting.
    output: str = ""


def windows_path(path: str | Path, prefix: str | Path | None) -> str:
    raise NotImplementedError


def find_wine(game: DiscoveredGame, *, home: Path | None = None) -> str | None:
    """*home* defaults to ``Path.home()`` (tests point it at a fake home)."""
    raise NotImplementedError


def check_links(
    game: DiscoveredGame,
    *,
    link_mode: str = "symlink",
    wine: str | None = None,
    timeout: float = 120,
    home: Path | None = None,
) -> LinkCheck:
    raise NotImplementedError
