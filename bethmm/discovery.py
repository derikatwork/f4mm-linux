"""Find installed Bethesda games and their Wine prefixes.

Sources, merged:

* **Folder scan** of the Lutris games folder (``Settings.games_dir``, default
  ``~/Games``) plus Lutris's own ``game_path`` from ``system.yml``, down to
  ``Settings.scan_depth`` levels, looking for the executables in
  ``games.EXE_TO_GAME`` (case-insensitive). The folder holding the exe is the
  game root. The scan never follows symlinks (a Wine prefix's
  ``dosdevices/z:`` points at ``/``), skips hidden folders, ``dosdevices``,
  ``drive_c/windows`` and ``drive_c/users``, and does not descend below a
  folder where a game exe was found.
* **Lutris game configs**: every ``*.yml`` in the Lutris game-config dirs
  (``~/.local/share/lutris/games``, ``~/.config/lutris/games`` and the Flatpak
  equivalents under ``~/.var/app/net.lutris.Lutris/{data,config}/lutris/games``).
  Relevant keys: ``game.exe`` (absolute, or relative to the game directory or
  prefix), ``game.prefix``, ``game.working_dir``. A game found this way is
  scanned around: the exe's own folder, the working_dir, and a bounded scan of
  the prefix/directory.
* **Lutris database** ``pga.db`` (``~/.local/share/lutris/pga.db`` and the
  Flatpak path), table ``games``: columns ``slug``, ``name``, ``directory``,
  ``configpath`` (yml file stem), ``runner``, ``installed``. Opened read-only
  (``file:...?mode=ro`` URI); a missing/locked/corrupt db is a warning, not an
  error.
* **Manual overrides** from ``Settings.game_overrides``: ``root`` replaces the
  detected root (a warning is recorded if no known exe is there), ``prefix``
  and ``wine_user`` replace detected values, ``ignore`` hides the game.

One active install per game id. When several roots are found for the same game
the preferred one is: manual override > found via Lutris config/db > folder
scan; ties go to the shortest path. The others are listed in
``alternatives``. Roots are compared after ``os.path.realpath``.

Wine details:

* prefix: the Lutris ``game.prefix`` when it contains the root, otherwise the
  nearest ancestor of the root containing ``drive_c`` and ``system.reg`` or
  ``user.reg``.
* Wine user: the folder in ``<prefix>/drive_c/users`` named ``$USER``, else the
  only folder that isn't ``Public``, else ``steamuser`` if present, else the
  first in sorted order; ``None`` without a users folder.
* plugins.txt folder: ``<prefix>/drive_c/users/<user>/AppData/Local/<folder>``
  (newer Wine) or ``.../Local Settings/Application Data/<folder>`` (older
  Wine), where ``<folder>`` comes from ``GameDef.appdata_folder_candidates``
  for the detected store. Preference: a candidate containing plugins.txt, then
  any existing candidate, then the store's default under whichever of
  ``AppData/Local`` / ``Local Settings/Application Data`` exists (AppData/Local
  if neither).
* store: "gog" if the root has a ``goggame-*.info`` or ``goggame-*.ico`` file,
  "steam" if it has ``steam_api64.dll``/``steam_api.dll``, "epic" if it has
  ``EOSSDK-Win64-Shipping.dll``, else "unknown" (which uses the default folder).

The Data folder is the root's ``Data`` sub-folder matched case-insensitively
(``root / "Data"`` if none exists, with a warning).

Results are cached as JSON in ``paths.games_cache_path()``; ``get_games``
returns the cache unless ``rescan=True`` or no cache exists. Nothing here
writes anywhere except that cache file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bethmm.settings import Settings


@dataclass
class DiscoveredGame:
    game_id: str
    name: str
    root: Path
    exe: Path
    data_dir: Path
    prefix: Path | None = None
    wine_user: str | None = None
    #: Folder that holds (or would hold) plugins.txt.
    appdata_dir: Path | None = None
    plugins_txt: Path | None = None
    #: "gog" | "steam" | "epic" | "unknown"
    store: str = "unknown"
    #: "manual" | "lutris" | "scan"
    source: str = "scan"
    lutris_slug: str | None = None
    lutris_name: str | None = None
    #: Other roots found for the same game (as strings).
    alternatives: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveredGame":
        raise NotImplementedError


@dataclass
class LutrisGame:
    slug: str
    name: str | None = None
    config_path: Path | None = None
    directory: Path | None = None
    exe: Path | None = None
    prefix: Path | None = None
    working_dir: Path | None = None
    runner: str | None = None


@dataclass
class LutrisPaths:
    config_dirs: list[Path] = field(default_factory=list)
    pga_dbs: list[Path] = field(default_factory=list)
    system_ymls: list[Path] = field(default_factory=list)

    @classmethod
    def default(cls, home: Path | None = None) -> "LutrisPaths":
        """Native and Flatpak Lutris locations under *home* (default: ``Path.home()``)."""
        raise NotImplementedError


def scan_for_games(root: Path, max_depth: int) -> list[tuple[str, Path]]:
    """Return ``(game_id, exe_path)`` for every known exe under *root*,
    at most *max_depth* directory levels below it (root itself is level 0).
    Missing/unreadable folders are skipped silently."""
    raise NotImplementedError


def read_lutris_games(lutris: LutrisPaths) -> tuple[list[LutrisGame], list[str]]:
    """Read Lutris configs and pga.db. Returns ``(games, warnings)``; db rows
    and yml files with the same slug/configpath are merged into one entry."""
    raise NotImplementedError


def lutris_game_paths(lutris: LutrisPaths) -> list[Path]:
    """``game_path`` values from Lutris ``system.yml`` files that exist."""
    raise NotImplementedError


def find_wine_prefix(path: Path) -> Path | None:
    raise NotImplementedError


def find_wine_user(prefix: Path, preferred: str | None = None) -> str | None:
    """*preferred* defaults to ``$USER``."""
    raise NotImplementedError


def detect_store(root: Path) -> str:
    raise NotImplementedError


def find_appdata_dir(game_id: str, prefix: Path, wine_user: str, store: str = "unknown") -> Path:
    raise NotImplementedError


def discover_games(
    settings: Settings | None = None,
    *,
    home: Path | None = None,
    lutris: LutrisPaths | None = None,
) -> tuple[list[DiscoveredGame], list[str]]:
    """Run every source and merge. Returns ``(games, warnings)``, games sorted
    by ``GAMES`` order. *home* defaults to ``Path.home()`` and is used for
    ``~`` expansion and Lutris paths (tests point it at a fake home)."""
    raise NotImplementedError


def load_cached_games(path: Path | None = None) -> list[DiscoveredGame] | None:
    """``None`` when there is no (readable) cache."""
    raise NotImplementedError


def save_cached_games(games: list[DiscoveredGame], path: Path | None = None) -> None:
    raise NotImplementedError


def get_games(
    settings: Settings | None = None,
    *,
    rescan: bool = False,
    cache_path: Path | None = None,
    home: Path | None = None,
    lutris: LutrisPaths | None = None,
) -> tuple[list[DiscoveredGame], list[str]]:
    """Cached discovery. The cache holds results *before* manual overrides;
    overrides from *settings* are applied every time this returns, so editing
    settings takes effect without a rescan."""
    raise NotImplementedError
