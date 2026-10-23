"""The GUI's FOMOD installer wizard and Plugins tab, headless."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QMimeData, QModelIndex, Qt  # noqa: E402

import test_cli_mods as climods  # noqa: E402
import test_fomod_install as fi  # noqa: E402
import test_gui as tg  # noqa: E402
from bethmm import fomod, service, staging  # noqa: E402
from bethmm.gui.dialogs import FomodDialog, InstallDialog  # noqa: E402
from bethmm.gui.models import PCOL_ACTIVE, PLUGIN_MIME_TYPE  # noqa: E402
from bethmm.manifest import Manifest  # noqa: E402

pytestmark = climods.pytestmark
home, fo4 = climods.home, climods.fo4
answers, window, idle, two_mods = tg.answers, tg.window, tg.idle, tg.two_mods
mod_zip, esp = climods.mod_zip, climods.esp


def lines(home) -> list[str]:
    path = home / "Games" / "fallout-4" / "drive_c" / "users" / climods.USER / "AppData" / "Local" / "Fallout4" / "plugins.txt"
    return path.read_bytes().decode("cp1252").split("\r\n")[:-1]


# ---------------------------------------------------------------- the FOMOD wizard


@pytest.fixture
def prepared(home, fo4, tmp_path):
    p = staging.prepare(fi.cool_zip(tmp_path))
    yield p
    staging.discard(p)


def test_the_wizard_walks_through_the_steps(qtbot, prepared):
    wizard = fomod.Wizard(service.fomod_module(prepared))
    dialog = FomodDialog(wizard, prepared)
    qtbot.addWidget(dialog)
    assert "Step 1 of 2" in dialog.step_label.text()
    two_k, four_k = dialog.widgets[0]
    assert two_k.isChecked() and "recommended" in two_k.text()
    four_k.setChecked(True)
    assert wizard.selection(0) == {0: [1]}
    assert dialog.next_button.text() == "Next" and dialog.next_button.isEnabled()
    dialog.next()
    assert "Step 2 of 2:</b> Patches" in dialog.step_label.text() and dialog.next_button.text() == "Install"
    dialog.widgets[0][0].setChecked(True)
    dialog.back()
    assert dialog.widgets[0][1].isChecked()  # the choice made is still there
    dialog.next()
    dialog.next()
    assert dialog.result() == dialog.DialogCode.Accepted
    out = wizard.outcome(prepared.files)
    assert {m.dest for m in out.mappings} == {"Data/Cool.esp", "Data/textures/cool/b.dds", "Data/CoolUSO.esp"}


def test_a_group_left_without_a_required_choice_holds_next(qtbot, prepared):
    data = fi.CONFIG.replace('type="SelectExactlyOne"', 'type="SelectAtLeastOne"').encode("utf-16")
    wizard = fomod.Wizard(fomod.parse_module(data, "Cool Mod"))
    dialog = FomodDialog(wizard, prepared)
    qtbot.addWidget(dialog)
    dialog.widgets[0][0].setChecked(False)
    assert not dialog.next_button.isEnabled() and "at least one" in dialog.problem_label.text()


def test_installing_a_fomod_archive_through_the_window(qtbot, window, answers, tmp_path, fo4, home):
    def four_k(dialog) -> None:
        if isinstance(dialog, FomodDialog):
            dialog.widgets[0][1].setChecked(True)

    answers.before_accept = four_k
    window.install_paths([str(fi.cool_zip(tmp_path))])
    idle(qtbot, window)
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"4k" * 1000
    assert [type(d) for d in answers.dialogs] == [FomodDialog, InstallDialog]
    assert fi.staged_files() == ["Cool Mod/00 Core/Cool.esp", "Cool Mod/01 Options/4K/textures/cool/b.dds"]
    assert "plugins.txt: added Cool.esp" in window.messages.toPlainText()
    assert lines(home) == ["*Cool.esp"]


def test_cancelling_the_installer_installs_nothing(qtbot, window, answers, tmp_path, fo4):
    answers.before_accept = lambda dialog: setattr(answers, "accept", not isinstance(dialog, FomodDialog))
    window.install_paths([str(fi.cool_zip(tmp_path))])
    idle(qtbot, window)
    assert [type(d) for d in answers.dialogs] == [FomodDialog]
    with Manifest() as m:
        assert m.mods("fallout4") == []


def test_the_install_dialog_runs_the_installer_again(qtbot, window, answers, tmp_path, fo4):
    seen = []

    def again(dialog) -> None:
        seen.append(type(dialog).__name__)
        if isinstance(dialog, InstallDialog) and seen.count("InstallDialog") == 1:
            assert dialog.fomod_button.isVisible() or not dialog.isVisible()
            dialog.run_installer_again()
        elif isinstance(dialog, FomodDialog) and seen.count("FomodDialog") == 2:
            dialog.widgets[0][1].setChecked(True)  # the second time: 4K

    answers.before_accept = again
    window.install_paths([str(fi.cool_zip(tmp_path))])
    idle(qtbot, window)
    assert seen == ["FomodDialog", "InstallDialog", "FomodDialog"]
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"4k" * 1000


def test_an_update_offers_the_installers_previous_choices(qtbot, window, answers, tmp_path, fo4):
    answers.before_accept = lambda d: d.widgets[0][1].setChecked(True) if isinstance(d, FomodDialog) else None
    window.install_paths([str(fi.cool_zip(tmp_path))])
    idle(qtbot, window)
    answers.before_accept = None
    answers.dialogs.clear()
    window.install_paths([str(fi.cool_zip(tmp_path, "1-1", "1700000001"))])
    idle(qtbot, window)
    fomod_dialog = answers.dialogs[0]
    assert isinstance(fomod_dialog, FomodDialog) and fomod_dialog.widgets[0][1].isChecked()
    assert "preselected" in window.messages.toPlainText()
    assert (fo4 / "Data" / "textures" / "cool" / "b.dds").read_bytes() == b"4k" * 1000


def test_a_csharp_installer_opens_the_install_dialog_to_choose_folders(qtbot, window, answers, tmp_path, fo4):
    archive = mod_zip(tmp_path, "Old-55-1-0.zip", {"fomod/script.cs": b"class Script {}", "Core/old.esp": esp()})
    answers.accept = False
    window.install_paths([str(archive)])
    idle(qtbot, window)
    assert [type(d) for d in answers.dialogs] == [InstallDialog]
    assert answers.dialogs[0].choose_box.isVisibleTo(answers.dialogs[0])
    assert "C# script installer" in window.messages.toPlainText()


# ---------------------------------------------------------------- the Plugins tab


def plugin_row(window, name: str) -> int:
    row = window.plugin_model.row_of(name)
    assert row is not None, [p.name for p in window.plugin_model.rows]
    return row


def test_the_plugins_tab_switches_and_moves_plugins(qtbot, window, answers, tmp_path, fo4, home):
    two_mods(tmp_path)
    window.refresh()
    names = [p.name for p in window.plugin_model.rows]
    assert names == ["Fallout4.esm", "ArmorPack.esp", "BetterShared.esp"]
    assert window.lists.tabText(1) == "Plugins (3)"
    model = window.plugin_model
    model.setData(model.index(plugin_row(window, "ArmorPack.esp"), PCOL_ACTIVE), Qt.CheckState.Unchecked.value,
                  Qt.ItemDataRole.CheckStateRole)
    idle(qtbot, window)
    assert lines(home) == ["ArmorPack.esp", "*BetterShared.esp"]
    data = QMimeData()
    data.setData(PLUGIN_MIME_TYPE, b"BetterShared.esp")
    model.dropMimeData(data, Qt.DropAction.MoveAction, plugin_row(window, "ArmorPack.esp"), 0, QModelIndex())
    idle(qtbot, window)
    assert lines(home) == ["*BetterShared.esp", "ArmorPack.esp"]
    assert not model.flags(model.index(0, PCOL_ACTIVE)) & Qt.ItemFlag.ItemIsUserCheckable  # Fallout4.esm


def test_dry_run_mode_shows_the_plugins_change_and_writes_nothing(qtbot, window, answers, tmp_path, fo4, home):
    two_mods(tmp_path)
    before = lines(home)
    assert climods.main(["config", "--dry-run", "on"]) == 0
    window.refresh()
    model = window.plugin_model
    model.setData(model.index(plugin_row(window, "ArmorPack.esp"), PCOL_ACTIVE), Qt.CheckState.Unchecked.value,
                  Qt.ItemDataRole.CheckStateRole)
    idle(qtbot, window)
    assert lines(home) == before
    assert any("would switch ArmorPack.esp off" in text for _, text, _w in answers.told)


def test_a_missing_master_is_pointed_out(qtbot, window, tmp_path, fo4):
    archive = mod_zip(tmp_path, "A-1-1-0.zip", {"A.esp": esp("Fallout4.esm", "Needed.esm")})
    assert climods.main(["install", str(archive), "--yes"]) == 0
    window.refresh()
    assert "need a master" in window.plugins_info.text()
    assert "needs Needed.esm" in window.plugin_model.notes(window.plugin_model.rows[plugin_row(window, "A.esp")])


def test_the_plugins_tab_waits_while_a_job_runs(qtbot, window, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    assert window.plugin_table.isEnabled() and window.act_sync_plugins.isEnabled()
    window._installing = True  # as while an install is under way
    window._update_actions()
    assert not window.plugin_table.isEnabled() and not window.act_sync_plugins.isEnabled()
    window._installing = False
    window._update_actions()
    assert window.plugin_table.isEnabled() and window.act_sync_plugins.isEnabled()
