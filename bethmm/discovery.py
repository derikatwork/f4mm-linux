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
  the prefix/directory (never ``/``, the home folder or one of its ancestors).
* **Lutris database** ``pga.db`` (``~/.local/share/lutris/pga.db`` and the
  Flatpak path), table ``games``: columns ``slug``, ``name``, ``directory``,
  ``configpath`` (yml file stem), ``runner``, ``installed``. Opened read-only
  (``file:...?mode=ro`` URI); a missing/locked/corrupt db is a warning, not an
  error.
* **Manual overrides** from ``Settings.game_overrides``: ``root`` replaces the
  detected root (a warning is recorded if no known exe is there; a file such
  as the game exe or its launcher stands for its folder), ``prefix`` and
  ``wine_user`` replace detected values, ``ignore`` hides the game. Relative
  ``root``/``prefix``/``games_dir`` values are relative to the home folder.

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
  first non-``Public`` folder in sorted order; ``None`` without a users folder
  (or with nothing but ``Public`` in it).
* plugins.txt folder: ``<prefix>/drive_c/users/<user>/AppData/Local/<folder>``
  (newer Wine) or ``.../Local Settings/Application Data/<folder>`` (older
  Wine), where ``<folder>`` comes from ``GameDef.appdata_folder_candidates``
  for the detected store. Preference: a candidate containing plugins.txt, then
  any existing candidate, then the store's default under whichever of
  ``AppData/Local`` / ``Local Settings/Application Data`` exists (AppData/Local
  if neither). Existing folders are matched case-insensitively.
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

import copy
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from bethmm import paths
from bethmm.games import EXE_TO_GAME, GAMES, get_game
from bethmm.settings import Settings, load_settings, write_json_atomic

log = logging.getLogger(__name__)

#: Bumped when the cache layout changes; other versions are ignored (rescan).
CACHE_VERSION = 1

_PATH_FIELDS = frozenset({"root", "exe", "data_dir", "prefix", "appdata_dir", "plugins_txt"})
_REQUIRED_PATH_FIELDS = frozenset({"root", "exe", "data_dir"})
_OPTIONAL_STR_FIELDS = frozenset({"wine_user", "lutris_slug", "lutris_name"})
_PGA_COLUMNS = ("slug", "name", "directory", "configpath", "runner", "installed")
_YML_TIMESTAMP = re.compile(r"-\d+$")
_WINDOWS_C_DRIVE = re.compile(r"^[cC]:[\\/]")
_GOG_MARKER = re.compile(r"^goggame-.*\.(info|ico)$")
_STEAM_DLLS = frozenset({"steam_api64.dll", "steam_api.dll"})
_EPIC_DLL = "eossdk-win64-shipping.dll"
#: Seconds to wait for a pga.db that Lutris is writing before reading it as immutable.
_DB_TIMEOUT = 1.0


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
        data: dict = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, list):
                value = list(value)
            data[f.name] = value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveredGame":
        """Inverse of :meth:`to_dict`; unknown keys are ignored. Raises
        ``KeyError``/``TypeError``/``ValueError`` for unusable data."""
        if not isinstance(data, dict):
            raise TypeError("game entry is not a mapping")
        kwargs: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in _PATH_FIELDS:
                if not isinstance(value, str) and (value is not None or f.name in _REQUIRED_PATH_FIELDS):
                    raise TypeError(f"{f.name} must be a string")
                value = Path(value) if value is not None else None
            elif f.name in ("alternatives", "warnings"):
                if not isinstance(value, list):
                    raise TypeError(f"{f.name} must be a list")
                value = [str(v) for v in value]
            elif not isinstance(value, str) and not (value is None and f.name in _OPTIONAL_STR_FIELDS):
                raise TypeError(f"{f.name} must be a string")
            kwargs[f.name] = value
        if not isinstance(kwargs.get("game_id"), str):
            raise ValueError("game_id missing")
        if "name" not in kwargs and kwargs["game_id"] in GAMES:
            kwargs["name"] = GAMES[kwargs["game_id"]].name
        return cls(**kwargs)


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
        home = home or Path.home()
        share = home / ".local" / "share" / "lutris"
        config = home / ".config" / "lutris"
        flatpak = home / ".var" / "app" / "net.lutris.Lutris"
        fp_data = flatpak / "data" / "lutris"
        fp_config = flatpak / "config" / "lutris"
        return cls(
            config_dirs=[share / "games", config / "games", fp_data / "games", fp_config / "games"],
            pga_dbs=[share / "pga.db", fp_data / "pga.db"],
            system_ymls=[
                config / "system.yml",
                share / "system.yml",
                fp_config / "system.yml",
                fp_data / "system.yml",
            ],
        )


# ---------------------------------------------------------------- folder scan


def scan_for_games(root: Path, max_depth: int) -> list[tuple[str, Path]]:
    """Return ``(game_id, exe_path)`` for every known exe under *root*,
    at most *max_depth* directory levels below it (root itself is level 0).
    Missing/unreadable folders are skipped silently."""
    found: list[tuple[str, Path]] = []
    stack: list[tuple[str, int]] = [(os.fspath(root), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (OSError, ValueError):  # ValueError: a NUL byte in the path
            continue
        hits: list[tuple[str, Path]] = []
        subdirs: list[str] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.name)
                elif entry.name.lower() in EXE_TO_GAME and entry.is_file():
                    hits.append((EXE_TO_GAME[entry.name.lower()], Path(entry.path)))
            except OSError:
                continue
        if hits:
            found.extend(hits)
            continue
        if depth >= max_depth:
            continue
        in_drive_c = os.path.basename(os.path.normpath(current)).lower() == "drive_c"
        for name in reversed(subdirs):
            if not _skip_dir(name, in_drive_c):
                stack.append((os.path.join(current, name), depth + 1))
    return found


def _skip_dir(name: str, in_drive_c: bool) -> bool:
    lower = name.lower()
    if name.startswith(".") or lower == "dosdevices":
        return True
    return in_drive_c and lower in ("windows", "users")


# ---------------------------------------------------------------- Lutris


def read_lutris_games(
    lutris: LutrisPaths, *, home: Path | None = None
) -> tuple[list[LutrisGame], list[str]]:
    """Read Lutris configs and pga.db. Returns ``(games, warnings)``; db rows
    and yml files with the same slug/configpath are merged into one entry.
    *home* (default ``Path.home()``) expands ``~`` in config paths."""
    home = home or Path.home()
    warnings: list[str] = []

    ymls: dict[str, tuple[Path, dict]] = {}
    for config_dir in lutris.config_dirs:
        try:
            files = sorted(p for p in config_dir.iterdir() if p.suffix == ".yml" and os.path.isfile(p))
        except OSError:
            continue
        for path in files:
            if path.stem in ymls:
                continue
            try:
                data = _read_yaml(path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                warnings.append(f"could not read Lutris config {path}: {_one_line(exc)}")
                continue
            if not isinstance(data, dict):
                warnings.append(f"Lutris config {path} is not a YAML mapping; skipped")
                continue
            ymls[path.stem] = (path, data)

    rows: list[dict] = []
    for db in lutris.pga_dbs:
        db_rows, db_warnings = _read_pga_db(db)
        rows.extend(db_rows)
        warnings.extend(db_warnings)

    # Exact configpath links first, so a slug guess never takes a yml another row names.
    used: set[str] = set()
    stems: list[str | None] = []
    for row in rows:
        configpath = _str_or_none(row.get("configpath"))
        stem = configpath if configpath in ymls and configpath not in used else None
        if stem is not None:
            used.add(stem)
        stems.append(stem)

    games: list[LutrisGame] = []
    for row, stem in zip(rows, stems):
        # Lutris keeps rows for uninstalled games and for store-library titles
        # that were never installed; their folders are not games to manage.
        if row.get("installed") in (0, "0", False):
            continue
        slug = _str_or_none(row.get("slug"))
        configpath = _str_or_none(row.get("configpath"))
        if stem is None and slug:
            stem = next((s for s in ymls if s not in used and _slug_from_stem(s) == slug), None)
            if stem is not None:
                used.add(stem)
        game = LutrisGame(
            slug=slug or (_slug_from_stem(stem) if stem else "") or (configpath or ""),
            name=_str_or_none(row.get("name")),
            directory=_abs_or_none(_expand_home(row.get("directory"), home)),
            runner=_str_or_none(row.get("runner")),
        )
        if stem is not None:
            _apply_yml(game, *ymls[stem], home)
        games.append(game)
    for stem, (path, data) in ymls.items():
        if stem not in used:
            slug = _str_or_none(data.get("game_slug")) or _slug_from_stem(stem)
            game = LutrisGame(slug=slug)
            _apply_yml(game, path, data, home)
            games.append(game)

    for game in games:
        _resolve_relative_paths(game)
    return games, warnings


def lutris_game_paths(lutris: LutrisPaths, *, home: Path | None = None) -> list[Path]:
    """``game_path`` values from Lutris ``system.yml`` files that exist."""
    home = home or Path.home()
    out: list[Path] = []
    seen: set[str] = set()
    for path in lutris.system_ymls:
        try:
            data = _read_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            if not isinstance(exc, FileNotFoundError):
                log.debug("could not read %s: %s", path, exc)
            continue
        system = data.get("system") if isinstance(data, dict) else None
        game_path = _abs_or_none(_expand_home(system.get("game_path"), home)) if isinstance(system, dict) else None
        if game_path is None or not os.path.isdir(game_path):
            continue
        real = os.path.realpath(game_path)
        if real not in seen:
            seen.add(real)
            out.append(game_path)
    return out


def _read_yaml(path: Path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _read_pga_db(path: Path) -> tuple[list[dict], list[str]]:
    if not os.path.isfile(path):
        return [], []
    base = "file:" + urllib.parse.quote(os.fspath(path))
    error: Exception | None = None
    # immutable=1 reads a db that is locked or whose folder we can't write to.
    for query in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            return _query_pga(base + query), []
        except sqlite3.OperationalError as exc:
            error = exc
        except sqlite3.Error as exc:
            error = exc
            break
    return [], [f"could not read Lutris database {path}: {error}"]


def _query_pga(uri: str) -> list[dict]:
    conn = sqlite3.connect(uri, uri=True, timeout=_DB_TIMEOUT)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
        if not columns:
            raise sqlite3.DatabaseError("it has no 'games' table")
        wanted = [c for c in _PGA_COLUMNS if c in columns]
        if not wanted:
            return []
        sql = "SELECT " + ", ".join(f'"{c}"' for c in wanted) + " FROM games"
        return [dict(zip(wanted, row)) for row in conn.execute(sql)]
    finally:
        conn.close()


def _apply_yml(game: LutrisGame, path: Path, data: dict, home: Path) -> None:
    game.config_path = path
    section = data.get("game")
    if isinstance(section, dict):
        game.exe = _expand_home(section.get("exe"), home)
        game.prefix = _expand_home(section.get("prefix"), home)
        game.working_dir = _expand_home(section.get("working_dir"), home)
    if game.name is None:
        game.name = _str_or_none(data.get("name"))
    if game.runner is None:
        game.runner = _str_or_none(data.get("runner")) or ("wine" if "wine" in data else None)


def _resolve_relative_paths(game: LutrisGame) -> None:
    """Make yml paths absolute: relative ones hang off the game directory or
    prefix; ``C:\\...`` exe paths map into the prefix's drive_c."""
    directory = game.directory if game.directory is not None and game.directory.is_absolute() else None
    if game.prefix is not None and not game.prefix.is_absolute():
        game.prefix = directory / game.prefix if directory else None
    bases = [b for b in (directory, game.prefix) if b is not None]
    if game.working_dir is not None and not game.working_dir.is_absolute():
        game.working_dir = bases[0] / game.working_dir if bases else None
    if game.exe is None or game.exe.is_absolute():
        return
    raw = str(game.exe)
    if _WINDOWS_C_DRIVE.match(raw):
        rel = raw[3:].replace("\\", "/")
        game.exe = game.prefix / "drive_c" / rel if game.prefix else None
        return
    options = [b / game.exe for b in bases + ([game.working_dir] if game.working_dir else [])]
    game.exe = next((o for o in options if os.path.isfile(o)), options[0] if options else None)


def _slug_from_stem(stem: str) -> str:
    return _YML_TIMESTAMP.sub("", stem) or stem


def _str_or_none(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _expand_home(value, home: Path) -> Path | None:
    """``~`` expansion against *home* (not ``$HOME``); non-strings and
    strings with a NUL byte (unusable as a path) give None."""
    value = _str_or_none(value)
    if value is None or "\0" in value:
        return None
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(os.path.expanduser(value))


def _home_relative(value, home: Path) -> Path | None:
    """:func:`_expand_home`, with relative paths taken relative to *home*
    (never the current directory)."""
    path = _expand_home(value, home)
    return home / path if path is not None and not path.is_absolute() else path


def _abs_or_none(path: Path | None) -> Path | None:
    return path if path is not None and path.is_absolute() else None


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= 200 else text[:197] + "..."


# ---------------------------------------------------------------- Wine details


def find_wine_prefix(path: Path) -> Path | None:
    """Nearest of *path* and its ancestors that looks like a Wine prefix
    (``drive_c`` plus ``system.reg`` or ``user.reg``)."""
    for candidate in (path, *path.parents):
        if os.path.isdir(candidate / "drive_c") and (
            os.path.isfile(candidate / "system.reg") or os.path.isfile(candidate / "user.reg")
        ):
            return candidate
    return None


def find_wine_user(prefix: Path, preferred: str | None = None) -> str | None:
    """*preferred* defaults to ``$USER``."""
    if preferred is None:
        preferred = os.environ.get("USER")
    try:
        with os.scandir(prefix / "drive_c" / "users") as it:
            names = sorted(e.name for e in it if not e.name.startswith(".") and _entry_is_dir(e))
    except (OSError, ValueError):
        return None
    if preferred:
        if preferred in names:
            return preferred
        folded = [n for n in names if n.lower() == preferred.lower()]
        if folded:
            return folded[0]
    users = [n for n in names if n.lower() != "public"]
    if len(users) == 1:
        return users[0]
    if "steamuser" in users:
        return "steamuser"
    return users[0] if users else None


def _user_with_plugins_txt(game_id: str, prefix: Path, store: str) -> str | None:
    """The Wine user whose AppData already holds this game's plugins.txt.

    A prefix can have both ``<you>`` and ``steamuser`` (umu/Proton-based Lutris
    runners use steamuser); the one the game actually wrote to wins, the most
    recently written if several did.
    """
    try:
        with os.scandir(prefix / "drive_c" / "users") as it:
            users = [e.name for e in it if _entry_is_dir(e) and e.name.lower() != "public"]
    except (OSError, ValueError):
        return None
    found: list[tuple[float, str]] = []
    for user in users:
        plugins = _child(find_appdata_dir(game_id, prefix, user, store), "plugins.txt", want_dir=False)
        if plugins is not None:
            try:
                found.append((plugins.stat().st_mtime, user))
            except OSError:
                continue
    return max(found)[1] if found else None


def detect_store(root: Path) -> str:
    try:
        with os.scandir(root) as it:
            names = {e.name.lower() for e in it}
    except (OSError, ValueError):
        return "unknown"
    if any(_GOG_MARKER.match(n) for n in names):
        return "gog"
    if names & _STEAM_DLLS:
        return "steam"
    if _EPIC_DLL in names:
        return "epic"
    return "unknown"


def find_appdata_dir(game_id: str, prefix: Path, wine_user: str, store: str = "unknown") -> Path:
    gdef = get_game(game_id)
    user_dir = prefix / "drive_c" / "users" / wine_user
    appdata = _child(user_dir, "AppData", want_dir=True)
    local = _child(appdata, "Local", want_dir=True) if appdata else None
    settings_dir = _child(user_dir, "Local Settings", want_dir=True)
    old_local = _child(settings_dir, "Application Data", want_dir=True) if settings_dir else None
    bases = [b for b in (local, old_local) if b is not None]
    names = gdef.appdata_folder_candidates(store)

    existing: list[Path] = []
    for name in names:
        for base in bases:
            folder = _child(base, name, want_dir=True)
            if folder is None:
                continue
            if _child(folder, "plugins.txt", want_dir=False) is not None:
                return folder
            existing.append(folder)
    if existing:
        return existing[0]
    base = bases[0] if bases else user_dir / "AppData" / "Local"
    return base / names[0]


def _child(parent: Path, name: str, *, want_dir: bool) -> Path | None:
    """Existing entry of *parent* named *name* in any case (exact case wins)."""
    try:
        with os.scandir(parent) as it:
            matches = sorted(
                e.name
                for e in it
                if e.name.lower() == name.lower() and (_entry_is_dir(e) if want_dir else _entry_is_file(e))
            )
    except (OSError, ValueError):
        return None
    if not matches:
        return None
    return parent / (name if name in matches else matches[0])


def _entry_is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _entry_is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _is_within(child: Path | str, parent: Path | str) -> bool:
    return Path(os.path.realpath(child)).is_relative_to(os.path.realpath(parent))


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


# ---------------------------------------------------------------- discovery


@dataclass
class _Candidate:
    game_id: str
    root: Path
    exe: Path
    lutris: LutrisGame | None = None
    #: The Lutris exe/working_dir pointed straight at this root.
    direct: bool = False


class _Scanner:
    """scan_for_games with memoisation and a guard against huge scan roots."""

    def __init__(self, home: Path) -> None:
        self._home = os.path.realpath(home)
        self._memo: dict[tuple[str, int], list[tuple[str, Path]]] = {}

    def scan(self, root: Path, depth: int) -> list[tuple[str, Path]]:
        key = (os.path.abspath(root), depth)
        if key not in self._memo:
            self._memo[key] = scan_for_games(root, depth)
        return self._memo[key]

    def too_broad(self, path: Path) -> bool:
        """``/``, the home folder and its ancestors are never deep-scanned
        on Lutris's say-so."""
        real = os.path.realpath(path)
        return real == os.sep or _is_within(self._home, real)


def discover_games(
    settings: Settings | None = None,
    *,
    home: Path | None = None,
    lutris: LutrisPaths | None = None,
) -> tuple[list[DiscoveredGame], list[str]]:
    """Run every source and merge. Returns ``(games, warnings)``, games sorted
    by ``GAMES`` order. *home* defaults to ``Path.home()`` and is used for
    ``~`` expansion and Lutris paths (tests point it at a fake home).
    *settings* defaults to the saved settings (``load_settings()``)."""
    settings = settings if settings is not None else load_settings()
    home = home or Path.home()
    games, warnings = _discover_raw(settings, home, lutris)
    games, override_warnings = _apply_overrides(games, settings, home)
    return games, warnings + override_warnings


def _discover_raw(
    settings: Settings, home: Path, lutris: LutrisPaths | None
) -> tuple[list[DiscoveredGame], list[str]]:
    """Discovery without manual overrides (what the cache stores)."""
    lutris = lutris or LutrisPaths.default(home)
    depth = max(0, settings.scan_depth)
    scanner = _Scanner(home)
    warnings: list[str] = []
    candidates: dict[tuple[str, str], _Candidate] = {}

    def add(game_id: str, exe: Path, lutris_game: LutrisGame | None = None, direct: bool = False) -> None:
        root = exe.parent
        key = (game_id, os.path.realpath(root))
        cand = candidates.get(key)
        if cand is None:
            cand = candidates[key] = _Candidate(game_id, root, exe)
        if lutris_game is not None and (cand.lutris is None or (direct and not cand.direct)):
            cand.lutris, cand.direct = lutris_game, direct
            cand.root, cand.exe = root, exe

    scan_roots: list[Path] = []
    games_dir = _home_relative(settings.games_dir, home)
    if games_dir is None:
        if _str_or_none(settings.games_dir):
            warnings.append(f"games folder {settings.games_dir!r} is not a usable path")
        else:
            warnings.append("no games folder is set")
    elif os.path.realpath(games_dir) == os.sep:
        warnings.append("the games folder is set to /; not scanning the whole disk")
    elif not os.path.isdir(games_dir):
        warnings.append(f"games folder {games_dir} does not exist")
    elif not os.access(games_dir, os.R_OK | os.X_OK):
        warnings.append(f"cannot read the games folder {games_dir}: permission denied")
    else:
        scan_roots.append(games_dir)
    for game_path in lutris_game_paths(lutris, home=home):
        if not scanner.too_broad(game_path) and not any(_same_path(game_path, r) for r in scan_roots):
            scan_roots.append(game_path)
    for root in scan_roots:
        for game_id, exe in scanner.scan(root, depth):
            add(game_id, exe)

    lutris_games, lutris_warnings = read_lutris_games(lutris, home=home)
    warnings.extend(lutris_warnings)
    for lg in lutris_games:
        for game_id, exe, direct in _scan_lutris_game(lg, scanner, depth):
            add(game_id, exe, lg, direct)

    by_game: dict[str, list[_Candidate]] = {}
    for cand in candidates.values():
        by_game.setdefault(cand.game_id, []).append(cand)
    games: list[DiscoveredGame] = []
    for game_id, cands in by_game.items():
        cands.sort(key=lambda c: (
            c.lutris is None, not c.direct, find_wine_prefix(c.root) is None, len(str(c.root)), str(c.root)
        ))
        best = cands[0]
        game = _build_game(
            game_id,
            best.root,
            best.exe,
            source="lutris" if best.lutris else "scan",
            lutris_prefix=best.lutris.prefix if best.lutris else None,
        )
        if best.lutris:
            game.lutris_slug = best.lutris.slug or None
            game.lutris_name = best.lutris.name
        game.alternatives = [str(c.root) for c in cands[1:]]
        log.debug("found %s at %s (%s)", game_id, game.root, game.source)
        games.append(game)
    games.sort(key=_game_order)
    return games, warnings


def _on_disk(path: Path) -> Path | None:
    """*path* as spelled on disk, also when the case of some of its folders
    differs (typed by hand, or renamed): Lutris runs such a game all the same."""
    if os.path.isdir(path):
        return path
    current = Path(path.anchor or "/")
    for part in path.parts[1:]:
        found = _child(current, part, want_dir=True)
        if found is None:
            return None
        current = found
    return current


def _scan_lutris_game(lg: LutrisGame, scanner: _Scanner, depth: int) -> list[tuple[str, Path, bool]]:
    hits: list[tuple[str, Path, bool]] = []
    for folder in (lg.exe.parent if lg.exe else None, lg.working_dir):
        folder = _on_disk(folder) if folder is not None else None
        if folder is not None:
            hits.extend((gid, exe, True) for gid, exe in scanner.scan(folder, 0))
    for folder in (lg.prefix, lg.directory):
        if folder is None or not os.path.isdir(folder):
            continue
        if scanner.too_broad(folder):
            log.debug("not scanning %s for Lutris game %s: too broad", folder, lg.slug)
            continue
        hits.extend((gid, exe, False) for gid, exe in scanner.scan(folder, depth))
    return hits


def _build_game(
    game_id: str,
    root: Path,
    exe: Path,
    *,
    source: str,
    lutris_prefix: Path | None = None,
    prefix_hints: tuple[Path | None, ...] = (),
    prefix_override: Path | None = None,
    user_override: str | None = None,
) -> DiscoveredGame:
    """Fill in everything that follows from *root* (and the Wine prefix)."""
    gdef = GAMES[game_id]
    warnings: list[str] = []

    data_dir = _child(root, gdef.data_dir_name, want_dir=True)
    if data_dir is None:
        data_dir = root / gdef.data_dir_name
        warnings.append(f"no {gdef.data_dir_name} folder in {root}")

    if prefix_override is not None:
        prefix: Path | None = prefix_override
        if not os.path.isdir(prefix / "drive_c"):
            warnings.append(f"Wine prefix {prefix} (manual) has no drive_c folder")
    else:
        # The prefix Lutris runs this game with is where Wine keeps its
        # plugins.txt, also when the game is kept elsewhere (on another disk, or
        # in the prefix its installer ran in). Otherwise the prefix the folder
        # is in, or (for a game moved out of one) a hint.
        if lutris_prefix is not None and os.path.isdir(lutris_prefix / "drive_c"):
            prefix: Path | None = lutris_prefix
        else:
            prefix = next((p for p in prefix_hints if p is not None and _is_within(root, p)), None)
            prefix = prefix or find_wine_prefix(root)
            prefix = prefix or next(
                (p for p in prefix_hints if p is not None and os.path.isdir(p / "drive_c")), None
            )
        if prefix is None:
            warnings.append(f"no Wine prefix found above {root}; plugins.txt location unknown")

    store = detect_store(root)
    wine_user = user_override
    if prefix is not None:
        users_dir = prefix / "drive_c" / "users"
        if wine_user is None:
            wine_user = _user_with_plugins_txt(game_id, prefix, store) or find_wine_user(prefix)
            if wine_user is None:
                warnings.append(f"no Wine user folder in {users_dir}")
        elif not os.path.isdir(users_dir / wine_user):
            warnings.append(f"Wine user folder {users_dir / wine_user} (manual) does not exist")

    appdata_dir = plugins_txt = None
    if prefix is not None and wine_user:
        appdata_dir = find_appdata_dir(game_id, prefix, wine_user, store)
        plugins_txt = _child(appdata_dir, "plugins.txt", want_dir=False) or appdata_dir / "plugins.txt"

    return DiscoveredGame(
        game_id=game_id,
        name=gdef.name,
        root=root,
        exe=exe,
        data_dir=data_dir,
        prefix=prefix,
        wine_user=wine_user,
        appdata_dir=appdata_dir,
        plugins_txt=plugins_txt,
        store=store,
        source=source,
        warnings=warnings,
    )


def _apply_overrides(
    games: list[DiscoveredGame], settings: Settings, home: Path
) -> tuple[list[DiscoveredGame], list[str]]:
    result = {g.game_id: copy.deepcopy(g) for g in games}
    warnings: list[str] = []
    for game_id, override in sorted(settings.game_overrides.items()):
        if game_id not in GAMES:
            warnings.append(f"settings have an override for unknown game {game_id!r}; ignored")
            continue
        if override.ignore:
            result.pop(game_id, None)
            continue
        current = result.get(game_id)
        root_override = _home_relative(override.root, home)
        prefix_override = _home_relative(override.prefix, home)
        user_override = _str_or_none(override.wine_user)
        if root_override is None and prefix_override is None and user_override is None:
            continue

        root_warnings: list[str] = []
        if root_override is not None:
            root, exe, root_warnings = _manual_root(game_id, root_override)
            same = current is not None and _same_path(current.root, root)
            alternatives = [] if current is None else [str(current.root)] + current.alternatives
            alternatives = [a for a in alternatives if not _same_path(a, root)]
            source = "manual"
        elif current is None:
            continue
        else:
            root, exe, same = current.root, current.exe, True
            alternatives, source = current.alternatives, current.source

        game = _build_game(
            game_id,
            root,
            exe,
            source=source,
            # The same folder keeps the prefix discovery chose (Lutris's, if it runs the game).
            lutris_prefix=current.prefix if same and current is not None else None,
            prefix_hints=(current.prefix,) if current is not None else (),
            prefix_override=prefix_override,
            user_override=user_override,
        )
        if same and current is not None:
            game.lutris_slug, game.lutris_name = current.lutris_slug, current.lutris_name
        game.alternatives = list(dict.fromkeys(alternatives))
        game.warnings = root_warnings + game.warnings
        result[game_id] = game
    return sorted(result.values(), key=_game_order), warnings


def _manual_root(game_id: str, path: Path) -> tuple[Path, Path, list[str]]:
    """``(root, exe, warnings)`` for a manually set game folder, or a file in
    it (the game exe, or e.g. the launcher Lutris runs)."""
    gdef = GAMES[game_id]
    if os.path.isfile(path):
        if path.name.lower() in {n.lower() for n in gdef.exe_names}:
            return path.parent, path, []
        path = path.parent
    if not os.path.isdir(path):
        return path, path / gdef.exe_names[0], [f"manual game folder {path} does not exist"]
    for name in gdef.exe_names:
        exe = _child(path, name, want_dir=False)
        if exe is not None:
            return path, exe, []
    return path, path / gdef.exe_names[0], [f"manual game folder {path} has no {gdef.exe_names[0]}"]


def _game_order(game: DiscoveredGame) -> int:
    order = list(GAMES)
    return order.index(game.game_id) if game.game_id in order else len(order)


# ---------------------------------------------------------------- cache


def _scan_key(settings: Settings) -> dict:
    """The settings a cached scan depends on."""
    return {"games_dir": str(settings.games_dir), "scan_depth": settings.scan_depth}


def cache_is_current(settings: Settings, cache_path: Path | None = None) -> bool:
    """Whether ``get_games(settings)`` would answer from the cache."""
    return _cache_matches(_read_cache(cache_path), settings)


def _cache_matches(cached, settings: Settings) -> bool:
    return cached is not None and (cached[2] is None or cached[2] == _scan_key(settings))


def load_cached_games(path: Path | None = None) -> list[DiscoveredGame] | None:
    """``None`` when there is no (readable) cache."""
    cached = _read_cache(path)
    return cached[0] if cached is not None else None


def save_cached_games(
    games: list[DiscoveredGame],
    path: Path | None = None,
    *,
    warnings: list[str] | None = None,
    scan_key: dict | None = None,
) -> None:
    data = {
        "version": CACHE_VERSION,
        "games": [g.to_dict() for g in games],
        "warnings": list(warnings or []),
    }
    if scan_key is not None:
        data["scan_key"] = scan_key
    write_json_atomic(path or paths.games_cache_path(), data)


def _read_cache(path: Path | None) -> tuple[list[DiscoveredGame], list[str], dict | None] | None:
    path = path or paths.games_cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    try:
        games = [
            DiscoveredGame.from_dict(entry)
            for entry in data["games"]
            if not isinstance(entry, dict) or entry.get("game_id") in GAMES
        ]
        warnings = [str(w) for w in data.get("warnings") or []]
    except (KeyError, TypeError, ValueError):
        return None
    scan_key = data.get("scan_key")
    return games, warnings, scan_key if isinstance(scan_key, dict) else None


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
    settings takes effect without a rescan. Changing the games folder or the
    scan depth also triggers a rescan. *settings* defaults to the saved
    settings (``load_settings()``)."""
    settings = settings if settings is not None else load_settings()
    home = home or Path.home()
    scan_key = _scan_key(settings)
    cached = None if rescan else _read_cache(cache_path)
    if not _cache_matches(cached, settings):
        cached = None
    if cached is None:
        games, warnings = _discover_raw(settings, home, lutris)
        try:
            save_cached_games(games, cache_path, warnings=warnings, scan_key=scan_key)
        except OSError as exc:
            warnings = warnings + [f"could not save the games cache: {exc}"]
    else:
        games, warnings, _ = cached
    games, override_warnings = _apply_overrides(games, settings, home)
    return games, warnings + override_warnings
