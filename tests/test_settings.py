import json
from pathlib import Path

import pytest

from bethmm import paths
from bethmm.settings import GameOverride, Settings, SettingsError, load_settings, save_settings


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path / "missing.json")
    assert s.games_dir == "~/Games"
    assert s.scan_depth == 6
    assert s.game_overrides == {}


def test_round_trip(tmp_path):
    s = Settings(games_dir="/mnt/games", scan_depth=4, sevenzip="/usr/bin/7zz", dry_run=True)
    s.game_overrides["fallout4"] = GameOverride(root="/g/fo4", link_mode="hardlink")
    save_settings(s, tmp_path / "s.json")
    loaded = load_settings(tmp_path / "s.json")
    assert loaded == s
    assert not list(tmp_path.glob(".s.json.*")), "temp file left behind"


def test_bad_values_fall_back_to_defaults(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({
        "scan_depth": -3,
        "games_dir": 5,
        "game_overrides": {"fallout4": {"link_mode": "copy", "bogus": 1}, "x": "nope"},
    }))
    s = load_settings(tmp_path / "s.json")
    assert s.scan_depth == 6 and s.games_dir == "~/Games"
    assert s.game_overrides == {"fallout4": GameOverride(link_mode="symlink")}


def test_unreadable_settings_raise(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "s.json")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "list.json")


def test_app_dirs_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("BETHMM_CONFIG_DIR")
    monkeypatch.delenv("BETHMM_DATA_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert paths.config_dir() == tmp_path / "cfg" / "beth-mod-manager"
    assert paths.mods_dir("fallout4") == tmp_path / "share" / "beth-mod-manager" / "mods" / "fallout4"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/ignored")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.data_dir() == tmp_path / "home" / ".local" / "share" / "beth-mod-manager"


def test_tests_never_use_real_home():
    assert "appdirs" in str(paths.config_dir())
    assert not str(paths.data_dir()).startswith(str(Path.home() / ".local"))
