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
    out = capsys.readouterr().out
    assert "Nothing was installed (dry run)" in out and "link     Data/A.esp  ->  A" in out
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


def test_unidentified_mod_goes_to_current_game(home, fo4, tmp_path, capsys):
    tex = mod_zip(tmp_path, "Tex-1-1-0.zip", {"textures/x.dds": b"x"})
    assert load_settings().current_game == "fallout4"
    assert main(["install", str(tex), "--yes"]) == 0
    assert "installing into Fallout 4 (your current game)" in capsys.readouterr().out
    assert (fo4 / "Data" / "textures" / "x.dds").read_bytes() == b"x"


def test_without_current_game_two_games_need_game_flag(home, fo4, tmp_path, capsys):
    from bethmm.settings import save_settings

    make_tree(home / "Games" / "skyrim" / "drive_c" / "GOG Games" / "Skyrim", ["SkyrimSE.exe", "Data/"])
    assert main(["games", "--rescan"]) == 0
    settings = load_settings()
    settings.current_game = None
    save_settings(settings)
    capsys.readouterr()
    tex = mod_zip(tmp_path, "Tex-1-1-0.zip", {"textures/x.dds": b"x"})
    assert main(["install", str(tex), "--yes"]) == 1
    assert "which game?" in capsys.readouterr().err
    assert main(["install", str(tex), "--yes", "--game", "fallout4"]) == 0


def test_optional_file_from_same_nexus_page_installs_separately(home, fo4, tmp_path, capsys):
    main_file = mod_zip(tmp_path, "Armorsmith Extended-2228-4-6-1700000000.zip", {"AE.esp": esp()})
    patch = mod_zip(tmp_path, "AE DLC Patch-2228-1-0-1700000500.zip", {"AE_DLC.esp": esp()})
    assert main(["install", str(main_file), "--yes"]) == 0
    assert main(["install", str(patch), "--yes"]) == 0
    capsys.readouterr()
    with Manifest() as m:
        assert [x.name for x in m.mods("fallout4")] == ["Armorsmith Extended", "AE DLC Patch"]
    assert (fo4 / "Data" / "AE.esp").is_symlink() and (fo4 / "Data" / "AE_DLC.esp").is_symlink()


def test_update_with_no_deploy_disables_the_mod(home, fo4, tmp_path, capsys):
    v1 = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/old.dds": b"old"})
    v2 = mod_zip(tmp_path, "Cool Mod-555-2-0-1700000100.zip", {"Cool.esp": esp(), "textures/new.dds": b"new"})
    assert main(["install", str(v1), "--yes"]) == 0
    with Manifest() as m:
        old_folder = m.find_mod("fallout4", "Cool Mod").staging_dir()
    capsys.readouterr()
    assert main(["install", str(v2), "--yes", "--no-deploy"]) == 0
    out = capsys.readouterr().out
    assert "disabled" in out
    assert not old_folder.exists()
    assert not os.path.lexists(fo4 / "Data" / "textures" / "old.dds")
    assert not os.path.lexists(fo4 / "Data" / "textures" / "new.dds")
    with Manifest() as m:
        assert not m.find_mod("fallout4", "Cool Mod").enabled
    assert main(["enable", "Cool Mod"]) == 0
    assert (fo4 / "Data" / "textures" / "new.dds").read_bytes() == b"new"


def test_new_mod_with_no_deploy_stays_out_of_the_game(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    b = mod_zip(tmp_path, "B-2-1-0.zip", {"B.esp": esp()})
    assert main(["install", str(a), "--yes", "--no-deploy"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert main(["install", str(b), "--yes"]) == 0  # the next command doesn't deploy A
    assert not os.path.lexists(fo4 / "Data" / "A.esp")
    assert (fo4 / "Data" / "B.esp").is_file()


def test_update_keeps_old_files_while_linked_then_repair_deletes_them(home, fo4, tmp_path, capsys, monkeypatch):
    v1 = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/old.dds": b"old"})
    v2 = mod_zip(tmp_path, "Cool Mod-555-2-0-1700000100.zip", {"Cool.esp": esp(), "textures/new.dds": b"new"})
    assert main(["install", str(v1), "--yes"]) == 0
    with Manifest() as m:
        old_folder = m.find_mod("fallout4", "Cool Mod").staging_dir()
    real_unlink = os.unlink

    def unlink(path, *a, **kw):
        if str(path).endswith("old.dds") and str(fo4) in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_unlink(path, *a, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(os, "unlink", unlink)
        assert main(["install", str(v2), "--yes"]) == 1
    assert "kept the previous files" in capsys.readouterr().out
    assert old_folder.is_dir()
    assert (fo4 / "Data" / "textures" / "old.dds").read_bytes() == b"old", "old links still work"
    assert main(["deploy"]) == 0
    assert main(["verify", "--repair"]) == 0
    assert not old_folder.exists()
    assert (fo4 / "Data" / "textures" / "new.dds").read_bytes() == b"new"


def test_verify_never_deletes_unknown_folders_in_the_mods_folder(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    mine = paths.mods_dir("fallout4") / "My Own Tweaks"
    make_tree(mine, {"Data/tweak.ini": b"x"})
    capsys.readouterr()
    assert main(["verify"]) == 0
    assert "My Own Tweaks" in capsys.readouterr().out
    assert main(["verify", "--repair"]) == 0
    assert (mine / "Data" / "tweak.ini").read_bytes() == b"x"


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
    with Manifest() as m:
        stray.symlink_to(m.find_mod("fallout4", "A").staging_dir() / "A.esp")

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
    assert main(["status", "--deployed-only"]) == 3
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


# ---------------------------------------------------------------- review fixes


def test_install_dry_run_shows_the_real_plan(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp(), "textures/shared.dds": b"A"})
    b = mod_zip(tmp_path, "B-2-1-0.zip", {"B.esp": esp(), "textures/SHARED.dds": b"B", "textures/vanilla.dds": b"B"})
    assert main(["install", str(a), "--yes"]) == 0
    before = snapshot(fo4)
    with Manifest() as m:
        records = (m.deployed("fallout4"), [x.name for x in m.mods("fallout4")])
    capsys.readouterr()
    assert main(["install", str(b), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "overrides A" in out  # A's link, not a game file
    assert "replaces an existing game file" in out  # vanilla.dds
    dry_ops = sorted(line.split()[1] for line in out.splitlines() if line.startswith("  link ")
                     or line.startswith("  backup ") or line.startswith("  unlink "))
    assert dry_ops == ["Data/B.esp", "Data/textures/shared.dds", "Data/textures/shared.dds",
                       "Data/textures/vanilla.dds", "Data/textures/vanilla.dds"]
    assert snapshot(fo4) == before
    with Manifest() as m:
        assert (m.deployed("fallout4"), [x.name for x in m.mods("fallout4")]) == records
    assert not [p for p in paths.mods_dir("fallout4").iterdir() if p.name != "A"]
    # The real run does exactly that.
    assert main(["install", str(b), "--yes"]) == 0
    assert "linked 3 files, removed 1 link, backed up 1 original game file" in capsys.readouterr().out


def test_install_same_file_name_of_another_nexus_mod_is_separate(home, fo4, tmp_path, capsys):
    one = mod_zip(tmp_path, "2K-1111-1-0-1700000000.zip", {"textures/one.dds": b"1"})
    two = mod_zip(tmp_path, "2K-2222-3-1-1700000000.zip", {"textures/two.dds": b"2"})
    assert main(["install", str(one), "--yes"]) == 0
    assert main(["install", str(two), "--yes"]) == 0
    with Manifest() as m:
        assert [x.nexus_id for x in m.mods("fallout4")] == [1111, 2222]
    assert (fo4 / "Data" / "textures" / "one.dds").is_file() and (fo4 / "Data" / "textures" / "two.dds").is_file()


def test_install_versioned_nexus_file_name_updates(home, fo4, tmp_path, capsys):
    v1 = mod_zip(tmp_path, "Cool Mod 1.2-555-1-2-1700000000.zip", {"Cool.esp": esp(), "textures/old.dds": b"o"})
    v2 = mod_zip(tmp_path, "Cool Mod 1.3-555-1-3-1700000100.zip", {"Cool.esp": esp(), "textures/new.dds": b"n"})
    assert main(["install", str(v1), "--yes"]) == 0
    assert main(["install", str(v2), "--yes"]) == 0
    assert "This updates 'Cool Mod 1.2'" in capsys.readouterr().out
    with Manifest() as m:
        assert len(m.mods("fallout4")) == 1
    assert not os.path.lexists(fo4 / "Data" / "textures" / "old.dds")


def test_install_replace_and_rename(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "Something.zip", {"A.esp": esp(), "textures/old.dds": b"o"})
    b = mod_zip(tmp_path, "Unrelated name.zip", {"A.esp": esp(), "textures/new.dds": b"n"})
    assert main(["install", str(a), "--yes"]) == 0
    assert main(["install", str(b), "--yes", "--replace", "Something", "--name", "My Mod"]) == 0
    out = capsys.readouterr().out
    assert "This updates 'Something'" in out and "renamed to 'My Mod'" in out
    with Manifest() as m:
        assert [x.name for x in m.mods("fallout4")] == ["My Mod"]
    assert (fo4 / "Data" / "textures" / "new.dds").is_file()
    assert not os.path.lexists(fo4 / "Data" / "textures" / "old.dds")


def test_install_hints(home, fo4, tmp_path, capsys):
    empty = mod_zip(tmp_path, "empty.zip", {})
    readme = mod_zip(tmp_path, "readme.zip", {"readme.txt": b"hi"})
    for archive in (empty, readme):
        assert main(["install", str(archive), "--yes"]) == 1
        err = capsys.readouterr().err
        assert "no files to install" in err and "--target" not in err
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"01 Main/A.esp": esp()})
    assert main(["install", str(a), "--yes", "--root", "03 Missing"]) == 1
    err = capsys.readouterr().err
    assert "not a folder" in err and "--target" not in err


def test_install_current_game_is_not_called_requested(home, fo4, tmp_path, capsys):
    tex = mod_zip(tmp_path, "tex.zip", {"textures/x.dds": b"x"})
    assert main(["install", str(tex), "--yes"]) == 0
    assert "as requested" not in capsys.readouterr().out


def test_hardlink_across_filesystems_is_caught_before_changing_anything(home, fo4, tmp_path, capsys, monkeypatch):
    from bethmm import deploy
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    monkeypatch.setattr(deploy, "_same_dev", lambda x, y: False)
    capsys.readouterr()
    assert main(["override", "fallout4", "--link-mode", "hardlink"]) == 0
    assert "same filesystem" in capsys.readouterr().err
    for command in (["disable", "A"], ["disable", "A", "--dry-run"], ["uninstall", "A", "--yes"]):
        assert main(command) == 1
        assert "same filesystem" in capsys.readouterr().err
    with Manifest() as m:
        assert m.find_mod("fallout4", "A").enabled
    assert (fo4 / "Data" / "A.esp").is_symlink()


def test_verify_reports_held_originals_and_left_alone_files(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    link = fo4 / "Data" / "textures" / "vanilla.dds"
    link.unlink()
    link.write_bytes(b"patched by a game update")
    assert main(["deploy"]) == 0
    capsys.readouterr()
    assert main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "original kept in the backups" in out and "left alone" in out
    assert main(["verify", "--repair"]) == 0
    assert "--release-held" in capsys.readouterr().out
    assert main(["purge", "--all", "--yes"]) == 0
    capsys.readouterr()
    # Only a held original is left: safe to uninstall the app, and said so.
    assert main(["status", "--deployed-only"]) == 0
    assert "kept in the backups" in capsys.readouterr().out
    assert main(["verify", "--repair", "--release-held"]) == 0
    with Manifest() as m:
        assert not m.has_footprint("fallout4", dirs=False)
    assert link.read_bytes() == b"patched by a game update"
    kept = list((paths.backups_dir("fallout4") / ".superseded").rglob("vanilla.dds"))
    assert [k.read_bytes() for k in kept] == [b"vanilla"]


def test_status_deployed_only_lists_what_is_left(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["status", "--deployed-only"]) == 0
    assert main(["install", str(a), "--yes"]) == 0
    capsys.readouterr()
    assert main(["status", "--deployed-only"]) == 3
    out = capsys.readouterr().out
    assert "2 link(s)" in out and "1 original game file(s)" in out and "purge" in out
    assert main(["purge", "--yes"]) == 0
    assert main(["status", "--deployed-only"]) == 0


def test_purge_after_the_game_folder_is_gone(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    shutil.rmtree(fo4)
    assert main(["games", "--rescan"]) == 0
    capsys.readouterr()
    assert main(["purge", "--all", "--yes"]) == 1  # maybe just not mounted: refuses
    assert "--forget-missing" in capsys.readouterr().out
    assert main(["status", "--deployed-only"]) == 3
    assert main(["purge", "--all", "--yes", "--forget-missing"]) == 0
    out = capsys.readouterr().out
    assert "superseded" in out
    assert main(["status", "--deployed-only"]) == 0


def test_check_links_and_config_in_dry_run_mode(home, fo4, capsys):
    assert main(["config", "--dry-run", "on"]) == 0
    assert "--rescan" not in capsys.readouterr().out
    assert main(["check-links"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert sorted(os.listdir(fo4 / "Data")) == ["Fallout4 - Textures1.ba2", "Fallout4.esm", "textures"]
    assert main(["config", "--dry-run", "off"]) == 0


def test_conflicts_files_shows_the_deployed_path(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"Textures/SomeMod/a.dds": b"A"})
    b = mod_zip(tmp_path, "B-2-1-0.zip", {"textures/somemod/A.DDS": b"B"})
    assert main(["install", str(a), "--yes"]) == 0
    assert main(["install", str(b), "--yes"]) == 0
    capsys.readouterr()
    assert main(["conflicts", "--files"]) == 0
    out = capsys.readouterr().out
    assert "Data/textures/SomeMod/a.dds: A < B" in out


# ---------------------------------------------------------------- second review round


def test_dry_run_output_is_printed_after_the_manifest_is_released(home, fo4, tmp_path, monkeypatch):
    """A dry run piped into a pager must not keep other commands waiting."""
    import sqlite3
    import sys
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    seen = []

    class Pager:
        def write(self, text):
            if not seen:
                db = sqlite3.connect(paths.manifest_path(), timeout=0.2, isolation_level=None)
                try:
                    db.execute("BEGIN IMMEDIATE")  # what another bethmm command needs
                    db.execute("ROLLBACK")
                    seen.append("free")
                except sqlite3.OperationalError:
                    seen.append("locked")
                finally:
                    db.close()
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Pager())
    assert main(["disable", "A", "--dry-run"]) == 0
    assert seen == ["free"]


def test_opening_the_manifest_never_waits_for_a_writer(home, fo4, tmp_path):
    import sqlite3
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    db = sqlite3.connect(paths.manifest_path(), isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    try:
        with Manifest() as m:
            assert m.find_mod("fallout4", "A")
    finally:
        db.execute("ROLLBACK")
        db.close()


def test_interrupted_first_install_is_seen_by_status_and_purge_all(home, fo4, tmp_path, monkeypatch, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    real = Manifest.record_link

    def killed(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Manifest, "record_link", killed)
    assert main(["install", str(a), "--yes"]) == 130
    monkeypatch.setattr(Manifest, "record_link", real)
    assert (fo4 / "Data" / "A.esp").is_symlink()
    capsys.readouterr()
    assert main(["status", "--deployed-only"]) == 3
    assert "interrupted" in capsys.readouterr().out
    assert main(["purge", "--all", "--yes"]) == 0
    assert not os.path.lexists(fo4 / "Data" / "A.esp")
    assert main(["status", "--deployed-only"]) == 0


def test_verify_repair_on_a_purged_game_puts_back_a_freed_original(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    link = fo4 / "Data" / "textures" / "vanilla.dds"
    link.unlink()
    link.write_bytes(b"patched by a game update")
    assert main(["purge", "--yes"]) == 0
    link.unlink()
    capsys.readouterr()
    assert main(["verify"]) == 1
    assert "not back in the game folder" in capsys.readouterr().out
    assert main(["verify", "--repair"]) == 0
    assert link.read_bytes() == b"vanilla"


def test_verify_with_the_game_folder_missing(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    away = fo4.with_name("away")
    os.rename(fo4, away)
    capsys.readouterr()
    assert main(["verify", "--repair"]) == 1
    assert "can't be used" in capsys.readouterr().out
    with Manifest() as m:
        assert len(m.deployed("fallout4")) == 1
    os.rename(away, fo4)
    assert main(["verify"]) == 0


def test_install_name_on_update_of_a_plain_archive_renames(home, fo4, tmp_path, capsys):
    v1 = mod_zip(tmp_path, "mymod.zip", {"MyMod.esp": esp(), "textures/a.dds": b"1"})
    assert main(["install", str(v1), "--yes"]) == 0
    (tmp_path / "new").mkdir()
    v2 = mod_zip(tmp_path / "new", "mymod.zip", {"MyMod.esp": esp(), "textures/a.dds": b"2"})
    assert main(["install", str(v2), "--yes", "--name", "My Mod"]) == 0
    with Manifest() as m:
        assert [x.name for x in m.mods("fallout4")] == ["My Mod"]
    assert (fo4 / "Data" / "textures" / "a.dds").read_bytes() == b"2"


def test_same_generic_name_without_shared_files_is_a_separate_mod(home, fo4, tmp_path, capsys):
    one = mod_zip(tmp_path, "main.zip", {"One.esp": esp()})
    assert main(["install", str(one), "--yes"]) == 0
    (tmp_path / "other").mkdir()
    two = mod_zip(tmp_path / "other", "main.zip", {"Two.esp": esp()})
    assert main(["install", str(two), "--yes"]) == 0
    with Manifest() as m:
        assert len(m.mods("fallout4")) == 2
    assert (fo4 / "Data" / "One.esp").is_file() and (fo4 / "Data" / "Two.esp").is_file()


def test_install_dry_run_refuses_unusable_file_names_cleanly(home, fo4, tmp_path, capsys):
    src = tmp_path / "Odd Mod"
    make_tree(src, {"Odd.esp": esp()})
    bad = os.path.join(os.fsencode(src / "textures"), b"\xff.dds")
    os.makedirs(os.path.dirname(bad))
    with open(bad, "wb") as fh:
        fh.write(b"x")
    for extra in (["--dry-run"], ["--yes"]):
        result = main(["install", str(src), *extra])
        err = capsys.readouterr().err
        assert result == 1 and "error:" in err and "Traceback" not in err, extra


# ---------------------------------------------------------------- third review round


def test_purge_forget_missing_for_a_game_that_is_no_longer_found(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    shutil.rmtree(home / "Games" / "fallout-4")
    assert main(["games", "--rescan"]) == 0
    capsys.readouterr()
    assert main(["verify", "--game", "fallout4"]) == 1
    assert "can't be used" in capsys.readouterr().out
    assert main(["purge", "--game", "fallout4", "--yes"]) == 1
    assert main(["purge", "--game", "fallout4", "--forget-missing", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would forget" in out.lower() and "nothing would change" not in out.lower()
    assert main(["purge", "--game", "fallout4", "--forget-missing", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Forgot what the app had" in out and "'bethmm deploy' puts them back" not in out
    assert main(["status", "--deployed-only"]) == 0


def test_install_dry_run_with_an_odd_name_matches_the_real_install(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "Good.zip", {"Good.esp": esp()})
    odd = "G" + b"\xff".decode("utf-8", "surrogateescape") + "ood"
    assert main(["install", str(a), "--name", odd, "--dry-run"]) == 0
    assert "Traceback" not in capsys.readouterr().err
    assert main(["install", str(a), "--name", odd, "--yes"]) == 0
    with Manifest() as m:
        assert [x.name for x in m.mods("fallout4")] == ["G�ood"]


def test_interrupted_uninstall_leaves_a_folder_verify_repair_deletes(home, fo4, tmp_path, monkeypatch, capsys):
    from bethmm import staging
    a = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    with Manifest() as m:
        folder = m.find_mod("fallout4", "Cool Mod").staging_dir()

    def killed(path):
        raise KeyboardInterrupt

    with monkeypatch.context() as mp:
        mp.setattr(staging, "remove_staging", killed)
        assert main(["uninstall", "Cool Mod", "--yes"]) == 130
    assert folder.is_dir()
    assert main(["verify", "--repair"]) == 0
    assert not folder.exists()


def test_retexture_with_the_same_generic_name_is_a_separate_mod(home, fo4, tmp_path, capsys):
    main_zip = mod_zip(tmp_path, "main.zip", {"ArmorMod.esp": esp(), "textures/armor/a.dds": b"a",
                                              "textures/armor/b.dds": b"b", "meshes/armor/a.nif": b"n"})
    retex = mod_zip(tmp_path, "main (1).zip", {"textures/armor/a.dds": b"4K"})
    assert main(["install", str(main_zip), "--yes"]) == 0
    capsys.readouterr()
    assert main(["install", str(retex), "--yes"]) == 0
    assert "--replace" in capsys.readouterr().out
    with Manifest() as m:
        assert len(m.mods("fallout4")) == 2
    assert (fo4 / "Data" / "ArmorMod.esp").is_file()
    assert (fo4 / "Data" / "textures" / "armor" / "a.dds").read_bytes() == b"4K"


def test_a_renamed_mod_is_still_updated(home, fo4, tmp_path, capsys):
    for n, files in enumerate(({"CoolMod.esp": esp(), "textures/v1.dds": b"1"},
                               {"CoolMod.esp": esp(), "textures/v2.dds": b"2"},
                               {"CoolMod.esp": esp(), "textures/v3.dds": b"3"}), start=1):
        (tmp_path / f"v{n}").mkdir()
        archive = mod_zip(tmp_path / f"v{n}", "CoolMod.zip", files)
        extra = ["--name", "Cool Mod 2"] if n == 2 else []
        assert main(["install", str(archive), "--yes", *extra]) == 0
    with Manifest() as m:
        assert [x.name for x in m.mods("fallout4")] == ["Cool Mod 2"]
    assert not os.path.lexists(fo4 / "Data" / "textures" / "v2.dds")
    assert (fo4 / "Data" / "textures" / "v3.dds").read_bytes() == b"3"


def test_install_plan_labels_files_of_the_version_it_replaces(home, fo4, tmp_path, capsys):
    v1 = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp()})
    v2 = mod_zip(tmp_path, "Cool Mod-555-2-0-1700000100.zip", {"Cool.esp": esp()})
    assert main(["install", str(v1), "--yes"]) == 0
    capsys.readouterr()
    assert main(["install", str(v2), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "replaces the previous version's file" in out and "stays installed" not in out


# ---------------------------------------------------------------- fourth review round


def test_uninstall_killed_while_deleting_leaves_a_folder_verify_repair_deletes(home, fo4, tmp_path, monkeypatch):
    from bethmm import staging
    a = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/a.dds": b"a"})
    assert main(["install", str(a), "--yes"]) == 0
    real = staging.shutil.rmtree

    def killed(path, *args, **kwargs):
        os.unlink(os.path.join(path, staging.RETIRED_MARKER))  # the mark may go first
        raise KeyboardInterrupt

    with monkeypatch.context() as mp:
        mp.setattr(staging.shutil, "rmtree", killed)
        assert main(["uninstall", "Cool Mod", "--yes"]) == 130
    left = [p for p in paths.mods_dir("fallout4").iterdir()]
    assert len(left) == 1 and left[0].name.endswith(staging.DELETING_SUFFIX)
    monkeypatch.setattr(staging.shutil, "rmtree", real)
    assert main(["verify", "--repair"]) == 0
    assert list(paths.mods_dir("fallout4").iterdir()) == []


def test_retexture_of_half_a_plugin_mod_with_the_same_name_is_separate(home, fo4, tmp_path, capsys):
    one = mod_zip(tmp_path, "main.zip", {"Armor.esp": esp(), "textures/a.dds": b"a"})
    (tmp_path / "other").mkdir()
    two = mod_zip(tmp_path / "other", "main.zip", {"textures/a.dds": b"4K"})
    assert main(["install", str(one), "--yes"]) == 0
    assert main(["install", str(two), "--yes"]) == 0
    with Manifest() as m:
        assert len(m.mods("fallout4")) == 2
    assert (fo4 / "Data" / "Armor.esp").is_file()


# ---------------------------------------------------------------- fifth review round


def test_update_rules_for_plugin_hotfixes_presets_and_renamed_plugins(home, fo4, tmp_path, capsys):
    def install(folder, name, files):
        (tmp_path / folder).mkdir()
        assert main(["install", str(mod_zip(tmp_path / folder, name, files)), "--yes"]) == 0

    install("a1", "main.zip", {"ArmorMod.esp": esp(), "textures/armor/a.dds": b"a", "textures/armor/b.dds": b"b",
                               "meshes/armor/a.nif": b"n"})
    install("a2", "main.zip", {"ArmorMod.esp": esp()})  # a hotfix of the plugin: a mod of its own
    install("b1", "BetterX.zip", {"F4SE/Plugins/BetterX.dll": b"dll", "F4SE/Plugins/BetterX.ini": b"ini"})
    install("b2", "BetterX.zip", {"F4SE/Plugins/BetterX.ini": b"preset"})  # a preset: separate too
    install("c1", "CoolMod.zip", {"CoolMod.esp": esp(), "textures/c1.dds": b"1", "textures/c2.dds": b"2",
                                  "textures/c3.dds": b"3"})
    install("c2", "CoolMod.zip", {"CoolMod.esl": esp(), "textures/c1.dds": b"1", "textures/c2.dds": b"2",
                                  "textures/c3.dds": b"3"})  # the plugin became .esl: an update
    with Manifest() as m:
        names = [x.name for x in m.mods("fallout4")]
    assert names.count("main") == 2 and names.count("BetterX") == 2 and names.count("CoolMod") == 1
    assert not os.path.lexists(fo4 / "Data" / "CoolMod.esp") and (fo4 / "Data" / "CoolMod.esl").is_file()


def test_status_names_the_real_reason_a_folder_cant_be_used(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"textures/vanilla.dds": b"mod", "A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    away = fo4.with_name("data drive")
    os.rename(fo4 / "Data", away)
    (fo4 / "Data").mkdir()
    capsys.readouterr()
    assert main(["status", "--deployed-only"]) == 3
    assert "Data folder" in capsys.readouterr().out
    (fo4 / "Data").rmdir()
    os.rename(away, fo4 / "Data")


def test_verify_repair_leaves_links_in_another_copy_alone(home, fo4, tmp_path, capsys):
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})
    assert main(["install", str(a), "--yes"]) == 0
    assert main(["purge", "--yes"]) == 0
    copy = home / "Games" / "fallout-4-copy"
    shutil.copytree(home / "Games" / "fallout-4", copy, symlinks=True)
    (copy / "drive_c" / "GOG Games" / "Fallout 4" / "Data" / "A.esp").symlink_to(
        paths.mods_dir("fallout4") / "A" / "A.esp")
    assert main(["override", "fallout4", "--root", str(copy / "drive_c" / "GOG Games" / "Fallout 4")]) == 0
    with Manifest() as m:
        before = m.game_state("fallout4").root
    for _ in range(2):
        main(["verify", "--repair"])
    assert (copy / "drive_c" / "GOG Games" / "Fallout 4" / "Data" / "A.esp").is_symlink()
    with Manifest() as m:
        assert m.game_state("fallout4").root == before
