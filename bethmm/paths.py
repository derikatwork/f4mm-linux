"""Where BETH Mod Manager keeps its files.

Config lives in ``~/.config/beth-mod-manager`` and data in
``~/.local/share/beth-mod-manager``, honouring ``XDG_CONFIG_HOME`` /
``XDG_DATA_HOME``. ``BETHMM_CONFIG_DIR`` and ``BETHMM_DATA_DIR`` override both
(used by tests and for portable setups).
"""

from __future__ import annotations

import os
from pathlib import Path

from bethmm import APP_SLUG


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    override = os.environ.get("BETHMM_CONFIG_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_SLUG


def data_dir() -> Path:
    override = os.environ.get("BETHMM_DATA_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_SLUG


def settings_path() -> Path:
    return config_dir() / "settings.json"


def games_cache_path() -> Path:
    return data_dir() / "games.json"


def manifest_path() -> Path:
    return data_dir() / "manifest.db"


def mods_dir(game_id: str | None = None) -> Path:
    base = data_dir() / "mods"
    return base / game_id if game_id else base


def backups_dir(game_id: str | None = None) -> Path:
    base = data_dir() / "backups"
    return base / game_id if game_id else base


def plugins_backups_dir(game_id: str) -> Path:
    """Copies of a game's plugins.txt, made before each change the app makes to it."""
    return data_dir() / "plugins-backups" / game_id


def tmp_dir() -> Path:
    return data_dir() / "tmp"


def logs_dir() -> Path:
    return data_dir() / "logs"


def log_path() -> Path:
    return logs_dir() / "bethmm.log"
