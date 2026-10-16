"""End-to-end checks of the read-only phase 1 commands against a fake home."""

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from bethmm import paths
from bethmm.cli import main
from bethmm.settings import load_settings
from helpers import make_plugin, make_tree

needs_7z = pytest.mark.skipif(not any(shutil.which(n) for n in ("7z", "7zz", "7za")), reason="7z not installed")
USER = "derik"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", USER)
    monkeypatch.chdir(tmp_path)
    return home


def gog_game(home: Path, slug: str, title: str, exe: str, data_dirs=(), appdata: str | None = None) -> Path:
    prefix = home / "Games" / slug
    make_tree(prefix, ["system.reg", "user.reg", "drive_c/users/Public/", "drive_c/windows/system32/"])
    local = prefix / "drive_c" / "users" / USER / "AppData" / "Local"
    local.mkdir(parents=True)
    if appdata:
        make_tree(local / appdata, {"plugins.txt": "*Unofficial Patch.esp\n"})
    (prefix / "dosdevices").mkdir()
    (prefix / "dosdevices" / "z:").symlink_to("/")
    root = prefix / "drive_c" / "GOG Games" / title
    make_tree(root, [exe, "goggame-1.info", *data_dirs])
    return root


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def zip_mod(path: Path, entries: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_games_lists_gog_installs(home, capsys):
    fo4 = gog_game(home, "fallout-4-goty", "Fallout 4 GOTY", "Fallout4.exe", ["Data/Textures/"], appdata="Fallout4")
    sse = gog_game(home, "skyrim-se", "Skyrim Anniversary Edition", "SkyrimSE.exe", ["Data/"],
                   appdata="Skyrim Special Edition GOG")
    assert main(["games"]) == 0
    out = capsys.readouterr().out
    assert "Fallout 4  [fallout4]" in out and str(fo4) in out
    assert "Skyrim Special Edition  [skyrimse]" in out and str(sse) in out
    assert "AppData/Local/Skyrim Special Edition GOG/plugins.txt" in out
    assert "store       : gog" in out

    assert main(["games", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [g["game_id"] for g in data["games"]] == ["fallout4", "skyrimse"]


@needs_7z
def test_analyze_archive_matches_existing_case_and_changes_nothing(home, tmp_path, capsys):
    fo4 = gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe",
                   ["data/textures/armor/", "data/Fallout4.esm"], appdata="Fallout4")
    before = snapshot(home / "Games")
    mod = zip_mod(tmp_path / "Cool Armor-12345-1-2-1700000000.zip", {
        "Cool Armor v1.2/Textures/Armor/plate.dds": b"dds",
        "Cool Armor v1.2/CoolArmor.esp": make_plugin(["Fallout4.esm"]),
        "Cool Armor v1.2/Fallout4.esm": b"replacement master (silly, but tests replacement)",
        "readme.txt": b"read me",
    })

    assert main(["analyze", str(mod)]) == 0
    out = capsys.readouterr().out
    assert "Mod      : Cool Armor, version 1.2, Nexus mod 12345" in out
    assert "Game     : Fallout 4 (high confidence)" in out
    assert "CoolArmor.esp requires Fallout4.esm" in out
    assert "into the Data folder - high confidence (plugins/archives/Data folders)" in out
    assert ("Cool Armor v1.2/Textures/Armor/plate.dds  ->  data/textures/armor/plate.dds"
            "   [case matched to existing folder]") in out
    assert "Cool Armor v1.2/Fallout4.esm  ->  data/Fallout4.esm   [replaces an existing game file" in out
    assert "readme.txt" in out.split("Not installed")[1]

    assert snapshot(home / "Games") == before, "analyze must not touch the game folder"
    assert not any(paths.tmp_dir().iterdir()), "temporary extraction is cleaned up"


@needs_7z
def test_analyze_json_and_unknown_layout(home, tmp_path, capsys):
    mod = zip_mod(tmp_path / "options.zip", {
        "00 Core/Data/a.esp": make_plugin(["Skyrim.esm"], hedr_version=1.71, form_version=44),
        "01 Optional/Data/b.esp": make_plugin(["Skyrim.esm"], hedr_version=1.71, form_version=44),
    })
    assert main(["analyze", str(mod), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["target"] == "unknown" and data["mappings"] == []
    assert [c["path"] for c in data["candidate_roots"]] == ["00 Core", "01 Optional"]
    assert data["game"]["game_id"] == "skyrimse"


def test_analyze_extracted_folder_and_missing_path(home, tmp_path, capsys):
    folder = make_tree(tmp_path / "ENB", ["d3d11.dll", "enbseries.ini", "enbseries/a.fx"])
    assert main(["analyze", str(folder)]) == 0
    assert "into the game folder, next to the .exe" in capsys.readouterr().out
    assert main(["analyze", str(tmp_path / "nope.7z")]) == 1
    assert "does not exist" in capsys.readouterr().err


@needs_7z
def test_analyze_reports_bad_archive(home, tmp_path, capsys):
    bad = tmp_path / "broken.7z"
    bad.write_bytes(b"garbage")
    assert main(["analyze", str(bad)]) == 1
    assert "7z could not read broken.7z" in capsys.readouterr().err
    assert not any(paths.tmp_dir().iterdir())


def test_plugin_info(home, tmp_path, capsys):
    esp = tmp_path / "x.esp"
    esp.write_bytes(make_plugin(["Skyrim.esm"], hedr_version=1.71, form_version=44, flags=0x201))
    junk = tmp_path / "junk.esp"
    junk.write_bytes(b"nope")
    assert main(["plugin-info", str(esp), str(junk)]) == 1
    out = capsys.readouterr().out
    assert "masters : Skyrim.esm" in out
    assert "flags ESM, light/ESL" in out
    assert "game    : Skyrim Special Edition" in out
    assert "junk.esp: not a plugin" in out


def test_starfield_plugin_flags_are_not_called_esl(home, tmp_path, capsys):
    esp = tmp_path / "sf.esm"
    esp.write_bytes(make_plugin(["Starfield.esm"], hedr_version=0.96, form_version=555, flags=0x201))
    assert main(["plugin-info", str(esp)]) == 0
    out = capsys.readouterr().out
    assert "light/ESL" not in out and "flags ESM, 0x200" in out


def test_override_and_config_commands(home, tmp_path, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/"])
    manual = make_tree(tmp_path / "elsewhere" / "FO4", ["Fallout4.exe", "Data/"])
    assert main(["override", "fallout4", "--root", "elsewhere/FO4"]) == 0
    assert load_settings().game_overrides["fallout4"].root == str(manual), "stored as an absolute path"
    assert f"game folder : {manual}" in capsys.readouterr().out

    assert main(["override", "fallout4", "--ignore"]) == 0
    assert "not currently listed" in capsys.readouterr().out
    assert main(["override", "fallout4", "--clear"]) == 0
    assert "fallout4" not in load_settings().game_overrides

    assert main(["config", "--games-dir", "/mnt/games", "--scan-depth", "4"]) == 0
    s = load_settings()
    assert s.games_dir == "/mnt/games" and s.scan_depth == 4
    assert main(["config", "--scan-depth", "0"]) == 1


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "analyze" in capsys.readouterr().out


def test_new_folders_follow_game_naming_and_are_labelled(home, tmp_path, capsys):
    gog_game(home, "skyrim", "Skyrim", "SkyrimSE.exe", ["Data/Skyrim.esm", "Data/textures/", "Data/meshes/"])
    folder = make_tree(tmp_path / "mod", {
        "Scripts/a.pex": b"x",
        "Textures/b.dds": b"x",
        "m.esp": make_plugin(["Skyrim.esm"], hedr_version=1.71, form_version=44),
    })
    assert main(["analyze", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "Scripts/a.pex  ->  Data/scripts/a.pex   [new folder spelled like the game's folders]" in out
    assert "Textures/b.dds  ->  Data/textures/b.dds   [case matched to existing folder]" in out
    assert "m.esp  ->  Data/m.esp\n" in out


# --- final review additions --------------------------------------------------


def test_unknown_layout_shows_tree_and_root_target_preview(home, tmp_path, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/Fallout4.esm", "Data/Meshes/"])
    folder = make_tree(tmp_path / "m", {
        "Option A/Stuff/a.nif": b"x",
        "Option B/Stuff/b.nif": b"x",
        "Option A/settings.ini": b"x",
        "readme.txt": b"x",
    })
    assert main(["analyze", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "Files in the mod (3):" in out
    assert "  Option A/\n      settings.ini\n      Stuff/\n          a.nif" in out
    assert "--root 'Option A'" in out or "--root Option" in out

    assert main(["analyze", str(folder), "--game", "fallout4", "--root", "Option A/Stuff", "--target", "data"]) == 0
    out = capsys.readouterr().out
    assert "Option A/Stuff/a.nif  ->  Data/a.nif" in out
    assert "into the Data folder - high confidence (your choice)" in out

    assert main(["analyze", str(folder), "--root", "Option A"]) == 1
    assert "add --target data or --target game" in capsys.readouterr().err


def test_target_override_keeps_detected_root(home, tmp_path, capsys):
    folder = make_tree(tmp_path / "Wrap", {"Inner/textures/a.dds": b"x"})
    assert main(["analyze", str(folder), "--target", "game"]) == 0
    assert "Inner/textures/a.dds  ->  textures/a.dds" in capsys.readouterr().out


def test_unidentified_game_suggests_game_flag(home, tmp_path, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/"])
    gog_game(home, "skyrim", "Skyrim", "SkyrimSE.exe", ["Data/"])
    folder = make_tree(tmp_path / "tex", {"Textures/a.dds": b"x"})
    assert main(["analyze", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "re-run with --game fallout4|skyrimse" in out
    assert "no game chosen" in out


def test_case_duplicates_and_planned_folders_are_labelled_truthfully(home, tmp_path, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/Fallout4.esm", "Data/Textures/", "Data/textures/"])
    folder = make_tree(tmp_path / "mod", {
        "m.esp": make_plugin(["Fallout4.esm"]),
        "textures/a.dds": b"x",
        "F4SE/Plugins/x.dll": b"x",
        "F4SE/Plugins/x.ini": b"x",
    })
    assert main(["analyze", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "! 'Data' has entries differing only in case" in out
    assert "F4SE/Plugins/x.ini  ->  Data/F4SE/Plugins/x.ini\n" in out, "a folder planned by this mod is not 'existing'"


def test_games_cached_note_only_when_cache_was_used(home, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/"])
    assert main(["games"]) == 0
    assert "cached results" not in capsys.readouterr().out
    assert main(["games"]) == 0
    assert "cached results" in capsys.readouterr().out


@needs_7z
def test_every_file_operation_is_logged(home, tmp_path, capsys):
    gog_game(home, "fallout-4", "Fallout 4", "Fallout4.exe", ["Data/"])
    mod = zip_mod(tmp_path / "m.zip", {"m.esp": b"x"})
    assert main(["config", "--scan-depth", "5"]) == 0
    assert main(["games", "--rescan"]) == 0
    assert main(["analyze", str(mod)]) == 0
    text = paths.log_path().read_text()
    assert f"WRITE {paths.settings_path()}" in text
    assert f"WRITE {paths.games_cache_path()}" in text
    assert "MKDIR" in text and "EXTRACT" in text and "EXTRACTED 1 files" in text and "DELETE" in text


def test_os_errors_are_reported_without_traceback(home, tmp_path, capsys, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(paths, "tmp_dir", lambda: blocker / "tmp")
    mod = zip_mod(tmp_path / "m.zip", {"m.esp": b"x"})
    assert main(["analyze", str(mod)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err


def test_stale_extractions_are_cleaned(home, tmp_path):
    from bethmm import cli

    tmp = paths.tmp_dir()
    old = make_tree(tmp / "analyze-old", ["x/a.dds"])
    new = make_tree(tmp / "analyze-new", ["x/a.dds"])
    other = make_tree(tmp / "keep-me", ["a"])
    os.utime(old, (1_000_000, 1_000_000))
    cli._remove_stale_extractions(tmp)
    assert not old.exists() and new.exists() and other.exists()


def test_non_utf8_file_names_do_not_crash_output(home, tmp_path):
    import subprocess
    import sys

    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / os.fsdecode(b"caf\xe9.esp")).write_bytes(make_plugin(["Fallout4.esm"]))
    env = dict(os.environ, PYTHONIOENCODING="utf-8:strict")
    proc = subprocess.run([sys.executable, "-m", "bethmm", "analyze", str(folder)],
                          capture_output=True, env=env, cwd=Path(__file__).resolve().parent.parent)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"caf\\udce9.esp" in proc.stdout
