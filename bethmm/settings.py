"""User settings, stored as JSON in ``~/.config/beth-mod-manager/settings.json``."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bethmm import paths

LINK_MODES = ("symlink", "hardlink")


@dataclass
class GameOverride:
    """Per-game manual settings. ``None`` means "use what discovery found"."""

    root: str | None = None
    prefix: str | None = None
    wine_user: str | None = None
    link_mode: str = "symlink"
    #: Hide this game even if discovery finds it.
    ignore: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "GameOverride":
        known = {k: data[k] for k in ("root", "prefix", "wine_user", "link_mode", "ignore") if k in data}
        override = cls(**known)
        if override.link_mode not in LINK_MODES:
            override.link_mode = "symlink"
        return override


@dataclass
class Settings:
    #: Lutris's default install folder; scanned for game executables.
    games_dir: str = "~/Games"
    #: How many folder levels below each scan root to search for game executables.
    scan_depth: int = 6
    #: Explicit path to the 7z binary; ``None`` = search PATH.
    sevenzip: str | None = None
    #: Show what would happen without touching game folders.
    dry_run: bool = False
    game_overrides: dict[str, GameOverride] = field(default_factory=dict)

    def games_dir_path(self) -> Path:
        return Path(os.path.expanduser(self.games_dir))

    def override(self, game_id: str) -> GameOverride:
        return self.game_overrides.get(game_id) or GameOverride()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["game_overrides"] = {k: asdict(v) for k, v in self.game_overrides.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        settings = cls()
        if isinstance(data.get("games_dir"), str):
            settings.games_dir = data["games_dir"]
        if isinstance(data.get("scan_depth"), int) and data["scan_depth"] > 0:
            settings.scan_depth = data["scan_depth"]
        if isinstance(data.get("sevenzip"), str) or data.get("sevenzip") is None:
            settings.sevenzip = data.get("sevenzip")
        settings.dry_run = bool(data.get("dry_run", False))
        overrides = data.get("game_overrides") or {}
        if isinstance(overrides, dict):
            settings.game_overrides = {
                k: GameOverride.from_dict(v) for k, v in overrides.items() if isinstance(v, dict)
            }
        return settings


def load_settings(path: Path | None = None) -> Settings:
    path = path or paths.settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        raise SettingsError(f"could not read settings from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"settings file {path} does not contain a JSON object")
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_path()
    write_json_atomic(path, settings.to_dict())


def write_json_atomic(path: Path, data) -> None:
    """Write JSON via a temp file + rename so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class SettingsError(Exception):
    pass
