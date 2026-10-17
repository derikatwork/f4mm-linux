"""End-to-end checks of the phase 2 mod commands against a fake home and game."""

import os
import shutil
import zipfile
from pathlib import Path

import pytest

from bethmm import paths
from bethmm.cli import main
from bethmm.manifest import Manifest
from bethmm.settings import load_settings
from helpers import make_plugin, make_tree

pytestmark = pytest.mark.skipif(not shutil.which("7z"), reason="7z not installed")
USER = "derik"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", USER)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def fo4(home) -> Path:
    prefix = home / "Games" / "fallout-4"
    make_tree(prefix, ["system.reg", "user.reg", "drive_c/users/Public/", f"drive_c/users/{USER}/AppData/Local/"])
    root = prefix / "drive_c" / "GOG Games" / "Fallout 4"
    make_tree(root, {
        "Fallout4.exe": b"exe",
        "goggame-1.info": b"{}",
        "Data/Fallout4.esm": make_plugin(),
        "Data/textures/vanilla.dds": b"vanilla",
        "Data/Fallout4 - Textures1.ba2": b"ba2",
    })
    assert main(["use", "fallout4"]) == 0
    return root


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            content = open(p, "rb").read() if os.path.isfile(p) and not os.path.islink(p) else None
            out[os.path.relpath(p, root)] = (st.st_mode, content, st.st_ino if content is not None else None)
    return out


def mod_zip(tmp_path: Path, name: str, entries: dict) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for rel, data in entries.items():
            zf.writestr(rel, data)
    return path


def esp(*masters: str) -> bytes:
    return make_plugin(masters or ("Fallout4.esm",))


def test_install_list_conflicts_move_uninstall_purge(home, fo4, tmp_path, capsys):
    vanilla = snapshot(fo4)
    a = mod_zip(tmp_path, "Armor Pack-1111-1-0-1700000000.zip", {
        "Armor Pack/ArmorPack.esp": esp(),
        "Armor Pack/Textures/Armor/plate.dds": b"A-plate",
        "Armor Pack/Textures/shared.dds": b"A-shared",
        "Armor Pack/readme.txt": b"hi",
    })
    b = mod_zip(tmp_path, "Better Shared-2222-2-0-1700000000.zip", {
        "textures/SHARED.dds": b"B-shared",
        "Fallout4.esm": esp(),
    })

    assert main(["install", str(a), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Installed 'Armor Pack'." in out and "linked 3 files" in out
    plate = fo4 / "Data" / "textures" / "Armor" / "plate.dds"
    assert plate.is_symlink() and plate.read_bytes() == b"A-plate"
    assert not (fo4 / "Data" / "Textures").exists(), "merged into the existing lowercase folder"
    assert not (fo4 / "Data" / "readme.txt").exists()

    assert main(["install", str(b), "--yes"]) == 0
    capsys.readouterr()
    shared = fo4 / "Data" / "textures" / "shared.dds"
    assert shared.read_bytes() == b"B-shared", "the newer mod is at the bottom and wins"
    esm = fo4 / "Data" / "Fallout4.esm"
    assert esm.is_symlink(), "the vanilla master was moved aside for the mod's file"
    assert any(paths.backups_dir("fallout4").rglob("Fallout4.esm"))

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "  1 [x] Armor Pack" in out and "  2 [x] Better Shared" in out
    assert "overwritten by Better Shared" in out

    assert main(["conflicts", "--files"]) == 0
    assert "Armor Pack < Better Shared" in capsys.readouterr().out

    assert main(["move", "Armor", "--bottom"]) == 0
    capsys.readouterr()
    assert shared.read_bytes() == b"A-shared"

    assert main(["disable", "2"]) == 0
    capsys.readouterr()
    assert shared.read_bytes() == b"B-shared", "Armor Pack (now #2) disabled -> Better Shared provides it"
    assert not plate.exists()
    assert main(["enable", "Armor Pack"]) == 0
    capsys.readouterr()

    assert main(["uninstall", "Better Shared", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Uninstalled 'Better Shared'." in out
    assert not esm.is_symlink() and esm.read_bytes() == make_plugin(), "original restored"
    assert not (paths.mods_dir("fallout4") / "Better Shared").exists()

    assert main(["purge", "--yes"]) == 0
    capsys.readouterr()
    assert snapshot(fo4) == vanilla, "purge returns the game folder to vanilla"
    assert main(["list"]) == 0
    assert "PURGED" in capsys.readouterr().out

    assert main(["disable", "Armor Pack"]) == 0
    assert "purged" in capsys.readouterr().out.lower()
    assert snapshot(fo4) == vanilla, "changes after a purge don't touch the game folder"
    assert main(["enable", "Armor Pack"]) == 0
    assert main(["deploy"]) == 0
    capsys.readouterr()
    assert plate.read_bytes() == b"A-plate"


def test_dry_runs_change_nothing(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp(), "textures/a.dds": b"a"})
    before = snapshot(fo4)
    assert main(["install", str(a), "--dry-run"]) == 0
    assert "Dry run: nothing was installed" in capsys.readouterr().out
    assert snapshot(fo4) == before
    staged = paths.mods_dir("fallout4")
    assert not staged.exists() or not any(staged.iterdir())

    assert main(["install", str(a), "--yes"]) == 0
    capsys.readouterr()
    deployed = snapshot(fo4)
    with Manifest() as m:
        mods_before = [(x.name, x.enabled, x.priority) for x in m.mods("fallout4")]
    for argv in (["disable", "A", "--dry-run"], ["uninstall", "A", "--dry-run"], ["purge", "--dry-run"],
                 ["move", "A", "--top", "--dry-run"], ["deploy", "--dry-run"]):
        assert main(argv) == 0, argv
        out = capsys.readouterr().out
        assert "Dry run" in out or "Would" in out, argv
        assert snapshot(fo4) == deployed, argv
        with Manifest() as m:
            assert [(x.name, x.enabled, x.priority) for x in m.mods("fallout4")] == mods_before, argv

    assert main(["config", "--dry-run", "on"]) == 0
    capsys.readouterr()
    assert main(["disable", "A"]) == 0
    assert "dry-run mode is on" in capsys.readouterr().out
    assert snapshot(fo4) == deployed
    assert main(["config", "--dry-run", "off"]) == 0


def test_update_keeps_position_and_removes_old_files(home, fo4, tmp_path, capsys):
    v1 = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/old.dds": b"old"})
    other = mod_zip(tmp_path, "Other-777-1-0-1700000000.zip", {"Other.esp": esp()})
    v2 = mod_zip(tmp_path, "Cool Mod-555-2-0-1700000100.zip", {"Cool.esp": esp(), "textures/new.dds": b"new"})
    for archive in (v1, other):
        assert main(["install", str(archive), "--yes"]) == 0
    with Manifest() as m:
        old_folder = m.find_mod("fallout4", "Cool Mod").staging_dir()
    capsys.readouterr()

    assert main(["install", str(v2), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "This updates 'Cool Mod' 1.0 to 2.0" in out and "Updated 'Cool Mod'." in out
    with Manifest() as m:
        mods = m.mods("fallout4")
        assert [x.name for x in mods] == ["Cool Mod", "Other"], "position kept"
        assert mods[0].version == "2.0"
    assert not (fo4 / "Data" / "textures" / "old.dds").exists()
    assert (fo4 / "Data" / "textures" / "new.dds").read_bytes() == b"new"
    assert not old_folder.exists()


def test_install_needs_choices_for_unknown_and_fomod(home, fo4, tmp_path, capsys):
    unknown = mod_zip(tmp_path, "Opts-1-1-0.zip", {"Option A/Stuff/a.nif": b"a", "Option B/Stuff/b.nif": b"b"})
    assert main(["install", str(unknown), "--yes"]) == 1
    assert "--root" in capsys.readouterr().err
    assert main(["install", str(unknown), "--yes", "--root", "Option A/Stuff", "--target", "data"]) == 0
    capsys.readouterr()
    assert (fo4 / "Data" / "a.nif").read_bytes() == b"a"

    fomod = mod_zip(tmp_path, "Fomod-2-1-0.zip", {
        "fomod/ModuleConfig.xml": b"<config/>", "00 Core/core.esp": esp(), "01 Opt/opt.esp": esp(),
    })
    assert main(["install", str(fomod), "--yes"]) == 1
    assert "FOMOD" in capsys.readouterr().err
    assert main(["install", str(fomod), "--yes", "--root", "00 Core"]) == 0
    capsys.readouterr()
    assert (fo4 / "Data" / "core.esp").is_symlink() and not (fo4 / "Data" / "opt.esp").exists()


def test_install_requires_confirmation(home, fo4, tmp_path, capsys, monkeypatch):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["install", str(a)]) == 1
    assert "--yes" in capsys.readouterr().err
    with Manifest() as m:
        assert m.mods("fallout4") == []


def test_low_confidence_game_needs_game_flag_with_yes(home, fo4, tmp_path, capsys):
    make_tree(home / "Games" / "skyrim" / "drive_c" / "GOG Games" / "Skyrim", ["SkyrimSE.exe", "Data/"])
    tex = mod_zip(tmp_path, "Tex-1-1-0.zip", {"textures/x.dds": b"x"})
    load = load_settings()
    assert load.current_game == "fallout4"
    assert main(["install", str(tex), "--yes"]) == 1
    assert "add --game" in capsys.readouterr().err
    assert main(["install", str(tex), "--yes", "--game", "fallout4"]) == 0


def test_verify_and_repair(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp(), "textures/a.dds": b"a", "textures/b.dds": b"b"})
    assert main(["install", str(a), "--yes"]) == 0
    capsys.readouterr()
    assert main(["verify"]) == 0
    capsys.readouterr()

    (fo4 / "Data" / "textures" / "a.dds").unlink()
    b_link = fo4 / "Data" / "textures" / "b.dds"
    b_link.unlink()
    b_link.write_bytes(b"user's own edit")
    stray = fo4 / "Data" / "stray.esp"
    stray.symlink_to(paths.mods_dir("fallout4") / "A-1-1-0" / "A.esp")

    assert main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "missing link: Data/textures/a.dds" in out
    assert "replaced by another file (left alone): Data/textures/b.dds" in out
    assert "stray link into the mods folder: Data/stray.esp" in out

    assert main(["verify", "--repair"]) == 0
    capsys.readouterr()
    assert (fo4 / "Data" / "textures" / "a.dds").is_symlink()
    assert b_link.read_bytes() == b"user's own edit", "the user's file is never touched"
    assert not stray.exists()


def test_status_deployed_only_and_history(home, fo4, tmp_path, capsys):
    assert main(["status", "--deployed-only"]) == 0
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    assert main(["status", "--deployed-only"]) == 1
    assert main(["status"]) == 0
    assert "1 mod(s), 1 enabled; 1 link(s)" in capsys.readouterr().out
    assert main(["history"]) == 0
    assert "link" in capsys.readouterr().out
    assert main(["purge", "--all", "--yes"]) == 0
    assert main(["status", "--deployed-only"]) == 0


def test_locked_manifest_gives_clean_error(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    with Manifest(lock=True):
        assert main(["install", str(a), "--yes"]) == 1
    assert "another bethmm process" in capsys.readouterr().err


def test_which_game_errors(home, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["list"]) == 1
    assert "which game?" in capsys.readouterr().err
    assert main(["install", str(a), "--yes", "--game", "skyrimse"]) == 1
    assert "was not found" in capsys.readouterr().err
