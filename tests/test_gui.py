"""The GUI against a fake game, headless: the window lists mods, ticking and
dragging change the mod list, and install / purge / deploy / uninstall /
verify go through their dialogs (answered by the test) to the same
operations the command line uses."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QMimeData, QModelIndex, Qt  # noqa: E402

import test_cli_mods as climods  # noqa: E402
from bethmm import paths  # noqa: E402
from bethmm.cli import main  # noqa: E402
from bethmm.gui.dialogs import InstallDialog, PlanDialog, VerifyDialog  # noqa: E402
from bethmm.gui.main_window import MainWindow  # noqa: E402
from bethmm.gui.models import COL_ENABLED, MIME_TYPE  # noqa: E402
from bethmm.manifest import Manifest  # noqa: E402
from bethmm.placement import Target  # noqa: E402
from bethmm.settings import load_settings  # noqa: E402

pytestmark = climods.pytestmark
home, fo4 = climods.home, climods.fo4
mod_zip, esp = climods.mod_zip, climods.esp


class Answers:
    """What the 'user' answers: every dialog is accepted when it can be,
    unless the test says otherwise."""

    def __init__(self) -> None:
        self.dialogs: list = []
        self.accept = True
        self.before_accept = None  # callable(dialog) run before answering
        self.told: list[tuple[str, str, bool]] = []
        self.asked: list[str] = []
        self.ask_answer = True

    def run_dialog(self, dialog) -> bool:
        self.dialogs.append(dialog)
        if self.before_accept is not None:
            self.before_accept(dialog)
        if not self.accept:
            return False
        if isinstance(dialog, InstallDialog):
            return dialog.install_button.isEnabled()
        if isinstance(dialog, PlanDialog):
            return hasattr(dialog, "apply_button")
        if isinstance(dialog, VerifyDialog):
            return dialog.repair_button is not None
        return True

    def ask(self, title: str, text: str) -> bool:
        self.asked.append(f"{title}: {text}")
        return self.ask_answer

    def tell(self, title: str, text: str, *, warning: bool = False) -> None:
        self.told.append((title, text, warning))


@pytest.fixture
def answers() -> Answers:
    return Answers()


@pytest.fixture
def window(qtbot, home, fo4, answers):
    w = MainWindow(load_settings())
    qtbot.addWidget(w)
    w.run_dialog = answers.run_dialog
    w.ask = answers.ask
    w.tell = answers.tell
    yield w
    w.tasks.wait()


def idle(qtbot, w: MainWindow) -> None:
    qtbot.waitUntil(lambda: not w.busy, timeout=30000)


def two_mods(tmp_path):
    a = mod_zip(tmp_path, "Armor Pack-1111-1-0-1700000000.zip",
                {"ArmorPack.esp": esp(), "textures/shared.dds": b"A", "textures/vanilla.dds": b"A"})
    b = mod_zip(tmp_path, "Better Shared-2222-2-1-1700000000.zip", {"BetterShared.esp": esp(),
                                                                    "textures/SHARED.dds": b"B"})
    assert main(["install", str(a), "--yes"]) == 0
    assert main(["install", str(b), "--yes"]) == 0


def test_window_lists_the_mods_and_their_conflicts(window, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    assert [r.mod.name for r in window.model.rows] == ["Armor Pack", "Better Shared"]
    assert window.model.data(window.model.index(0, 5)) == "loses to 1"
    assert window.model.data(window.model.index(1, 5)) == "wins over 1"
    window.table.selectRow(0)
    files = {window.files_tree.topLevelItem(i).text(0): window.files_tree.topLevelItem(i).text(2)
             for i in range(window.files_tree.topLevelItemCount())}
    assert files == {"ArmorPack.esp": "linked", "textures/shared.dds": "overridden by Better Shared",
                     "textures/vanilla.dds": "linked"}
    assert window.conflicts_tree.topLevelItemCount() == 1
    assert "Armor Pack" in window.info.toPlainText()


def test_unticking_a_mod_disables_it(qtbot, window, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    window.model.setData(window.model.index(0, COL_ENABLED), Qt.CheckState.Unchecked.value,
                         Qt.ItemDataRole.CheckStateRole)
    idle(qtbot, window)
    assert not os.path.lexists(fo4 / "Data" / "ArmorPack.esp")
    assert (fo4 / "Data" / "textures" / "vanilla.dds").read_bytes() == b"vanilla"  # the original is back
    assert window.model.data(window.model.index(0, COL_ENABLED), Qt.ItemDataRole.CheckStateRole) == \
        Qt.CheckState.Unchecked
    assert "Disabled 'Armor Pack'" in window.messages.toPlainText()


def test_dragging_a_mod_reorders_the_list(qtbot, window, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    data = window.model.mimeData([window.model.index(1, 2)])  # Better Shared ...
    window.model.dropMimeData(data, Qt.DropAction.MoveAction, 0, 0, QModelIndex())  # ... to the top
    idle(qtbot, window)
    assert [r.mod.name for r in window.model.rows] == ["Better Shared", "Armor Pack"]
    assert (fo4 / "Data" / "textures" / "shared.dds").read_bytes() == b"A"  # Armor Pack wins now


def test_install_through_the_dialog(qtbot, window, answers, tmp_path, fo4):
    archive = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/c.dds": b"c"})
    window.install_paths([str(archive)])
    idle(qtbot, window)
    (dialog,) = [d for d in answers.dialogs if isinstance(d, InstallDialog)]
    assert dialog.choice is not None and dialog.choice.existing is None
    assert "link" in dialog.changes_text.toPlainText()
    assert (fo4 / "Data" / "Cool.esp").is_symlink()
    assert [r.mod.name for r in window.model.rows] == ["Cool Mod"]
    assert not any(p.name.startswith("install-") for p in paths.tmp_dir().iterdir())


def test_install_of_a_new_version_updates_in_place(qtbot, window, answers, tmp_path, fo4):
    v1 = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp(), "textures/old.dds": b"o"})
    v2 = mod_zip(tmp_path, "Cool Mod-555-2-0-1700000100.zip", {"Cool.esp": esp(), "textures/new.dds": b"n"})
    window.install_paths([str(v1), str(v2)])
    idle(qtbot, window)
    install_dialogs = [d for d in answers.dialogs if isinstance(d, InstallDialog)]
    assert len(install_dialogs) == 2
    assert install_dialogs[1].choice.existing is not None
    assert install_dialogs[1].install_button.text() == "Update"
    assert [r.mod.name for r in window.model.rows] == ["Cool Mod"]
    assert not os.path.lexists(fo4 / "Data" / "textures" / "old.dds")
    assert (fo4 / "Data" / "textures" / "new.dds").is_symlink()


def test_cancelled_install_leaves_nothing(qtbot, window, answers, tmp_path, fo4):
    archive = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp()})
    answers.accept = False
    window.install_paths([str(archive)])
    idle(qtbot, window)
    assert window.model.rows == []
    assert not os.path.lexists(fo4 / "Data" / "Cool.esp")
    assert not any(p.name.startswith("install-") for p in paths.tmp_dir().iterdir())


def test_unclear_archive_lets_the_user_choose_folders(qtbot, window, answers, tmp_path, fo4):
    archive = mod_zip(tmp_path, "Weird Mod.zip", {"00 Core/stuff/a.dds": b"x", "readme.txt": b"r"})

    def choose(dialog) -> None:
        if isinstance(dialog, InstallDialog):
            assert not dialog.install_button.isEnabled()
            top = dialog.folder_tree.topLevelItem(0)
            core = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "00 Core")
            core.setCheckState(0, Qt.CheckState.Checked)
            next(b for b in dialog.target_group.buttons() if b.property("target") == Target.DATA).setChecked(True)
            dialog.apply_choice.click()
            assert dialog.install_button.isEnabled(), dialog.problem_label.text()

    answers.before_accept = choose
    window.install_paths([str(archive)])
    idle(qtbot, window)
    assert (fo4 / "Data" / "stuff" / "a.dds").is_symlink()


def test_purge_then_deploy(qtbot, window, answers, tmp_path, fo4):
    vanilla = climods.snapshot(fo4)
    two_mods(tmp_path)
    window.refresh()
    window.purge()
    idle(qtbot, window)
    assert climods.snapshot(fo4) == vanilla
    assert isinstance(answers.dialogs[-1], PlanDialog)
    window.deploy()
    idle(qtbot, window)
    assert (fo4 / "Data" / "ArmorPack.esp").is_symlink()
    assert "Deployed." in window.messages.toPlainText()


def test_purge_with_the_game_folder_gone_asks_first(qtbot, window, answers, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    away = fo4.with_name("away")
    os.rename(fo4, away)
    answers.ask_answer = False  # "the drive just isn't mounted"
    window.purge()
    idle(qtbot, window)
    assert answers.asked and "gone for good" in answers.asked[-1]
    with Manifest() as m:
        assert m.deployed("fallout4")  # nothing forgotten
    os.rename(away, fo4)


def test_uninstall_the_selected_mod(qtbot, window, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    window.table.selectRow(0)
    folder = window.selected_mods()[0].staging_dir()
    window.uninstall_selected()
    idle(qtbot, window)
    assert [r.mod.name for r in window.model.rows] == ["Better Shared"]
    assert not folder.exists()
    assert not os.path.lexists(fo4 / "Data" / "ArmorPack.esp")


def test_verify_and_repair(qtbot, window, answers, tmp_path, fo4):
    two_mods(tmp_path)
    window.refresh()
    (fo4 / "Data" / "ArmorPack.esp").unlink()
    window.verify()
    idle(qtbot, window)
    (dialog,) = [d for d in answers.dialogs if isinstance(d, VerifyDialog)]
    assert dialog.repair_button is not None
    assert (fo4 / "Data" / "ArmorPack.esp").is_symlink()  # repaired and redeployed
    assert "Repair:" in window.messages.toPlainText()


def test_dry_run_mode_changes_nothing(qtbot, window, answers, tmp_path, fo4):
    two_mods(tmp_path)
    window.settings.dry_run = True
    window.refresh()
    before = climods.snapshot(fo4)
    window.model.setData(window.model.index(0, COL_ENABLED), Qt.CheckState.Unchecked.value,
                         Qt.ItemDataRole.CheckStateRole)
    idle(qtbot, window)
    plan = answers.dialogs[-1]
    assert isinstance(plan, PlanDialog) and not hasattr(plan, "apply_button")
    window.purge()
    idle(qtbot, window)
    assert climods.snapshot(fo4) == before
    with Manifest() as m:
        assert all(x.enabled for x in m.mods("fallout4"))
    assert "DRY-RUN" in window.dry_label.text()


def test_dropping_files_onto_the_list_installs_them(qtbot, window, tmp_path, fo4):
    from PySide6.QtCore import QUrl
    archive = mod_zip(tmp_path, "Cool Mod-555-1-0-1700000000.zip", {"Cool.esp": esp()})
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(archive))])
    assert not data.hasFormat(MIME_TYPE)
    window.model.dropMimeData(data, Qt.DropAction.CopyAction, -1, 0, QModelIndex())
    idle(qtbot, window)
    assert (fo4 / "Data" / "Cool.esp").is_symlink()


def test_errors_are_shown_not_raised(qtbot, window, answers, tmp_path, fo4, monkeypatch):
    from bethmm import deploy
    two_mods(tmp_path)
    window.refresh()
    monkeypatch.setattr(deploy, "_same_dev", lambda a, b: False)
    window.settings.game_overrides["fallout4"] = __import__("bethmm.settings").settings.GameOverride(
        link_mode="hardlink")
    window.refresh()
    window.set_enabled(window.model.rows[0].mod.id, False)
    idle(qtbot, window)
    assert any("same filesystem" in text for _, text, warning in answers.told if warning)
    with Manifest() as m:
        assert m.mods("fallout4")[0].enabled  # nothing changed


def test_no_games_found(qtbot, tmp_path, monkeypatch, answers):
    monkeypatch.setenv("HOME", str(tmp_path / "empty home"))
    (tmp_path / "empty home").mkdir()
    w = MainWindow(load_settings())
    qtbot.addWidget(w)
    assert w.game is None
    assert not w.act_install.isEnabled() and not w.act_deploy.isEnabled()
    assert "No Bethesda games" in w.messages.toPlainText()
