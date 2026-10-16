"""Adversarial tests for bethmm.discovery: corrupt caches, db/yml merge order,
garbage paths, realistic manual overrides and scan cost."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bethmm import discovery
from bethmm.discovery import (
    DiscoveredGame,
    LutrisPaths,
    detect_store,
    discover_games,
    find_wine_user,
    get_games,
    load_cached_games,
    read_lutris_games,
    scan_for_games,
)
from bethmm.settings import GameOverride, Settings
from helpers import make_tree
from test_discovery import (
    LUTRIS_CONFIG_DIRS,
    USER,
    appdata,
    lutris_yml,
    make_gog_game,
    make_pga,
    make_prefix,
    write_yml,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", USER)
    return home


# ---------------------------------------------------------------- cache


@pytest.mark.parametrize("entry", [
    {"game_id": "fallout4", "root": None, "exe": None, "data_dir": None},
    {"game_id": "fallout4", "root": "/g", "exe": "/g/Fallout4.exe", "data_dir": None},
    {"game_id": "fallout4", "root": "/g", "exe": "/g/Fallout4.exe", "data_dir": "/g/Data", "name": None},
    {"game_id": "fallout4", "root": "/g", "exe": "/g/Fallout4.exe", "data_dir": "/g/Data", "wine_user": 7},
    {"game_id": "fallout4", "root": "/g", "exe": "/g/Fallout4.exe", "data_dir": "/g/Data", "store": ["gog"]},
])
def test_cache_entry_with_unusable_fields_is_unreadable(home, tmp_path, entry):
    cache = tmp_path / "games.json"
    cache.write_text(json.dumps({"version": 1, "games": [entry], "warnings": []}))
    with pytest.raises((KeyError, TypeError, ValueError)):
        DiscoveredGame.from_dict(entry)
    assert load_cached_games(cache) is None

    root = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    settings = Settings(game_overrides={"fallout4": GameOverride(wine_user=USER)})
    (game,), _ = get_games(settings, cache_path=cache, home=home)
    assert game.root == root, "a corrupt cache means a rescan, not a crash"


def test_from_dict_keeps_optional_nones(tmp_path):
    data = {"game_id": "oblivion", "root": "/g", "exe": "/g/Oblivion.exe", "data_dir": "/g/Data",
            "prefix": None, "wine_user": None, "lutris_slug": None, "extra": {"ignored": True}}
    game = DiscoveredGame.from_dict(data)
    assert game.prefix is None and game.wine_user is None and game.name == "Oblivion"


# ---------------------------------------------------------------- Lutris merge


def test_configpath_link_beats_slug_guess(home):
    """A duplicate row without a configpath must not take the yml that another
    row names exactly."""
    cfg = home / LUTRIS_CONFIG_DIRS[0]
    write_yml(cfg / "fallout-4-1700000001.yml", lutris_yml("/new/drive_c/Fallout4Launcher.exe", Path("/new")))
    lutris = LutrisPaths.default(home)
    make_pga(lutris.pga_dbs[0], [
        {"id": 1, "slug": "fallout-4", "name": "Fallout 4 (old)", "directory": "/old",
         "configpath": None, "installed": 1},
        {"id": 2, "slug": "fallout-4", "name": "Fallout 4 GOTY", "directory": "/new",
         "configpath": "fallout-4-1700000001", "installed": 1},
    ])

    games, warnings = read_lutris_games(lutris, home=home)

    assert warnings == []
    by_name = {g.name: g for g in games}
    assert by_name["Fallout 4 GOTY"].config_path == cfg / "fallout-4-1700000001.yml"
    assert by_name["Fallout 4 GOTY"].exe == Path("/new/drive_c/Fallout4Launcher.exe")
    assert by_name["Fallout 4 (old)"].config_path is None and by_name["Fallout 4 (old)"].exe is None


# ---------------------------------------------------------------- garbage paths


def test_nul_bytes_in_lutris_files_are_ignored(home, tmp_path):
    root = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    cfg = home / LUTRIS_CONFIG_DIRS[0]
    write_yml(cfg / "nul-1.yml", 'game:\n  exe: "/a\\0b/Fallout4.exe"\n  prefix: "/p\\0q"\n  working_dir: "/w\\0"\n')
    write_yml(home / ".config/lutris/system.yml", 'system:\n  game_path: "/g\\0h"\n')
    make_pga(home / ".local/share/lutris/pga.db", [{"id": 1, "slug": "x", "directory": "/d\0e"}])

    games, _ = discover_games(Settings(), home=home)

    assert [g.root for g in games] == [root]


@pytest.mark.parametrize("override", [
    GameOverride(root="/x\0y"),
    GameOverride(prefix="/x\0y"),
    GameOverride(wine_user="a\0b"),
])
def test_nul_bytes_in_overrides_are_warnings_not_crashes(home, override):
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    (game,), _ = discover_games(Settings(game_overrides={"fallout4": override}), home=home)
    assert game.game_id == "fallout4"


def test_nul_byte_games_dir_is_a_warning(home):
    games, warnings = discover_games(Settings(games_dir="/x\0y"), home=home)
    assert games == [] and any("games folder" in w for w in warnings)


def test_public_helpers_skip_unusable_paths(tmp_path):
    bad = tmp_path / "a\0b"
    assert scan_for_games(bad, 3) == []
    assert detect_store(bad) == "unknown"
    assert find_wine_user(bad) is None


# ---------------------------------------------------------------- overrides


def test_manual_root_pointing_at_launcher_uses_its_folder(home, tmp_path):
    """Lutris shows Fallout4Launcher.exe as the game's executable, so that is
    what users paste."""
    prefix = make_prefix(tmp_path / "pfx")
    root = make_gog_game(prefix, "Fallout 4 GOTY", "Fallout4.exe")
    make_tree(root, ["Fallout4Launcher.exe"])
    settings = Settings(game_overrides={"fallout4": GameOverride(root=str(root / "Fallout4Launcher.exe"))})

    (game,), _ = discover_games(settings, home=home)

    assert game.root == root and game.exe == root / "Fallout4.exe"
    assert game.data_dir == root / "Data" and game.prefix == prefix
    assert game.warnings == []


def test_manual_root_pointing_at_unrelated_file_warns(home, tmp_path):
    folder = make_tree(tmp_path / "stuff", ["notes.txt"])
    settings = Settings(game_overrides={"fallout4": GameOverride(root=str(folder / "notes.txt"))})
    (game,), _ = discover_games(settings, home=home)
    assert game.root == folder
    assert any("has no Fallout4.exe" in w for w in game.warnings)


def test_relative_override_paths_are_relative_to_home_not_cwd(home, tmp_path, monkeypatch):
    root = make_tree(home / "Odd Place" / "FNV", ["FalloutNV.exe", "Data/"])
    prefix = make_prefix(home / "prefixes" / "fnv")
    elsewhere = make_tree(tmp_path / "cwd", ["Odd Place/FNV/FalloutNV.exe", "prefixes/fnv/drive_c/"])
    monkeypatch.chdir(elsewhere)
    settings = Settings(
        game_overrides={"falloutnv": GameOverride(root="Odd Place/FNV", prefix="prefixes/fnv")},
    )

    (game,), _ = discover_games(settings, home=home)

    assert game.root == root and game.prefix == prefix
    assert game.appdata_dir == appdata(prefix) / "FalloutNV"
    assert game.warnings == []


# ---------------------------------------------------------------- scan cost


def test_scan_visits_only_what_it_must(home, tmp_path, monkeypatch):
    """Several Lutris prefixes in ~/Games: count every folder listed and make
    sure none is a Wine system folder, a dosdevices link, a hidden folder or
    a folder below a found game exe."""
    decoy = make_tree(tmp_path / "decoy", ["Fallout3.exe"])
    games_dir = home / "Games"
    for slug in ("fallout-4", "skyrim-se", "witcher-3", "battlenet"):
        prefix = make_prefix(games_dir / slug, decoy=decoy)
        make_tree(prefix, [f"drive_c/windows/system32/d{i}/" for i in range(20)])
        make_tree(prefix, [f"drive_c/users/{USER}/Documents/My Games/g{i}/" for i in range(10)])
        make_tree(prefix, ["drive_c/.hidden/Fallout3.exe"])
        (prefix / "drive_c" / "linked").symlink_to(decoy)
    (games_dir / "shortcut").symlink_to(decoy)
    fo4 = make_gog_game(games_dir / "fallout-4", "Fallout 4 GOTY", "Fallout4.exe")
    make_tree(fo4, [f"Data/meshes/m{i}/x/" for i in range(50)])
    make_gog_game(games_dir / "skyrim-se", "Skyrim AE", "SkyrimSE.exe")
    make_tree(games_dir / "witcher-3/drive_c/GOG Games/Witcher 3", [f"content/c{i}/a/b/c/" for i in range(30)])
    for slug in ("fallout-4", "skyrim-se"):
        write_yml(home / LUTRIS_CONFIG_DIRS[0] / f"{slug}-1.yml", lutris_yml("/nowhere/x.exe", games_dir / slug))

    listed: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path="."):
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(discovery.os, "scandir", counting_scandir)
    games, _ = discover_games(Settings(), home=home)

    assert [g.game_id for g in games] == ["fallout4", "skyrimse"]
    forbidden = {"windows", "dosdevices", "meshes", "Documents", ".hidden", "z:", "c:"}
    assert not [p for p in listed if forbidden & set(Path(p).parts)]
    assert not [p for p in listed if Path(p).is_relative_to(decoy)]
    # Each prefix holds only a handful of real folders outside the skipped ones.
    assert len(set(listed)) < 120, len(set(listed))


def test_gog_galaxy_layout_within_default_depth(home):
    """GOG Galaxy's own install path is 6 levels below ~/Games."""
    prefix = make_prefix(home / "Games" / "gog-galaxy")
    root = make_tree(prefix / "drive_c/Program Files (x86)/GOG Galaxy/Games/Fallout 4 GOTY",
                     ["Fallout4.exe", "goggame-1998527297.info", "Data/"])
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == root and game.store == "gog" and game.prefix == prefix
