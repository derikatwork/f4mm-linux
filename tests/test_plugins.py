"""plugins.txt: reading and writing both formats, and keeping it in line with
the plugins the app deploys (added, taken out and put back in place, switched,
moved, dated for games whose load order goes by file dates)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import test_cli_mods as climods
from bethmm import paths, plugins
from bethmm.cli import main
from bethmm.games import PluginsTxtFormat
from bethmm.manifest import Manifest
from helpers import make_plugin, make_tree

home, fo4 = climods.home, climods.fo4
mod_zip, esp = climods.mod_zip, climods.esp
pytestmark = climods.pytestmark
ASTERISK, PLAIN = PluginsTxtFormat.ASTERISK, PluginsTxtFormat.PLAIN


def plugins_txt(home: Path, folder: str = "Fallout4", prefix: str = "fallout-4") -> Path:
    return home / "Games" / prefix / "drive_c" / "users" / climods.USER / "AppData" / "Local" / folder / "plugins.txt"


def lines(path: Path) -> list[str]:
    return path.read_bytes().decode("cp1252").split("\r\n")[:-1]


# ---------------------------------------------------------------- the file


def test_reading_and_writing_keeps_comments_encoding_and_line_endings():
    data = "# This file is used by Fallout4.\r\n*A.esp\r\nB.esp\r\n*Café.esp\r\nnot a plugin\r\n*A.esp\r\n".encode("cp1252")
    pf = plugins.parse(data, ASTERISK)
    assert [(e.name, e.active) for e in pf.entries] == [("A.esp", True), ("B.esp", False), ("Café.esp", True)]
    assert pf.encoding == "cp1252" and pf.newline == "\r\n"
    assert plugins.render(pf) == data  # a duplicate and a stray line are kept as they were
    plain = plugins.parse(b"FalloutNV.esm\nMod.esp\n", PLAIN)
    assert [(e.name, e.active) for e in plain.entries] == [("FalloutNV.esm", True), ("Mod.esp", True)]
    assert plugins.render(plain) == b"FalloutNV.esm\nMod.esp\n"
    utf8 = plugins.parse("*Ünïcødé ✓.esp\n".encode("utf-8"), ASTERISK)
    assert utf8.encoding == "utf-8" and plugins.render(utf8) == "*Ünïcødé ✓.esp\n".encode("utf-8")


# ---------------------------------------------------------------- Fallout 4: the asterisk format


def test_deployed_plugins_are_added_and_the_users_lines_kept(home, fo4, tmp_path, capsys):
    txt = plugins_txt(home)
    txt.parent.mkdir()
    txt.write_bytes(b"# made by the game\r\n*Unofficial.esp\r\nOld.esp\r\n")
    (fo4 / "Data" / "Unofficial.esp").write_bytes(esp())
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp(), "AMaster.esm": make_plugin(("Fallout4.esm",), flags=1)})
    assert main(["install", str(a), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "plugins.txt: added A.esp, AMaster.esm" in out and "previous plugins.txt is kept" in out
    assert lines(txt) == ["# made by the game", "*AMaster.esm", "*Unofficial.esp", "Old.esp", "*A.esp"]
    backups = list(paths.plugins_backups_dir("fallout4").iterdir())
    assert any(b.read_bytes() == b"# made by the game\r\n*Unofficial.esp\r\nOld.esp\r\n" for b in backups)


def test_a_disabled_mods_plugin_comes_out_and_goes_back_to_its_place(home, fo4, tmp_path, capsys):
    for name in ("A", "B", "C"):
        assert main(["install", str(mod_zip(tmp_path, f"{name}-1-1-0.zip", {f"{name}.esp": esp()})), "--yes"]) == 0
    txt = plugins_txt(home)
    assert lines(txt) == ["*A.esp", "*B.esp", "*C.esp"]
    assert main(["plugins", "move", "C", "--top"]) == 0
    assert main(["plugins", "disable", "B"]) == 0
    assert lines(txt) == ["*C.esp", "*A.esp", "B.esp"]
    assert main(["disable", "A"]) == 0
    assert lines(txt) == ["*C.esp", "B.esp"]
    assert main(["enable", "A"]) == 0
    assert lines(txt) == ["*C.esp", "*A.esp", "B.esp"]
    # A purge takes them all out; deploy puts them back in the same order and state.
    assert main(["purge", "--yes"]) == 0
    assert lines(txt) == [] or not txt.read_bytes().strip()
    assert main(["deploy"]) == 0
    assert lines(txt) == ["*C.esp", "*A.esp", "B.esp"]
    capsys.readouterr()


def test_a_plugin_switched_off_by_hand_stays_off(home, fo4, tmp_path):
    assert main(["install", str(mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})), "--yes"]) == 0
    txt = plugins_txt(home)
    txt.write_bytes(b"A.esp\r\n")  # unticked in the game's launcher
    assert main(["deploy"]) == 0
    assert main(["install", str(mod_zip(tmp_path, "B-2-1-0.zip", {"B.esp": esp()})), "--yes"]) == 0
    assert lines(txt) == ["A.esp", "*B.esp"]


def test_a_dry_run_shows_the_plugins_changes_and_writes_nothing(home, fo4, tmp_path, capsys):
    assert main(["install", str(mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})), "--yes"]) == 0
    txt = plugins_txt(home)
    before = txt.read_bytes()
    capsys.readouterr()
    assert main(["disable", "A", "--dry-run"]) == 0
    assert "plugins.txt: would remove A.esp" in capsys.readouterr().out
    assert main(["install", str(mod_zip(tmp_path, "B-2-1-0.zip", {"B.esp": esp()})), "--dry-run"]) == 0
    assert "plugins.txt: would add B.esp" in capsys.readouterr().out
    assert main(["plugins", "disable", "A", "--dry-run"]) == 0
    assert txt.read_bytes() == before


def test_the_load_order_listing_and_missing_masters(home, fo4, tmp_path, capsys):
    cc = fo4 / "Data" / "ccBGSFO4001-PipBoy(Black).esl"
    cc.write_bytes(make_plugin(("Fallout4.esm",), flags=0x201))
    (fo4 / "Fallout4.ccc").write_text("ccBGSFO4001-PipBoy(Black).esl\r\n")
    a = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp("Fallout4.esm", "Needed.esm")})
    assert main(["install", str(a), "--yes"]) == 0
    capsys.readouterr()
    assert main(["plugins"]) == 1  # a master is missing
    out = capsys.readouterr().out
    assert "[*] M Fallout4.esm" in out and "[*] M ccBGSFO4001-PipBoy(Black).esl" in out
    assert "[x]   A.esp" in out and "needs Needed.esm (not loaded)" in out


def test_without_a_known_prefix_the_deploy_says_plugins_txt_wasnt_updated(home, tmp_path, capsys):
    root = home / "Games" / "fo4" / "Fallout 4"
    make_tree(root, {"Fallout4.exe": b"exe", "Data/Fallout4.esm": make_plugin()})
    assert main(["games", "--rescan"]) == 0
    assert main(["use", "fallout4"]) == 0
    capsys.readouterr()
    assert main(["install", str(mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})), "--yes"]) == 0
    assert "plugins.txt wasn't updated: the game's Wine prefix" in capsys.readouterr().out
    assert main(["plugins", "disable", "A"]) == 1


def test_plugins_txt_is_backed_up_before_every_write_and_old_copies_pruned(home, fo4, tmp_path):
    txt = plugins_txt(home)
    txt.parent.mkdir()
    txt.write_bytes(b"*Mine.esp\r\n")
    assert main(["install", str(mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp()})), "--yes"]) == 0
    for _ in range(plugins.KEEP_BACKUPS + 3):
        assert main(["plugins", "disable", "A"]) == 0
        assert main(["plugins", "enable", "A"]) == 0
    copies = sorted(paths.plugins_backups_dir("fallout4").iterdir())
    assert (paths.plugins_backups_dir("fallout4") / "plugins-first.txt").read_bytes() == b"*Mine.esp\r\n"
    assert len(copies) == plugins.KEEP_BACKUPS + 1


def test_a_users_own_copy_of_a_plugin_keeps_its_line(home, fo4, tmp_path):
    (fo4 / "Data" / "Shared.esp").write_bytes(esp())  # the user's own file, replaced by the mod's
    assert main(["install", str(mod_zip(tmp_path, "S-1-1-0.zip", {"Shared.esp": esp()})), "--yes"]) == 0
    txt = plugins_txt(home)
    assert lines(txt) == ["*Shared.esp"]
    assert main(["uninstall", "S", "--yes"]) == 0
    assert (fo4 / "Data" / "Shared.esp").is_file() and lines(txt) == ["*Shared.esp"]  # the original is back


# ---------------------------------------------------------------- New Vegas: plain list, load order by date


@pytest.fixture
def fnv(home) -> Path:
    prefix = home / "Games" / "fallout-new-vegas"
    make_tree(prefix, ["system.reg", "user.reg", "drive_c/users/Public/", f"drive_c/users/{climods.USER}/AppData/Local/"])
    root = prefix / "drive_c" / "GOG Games" / "Fallout New Vegas"
    make_tree(root, {
        "FalloutNV.exe": b"exe",
        "goggame-1.info": b"{}",
        "Data/FalloutNV.esm": make_plugin(hedr_version=1.34, flags=1),
        "Data/DeadMoney.esm": make_plugin(("FalloutNV.esm",), hedr_version=1.34, flags=1),
    })
    old = 1_300_000_000
    os.utime(root / "Data" / "FalloutNV.esm", (old, old))
    os.utime(root / "Data" / "DeadMoney.esm", (old + 10, old + 10))
    assert main(["use", "falloutnv"]) == 0
    return root


def nv_esp(*masters: str) -> bytes:
    return make_plugin(masters or ("FalloutNV.esm",), hedr_version=1.34)


def test_plain_list_with_the_games_masters_and_dates_in_order(home, fnv, tmp_path, capsys):
    for name in ("A", "B"):
        archive = mod_zip(tmp_path, f"{name}-1-1-0.zip", {f"{name}.esp": nv_esp()})
        assert main(["install", str(archive), "--yes", "--game", "falloutnv"]) == 0
    txt = plugins_txt(home, "FalloutNV", "fallout-new-vegas")
    assert lines(txt) == ["FalloutNV.esm", "DeadMoney.esm", "A.esp", "B.esp"]
    a, b = (os.stat(fnv / "Data" / f"{n}.esp").st_mtime for n in "AB")
    assert 1_300_000_010 < a < b
    assert main(["plugins", "move", "B", "--before", "A"]) == 0
    a, b = (os.stat(fnv / "Data" / f"{n}.esp").st_mtime for n in "AB")
    assert b < a and lines(txt)[2:] == ["B.esp", "A.esp"]
    assert os.stat(fnv / "Data" / "FalloutNV.esm").st_mtime == 1_300_000_000  # never the game's own
    capsys.readouterr()
    assert main(["plugins", "move", "FalloutNV.esm", "--bottom"]) == 1
    assert "only changes the dates of the plugins it installed" in capsys.readouterr().err
    assert main(["plugins", "disable", "A"]) == 0
    assert lines(txt) == ["FalloutNV.esm", "DeadMoney.esm", "B.esp"]
    assert main(["deploy"]) == 0
    assert "A.esp" not in lines(txt)  # switched off stays off
    assert main(["plugins", "enable", "A"]) == 0
    assert lines(txt)[-1] == "A.esp"


def test_dating_a_hardlinked_plugin_keeps_verify_happy(home, fnv, tmp_path, capsys):
    assert main(["override", "falloutnv", "--link-mode", "hardlink"]) == 0
    archive = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": nv_esp()})
    assert main(["install", str(archive), "--yes", "--game", "falloutnv"]) == 0
    assert os.stat(fnv / "Data" / "A.esp").st_nlink == 2 and os.stat(fnv / "Data" / "A.esp").st_mtime > 1_300_000_010
    capsys.readouterr()
    assert main(["verify"]) == 0
    with Manifest() as m:
        record = m.deployed("falloutnv")["DATA/A.ESP"]
    assert record.mtime_ns == os.stat(fnv / "Data" / "A.esp").st_mtime_ns


def test_a_mod_without_plugins_doesnt_create_plugins_txt(home, fnv, tmp_path):
    archive = mod_zip(tmp_path, "Tex-1-1-0.zip", {"textures/x.dds": b"x"})
    assert main(["install", str(archive), "--yes", "--game", "falloutnv"]) == 0
    assert not plugins_txt(home, "FalloutNV", "fallout-new-vegas").exists()
