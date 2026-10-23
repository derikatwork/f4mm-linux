"""Installing a FOMOD installer archive from the command line: the installer
runs (asking in a terminal, or with its defaults / the choices remembered for
an update), only the chosen files are kept, and the choices are recorded."""

from __future__ import annotations

import os

import pytest

import test_cli_mods as climods
from bethmm import paths
from bethmm.cli import main
from bethmm.manifest import Manifest

home, fo4 = climods.home, climods.fo4
mod_zip, esp = climods.mod_zip, climods.esp

CONFIG = r"""<?xml version="1.0" encoding="UTF-16"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <moduleName>Cool Mod</moduleName>
  <requiredInstallFiles><folder source="00 Core" destination="" /></requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Textures">
      <optionalFileGroups order="Explicit">
        <group name="Resolution" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="2K">
              <description>Smaller textures.</description>
              <files><folder source="01 Options\2K" destination="" /></files>
              <conditionFlags><flag name="res">2k</flag></conditionFlags>
              <typeDescriptor><type name="Recommended" /></typeDescriptor>
            </plugin>
            <plugin name="4K">
              <description>Bigger textures.</description>
              <files><folder source="01 Options\4K" destination="" /></files>
              <conditionFlags><flag name="res">4k</flag></conditionFlags>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Patches">
      <optionalFileGroups>
        <group name="Patches" type="SelectAny">
          <plugins>
            <plugin name="USO patch">
              <description>For the unofficial patch.</description>
              <files><file source="02 Patches\CoolUSO.esp" destination="CoolUSO.esp" /></files>
              <typeDescriptor><dependencyType><defaultType name="Optional" /><patterns><pattern>
                <dependencies><fileDependency file="Unofficial.esp" state="Active" /></dependencies>
                <type name="Recommended" />
              </pattern></patterns></dependencyType></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>"""


def cool_zip(tmp_path, version: str = "1-0", stamp: str = "1700000000"):
    return mod_zip(tmp_path, f"Cool Mod-4242-{version}-{stamp}.zip", {
        "Cool Mod/fomod/ModuleConfig.xml": CONFIG.encode("utf-16"),
        "Cool Mod/fomod/info.xml": b"<fomod><Name>Cool Mod</Name><Version>1.0</Version></fomod>",
        "Cool Mod/00 Core/Cool.esp": esp(),
        "Cool Mod/01 Options/2K/textures/cool/b.dds": b"2k",
        "Cool Mod/01 Options/4K/textures/cool/b.dds": b"4k" * 1000,
        "Cool Mod/02 Patches/CoolUSO.esp": esp(),
    })


def staged_files(name: str = "Cool Mod") -> list[str]:
    with Manifest() as m:
        mod = m.find_mod("fallout4", name)
    root = mod.staging_dir()
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def answers(monkeypatch, *replies: str) -> list[str]:
    """The user's answers to the installer's questions (and the final y/N)."""
    asked: list[str] = []
    queue = list(replies)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)

    def fake_input(prompt=""):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def test_with_yes_the_installers_defaults_are_installed_and_only_those_files_kept(home, fo4, tmp_path, capsys):
    assert main(["install", str(cool_zip(tmp_path)), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "FOMOD installer: Cool Mod, version 1.0" in out and "default choices" in out
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"2k"
    assert (fo4 / "Data" / "Cool.esp").is_symlink() and not os.path.lexists(fo4 / "Data" / "CoolUSO.esp")
    assert staged_files() == ["Cool Mod/00 Core/Cool.esp", "Cool Mod/01 Options/2K/textures/cool/b.dds"]
    with Manifest() as m:
        plan = m.find_mod("fallout4", "Cool Mod").plan
    assert plan["rule"] == "fomod" and plan["fomod"]["choices"][0] == {
        "step": "Textures", "groups": [{"group": "Resolution", "options": ["2K"]}]}


def test_in_a_terminal_each_step_is_asked(home, fo4, tmp_path, capsys, monkeypatch):
    asked = answers(monkeypatch, "3", "1 2", "2", "b", "", "1", "y")  # bad, two, 4K, back, keep 4K, patch, yes
    assert main(["install", str(cool_zip(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "Step 1 of 2: Textures" in out and "(*) 2K  [recommended] - Smaller textures." in out
    assert "! no such option" in out and "! choose exactly one option in Resolution" in out
    assert len(asked) == 7
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"4k" * 1000
    assert (fo4 / "Data" / "CoolUSO.esp").is_symlink()
    assert "you ran its FOMOD installer and chose '4K', 'USO patch'" in out


def test_quitting_the_installer_installs_nothing(home, fo4, tmp_path, capsys, monkeypatch):
    answers(monkeypatch, "q")
    assert main(["install", str(cool_zip(tmp_path))]) == 1
    assert "Cancelled." in capsys.readouterr().out
    with Manifest() as m:
        assert m.mods("fallout4") == []


def test_an_update_offers_the_choices_made_last_time(home, fo4, tmp_path, capsys, monkeypatch):
    answers(monkeypatch, "2", "-", "y")
    assert main(["install", str(cool_zip(tmp_path))]) == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    capsys.readouterr()
    assert main(["install", str(cool_zip(tmp_path, "1-1", "1700000001")), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "the choices made when 'Cool Mod' was installed are preselected" in out and "updates" in out
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"4k" * 1000
    with Manifest() as m:
        assert len(m.mods("fallout4")) == 1


def test_a_condition_on_an_active_plugin_is_answered_from_plugins_txt(home, fo4, tmp_path, capsys):
    (fo4 / "Data" / "Unofficial.esp").write_bytes(esp())
    appdata = home / "Games" / "fallout-4" / "drive_c" / "users" / climods.USER / "AppData" / "Local" / "Fallout4"
    appdata.mkdir(parents=True)
    (appdata / "plugins.txt").write_text("# made by the game\r\n*Unofficial.esp\r\n")
    assert main(["games", "--rescan"]) == 0
    assert main(["install", str(cool_zip(tmp_path)), "--yes"]) == 0
    assert (fo4 / "Data" / "CoolUSO.esp").is_symlink()  # recommended, so preselected


def test_a_dry_run_runs_the_installer_and_changes_nothing(home, fo4, tmp_path, capsys):
    before = climods.snapshot(fo4)
    assert main(["install", str(cool_zip(tmp_path)), "--yes", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "01 Options/2K/textures/cool/b.dds  ->  Data/textures/cool/b.dds" in out
    assert climods.snapshot(fo4) == before
    assert not paths.manifest_path().exists() or not Manifest().mods("fallout4")


def test_a_csharp_installer_falls_back_to_choosing_folders(home, fo4, tmp_path, capsys):
    archive = mod_zip(tmp_path, "Old-55-1-0.zip", {"fomod/script.cs": b"class Script {}", "Core/old.esp": esp()})
    assert main(["install", str(archive), "--yes"]) == 1
    assert "C# script installer" in capsys.readouterr().err
    assert main(["install", str(archive), "--yes", "--root", "Core"]) == 0


@pytest.mark.parametrize("config", [b"<config><installSteps><broken></config>", b"\xff\xfe\x00"])
def test_a_broken_installer_says_so(home, fo4, tmp_path, capsys, config):
    archive = mod_zip(tmp_path, "Bad-56-1-0.zip", {"fomod/ModuleConfig.xml": config, "Core/bad.esp": esp()})
    assert main(["install", str(archive), "--yes"]) == 1
    assert "--root" in capsys.readouterr().err
