"""The main window: the game, its mod list (tick to enable, drag to reorder;
the lowest in the list wins), details of the selected mod, and the actions.

Every change runs in the background through :class:`TaskRunner` and the
shared operations in :mod:`bethmm.service`; changes that remove things (purge,
uninstall, repair) show their plan first. In dry-run mode nothing is changed:
the plan is shown instead."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QEvent, QItemSelection, QItemSelectionModel, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableView,
    QTextBrowser,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
)

from bethmm import APP_NAME, __version__, deploy, describe, paths, service, staging, winecheck
from bethmm.archive import ArchiveError
from bethmm.discovery import DiscoveredGame, get_games
from bethmm.games import GAMES
from bethmm.manifest import Manifest, ManifestError, ModRecord
from bethmm.pathresolve import fold_path
from bethmm.settings import Settings, SettingsError, load_settings, save_settings

from bethmm.gui.dialogs import (
    MAX_ROWS,
    GameSettingsDialog,
    InstallDialog,
    PlanDialog,
    SettingsDialog,
    VerifyDialog,
)
from bethmm.gui.models import COL_CONFLICTS, COL_ENABLED, COL_FILES, COL_NAME, COL_POS, COL_VERSION, ModListModel
from bethmm.gui.tasks import TaskRunner

log = logging.getLogger(__name__)

#: Errors that are explained to the user as they are (no traceback).
EXPECTED_ERRORS = (deploy.DeployError, staging.StagingError, ManifestError, service.ServiceError, ArchiveError,
                   SettingsError, ValueError, OSError)

#: Game id -> its domain on nexusmods.com.
NEXUS_DOMAINS = {"fallout4": "fallout4", "skyrimse": "skyrimspecialedition", "skyrim": "skyrim",
                 "falloutnv": "newvegas", "fallout3": "fallout3", "oblivion": "oblivion", "starfield": "starfield"}

ARCHIVE_FILTER = "Mod archives (*.zip *.7z *.rar *.tar *.gz *.bz2 *.xz *.tgz);;All files (*)"

DRY_RUN_NOTE = "Dry-run mode is on, so nothing is changed (turn it off in Settings)."


def _names(manifest: Manifest, game_id: str) -> dict[int, str]:
    return {m.id: m.name for m in manifest.mods(game_id)}


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings if settings is not None else load_settings()
        self.games: list[DiscoveredGame] = []
        self.tasks = TaskRunner(self)
        self.tasks.busyChanged.connect(self._busy_changed)
        self._install_queue: list[tuple[str, ModRecord | None]] = []
        self._installing = False
        # Once the window is closing, queued installs are dropped and results
        # that would open a dialog only clean up.
        self._closing = False
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 760)
        self.setAcceptDrops(True)
        icon = Path(__file__).with_name("icon.svg")
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self._build()
        self.reload_games()

    # ------------------------------------------------------------ hooks (tests replace these)

    def run_dialog(self, dialog: QDialog) -> bool:
        """Show *dialog*; whether it was accepted. The caller reads what it
        needs right away: the dialog is deleted once control returns to Qt."""
        try:
            return not self._closing and dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()

    def ask(self, title: str, text: str) -> bool:
        if self._closing:
            return False
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def tell(self, title: str, text: str, *, warning: bool = False) -> None:
        if self._closing:
            return
        (QMessageBox.warning if warning else QMessageBox.information)(self, title, text)

    # ------------------------------------------------------------ building

    def _build(self) -> None:
        style = self.style()
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(240)
        self.game_combo.currentIndexChanged.connect(self._game_chosen)

        def action(text: str, slot: Callable[[], Any], icon: QStyle.StandardPixmap | None = None,
                   shortcut: str | None = None, tip: str = "") -> QAction:
            act = QAction(text, self)
            if icon is not None:
                act.setIcon(style.standardIcon(icon))
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            if tip:
                act.setStatusTip(tip)
                act.setToolTip(tip)
            act.triggered.connect(lambda _checked=False: slot())
            return act

        sp = QStyle.StandardPixmap
        self.act_install = action("Install...", self.choose_archives, sp.SP_DialogOpenButton, "Ctrl+O",
                                  "Install mods from archives you downloaded")
        self.act_install_folder = action("Install extracted folder...", self.choose_folder)
        self.act_deploy = action("Deploy", self.deploy, sp.SP_DialogApplyButton, "Ctrl+D",
                                 "Link every enabled mod into the game folder")
        self.act_purge = action("Purge...", self.purge, sp.SP_TrashIcon, tip="Put the game folder back as it was "
                                "(mods stay installed)")
        self.act_verify = action("Verify...", self.verify, sp.SP_FileDialogDetailedView,
                                 tip="Check the game folder against what the app recorded")
        self.act_check_links = action("Check links", self.check_links, sp.SP_DriveNetIcon,
                                      tip="Test that Wine sees files through the app's links")
        self.act_refresh = action("Refresh", self.refresh, sp.SP_BrowserReload, "F5")
        self.act_rescan = action("Rescan for games", self.rescan_games)
        self.act_game_settings = action("Game settings...", self.open_game_settings)
        self.act_settings = action("Settings...", self.open_settings, sp.SP_FileDialogInfoView)
        self.act_enable = action("Enable", lambda: self.set_selected_enabled(True))
        self.act_disable = action("Disable", lambda: self.set_selected_enabled(False))
        self.act_up = action("Move up", lambda: self.move_selected(-1), shortcut="Ctrl+Up")
        self.act_down = action("Move down", lambda: self.move_selected(1), shortcut="Ctrl+Down")
        self.act_top = action("Move to top (loses all conflicts)", lambda: self.move_selected("top"))
        self.act_bottom = action("Move to bottom (wins all conflicts)", lambda: self.move_selected("bottom"))
        self.act_rename = action("Rename...", self.rename_selected, shortcut="F2")
        self.act_update = action("Update from archive...", self.update_selected)
        self.act_open_folder = action("Open its folder", self.open_selected_folder)
        self.act_nexus = action("Open its Nexus page", self.open_selected_nexus)
        self.act_uninstall = action("Uninstall...", self.uninstall_selected, sp.SP_DialogDiscardButton, "Del")

        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addActions([self.act_install, self.act_install_folder])
        file_menu.addSeparator()
        file_menu.addAction(action("Quit", self.close, shortcut="Ctrl+Q"))
        game_menu = menu.addMenu("&Game")
        game_menu.addActions([self.act_deploy, self.act_purge, self.act_verify, self.act_check_links])
        game_menu.addSeparator()
        game_menu.addActions([self.act_game_settings, self.act_rescan, self.act_refresh])
        self.mod_menu = menu.addMenu("&Mod")
        self._fill_mod_menu(self.mod_menu)
        settings_menu = menu.addMenu("&Settings")
        settings_menu.addAction(self.act_settings)
        help_menu = menu.addMenu("&Help")
        help_menu.addAction(action("Open the log file", lambda: self._open_path(paths.log_path())))
        help_menu.addAction(action("Open the data folder", lambda: self._open_path(paths.data_dir())))
        help_menu.addAction(action(f"About {APP_NAME}", self._about))

        toolbar = QToolBar("Main")
        toolbar.setObjectName("main-toolbar")
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addWidget(QLabel(" Game: "))
        toolbar.addWidget(self.game_combo)
        toolbar.addSeparator()
        toolbar.addActions([self.act_install, self.act_deploy, self.act_purge, self.act_verify,
                            self.act_check_links])
        toolbar.addSeparator()
        toolbar.addActions([self.act_refresh, self.act_settings])
        self.addToolBar(toolbar)

        self.model = ModListModel(self)
        self.model.enableRequested.connect(self.set_enabled)
        self.model.moveRequested.connect(self.move_mod)
        self.model.filesDropped.connect(self.install_paths)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setDragDropOverwriteMode(False)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (COL_ENABLED, COL_POS, COL_VERSION, COL_FILES, COL_CONFLICTS):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())

        self.details = QTabWidget()
        self.files_tree = QTreeWidget()
        self.files_tree.setHeaderLabels(["File", "In the game folder", "Status"])
        self.files_tree.setRootIsDecorated(False)
        self.conflicts_tree = QTreeWidget()
        self.conflicts_tree.setHeaderLabels(["File", "Provided by (lowest first)", "Used"])
        self.conflicts_tree.setRootIsDecorated(False)
        self.info = QTextBrowser()
        self.info.setOpenExternalLinks(True)
        self.details.addTab(self.files_tree, "Files")
        self.details.addTab(self.conflicts_tree, "Conflicts")
        self.details.addTab(self.info, "Info")

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.table)
        top.addWidget(self.details)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)

        self.bottom = QTabWidget()
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setMaximumBlockCount(5000)
        self.history = QTreeWidget()
        self.history.setHeaderLabels(["When", "Operation", "Path", "Result"])
        self.history.setRootIsDecorated(False)
        self.bottom.addTab(self.messages, "Messages")
        self.bottom.addTab(self.history, "History")

        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(self.bottom)
        main.setStretchFactor(0, 4)
        main.setStretchFactor(1, 1)
        self.setCentralWidget(main)

        self.state_label = QLabel()
        self.dry_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(140)
        self.progress.hide()
        self.statusBar().addWidget(self.state_label, 1)
        self.statusBar().addPermanentWidget(self.dry_label)
        self.statusBar().addPermanentWidget(self.progress)

    def _fill_mod_menu(self, menu: QMenu) -> None:
        menu.addActions([self.act_enable, self.act_disable])
        menu.addSeparator()
        menu.addActions([self.act_up, self.act_down, self.act_top, self.act_bottom])
        menu.addSeparator()
        menu.addActions([self.act_rename, self.act_update, self.act_open_folder, self.act_nexus])
        menu.addSeparator()
        menu.addAction(self.act_uninstall)

    # ------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        return self.tasks.busy or self._installing

    @property
    def game(self) -> DiscoveredGame | None:
        game_id = self.game_combo.currentData()
        return next((g for g in self.games if g.game_id == game_id), None)

    @property
    def target(self) -> deploy.GameTarget | None:
        game = self.game
        return service.target_for(game, self.settings) if game is not None else None

    def selected_mods(self) -> list[ModRecord]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r].mod for r in rows if r < len(self.model.rows)]

    def _fresh_settings(self) -> Settings:
        """The settings as saved now: the command line (or another window) may
        have changed them, e.g. switched dry-run mode on. Read before every
        action, and before saving, so a change made elsewhere is neither
        ignored nor overwritten."""
        try:
            self.settings = load_settings()
        except SettingsError as exc:
            self.note(f"! {exc}")
        return self.settings

    def reload_games(self, *, rescan: bool = False) -> None:
        self._fresh_settings()
        try:
            self.games, warnings = get_games(self.settings, rescan=rescan)
        except (OSError, SettingsError) as exc:
            self.games, warnings = [], [str(exc)]
        for w in warnings:
            self.note(f"! {w}")
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in self.games:
            self.game_combo.addItem(game.name, game.game_id)
        if not self.games:
            self.game_combo.addItem("No games found", None)
        index = self.game_combo.findData(self.settings.current_game)
        self.game_combo.setCurrentIndex(index if index >= 0 else 0)
        self.game_combo.blockSignals(False)
        self.refresh()
        if not self.games:
            self.note("No Bethesda games were found under Lutris. Check Settings (the Lutris games folder) or "
                      "set the game folder in Game settings.")

    def _game_chosen(self) -> None:
        game = self.game
        if game is not None and game.game_id != self._fresh_settings().current_game:
            self.settings.current_game = game.game_id
            try:
                save_settings(self.settings)
            except OSError as exc:
                self.note(f"! could not save settings: {exc}")
        self.refresh()

    def refresh(self) -> None:
        """Reload the mod list, the details and the history from the manifest."""
        self._fresh_settings()
        game = self.game
        selected = [m.id for m in self.selected_mods()]
        current_row = self.table.currentIndex()
        current = (self.model.rows[current_row.row()].mod.id, current_row.column()) \
            if current_row.isValid() and current_row.row() < len(self.model.rows) else None
        rows: list[service.ModRow] = []
        state_text = "No game selected."
        history = []
        if game is not None:
            try:
                with Manifest() as manifest, manifest.snapshot():
                    rows = service.mod_rows(manifest, game.game_id)
                    state = manifest.game_state(game.game_id)
                    links = len(manifest.deployed(game.game_id))
                    history = manifest.history(game.game_id, limit=300)
            except Exception as exc:  # the window stays usable whatever the manifest holds
                if not isinstance(exc, ManifestError):
                    log.error("could not read the manifest: %s", "".join(traceback.format_exception(exc)))
                self.note(f"! could not read the mod list: {exc}")
                rows, history, state, links = [], [], None, 0
            mode = self.settings.override(game.game_id).link_mode
            purged = state is not None and not state.deployed
            state_text = (f"{game.root}  ·  {len(rows)} mod(s), {sum(r.mod.enabled for r in rows)} enabled  ·  "
                          + ("PURGED: Deploy puts the mods back" if purged else f"{links} {mode}(s) deployed"))
        self.model.set_rows(rows)
        keep = QItemSelection()
        for mod_id in selected:
            row = self.model.row_of(mod_id)
            if row is not None:
                keep.select(self.model.index(row, 0), self.model.index(row, self.model.columnCount() - 1))
        if not keep.isEmpty():
            self.table.selectionModel().select(keep, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                               | QItemSelectionModel.SelectionFlag.Rows)
        # The row the keyboard is on (Space toggles it), as before the reload.
        row = self.model.row_of(current[0]) if current is not None else None
        if row is not None:
            self.table.setAutoScroll(False)  # the list stays where the user scrolled it
            self.table.selectionModel().setCurrentIndex(self.model.index(row, current[1]),
                                                        QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.setAutoScroll(True)
        self.state_label.setText(state_text)
        self.dry_label.setText("<b>DRY-RUN MODE</b>" if self.settings.dry_run else "")
        self.history.clear()
        for e in reversed(history):
            result = e.status if e.status == "done" else f"{e.status}: {e.detail or ''}"
            QTreeWidgetItem(self.history, [e.ts.replace("T", " ")[:19], e.action, e.path or (e.detail or ""), result])
        self._selection_changed()

    def _busy_changed(self, busy: bool) -> None:
        self.progress.setVisible(busy or self._installing)
        self._update_actions()

    def _update_actions(self) -> None:
        idle = not self.busy
        has_game = self.game is not None
        selected = self.selected_mods()
        for act in (self.act_install, self.act_install_folder, self.act_deploy, self.act_purge, self.act_verify,
                    self.act_check_links):
            act.setEnabled(idle and has_game)
        # Game settings also sets up a game that wasn't found, or brings back a hidden one.
        for act in (self.act_settings, self.act_rescan, self.act_refresh, self.act_game_settings):
            act.setEnabled(idle)
        for act in (self.act_enable, self.act_disable, self.act_uninstall):
            act.setEnabled(idle and bool(selected))
        one = idle and len(selected) == 1
        for act in (self.act_rename, self.act_update, self.act_open_folder, self.act_up, self.act_down,
                    self.act_top, self.act_bottom):
            act.setEnabled(one)
        self.act_nexus.setEnabled(one and selected[0].nexus_id is not None)
        self.table.setEnabled(idle)
        self.game_combo.setEnabled(idle)

    # ------------------------------------------------------------ messages

    def note(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        for i, line in enumerate(text.splitlines() or [""]):
            self.messages.appendPlainText(f"{stamp}  {line}" if i == 0 else f"          {line}")

    def report(self, title: str, result: deploy.SyncResult | None, names: dict[int, str]) -> None:
        """Put a deploy/purge result in the messages; failures also pop up."""
        if result is None:
            self.note(f"{title} The game is purged, so nothing was linked; Deploy puts the mods back.")
            return
        if result.forgotten:
            self.note(f"{title} " + "; ".join(result.forgotten))
            return
        done = describe.summary(describe.changes(result))
        lines = [f"{title} {done[0].upper() + done[1:]}." if done else f"{title} Nothing needed to change."]
        lines += [f"! not deployed: {path}: {reason}" for path, reason in result.plan.blocked]
        lines += [f"! {w}" for w in result.warnings]
        lines += [f"! failed: {describe.describe_op(op, names)}: {reason}" for op, reason in result.failed]
        self.note("\n".join(lines))
        if result.failed:
            self.tell("Some changes failed", "\n".join(
                f"{describe.describe_op(op, names)}: {reason}" for op, reason in result.failed[:20]), warning=True)

    def show_error(self, exc: BaseException) -> None:
        if isinstance(exc, EXPECTED_ERRORS):
            text = str(exc) if not isinstance(exc, OSError) else f"{exc.strerror or exc}" + (
                f": {exc.filename}" if getattr(exc, "filename", None) else "")
            self.note(f"! {text}")
            self.tell("Couldn't do that", text, warning=True)
        else:
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log.error("unexpected error: %s", detail)
            self.note(f"! unexpected error: {exc!r}")
            if not self._closing:
                box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error", f"{exc!r}", parent=self)
                box.setDetailedText(detail)
                box.exec()
                box.deleteLater()
        self.refresh()

    def _run(self, job: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if self._closing:  # nothing new starts once the window is closing
            return

        def finished(result: Any) -> None:
            if self._closing:  # what it would show or ask no longer matters
                return
            try:
                done(result)
            finally:
                self.refresh()

        self.tasks.run(job, finished, self.show_error)

    # ------------------------------------------------------------ mod list changes

    def _change(self, mutate: Callable[[Manifest], str]) -> None:
        """Change the mod list and redeploy -- in dry-run mode only show what
        that would do."""
        self._fresh_settings()
        target = self.target
        if target is None:
            return
        if self.settings.dry_run:
            def preview() -> tuple[str, deploy.SyncResult | None, dict[int, str]]:
                with Manifest() as manifest:
                    names = _names(manifest, target.game_id)
                    what, result = service.preview_change(manifest, target, mutate)
                    return what, result, names

            def show(r) -> None:
                what, result, names = r
                self.run_dialog(PlanDialog("Dry run", what, result, names, apply_text="", can_apply=False,
                                           why_not=DRY_RUN_NOTE, parent=self))

            self._run(preview, show)
            return

        def apply() -> tuple[str, deploy.SyncResult | None, dict[int, str]]:
            with Manifest(lock=True) as manifest:
                what, result = service.apply_change(manifest, target, mutate)
                return what, result, _names(manifest, target.game_id)

        self._run(apply, lambda r: self.report(r[0], r[1], r[2]))

    def set_enabled(self, mod_id: int, enabled: bool) -> None:
        self.set_mods_enabled([mod_id], enabled)

    def set_mods_enabled(self, mod_ids: list[int], enabled: bool) -> None:
        game, shown = self.game, [r.mod for r in self.model.rows]
        if game is None:
            return

        def mutate(manifest: Manifest) -> str:
            service.check_unchanged(manifest, game.game_id, shown)
            names = []
            for mod_id in mod_ids:
                manifest.set_enabled(mod_id, enabled)
                names.append(manifest.mod(mod_id).name)
            return f"{'Enabled' if enabled else 'Disabled'} " + ", ".join(f"'{n}'" for n in names) + "."
        self._change(mutate)

    def set_selected_enabled(self, enabled: bool) -> None:
        mods = [m.id for m in self.selected_mods() if m.enabled != enabled]
        if mods:
            self.set_mods_enabled(mods, enabled)

    def move_mod(self, mod_id: int, new_index: int) -> None:
        game, shown = self.game, [r.mod for r in self.model.rows]
        if game is None:
            return

        def mutate(manifest: Manifest) -> str:
            service.check_unchanged(manifest, game.game_id, shown)
            order = [m.id for m in manifest.mods(game.game_id)]
            new_order = service.moved_order(order, mod_id, new_index)
            manifest.set_order(game.game_id, new_order)
            return f"Moved '{manifest.mod(mod_id).name}' to position {new_order.index(mod_id) + 1} of {len(order)}."
        self._change(mutate)

    def move_selected(self, where: int | str) -> None:
        mods = self.selected_mods()
        if len(mods) != 1:
            return
        row = self.model.row_of(mods[0].id) or 0
        new = {"top": 0, "bottom": len(self.model.rows) - 1}.get(where, None) if isinstance(where, str) else row + where
        if new is not None and new != row and 0 <= new < len(self.model.rows):
            self.move_mod(mods[0].id, new)

    def rename_selected(self) -> None:
        mods = self.selected_mods()
        if len(mods) != 1:
            return
        mod = mods[0]
        name, ok = QInputDialog.getText(self, "Rename", "Name in the mod list:", text=mod.name)
        name = staging.clean_text(name or "")
        if not ok or not name or name == mod.name:
            return
        if self._fresh_settings().dry_run:
            self.tell("Dry run", f"Would rename '{mod.name}' to '{name}'. {DRY_RUN_NOTE}")
            return

        def job() -> str:
            with Manifest(lock=True) as manifest:
                service.current(manifest, [mod])
                manifest.update_mod(mod.id, name=name)
            return f"Renamed '{mod.name}' to '{name}'."

        self._run(job, self.note)

    def uninstall_selected(self) -> None:
        self._fresh_settings()
        mods, target, shown = self.selected_mods(), self.target, [r.mod for r in self.model.rows]
        if not mods or target is None:
            return
        listed = ", ".join(f"'{m.name}'" for m in mods)

        def preview() -> tuple[deploy.SyncResult | None, dict[int, str]]:
            with Manifest() as manifest:
                service.check_unchanged(manifest, target.game_id, shown)
                return service.preview_uninstall(manifest, target, mods), _names(manifest, target.game_id)

        def confirm(r) -> None:
            result, names = r
            dialog = PlanDialog(f"Uninstall {len(mods)} mod(s)",
                                f"Uninstall {listed}? Their links are removed from the game folder, the original "
                                "files they replaced go back, and their staged files are deleted.",
                                result, names, apply_text="Uninstall", can_apply=not self.settings.dry_run,
                                why_not=DRY_RUN_NOTE, parent=self)
            if self.run_dialog(dialog):
                self._run(apply, done)

        def apply() -> tuple[service.UninstallResult, dict[int, str]]:
            with Manifest(lock=True) as manifest:
                service.check_unchanged(manifest, target.game_id, shown)
                names = _names(manifest, target.game_id)
                return service.uninstall(manifest, target, mods), names

        def done(r) -> None:
            outcome, names = r
            self.report(f"Uninstalled {', '.join(repr(m.name) for m in outcome.removed) or 'nothing'}.",
                        outcome.sync, names)
            for mod in outcome.still_linked:
                self.note(f"! '{mod.name}' still has links that could not be removed; it stays installed "
                          "(disabled). Verify and repair, then try again.")
            for mod, folder in outcome.kept_folders:
                self.note(f"! kept the files of '{mod.name}' in {folder}: an interrupted run may have linked them; "
                          "Verify > Repair deletes the folder once nothing uses it.")
            for mod, folder, why in outcome.not_deleted:
                self.note(f"! could not delete the files of '{mod.name}' in {folder}: {why}. Verify > Repair "
                          "deletes them once that is fixed.")

        self._run(preview, confirm)

    # ------------------------------------------------------------ install

    def choose_archives(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Install mods", str(Path.home() / "Downloads"), ARCHIVE_FILTER)
        self.install_paths(files)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Install an extracted mod folder", str(Path.home()))
        if folder:
            self.install_paths([folder])

    def update_selected(self) -> None:
        mods = self.selected_mods()
        if len(mods) != 1:
            return
        files, _ = QFileDialog.getOpenFileNames(self, f"Update '{mods[0].name}' from", str(Path.home() / "Downloads"),
                                                ARCHIVE_FILTER)
        if files:
            self.install_paths(files[:1], replace=mods[0])

    def install_paths(self, sources: list[str], replace: ModRecord | None = None) -> None:
        """Install each archive or folder in turn (each through the install
        dialog); with *replace*, as the new version of that mod."""
        sources = [s for s in sources if s]
        if sources and self.game is None:
            self.note("! No game to install into: no Bethesda game was found. Check Settings, or set the game "
                      "folder in Game settings.")
            self.tell("Install", "No game to install into: no Bethesda game was found.", warning=True)
            return
        self._install_queue += [(s, replace) for s in sources]
        if not self._installing and not self.tasks.busy:
            self._next_install()

    def _next_install(self) -> None:
        if not self._install_queue or self.game is None or self._closing:
            self._install_queue.clear()
            self._installing = False
            self._busy_changed(self.tasks.busy)
            return
        self._installing = True
        source, replace = self._install_queue.pop(0)
        settings, installed = self.settings, [g.game_id for g in self.games]
        self.note(f"Reading {source} ...")

        def prepare() -> staging.Prepared:
            return staging.prepare(source, sevenzip=settings.sevenzip, installed_games=installed)

        def failed(exc: BaseException) -> None:
            try:
                self.show_error(exc)
            finally:
                self._next_install()

        self.tasks.run(prepare, lambda prepared: self._show_install(prepared, replace), failed)

    def _show_install(self, prepared: staging.Prepared, replace: ModRecord | None) -> None:
        if self._closing:
            staging.discard(prepared)
            self._installing = False
            return
        try:
            self._fresh_settings()
            if replace is not None:  # the mod that was selected, if it is still that mod
                with Manifest() as manifest:
                    replace = service.current(manifest, [replace])[0]
            dialog = InstallDialog(prepared, self.games, self.settings, replace=replace,
                                   current=self.game.game_id if self.game is not None else None,
                                   can_install=not self.settings.dry_run, why_not=DRY_RUN_NOTE, parent=self)
        except Exception as exc:
            staging.discard(prepared)
            self.show_error(exc)
            self._next_install()
            return
        if not self.run_dialog(dialog) or dialog.choice is None or dialog.game is None:
            staging.discard(dialog.prepared)
            self.note(f"Cancelled installing {prepared.source.name}.")
            self.refresh()
            self._next_install()
            return
        prepared, choice, game = dialog.prepared, dialog.choice, dialog.game
        target = service.target_for(game, self.settings)

        def install() -> tuple[service.InstallResult, dict[int, str]]:
            try:
                with Manifest(lock=True) as manifest:
                    # Asked again under the lock, with the same options.
                    existing = service.current(manifest, [choice.existing])[0] if choice.existing else None
                    again = service.choose_install(manifest, game.game_id, prepared, name=choice.given_name,
                                                   new=existing is None, replace=existing, disabled=choice.disabled)
                    result = service.install(manifest, target, prepared, again)
                    return result, _names(manifest, game.game_id)
            finally:
                staging.discard(prepared)

        def done(r) -> None:
            try:
                result, names = r
                mod = result.commit.mod
                title = f"{'Updated' if result.commit.replaced else 'Installed'} '{mod.name}'" + (
                    " (disabled)." if not mod.enabled else ".")
                if result.sync is not None:
                    self.report(title, result.sync, names)
                else:
                    self.note(title + (" The game is purged, so nothing was linked." if choice.purged else
                                       " Nothing was linked: enable it to put it into the game."))
                if result.cleanup_error is not None:
                    self.note(f"! could not delete the previous files in {result.kept_old_folder}: "
                              f"{result.cleanup_error}. Verify > Repair deletes them once that is fixed.")
                elif result.kept_old_folder is not None:
                    self.note(f"! kept the previous files in {result.kept_old_folder}: {result.still_linked} "
                              "link(s) still point there. Deploy, then Verify > Repair deletes the folder.")
                if game.game_id != (self.game.game_id if self.game else None):
                    self.game_combo.setCurrentIndex(max(self.game_combo.findData(game.game_id), 0))
            finally:
                self.refresh()
                self._next_install()

        def failed(exc: BaseException) -> None:
            try:
                self.show_error(exc)
            finally:
                self._next_install()

        self.tasks.run(install, done, failed)

    # ------------------------------------------------------------ game folder

    def deploy(self) -> None:
        self._fresh_settings()
        target = self.target
        if target is None:
            return

        def preview() -> tuple[deploy.SyncResult, dict[int, str], bool]:
            with Manifest() as manifest:
                state = manifest.game_state(target.game_id)
                return (deploy.deploy(manifest, target, dry_run=True), _names(manifest, target.game_id),
                        state is not None and not state.deployed)

        def confirm(r) -> None:
            result, names, purged = r
            if not describe.changes(result) and not result.plan.blocked and not result.warnings:
                if purged and not self.settings.dry_run:
                    # Nothing to link now, but mods enabled from now on are linked again.
                    self._run(apply, lambda r2: self.report("Deployed.", r2[0], r2[1]))
                    return
                self.note("Deploy: everything is already in place.")
                self.tell("Deploy", "Everything is already in place.")
                return
            dialog = PlanDialog("Deploy", "Link every enabled mod into the game folder:", result, names,
                                apply_text="Deploy", can_apply=not self.settings.dry_run, why_not=DRY_RUN_NOTE,
                                parent=self)
            if self.run_dialog(dialog):
                self._run(apply, lambda r2: self.report("Deployed.", r2[0], r2[1]))

        def apply() -> tuple[deploy.SyncResult, dict[int, str]]:
            with Manifest(lock=True) as manifest:
                return deploy.deploy(manifest, target), _names(manifest, target.game_id)

        self._run(preview, confirm)

    def purge(self, forget_missing: bool = False) -> None:
        self._fresh_settings()
        target = self.target
        if target is None or self._closing:
            return

        def preview() -> tuple[deploy.SyncResult, dict[int, str]]:
            with Manifest() as manifest:
                return (deploy.purge(manifest, target, dry_run=True, forget_missing=forget_missing),
                        _names(manifest, target.game_id))

        def confirm(r) -> None:
            result, names = r
            if forget_missing:
                intro = "The app forgets what it had in that game folder (the originals it backed up are kept):"
            else:
                intro = ("Remove everything the app put in the game folder and put the original files back. "
                         "Mods stay installed; Deploy puts them back.")
            dialog = PlanDialog("Purge", intro, result, names, apply_text="Purge",
                                can_apply=not self.settings.dry_run, why_not=DRY_RUN_NOTE, parent=self)
            if self.run_dialog(dialog):
                self._run(apply, lambda r2: self.report("Purged.", r2[0], r2[1]))

        def apply() -> tuple[deploy.SyncResult, dict[int, str]]:
            with Manifest(lock=True) as manifest:
                result = deploy.purge(manifest, target, forget_missing=forget_missing)
                return result, _names(manifest, target.game_id)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, deploy.GameFolderGone) and not forget_missing:
                self.note(f"! {exc}")
                if self.ask("The game folder can't be used",
                            f"{exc}.\n\nIs that install of the game gone for good? Answer No if its drive just "
                            "isn't mounted: nothing is changed then."):
                    self.purge(forget_missing=True)
                self.refresh()
            else:
                self.show_error(exc)

        def finished(r) -> None:
            if self._closing:
                return
            try:
                confirm(r)
            finally:
                self.refresh()

        self.tasks.run(preview, finished, failed)

    def verify(self) -> None:
        self._fresh_settings()
        target, game = self.target, self.game
        if target is None or game is None:
            return

        def check() -> service.Check:
            with Manifest() as manifest:
                return service.check(manifest, target)

        def show(found: service.Check) -> None:
            dialog = VerifyDialog(game.name, found, can_repair=not self.settings.dry_run, parent=self)
            if self.run_dialog(dialog) and dialog.repair_button is not None:
                release = dialog.release_held.isChecked()
                self._run(lambda: repair(release), repaired)

        def repair(release_held: bool) -> tuple[service.RepairOutcome, dict[int, str]]:
            with Manifest(lock=True) as manifest:
                found = service.check(manifest, target)  # again, under the lock
                outcome = service.repair(manifest, target, found, release_held=release_held, data_dir=game.data_dir)
                return outcome, _names(manifest, target.game_id)

        def repaired(r) -> None:
            outcome, names = r
            self.note("Repair:\n" + "\n".join(f"  {a}" for a in outcome.actions or ["nothing to do"]))
            for line in outcome.leftovers:
                self.note(line)
            if outcome.sync_error:
                self.note(f"! {outcome.sync_error}")
                self.tell("Repair", outcome.sync_error, warning=True)
            else:
                self.report("Purged again." if outcome.purged else "Redeployed.", outcome.sync, names)
            for folder in outcome.deleted:
                self.note(f"deleted {folder}")

        self._run(check, show)

    def check_links(self) -> None:
        self._fresh_settings()
        game = self.game
        if game is None:
            return
        mode = self.settings.override(game.game_id).link_mode
        if self.settings.dry_run:
            self.tell("Check links", f"Would put a test link in {game.data_dir}, have Wine read it, and remove it "
                                     f"again. {DRY_RUN_NOTE}")
            return
        if not self.ask("Check links", f"Put a test file into {game.data_dir} through a {mode}, have Wine read it, "
                                       "and remove it again? This can take up to two minutes."):
            return

        def job() -> winecheck.LinkCheck:
            with Manifest(lock=True):  # keeps a concurrent repair from removing the test link
                return winecheck.check_links(game, link_mode=mode)

        def done(result: winecheck.LinkCheck) -> None:
            text = result.message + (f"\n\nWine: {result.wine}" if result.wine else "")
            self.note(f"Check links: {result.message}")
            if result.output and result.ok is not True:
                text += f"\n\nOutput:\n{result.output}"
            self.tell("Check links", text, warning=result.ok is not True)

        self._run(job, done)

    # ------------------------------------------------------------ settings

    def open_settings(self) -> None:
        dialog = SettingsDialog(self._fresh_settings(), parent=self)
        if self.run_dialog(dialog):
            rescan = dialog.apply()
            self._save_settings()
            self.reload_games(rescan=rescan)

    def open_game_settings(self) -> None:
        self._fresh_settings()
        game_id = self.game_combo.currentData()
        hidden = {g for g in GAMES if self.settings.override(g).ignore}
        if game_id is None or hidden:
            # Every game, so one that wasn't found can be set up and a hidden one brought back.
            ids = sorted(GAMES, key=lambda g: GAMES[g].name)
            labels = [GAMES[g].name + (" (hidden)" if g in hidden else "") for g in ids]
            start = game_id or self.settings.current_game
            label, ok = QInputDialog.getItem(self, "Game settings", "Game:", labels,
                                             ids.index(start) if start in ids else 0, False)
            if not ok:
                return
            game_id = ids[labels.index(label)]
        game = next((g for g in self.games if g.game_id == game_id), None)
        dialog = GameSettingsDialog(game_id, self.settings, game, parent=self)
        if self.run_dialog(dialog):
            dialog.apply()
            hidden_now = self.settings.override(game_id).ignore
            if not hidden_now:
                self.settings.current_game = game_id
            elif self.settings.current_game == game_id:
                self.settings.current_game = None  # a hidden game isn't the current one any more
            self._save_settings()
            if self.settings.override(game_id).link_mode == "hardlink" and game is not None and not hidden_now:
                try:
                    deploy.check_link_mode(service.target_for(game, self.settings))
                except deploy.DeployError as exc:
                    self.tell("Hardlinks won't work here", str(exc), warning=True)
            self.reload_games()
            if self.settings.current_game is None and self.game is not None:
                self.settings.current_game = self.game.game_id  # the game shown now, for the command line too
                self._save_settings()

    def rescan_games(self) -> None:
        self.reload_games(rescan=True)

    def _save_settings(self) -> None:
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.tell("Settings", f"Could not save the settings: {exc}", warning=True)

    # ------------------------------------------------------------ details

    def _selection_changed(self) -> None:
        self._update_actions()
        mods = self.selected_mods()
        self.files_tree.clear()
        self.conflicts_tree.clear()
        self.details.setTabText(1, "Conflicts")
        self.info.clear()
        game = self.game
        if len(mods) != 1 or game is None:
            return
        mod = mods[0]
        try:
            with Manifest() as manifest:
                files = manifest.mod_files(mod.id)
                deployed = manifest.deployed(game.game_id)
                names = _names(manifest, game.game_id)
                _desired, providers = deploy.desired_files(manifest, game.game_id)
                state = manifest.game_state(game.game_id)
        except ManifestError as exc:
            self.note(f"! {exc}")
            return
        purged = state is not None and not state.deployed
        for f in files[:MAX_ROWS]:
            key = fold_path(f.dest)
            record = deployed.get(key)
            if record is not None and record.mod_id == mod.id:
                status, where = "linked", record.path
            elif record is not None:
                status, where = f"overridden by {names.get(record.mod_id, 'another mod')}", record.path
            else:
                status = "disabled" if not mod.enabled else "purged" if purged else "not linked"
                where = f.dest
            QTreeWidgetItem(self.files_tree, [f.source, where, status])
        if len(files) > MAX_ROWS:
            QTreeWidgetItem(self.files_tree, [f"... and {len(files) - MAX_ROWS} more file(s)", "", ""])
        for col in (0, 1):
            self.files_tree.resizeColumnToContents(col)
        own = {fold_path(f.dest): f.dest for f in files}
        for key, mod_ids in sorted(providers.items()):
            if len(mod_ids) > 1 and mod.id in mod_ids:
                winner = mod_ids[-1]
                QTreeWidgetItem(self.conflicts_tree, [own.get(key, key), " < ".join(names.get(m, "?") for m in mod_ids),
                                                      "this mod's" if winner == mod.id else names.get(winner, "?")])
        for col in (0, 1):
            self.conflicts_tree.resizeColumnToContents(col)
        self.details.setTabText(1, f"Conflicts ({self.conflicts_tree.topLevelItemCount()})")
        rows = [("Name", mod.name), ("Enabled", "yes" if mod.enabled else "no"),
                ("Version", mod.version or "?"), ("Installed", mod.installed_at.replace("T", " ")[:19]),
                ("Staged files", str(mod.staging_dir())), ("From", mod.source_archive or "?")]
        if mod.nexus_id is not None:
            domain = NEXUS_DOMAINS.get(mod.game_id, mod.game_id)
            url = f"https://www.nexusmods.com/{domain}/mods/{mod.nexus_id}"
            rows.append(("Nexus", f"<a href='{url}'>{url}</a>"))
        plan = mod.plan or {}
        if plan:
            rows.append(("Placement", f"{plan.get('rule', '?')} into {plan.get('target', '?')}"
                                      + (f", root '{plan['root']}'" if plan.get("root") else "")))
        html = "<table>" + "".join(f"<tr><td><b>{k}</b>&nbsp;&nbsp;</td><td>{_escape(v) if k != 'Nexus' else v}"
                                   "</td></tr>" for k, v in rows) + "</table>"
        self.info.setHtml(html)

    def _context_menu(self, pos) -> None:
        if self.table.indexAt(pos).isValid():
            menu = QMenu(self)
            self._fill_mod_menu(menu)
            menu.exec(self.table.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def open_selected_folder(self) -> None:
        mods = self.selected_mods()
        if len(mods) == 1:
            self._open_path(mods[0].staging_dir())

    def open_selected_nexus(self) -> None:
        mods = self.selected_mods()
        if len(mods) == 1 and mods[0].nexus_id is not None:
            domain = NEXUS_DOMAINS.get(mods[0].game_id, mods[0].game_id)
            QDesktopServices.openUrl(QUrl(f"https://www.nexusmods.com/{domain}/mods/{mods[0].nexus_id}"))

    def _open_path(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<b>{APP_NAME}</b> {__version__}<br>A mod manager for Bethesda games installed through "
                          f"Lutris/Wine.<br><br>Data: {paths.data_dir()}<br>Settings: {paths.config_dir()}")

    # ------------------------------------------------------------ drag and drop onto the window

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and not self.busy and self.game is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        local = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if local:
            event.acceptProposedAction()
            self.install_paths(local)

    def closeEvent(self, event) -> None:
        if self.busy:
            self.tasks.hold()  # no dialog of a job that finishes meanwhile pops up over the question
            try:
                quit_anyway = self.ask("Still working", "An operation is still running. Quit when it has finished? "
                                       "Archives still waiting to be installed are skipped.")
                if quit_anyway:
                    self._closing = True
                    self._install_queue.clear()
            finally:
                self.tasks.release()
            if not quit_anyway:
                event.ignore()
                return
        self._closing = True
        self._install_queue.clear()
        self.tasks.wait()
        # Deliver the last results now, while the window still exists (an
        # extracted archive waiting for its dialog is deleted).
        QCoreApplication.sendPostedEvents(self.tasks, QEvent.Type.MetaCall.value)
        event.accept()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
