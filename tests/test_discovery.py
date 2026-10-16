import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from bethmm import paths
from bethmm.discovery import (
    DiscoveredGame,
    LutrisPaths,
    detect_store,
    discover_games,
    find_appdata_dir,
    find_wine_prefix,
    find_wine_user,
    get_games,
    load_cached_games,
    lutris_game_paths,
    read_lutris_games,
    save_cached_games,
    scan_for_games,
)
from bethmm.settings import GameOverride, Settings
from helpers import make_tree

USER = "derik"
FLATPAK = ".var/app/net.lutris.Lutris"
LUTRIS_CONFIG_DIRS = [
    ".local/share/lutris/games",
    ".config/lutris/games",
    f"{FLATPAK}/data/lutris/games",
    f"{FLATPAK}/config/lutris/games",
]
PGA_COLUMNS = (
    "id INTEGER PRIMARY KEY",
    "name TEXT",
    "slug TEXT",
    "installer_slug TEXT",
    "platform TEXT",
    "runner TEXT",
    "executable TEXT",
    "directory TEXT",
    "updated DATETIME",
    "lastplayed INTEGER",
    "installed INTEGER",
    "installed_at INTEGER",
    "year INTEGER",
    "configpath TEXT",
    "has_custom_banner INTEGER",
    "playtime REAL",
    "hidden INTEGER",
    "service TEXT",
    "service_id TEXT",
)


# ---------------------------------------------------------------- builders


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", USER)
    return home


def make_prefix(prefix: Path, user: str = USER, *, old_wine: bool = False, decoy: Path | None = None) -> Path:
    """A Wine prefix as Lutris creates it."""
    local = "Local Settings/Application Data/" if old_wine else "AppData/Local/"
    make_tree(prefix, [
        "system.reg",
        "user.reg",
        "userdef.reg",
        "drive_c/windows/system32/",
        "drive_c/users/Public/Documents/",
        f"drive_c/users/{user}/{local}",
    ])
    (prefix / "dosdevices").mkdir()
    (prefix / "dosdevices" / "c:").symlink_to("../drive_c")
    if decoy is not None:
        (prefix / "dosdevices" / "z:").symlink_to(decoy)
    return prefix


def make_gog_game(prefix: Path, title: str, exe: str, *, gog_id: str = "1998527297", data: str = "Data") -> Path:
    root = prefix / "drive_c" / "GOG Games" / title
    make_tree(root, [exe, f"goggame-{gog_id}.info", f"goggame-{gog_id}.ico", f"{data}/", "__redist/"])
    return root


def appdata(prefix: Path, user: str = USER) -> Path:
    return prefix / "drive_c" / "users" / user / "AppData" / "Local"


def write_yml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def lutris_yml(exe: Path | str, prefix: Path | None = None, working_dir: Path | None = None) -> dict:
    game = {"exe": str(exe)}
    if prefix is not None:
        game["prefix"] = str(prefix)
    if working_dir is not None:
        game["working_dir"] = str(working_dir)
    return {"game": game, "system": {}, "wine": {"version": "lutris-GE-Proton8-26-x86_64"}}


def make_pga(path: Path, rows: list[dict], columns=PGA_COLUMNS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(f"CREATE TABLE games ({', '.join(columns)})")
        for row in rows:
            cols = list(row)
            conn.execute(
                f"INSERT INTO games ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
    conn.close()
    return path


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


# ---------------------------------------------------------------- folder scan


def test_scan_finds_gog_fallout4_and_skyrim_se(home):
    fo4_prefix = make_prefix(home / "Games" / "fallout-4-game-of-the-year-edition")
    fo4 = make_gog_game(fo4_prefix, "Fallout 4 GOTY", "Fallout4.exe")
    make_tree(fo4, ["Fallout4Launcher.exe", "Data/Fallout4.esm"])
    make_tree(appdata(fo4_prefix), ["Fallout4/plugins.txt"])
    sse_prefix = make_prefix(home / "Games" / "the-elder-scrolls-v-skyrim-special-edition")
    sse = make_gog_game(sse_prefix, "Skyrim Anniversary Edition", "SkyrimSE.exe", gog_id="1711230643", data="data")

    games, warnings = discover_games(Settings(), home=home)

    assert warnings == []
    assert [g.game_id for g in games] == ["fallout4", "skyrimse"]
    g4, gs = games
    assert g4.name == "Fallout 4"
    assert g4.root == fo4
    assert g4.exe == fo4 / "Fallout4.exe"
    assert g4.data_dir == fo4 / "Data"
    assert g4.prefix == fo4_prefix
    assert g4.wine_user == USER
    assert g4.store == "gog"
    assert g4.source == "scan"
    assert g4.lutris_slug is None
    assert g4.appdata_dir == appdata(fo4_prefix) / "Fallout4"
    assert g4.plugins_txt == appdata(fo4_prefix) / "Fallout4" / "plugins.txt"
    assert g4.alternatives == [] and g4.warnings == []

    assert gs.name == "Skyrim Special Edition"
    assert gs.root == sse and gs.exe == sse / "SkyrimSE.exe"
    assert gs.data_dir == sse / "data", "the existing lowercase folder is used"
    assert gs.prefix == sse_prefix and gs.store == "gog"
    assert gs.appdata_dir == appdata(sse_prefix) / "Skyrim Special Edition GOG"
    assert gs.plugins_txt == gs.appdata_dir / "plugins.txt"
    assert not gs.appdata_dir.exists(), "discovery never creates folders"
    assert gs.warnings == []


def test_old_wine_local_settings_layout(home):
    prefix = make_prefix(home / "Games" / "fallout-new-vegas", old_wine=True)
    make_gog_game(prefix, "Fallout New Vegas", "FalloutNV.exe")
    (game,), _ = discover_games(Settings(), home=home)
    assert game.game_id == "falloutnv"
    assert game.appdata_dir == prefix / "drive_c/users" / USER / "Local Settings/Application Data/FalloutNV"


def test_missing_data_folder_and_prefix_are_warnings(home):
    root = home / "Games" / "loose" / "Oblivion"
    make_tree(root, ["Oblivion.exe"])
    (game,), _ = discover_games(Settings(), home=home)
    assert game.data_dir == root / "Data"
    assert game.prefix is None and game.wine_user is None
    assert game.appdata_dir is None and game.plugins_txt is None
    assert game.store == "unknown"
    assert any("no Data folder" in w for w in game.warnings)
    assert any("no Wine prefix" in w for w in game.warnings)


def test_games_sorted_in_registry_order(home):
    prefix = make_prefix(home / "Games" / "gog-galaxy")
    make_gog_game(prefix, "Oblivion", "Oblivion.exe")
    make_gog_game(prefix, "Fallout 3 GOTY", "Fallout3.exe")
    make_gog_game(prefix, "Fallout 4 GOTY", "Fallout4.exe")
    games, _ = discover_games(Settings(), home=home)
    assert [g.game_id for g in games] == ["fallout4", "fallout3", "oblivion"]


def test_scan_depth_is_bounded(tmp_path):
    root = make_tree(tmp_path / "scan", ["a/b/c/Fallout4.exe"])
    exe = root / "a/b/c/Fallout4.exe"
    assert scan_for_games(root, 2) == []
    assert scan_for_games(root, 3) == [("fallout4", exe)]
    assert scan_for_games(root / "a/b/c", 0) == [("fallout4", exe)], "the root itself is level 0"
    assert scan_for_games(tmp_path / "missing", 5) == []


def test_settings_scan_depth_respected(home):
    # ~/Games/<slug>/drive_c/GOG Games/<Title>/Fallout4.exe is 4 levels deep.
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    assert discover_games(Settings(scan_depth=3), home=home)[0] == []
    assert [g.game_id for g in discover_games(Settings(scan_depth=4), home=home)[0]] == ["fallout4"]


def test_scan_skips_hidden_and_wine_system_folders(tmp_path):
    root = tmp_path / "Games"
    make_tree(root, [
        ".cache/Fallout4.exe",
        "prefix/drive_c/windows/Fallout3.exe",
        "prefix/drive_c/users/derik/Oblivion.exe",
        "prefix/dosdevices/Starfield.exe",
        "prefix/drive_c/Program Files/Bethesda/Oblivion/Oblivion.exe",
        "windows/TESV.exe",  # only drive_c/windows is skipped
    ])
    assert sorted(scan_for_games(root, 6)) == [
        ("oblivion", root / "prefix/drive_c/Program Files/Bethesda/Oblivion/Oblivion.exe"),
        ("skyrim", root / "windows/TESV.exe"),
    ]


def test_scan_matches_case_insensitively_and_stops_at_game_folder(tmp_path):
    root = make_tree(tmp_path / "Games", ["fo4/FALLOUT4.EXE", "fo4/Data/Tools/Fallout3.exe"])
    assert scan_for_games(root, 6) == [("fallout4", root / "fo4/FALLOUT4.EXE")]


def test_dosdevices_and_other_symlinks_are_not_followed(home, tmp_path):
    decoy = make_tree(tmp_path / "decoy", ["Fallout3.exe", "deep/Fallout3.exe"])
    prefix = make_prefix(home / "Games" / "fallout-4", decoy=decoy)
    root = make_gog_game(prefix, "Fallout 4", "Fallout4.exe")
    (home / "Games" / "shortcut").symlink_to(decoy)
    (prefix / "drive_c" / "linked").symlink_to(decoy)
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fallout-4-1700000000.yml", lutris_yml(root / "Fallout4.exe", prefix))

    assert all(gid != "fallout3" for gid, _ in scan_for_games(home / "Games", 20))
    assert all(gid != "fallout3" for gid, _ in scan_for_games(prefix, 20))
    games, _ = discover_games(Settings(scan_depth=20), home=home)
    assert [g.game_id for g in games] == ["fallout4"]


# ---------------------------------------------------------------- Lutris


@pytest.mark.parametrize("config_dir", LUTRIS_CONFIG_DIRS)
def test_game_outside_games_dir_found_via_lutris_yml(home, tmp_path, config_dir):
    prefix = make_prefix(tmp_path / "ssd" / "wine" / "fo4")
    root = make_gog_game(prefix, "Fallout 4 GOTY", "Fallout4.exe")
    make_tree(root, ["Fallout4Launcher.exe"])
    write_yml(
        home / config_dir / "fallout-4-game-of-the-year-edition-1700000000.yml",
        lutris_yml(root / "Fallout4Launcher.exe", prefix, root),
    )

    games, warnings = discover_games(Settings(), home=home)

    (game,) = games
    assert game.root == root
    assert game.exe == root / "Fallout4.exe", "the launcher's folder is searched for the real exe"
    assert game.source == "lutris"
    assert game.lutris_slug == "fallout-4-game-of-the-year-edition"
    assert game.prefix == prefix and game.wine_user == USER and game.store == "gog"
    assert any("does not exist" in w for w in warnings), "~/Games is missing"


def test_launcher_exe_only_yml(home, tmp_path):
    prefix = make_prefix(tmp_path / "wine" / "skyrim")
    root = make_gog_game(prefix, "Skyrim", "SkyrimSE.exe")
    make_tree(root, ["SkyrimSELauncher.exe"])
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "skyrim-1.yml", {"game": {"exe": str(root / "SkyrimSELauncher.exe")}})
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == root and game.source == "lutris"
    assert game.prefix == prefix, "found by walking up from the root"


@pytest.mark.parametrize("db", [".local/share/lutris/pga.db", f"{FLATPAK}/data/lutris/pga.db"])
def test_game_found_via_pga_db_directory(home, tmp_path, db):
    prefix = make_prefix(tmp_path / "elsewhere" / "skyrim")
    root = make_gog_game(prefix, "Skyrim Anniversary Edition", "SkyrimSE.exe")
    make_pga(home / db, [{
        "id": 1,
        "name": "The Elder Scrolls V: Skyrim Special Edition",
        "slug": "the-elder-scrolls-v-skyrim-special-edition",
        "runner": "wine",
        "directory": str(prefix),
        "installed": 1,
        "configpath": "the-elder-scrolls-v-skyrim-special-edition-1700000000",
    }])

    (game,), _ = discover_games(Settings(), home=home)

    assert game.root == root and game.source == "lutris"
    assert game.lutris_slug == "the-elder-scrolls-v-skyrim-special-edition"
    assert game.lutris_name == "The Elder Scrolls V: Skyrim Special Edition"
    assert game.appdata_dir == appdata(prefix) / "Skyrim Special Edition GOG"


def test_db_rows_and_ymls_are_merged(home, tmp_path):
    lutris = LutrisPaths.default(home)
    cfg = home / LUTRIS_CONFIG_DIRS[0]
    write_yml(cfg / "fallout-4-1700000000.yml", lutris_yml("/games/fo4/Fallout4Launcher.exe", Path("/games/fo4pfx")))
    write_yml(cfg / "oblivion-1600000000.yml", lutris_yml("/games/obl/Oblivion.exe"))
    write_yml(cfg / "starfield-1650000000.yml", lutris_yml("/games/sf/Starfield.exe"))
    make_pga(lutris.pga_dbs[0], [
        {"id": 1, "name": "Fallout 4", "slug": "fallout-4", "directory": "/games/fo4pfx",
         "configpath": "fallout-4-1700000000", "runner": "wine", "installed": 1},
        # no configpath: matched by slug
        {"id": 2, "name": "Oblivion GOTY", "slug": "oblivion", "directory": None,
         "configpath": None, "runner": "wine", "installed": 1},
        # library entry that was never installed: not a game to manage
        {"id": 3, "name": "Fallout 3", "slug": "fallout-3", "directory": "", "installed": 0},
        # uninstalled game whose folder was left behind
        {"id": 4, "name": "Fallout NV", "slug": "fallout-new-vegas", "directory": "/games/fnv", "installed": 0},
    ])

    games, warnings = read_lutris_games(lutris, home=home)

    assert warnings == []
    by_slug = {g.slug: g for g in games}
    assert sorted(by_slug) == ["fallout-4", "oblivion", "starfield"]
    fo4 = by_slug["fallout-4"]
    assert fo4.name == "Fallout 4"
    assert fo4.config_path == cfg / "fallout-4-1700000000.yml"
    assert fo4.directory == Path("/games/fo4pfx") and fo4.prefix == Path("/games/fo4pfx")
    assert fo4.exe == Path("/games/fo4/Fallout4Launcher.exe")
    assert fo4.runner == "wine"
    assert by_slug["oblivion"].name == "Oblivion GOTY"
    assert by_slug["oblivion"].config_path == cfg / "oblivion-1600000000.yml"
    assert by_slug["starfield"].name is None and by_slug["starfield"].runner == "wine"


def test_relative_and_windows_style_exe_paths(home, tmp_path):
    prefix = make_prefix(tmp_path / "wine" / "fo4")
    root = make_gog_game(prefix, "Fallout 4 GOTY", "Fallout4.exe")
    make_tree(root, ["Fallout4Launcher.exe"])
    cfg = home / LUTRIS_CONFIG_DIRS[1]
    write_yml(cfg / "rel-1.yml", lutris_yml("drive_c/GOG Games/Fallout 4 GOTY/Fallout4Launcher.exe", prefix))
    write_yml(cfg / "win-2.yml", lutris_yml("C:\\GOG Games\\Fallout 4 GOTY\\Fallout4Launcher.exe", prefix))
    write_yml(cfg / "tilde-3.yml", lutris_yml("~/nowhere/Fallout4Launcher.exe"))
    write_yml(cfg / "orphan-4.yml", lutris_yml("Fallout4Launcher.exe"))

    games, warnings = read_lutris_games(LutrisPaths.default(home), home=home)

    assert warnings == []
    by_slug = {g.slug: g for g in games}
    assert by_slug["rel"].exe == root / "Fallout4Launcher.exe"
    assert by_slug["win"].exe == root / "Fallout4Launcher.exe"
    assert by_slug["tilde"].exe == home / "nowhere" / "Fallout4Launcher.exe"
    assert by_slug["orphan"].exe is None, "relative exe with nothing to resolve it against"
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == root and game.source == "lutris"


def test_broken_lutris_files_are_warnings(home):
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    lutris = LutrisPaths.default(home)
    cfg = home / LUTRIS_CONFIG_DIRS[0]
    write_yml(cfg / "broken-1.yml", "game: [unclosed\n  exe: x\n")
    write_yml(cfg / "list-2.yml", "- just\n- a list\n")
    write_yml(cfg / "nogame-3.yml", "system: {}\n")
    write_yml(cfg / "badexe-4.yml", {"game": {"exe": 42, "prefix": ["x"]}})
    (cfg / "binary-5.yml").write_bytes(b"\xff\xfe\x00\x81garbage")
    lutris.pga_dbs[0].parent.mkdir(parents=True, exist_ok=True)
    lutris.pga_dbs[0].write_bytes(b"this is not a database at all " * 100)

    games, warnings = read_lutris_games(lutris, home=home)

    assert {g.slug for g in games} == {"nogame", "badexe"}
    assert all(g.exe is None and g.prefix is None for g in games)
    assert any("broken-1.yml" in w for w in warnings)
    assert any("list-2.yml" in w for w in warnings)
    assert any("binary-5.yml" in w for w in warnings)
    assert any("pga.db" in w for w in warnings)
    assert not any("nogame" in w or "badexe" in w for w in warnings)

    found, all_warnings = discover_games(Settings(), home=home)
    assert [g.game_id for g in found] == ["fallout4"]
    assert len(all_warnings) == 4


def test_pga_db_with_old_schema_or_no_games_table(home, tmp_path):
    prefix = make_prefix(tmp_path / "old" / "obl")
    root = make_gog_game(prefix, "Oblivion", "Oblivion.exe")
    lutris = LutrisPaths(pga_dbs=[tmp_path / "old.db", tmp_path / "empty.db", tmp_path / "missing.db"])
    make_pga(lutris.pga_dbs[0], [{"slug": "oblivion", "directory": str(prefix)}],
             columns=("slug TEXT", "directory TEXT"))
    sqlite3.connect(lutris.pga_dbs[1]).close()

    games, warnings = read_lutris_games(lutris, home=home)
    assert [(g.slug, g.directory) for g in games] == [("oblivion", prefix)]
    assert len(warnings) == 1 and "empty.db" in warnings[0] and "games" in warnings[0]

    (game,), _ = discover_games(Settings(), home=home, lutris=lutris)
    assert game.root == root and game.source == "lutris"


def test_locked_pga_db_is_read_as_immutable(home, tmp_path, monkeypatch):
    monkeypatch.setattr("bethmm.discovery._DB_TIMEOUT", 0.05)
    db = make_pga(tmp_path / "pga.db", [{"slug": "fallout-4", "directory": "/games/fo4"}],
                  columns=("slug TEXT", "directory TEXT"))
    lock = sqlite3.connect(db, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        games, warnings = read_lutris_games(LutrisPaths(pga_dbs=[db]), home=home)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert warnings == []
    assert [(g.slug, g.directory) for g in games] == [("fallout-4", Path("/games/fo4"))]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "pga.db"], "no journal files created"


def test_lutris_prefix_used_when_it_contains_root(home, tmp_path):
    # A prefix folder without the usual registry files: only Lutris knows it.
    prefix = tmp_path / "bare-prefix"
    root = make_tree(prefix / "drive_c/GOG Games/Fallout 3", ["Fallout3.exe", "Data/"])
    make_tree(prefix, [f"drive_c/users/{USER}/AppData/Local/"])
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fo3-1.yml", lutris_yml(root / "Fallout3.exe", prefix))
    (game,), _ = discover_games(Settings(), home=home)
    assert game.prefix == prefix
    assert game.appdata_dir == appdata(prefix) / "Fallout3"


def test_lutris_prefix_ignored_when_it_does_not_contain_root(home, tmp_path):
    real_prefix = make_prefix(tmp_path / "real")
    root = make_gog_game(real_prefix, "Fallout 4", "Fallout4.exe")
    other = make_prefix(tmp_path / "other")
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fo4-1.yml", lutris_yml(root / "Fallout4.exe", other))
    (game,), _ = discover_games(Settings(), home=home)
    assert game.prefix == real_prefix


def test_lutris_home_prefix_is_not_deep_scanned(home):
    make_tree(home, ["stuff/Fallout 4/Fallout4.exe"])
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "weird-1.yml", {"game": {"exe": "/usr/bin/true", "prefix": str(home)}})
    assert discover_games(Settings(), home=home)[0] == []
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "direct-2.yml", lutris_yml(home / "stuff/Fallout 4/Fallout4.exe", home))
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == home / "stuff/Fallout 4"


def test_lutris_system_yml_game_path_adds_scan_root(home, tmp_path):
    big = tmp_path / "bigdisk" / "Games"
    prefix = make_prefix(big / "starfield")
    root = make_gog_game(prefix, "Starfield", "Starfield.exe")
    write_yml(home / ".config/lutris/system.yml", {"system": {"game_path": str(big)}})
    write_yml(home / f"{FLATPAK}/config/lutris/system.yml", {"system": {"game_path": "~/FlatGames"}})
    make_tree(home, ["FlatGames/"])
    write_yml(home / ".local/share/lutris/system.yml", "system: [broken\n")

    lutris = LutrisPaths.default(home)
    assert lutris_game_paths(lutris, home=home) == [big, home / "FlatGames"]
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == root and game.source == "scan"


def test_default_lutris_paths_include_flatpak(tmp_path):
    lutris = LutrisPaths.default(tmp_path)
    assert tmp_path / ".local/share/lutris/games" in lutris.config_dirs
    assert tmp_path / ".config/lutris/games" in lutris.config_dirs
    assert tmp_path / FLATPAK / "data/lutris/games" in lutris.config_dirs
    assert tmp_path / FLATPAK / "config/lutris/games" in lutris.config_dirs
    assert lutris.pga_dbs == [tmp_path / ".local/share/lutris/pga.db", tmp_path / FLATPAK / "data/lutris/pga.db"]
    assert tmp_path / ".config/lutris/system.yml" in lutris.system_ymls
    assert tmp_path / FLATPAK / "config/lutris/system.yml" in lutris.system_ymls


# ---------------------------------------------------------------- preference


def test_two_installs_scan_prefers_shortest_path(home):
    short = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    long = make_gog_game(
        make_prefix(home / "Games" / "fallout-4-game-of-the-year-edition"), "Fallout 4 GOTY", "Fallout4.exe"
    )
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == short
    assert game.alternatives == [str(long)]


def test_lutris_install_beats_scan(home):
    short_prefix = make_prefix(home / "Games" / "fallout-4")
    short = make_gog_game(short_prefix, "Fallout 4", "Fallout4.exe")
    long_prefix = make_prefix(home / "Games" / "fallout-4-game-of-the-year-edition")
    long = make_gog_game(long_prefix, "Fallout 4 GOTY", "Fallout4.exe")
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fallout-4-goty-1.yml", lutris_yml(long / "Fallout4.exe", long_prefix))
    (game,), _ = discover_games(Settings(), home=home)
    assert game.root == long and game.source == "lutris"
    assert game.alternatives == [str(short)]


def test_same_install_seen_through_symlink_is_one_root(home, tmp_path):
    prefix = make_prefix(home / "Games" / "fallout-4")
    root = make_gog_game(prefix, "Fallout 4", "Fallout4.exe")
    (tmp_path / "mnt").symlink_to(home / "Games")
    linked_root = tmp_path / "mnt" / "fallout-4/drive_c/GOG Games/Fallout 4"
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fo4-1.yml", lutris_yml(linked_root / "Fallout4.exe"))
    (game,), _ = discover_games(Settings(), home=home)
    assert os.path.realpath(game.root) == os.path.realpath(root)
    assert game.root == linked_root, "Lutris's spelling of the path is kept"
    assert game.alternatives == []
    assert game.source == "lutris"


# ---------------------------------------------------------------- overrides


def test_manual_root_override(home, tmp_path):
    scanned = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    manual_prefix = make_prefix(tmp_path / "manual")
    manual = make_gog_game(manual_prefix, "Fallout 4 Modding", "Fallout4.exe")
    settings = Settings(game_overrides={"fallout4": GameOverride(root=str(manual))})

    (game,), warnings = discover_games(settings, home=home)

    assert warnings == []
    assert game.root == manual and game.exe == manual / "Fallout4.exe"
    assert game.source == "manual" and game.prefix == manual_prefix
    assert game.alternatives == [str(scanned)]
    assert game.warnings == []


def test_manual_root_override_with_tilde_and_exe_path(home):
    root = make_tree(home / "Odd Place" / "FNV", ["FalloutNV.exe", "Data/"])
    settings = Settings(game_overrides={"falloutnv": GameOverride(root="~/Odd Place/FNV/FalloutNV.exe")})
    (game,), _ = discover_games(settings, home=home)
    assert game.root == root and game.exe == root / "FalloutNV.exe" and game.source == "manual"


@pytest.mark.parametrize("make_folder, message", [(True, "has no Fallout4.exe"), (False, "does not exist")])
def test_invalid_manual_root_is_a_warning(home, tmp_path, make_folder, message):
    found = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    bad = tmp_path / "not-a-game"
    if make_folder:
        make_tree(bad, ["readme.txt"])
    settings = Settings(game_overrides={"fallout4": GameOverride(root=str(bad))})
    (game,), _ = discover_games(settings, home=home)
    assert game.root == bad and game.exe == bad / "Fallout4.exe"
    assert any(message in w for w in game.warnings)
    assert game.alternatives == [str(found)]


def test_prefix_and_wine_user_overrides(home, tmp_path):
    prefix = make_prefix(home / "Games" / "skyrim")
    make_gog_game(prefix, "Skyrim", "SkyrimSE.exe")
    other = make_prefix(tmp_path / "other-prefix", user="steamuser")
    make_tree(appdata(other, "steamuser"), ["Skyrim Special Edition GOG/plugins.txt"])
    make_tree(prefix, ["drive_c/users/second/AppData/Local/"])

    settings = Settings(game_overrides={"skyrimse": GameOverride(prefix=str(other))})
    (game,), _ = discover_games(settings, home=home)
    assert game.prefix == other and game.wine_user == "steamuser" and game.source == "scan"
    assert game.plugins_txt == appdata(other, "steamuser") / "Skyrim Special Edition GOG/plugins.txt"

    settings = Settings(game_overrides={"skyrimse": GameOverride(wine_user="second")})
    (game,), _ = discover_games(settings, home=home)
    assert game.prefix == prefix and game.wine_user == "second"
    assert game.appdata_dir == appdata(prefix, "second") / "Skyrim Special Edition GOG"
    assert game.warnings == []

    settings = Settings(game_overrides={"skyrimse": GameOverride(wine_user="ghost", prefix=str(tmp_path / "nope"))})
    (game,), _ = discover_games(settings, home=home)
    assert any("drive_c" in w for w in game.warnings)
    assert any("ghost" in w for w in game.warnings)


def test_ignore_hides_game_and_unknown_ids_warn(home):
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    make_gog_game(make_prefix(home / "Games" / "oblivion"), "Oblivion", "Oblivion.exe")
    settings = Settings(game_overrides={
        "fallout4": GameOverride(ignore=True, root="/somewhere"),
        "morrowind": GameOverride(root="/x"),
        "fallout3": GameOverride(prefix="/y"),  # not found and no root: nothing to show
    })
    games, warnings = discover_games(settings, home=home)
    assert [g.game_id for g in games] == ["oblivion"]
    assert any("morrowind" in w for w in warnings)


def test_override_root_adds_undiscovered_game(home, tmp_path):
    prefix = make_prefix(tmp_path / "pfx")
    root = make_gog_game(prefix, "Oblivion", "oblivion.EXE")
    settings = Settings(game_overrides={"oblivion": GameOverride(root=str(root))})
    (game,), _ = discover_games(settings, home=home)
    assert game.root == root and game.exe == root / "oblivion.EXE"
    assert game.alternatives == [] and game.prefix == prefix


# ---------------------------------------------------------------- Wine helpers


@pytest.mark.parametrize("users, env_user, expected", [
    (["Public", USER, "steamuser"], USER, USER),
    (["Public", "DERIK"], USER, "DERIK"),
    (["Public", "someone"], USER, "someone"),
    (["Public", "steamuser", "zed"], USER, "steamuser"),
    (["Public", "bob", "alice"], USER, "alice"),
    (["Public", "bob", "alice"], None, "alice"),
    (["Public"], USER, None),
    ([], USER, None),
])
def test_find_wine_user(tmp_path, monkeypatch, users, env_user, expected):
    if env_user is None:
        monkeypatch.delenv("USER", raising=False)
    else:
        monkeypatch.setenv("USER", env_user)
    prefix = tmp_path / "pfx"
    make_tree(prefix, ["drive_c/users/"] + [f"drive_c/users/{u}/" for u in users])
    assert find_wine_user(prefix) == expected


def test_find_wine_user_preferred_and_missing_folder(tmp_path):
    prefix = make_tree(tmp_path / "pfx", ["drive_c/users/Public/", "drive_c/users/a/", "drive_c/users/b/"])
    assert find_wine_user(prefix, preferred="b") == "b"
    assert find_wine_user(tmp_path / "nothing") is None


def test_find_wine_prefix(tmp_path):
    prefix = make_prefix(tmp_path / "outer")
    nested = make_prefix(prefix / "drive_c" / "inner")
    game = make_tree(nested / "drive_c/Games/X", ["Fallout4.exe"])
    assert find_wine_prefix(game) == nested
    assert find_wine_prefix(prefix / "drive_c" / "Games") == prefix
    assert find_wine_prefix(prefix) == prefix
    assert find_wine_prefix(tmp_path / "loose") is None


@pytest.mark.parametrize("files, expected", [
    (["goggame-1998527297.info"], "gog"),
    (["goggame-1998527297.ico"], "gog"),
    (["GOGGAME-1.INFO", "steam_api64.dll"], "gog"),
    (["steam_api64.dll"], "steam"),
    (["Steam_API.dll"], "steam"),
    (["EOSSDK-Win64-Shipping.dll"], "epic"),
    (["goggame-1.dll", "readme.txt"], "unknown"),
    ([], "unknown"),
])
def test_detect_store(tmp_path, files, expected):
    root = make_tree(tmp_path / "game", files + ["Fallout4.exe"])
    assert detect_store(root) == expected


def test_detect_store_missing_folder(tmp_path):
    assert detect_store(tmp_path / "missing") == "unknown"


def test_find_appdata_dir_preference(tmp_path):
    prefix = make_prefix(tmp_path / "pfx")
    local = appdata(prefix)
    old_local = prefix / "drive_c/users" / USER / "Local Settings/Application Data"

    assert find_appdata_dir("skyrimse", prefix, USER, "gog") == local / "Skyrim Special Edition GOG"
    assert find_appdata_dir("skyrimse", prefix, USER, "steam") == local / "Skyrim Special Edition"
    assert find_appdata_dir("skyrimse", prefix, USER) == local / "Skyrim Special Edition"
    assert find_appdata_dir("fallout4", prefix, "nobody") == prefix / "drive_c/users/nobody/AppData/Local/Fallout4"

    make_tree(local, ["Skyrim Special Edition/"])
    assert find_appdata_dir("skyrimse", prefix, USER, "gog") == local / "Skyrim Special Edition", \
        "an existing candidate beats the store default"
    make_tree(local, ["Skyrim Special Edition GOG/"])
    assert find_appdata_dir("skyrimse", prefix, USER, "gog") == local / "Skyrim Special Edition GOG"
    make_tree(old_local, ["Skyrim Special Edition/plugins.txt"])
    assert find_appdata_dir("skyrimse", prefix, USER, "gog") == old_local / "Skyrim Special Edition", \
        "a folder that has plugins.txt wins"


def test_find_appdata_dir_case_insensitive(tmp_path):
    prefix = make_tree(tmp_path / "pfx", ["drive_c/users/u/appdata/local/OBLIVION/Plugins.txt"])
    assert find_appdata_dir("oblivion", prefix, "u") == prefix / "drive_c/users/u/appdata/local/OBLIVION"


def test_plugins_txt_keeps_existing_spelling(home):
    prefix = make_prefix(home / "Games" / "oblivion")
    make_gog_game(prefix, "Oblivion", "Oblivion.exe")
    make_tree(appdata(prefix), ["Oblivion/Plugins.txt"])
    (game,), _ = discover_games(Settings(), home=home)
    assert game.plugins_txt == appdata(prefix) / "Oblivion" / "Plugins.txt"


# ---------------------------------------------------------------- cache


def sample_game(tmp_path) -> DiscoveredGame:
    return DiscoveredGame(
        game_id="fallout4",
        name="Fallout 4",
        root=tmp_path / "root",
        exe=tmp_path / "root" / "Fallout4.exe",
        data_dir=tmp_path / "root" / "Data",
        prefix=tmp_path,
        wine_user=USER,
        appdata_dir=tmp_path / "AppData",
        plugins_txt=None,
        store="gog",
        source="lutris",
        lutris_slug="fallout-4",
        lutris_name="Fallout 4 GOTY",
        alternatives=["/x/y"],
        warnings=["careful"],
    )


def test_to_dict_from_dict_round_trip(tmp_path):
    game = sample_game(tmp_path)
    data = game.to_dict()
    assert data["root"] == str(tmp_path / "root") and data["plugins_txt"] is None
    assert json.loads(json.dumps(data)) == data
    assert DiscoveredGame.from_dict(data) == game
    minimal = {k: data[k] for k in ("game_id", "root", "exe", "data_dir")}
    assert DiscoveredGame.from_dict(minimal).name == "Fallout 4"
    with pytest.raises((KeyError, TypeError, ValueError)):
        DiscoveredGame.from_dict({"game_id": "fallout4"})


def test_save_and_load_cache(tmp_path):
    game = sample_game(tmp_path)
    path = tmp_path / "cache" / "games.json"
    save_cached_games([game], path)
    assert load_cached_games(path) == [game]
    save_cached_games([game])
    assert paths.games_cache_path().is_file()
    assert load_cached_games() == [game]


@pytest.mark.parametrize("content", [
    None,
    "{not json",
    "[]",
    json.dumps({"version": 999, "games": []}),
    json.dumps({"version": 1, "games": [{"game_id": "fallout4"}]}),
    json.dumps({"version": 1, "games": "nope"}),
    json.dumps({"version": 1}),
])
def test_unreadable_cache_is_none(tmp_path, content):
    path = tmp_path / "games.json"
    if content is not None:
        path.write_text(content)
    assert load_cached_games(path) is None


def test_get_games_uses_cache_and_applies_overrides(home, tmp_path):
    fo4 = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    cache = tmp_path / "cache.json"

    games, _ = get_games(Settings(), cache_path=cache, home=home)
    assert [g.game_id for g in games] == ["fallout4"] and cache.is_file()

    make_gog_game(make_prefix(home / "Games" / "skyrim"), "Skyrim", "SkyrimSE.exe")
    games, _ = get_games(Settings(), cache_path=cache, home=home)
    assert [g.game_id for g in games] == ["fallout4"], "cached result, no rescan"

    ignored = Settings(game_overrides={"fallout4": GameOverride(ignore=True)})
    assert get_games(ignored, cache_path=cache, home=home)[0] == []

    manual = make_tree(tmp_path / "manual-fo4", ["Fallout4.exe", "Data/"])
    moved = Settings(game_overrides={"fallout4": GameOverride(root=str(manual))})
    (game,), _ = get_games(moved, cache_path=cache, home=home)
    assert game.root == manual and game.alternatives == [str(fo4)]
    cached = json.loads(cache.read_text())
    assert [g["root"] for g in cached["games"]] == [str(fo4)], "the cache holds pre-override results"

    games, _ = get_games(Settings(), rescan=True, cache_path=cache, home=home)
    assert [g.game_id for g in games] == ["fallout4", "skyrimse"]
    assert [g.game_id for g in load_cached_games(cache)] == ["fallout4", "skyrimse"]


def test_get_games_caches_warnings(home, tmp_path):
    cache = tmp_path / "cache.json"
    _, warnings = get_games(Settings(), cache_path=cache, home=home)
    assert any("does not exist" in w for w in warnings)
    assert get_games(Settings(), cache_path=cache, home=home)[1] == warnings


def test_get_games_rescans_when_scan_settings_change(home, tmp_path):
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    make_gog_game(make_prefix(home / "Other" / "skyrim"), "Skyrim", "SkyrimSE.exe")
    cache = tmp_path / "cache.json"

    games, _ = get_games(Settings(), cache_path=cache, home=home)
    assert [g.game_id for g in games] == ["fallout4"]

    games, _ = get_games(Settings(games_dir="~/Other"), cache_path=cache, home=home)
    assert [g.game_id for g in games] == ["skyrimse"], "a new games folder triggers a rescan"

    games, _ = get_games(Settings(games_dir="~/Other", scan_depth=2), cache_path=cache, home=home)
    assert games == [], "a new scan depth triggers a rescan (Skyrim is 4 levels down)"


def test_get_games_without_settings_uses_saved_settings(home, tmp_path):
    from bethmm.settings import save_settings

    fo4 = make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    save_settings(Settings(game_overrides={"fallout4": GameOverride(ignore=True)}))
    assert get_games(cache_path=tmp_path / "c.json", home=home)[0] == []
    assert discover_games(home=home)[0] == []
    save_settings(Settings())
    (game,), _ = get_games(cache_path=tmp_path / "c.json", home=home)
    assert game.root == fo4


def test_get_games_default_cache_path(home):
    make_gog_game(make_prefix(home / "Games" / "fallout-4"), "Fallout 4", "Fallout4.exe")
    get_games(Settings(), home=home)
    assert [g.game_id for g in load_cached_games(paths.games_cache_path())] == ["fallout4"]


# ---------------------------------------------------------------- safety


def test_discovery_never_writes_to_home_or_games(home, tmp_path):
    decoy = make_tree(tmp_path / "decoy", ["Fallout3.exe"])
    prefix = make_prefix(home / "Games" / "fallout-4", decoy=decoy)
    root = make_gog_game(prefix, "Fallout 4", "Fallout4.exe")
    write_yml(home / LUTRIS_CONFIG_DIRS[0] / "fallout-4-1.yml", lutris_yml(root / "Fallout4Launcher.exe", prefix))
    make_pga(home / ".local/share/lutris/pga.db", [
        {"id": 1, "slug": "fallout-4", "name": "Fallout 4", "directory": str(prefix), "configpath": "fallout-4-1"},
    ])
    before = snapshot(tmp_path)

    games, _ = discover_games(Settings(), home=home)
    get_games(Settings(), rescan=True, home=home, cache_path=None)

    assert [g.game_id for g in games] == ["fallout4"]
    assert snapshot(tmp_path) == before
