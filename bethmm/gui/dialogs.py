"""Dialogs: the plan of a change (shown before it is made), verify/repair,
a FOMOD installer, installing a mod, and settings."""

from __future__ import annotations

import copy
import dataclasses
import html
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bethmm import deploy, describe, fomod, service, staging
from bethmm.discovery import DiscoveredGame
from bethmm.games import GAMES
from bethmm.manifest import Manifest, ModRecord
from bethmm.placement import Target
from bethmm.settings import LINK_MODES, GameOverride, Settings

#: File lists show this many rows (a big texture pack has tens of thousands).
MAX_ROWS = 2000

_TARGET_TEXT = {Target.DATA: "the Data folder", Target.GAME_ROOT: "the game folder (next to the .exe)",
                Target.UNKNOWN: "an unknown place"}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _label(text: str, *, rich: bool = False, wrap: bool = True, selectable: bool = True) -> QLabel:
    """A label showing *text* as it is -- or, with *rich*, as HTML: then every
    name or message in it must be escaped (an archive's file names can look
    like markup, and Qt would render it)."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _ops_text(result: deploy.SyncResult | None, names: dict[int, str]) -> str:
    """The full plan of a deploy/purge result as text (blocked files and
    warnings included)."""
    if result is None:
        return "The game is purged, so nothing would be linked."
    lines = []
    if result.forgotten:
        lines += result.forgotten
    for op in result.done:
        lines.append(describe.describe_op(op, names))
    lines += describe.plugins_lines(result.plugins)
    for path, reason in result.plan.blocked:
        lines.append(f"! not deployed: {path}: {reason}")
    for warning in result.warnings:
        lines.append(f"! {warning}")
    for op, reason in result.failed:
        lines.append(f"! failed: {describe.describe_op(op, names)}: {reason}")
    return "\n".join(lines) or "Nothing would change."


class PlanDialog(QDialog):
    """What a change would do to the game folder, with the button to do it
    (none when *can_apply* is False, e.g. in dry-run mode)."""

    def __init__(self, title: str, intro: str, result: deploy.SyncResult | None, names: dict[int, str], *,
                 apply_text: str, can_apply: bool = True, why_not: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(intro))
        if result is not None and not result.forgotten:
            changes = describe.changes(result)
            done = describe.summary(changes, dry_run=True)
            layout.addWidget(_label(f"<b>{_escape(done[0].upper() + done[1:])}</b> in {_escape(str(result.plan.root))}."
                                    if done else "<b>Nothing in the game folder would change.</b>", rich=True))
        text = QPlainTextEdit(_ops_text(result, names))
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(text, 1)
        if not can_apply and why_not:
            layout.addWidget(_label(f"<i>{_escape(why_not)}</i>", rich=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        if can_apply:
            self.apply_button = buttons.addButton(apply_text, QDialogButtonBox.ButtonRole.AcceptRole)
        else:
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class VerifyDialog(QDialog):
    """What verify found, with a Repair button (and the choice to release
    originals held because a newer file is at their path)."""

    def __init__(self, game_name: str, found: service.Check, *, can_repair: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Verify {game_name}")
        self.resize(820, 520)
        report = found.report
        layout = QVBoxLayout(self)
        tree = QTreeWidget()
        tree.setHeaderLabels(["What", "Where"])
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        problems = QTreeWidgetItem(["Problems"])
        notes = QTreeWidgetItem(["Notes"])

        def add(parent_item: QTreeWidgetItem, what: str, where: str) -> None:
            QTreeWidgetItem(parent_item, [what, where])

        if report.root_missing:
            layout.addWidget(_label(f"<b>The game folder can't be used</b> ({_escape(report.missing_reason)}), so "
                                    "nothing was checked. If it is on a drive that isn't mounted, mount it and verify "
                                    "again. If you moved or reinstalled the game, Game > Rescan for games finds it "
                                    "again. If that install is gone, use Purge with 'the game is gone' to forget the "
                                    "app's links there.", rich=True))
        for record in report.missing:
            add(problems, "missing link", record.path)
        for record in report.foreign:
            add(problems, "replaced by another file (left alone)", record.path)
        for rel in report.orphan_links:
            add(problems, "stray link into the mods folder", rel)
        for backup in report.missing_backups:
            add(problems, "backup file gone", f"{backup.path} ({backup.backup})")
        for full in report.orphan_backups:
            add(problems, "unrecorded backup", full)
        for backup in report.held_backups:
            add(problems, "original kept: another file is at its path", f"{backup.path} ({backup.backup})")
        for full in report.temp_leftovers:
            add(problems, "file left by an interrupted move", full)
        for backup in report.restorable:
            add(problems, "original not back in the game folder yet", backup.path)
        for path in report.missing_dirs:
            add(problems, "folder gone", path)
        for entry in report.interrupted:
            add(problems, "interrupted operation", f"{entry.action} {entry.path or ''} ({entry.ts})")
        for folder in found.retired:
            add(problems, "mod files nothing uses any more (a previous version, an uninstalled mod or an "
                "interrupted install)", str(folder))
        if report.out_of_date:
            add(problems, f"not up to date with the mod list (a change was saved but not deployed): a deploy "
                f"changes {len(report.out_of_date)} file(s)", ", ".join(report.out_of_date[:3])
                + (", ..." if len(report.out_of_date) > 3 else ""))
        if report.moved_from is not None:
            add(notes, "the game was moved; checked the new folder", f"{report.moved_from} -> {report.root}")
        for note in report.notes:
            add(notes, note, "")
        for path in report.left_alone:
            add(notes, "your file, where a mod's link was (left alone)", path)
        for old, now in report.case_moved:
            add(notes, "link in a renamed folder (the next deploy records it)", f"{old} -> {now}")
        for folder in found.unknown:
            add(notes, "folder in the mods folder no mod uses (left alone)", str(folder))
        for rel in report.links_elsewhere:
            add(notes, "the app's link in the game's current folder, not the deployed one (left alone)", rel)
        for item in (problems, notes):
            if item.childCount():
                tree.addTopLevelItem(item)
                item.setExpanded(True)
        if not report.root_missing:
            layout.addWidget(_label("<b>Everything the app deployed is in place.</b>" if not found.problems
                                    else f"<b>Found {problems.childCount()} problem(s).</b> Repair fixes what is "
                                         "safe to fix, then redeploys (or purges again, for a purged game).",
                                    rich=True))
        layout.addWidget(tree, 1)
        self.release_held = QCheckBox("Stop tracking originals kept because a newer file is at their path "
                                      "(e.g. after a game update; they are moved to backups/.superseded)")
        self.release_held.setVisible(bool(report.held_backups))
        layout.addWidget(self.release_held)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.repair_button = None
        if found.problems and not report.root_missing and can_repair:
            self.repair_button = buttons.addButton("Repair", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


_TYPE_NOTES = {fomod.OptionType.REQUIRED: "required", fomod.OptionType.RECOMMENDED: "recommended",
               fomod.OptionType.NOT_USABLE: "can't be used", fomod.OptionType.COULD_BE_USABLE: "may not work"}
_GROUP_HELP = {fomod.GroupType.EXACTLY_ONE: "choose one", fomod.GroupType.AT_MOST_ONE: "choose one or none",
               fomod.GroupType.AT_LEAST_ONE: "choose one or more", fomod.GroupType.ANY: "choose any",
               fomod.GroupType.ALL: "all are installed"}


class FomodDialog(QDialog):
    """A FOMOD installer, step by step: each group's options (one to choose
    with round buttons, several with tick boxes), the description and picture
    of the option under the mouse, Back and Next, Install at the last step.
    The choices are made in ``wizard``."""

    def __init__(self, wizard: fomod.Wizard, prepared: staging.Prepared, *, note: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.wizard = wizard
        self.prepared = prepared
        module = wizard.module
        self.setWindowTitle(f"{module.name}: installer")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        about = ", ".join(x for x in (f"version {module.info.version}" if module.info.version else "",
                                      f"by {module.info.author}" if module.info.author else "") if x)
        layout.addWidget(_label(f"<b>{_escape(module.name)}</b>" + (f" ({_escape(about)})" if about else ""),
                                rich=True))
        if note:
            layout.addWidget(_label(note))
        self.step_label = _label("", rich=True)
        layout.addWidget(self.step_label)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        split.addWidget(self.scroll)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(320, 200)
        side_layout.addWidget(self.image)
        self.description = QTextBrowser()
        side_layout.addWidget(self.description, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self.problem_label = _label("", rich=True)
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.back_button = buttons.addButton("Back", QDialogButtonBox.ButtonRole.ActionRole)
        self.next_button = buttons.addButton("Next", QDialogButtonBox.ButtonRole.ActionRole)
        self.back_button.clicked.connect(self.back)
        self.next_button.clicked.connect(self.next)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        index: dict[str, str] = {}
        for f in prepared.files:
            index.setdefault(f.upper(), f)
        self._files = index
        self.step = wizard.next_step(None)
        self.history: list[int] = []
        #: Per group of the step shown: its option widgets.
        self.widgets: list[list[QCheckBox | QRadioButton]] = []
        self._hover: dict[QObject, fomod.Option] = {}
        self._show_step()

    # ------------------------------------------------------------ one step

    def _show_step(self) -> None:
        self._hover.clear()
        self.widgets = []
        page = QWidget()
        page_layout = QVBoxLayout(page)
        step = self.step
        visible = self.wizard.visible_steps()
        if step is None:
            self.step_label.setText("This installer has no options to choose.")
        else:
            s = self.wizard.module.steps[step]
            self.step_label.setText(f"<b>Step {visible.index(step) + 1} of {len(visible)}:</b> "
                                    f"{_escape(s.name or '(unnamed)')}")
            selection = self.wizard.selection(step)
            first: fomod.Option | None = None
            for g, group in enumerate(s.groups):
                box = QGroupBox(f"{group.name or 'Options'} ({_GROUP_HELP[group.type]})")
                box_layout = QVBoxLayout(box)
                single = group.type in (fomod.GroupType.EXACTLY_ONE, fomod.GroupType.AT_MOST_ONE)
                exclusive = QButtonGroup(box) if single else None
                widgets: list[QCheckBox | QRadioButton] = []
                for i, option in enumerate(group.options):
                    kind = self.wizard.option_type(step, g, i)
                    text = option.name + (f"  ({_TYPE_NOTES[kind]})" if kind in _TYPE_NOTES else "")
                    widget = QRadioButton(text) if single else QCheckBox(text)
                    if exclusive is not None:
                        exclusive.addButton(widget)
                    widget.setChecked(i in selection[g])
                    widget.setEnabled(not self.wizard.locked(step, g, i))
                    widget.toggled.connect(lambda _on, g=g: self._changed(g))
                    widget.installEventFilter(self)
                    self._hover[widget] = option
                    widget.clicked.connect(lambda _c=False, option=option: self._describe(option))
                    box_layout.addWidget(widget)
                    widgets.append(widget)
                    first = first or option
                if group.type is fomod.GroupType.AT_MOST_ONE:
                    none = QRadioButton("None")
                    exclusive.addButton(none)
                    none.setChecked(not selection[g])
                    none.toggled.connect(lambda _on, g=g: self._changed(g))
                    box_layout.addWidget(none)
                self.widgets.append(widgets)
                page_layout.addWidget(box)
            if first is not None:
                self._describe(first)
        page_layout.addStretch(1)
        self.scroll.setWidget(page)
        self.back_button.setEnabled(bool(self.history))
        self._update()

    def _chosen(self, g: int) -> list[int]:
        return [i for i, w in enumerate(self.widgets[g]) if w.isChecked()]

    def _changed(self, g: int) -> None:
        if self.step is None:
            return
        chosen = self._chosen(g)
        if self.wizard.problem(self.step, g, chosen) is None:
            self.wizard.choose(self.step, g, chosen)
        self._update()

    def problems(self) -> list[str]:
        if self.step is None:
            return []
        return [why for g in range(len(self.widgets))
                if (why := self.wizard.problem(self.step, g, self._chosen(g))) is not None]

    def _update(self) -> None:
        problems = self.problems()
        self.problem_label.setText("<span style='color:#c0392b'>" + _escape("; ".join(problems)) + "</span>"
                                   if problems else "")
        last = self.step is None or self.wizard.next_step(self.step) is None
        self.next_button.setText("Install" if last else "Next")
        self.next_button.setEnabled(not problems)
        if self.step is not None:  # a choice can show or hide later steps
            visible = self.wizard.visible_steps()
            name = self.wizard.module.steps[self.step].name or "(unnamed)"
            self.step_label.setText(f"<b>Step {visible.index(self.step) + 1} of {len(visible)}:</b> {_escape(name)}")

    def _describe(self, option: fomod.Option) -> None:
        self.description.setPlainText(f"{option.name}\n\n{option.description or ''}".strip())
        pixmap = QPixmap()
        if option.image:
            rel = f"{self.wizard.module.root}/{option.image}" if self.wizard.module.root else option.image
            actual = self._files.get(rel.upper())
            if actual is not None:
                pixmap = QPixmap(str(self.prepared.files_dir / actual))
        self.image.setVisible(not pixmap.isNull())
        if pixmap.isNull():
            self.image.clear()
        else:
            self.image.setPixmap(pixmap.scaled(self.image.width(), max(self.image.height(), 200),
                                               Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Enter and watched in self._hover:
            self._describe(self._hover[watched])
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------ moving on

    def next(self) -> None:
        if self.problems():
            return
        following = None if self.step is None else self.wizard.next_step(self.step)
        if following is None:
            self.accept()
            return
        self.history.append(self.step)
        self.step = following
        self._show_step()

    def back(self) -> None:
        if self.history:
            self.step = self.history.pop()
            self._show_step()


def run_fomod(prepared: staging.Prepared, game: DiscoveredGame | None, run_dialog: Callable[[QDialog], bool], *,
              replace: ModRecord | None = None, wizard: fomod.Wizard | None = None,
              parent: QWidget | None = None) -> tuple[staging.Prepared | None, fomod.Wizard | None, str]:
    """Run *prepared*'s FOMOD installer (*wizard*: again, with the choices made
    before). Returns the prepared archive planned with the chosen files (None:
    cancelled), the wizard, and a note for the user (the installer couldn't
    be run, so its folders are chosen instead; which choices were reused)."""
    note = ""
    if wizard is None:
        try:
            module = service.fomod_module(prepared)
        except fomod.FomodError as exc:
            return prepared, None, f"{exc}. Choose which of its folders to install."
        wizard = fomod.Wizard(module, service.fomod_file_state(game))
        if game is not None:
            existing, previous = service.previous_fomod_choices(prepared, game.game_id, replace=replace)
            if previous:
                skipped = wizard.apply_choices(previous)
                note = (f"The choices made when '{existing.name}' was installed are preselected"
                        + (f" (not available any more: {', '.join(skipped)})." if skipped else "."))
    else:
        wizard = copy.copy(wizard)
        wizard.selections = copy.deepcopy(wizard.selections)
    needs = wizard.unmet_dependencies()
    if needs:
        note += f" The installer says it needs: {needs}."
    if wizard.module.steps and not run_dialog(FomodDialog(wizard, prepared, note=note.strip(), parent=parent)):
        return None, wizard, ""
    return service.apply_fomod(prepared, wizard), wizard, note.strip()


class InstallDialog(QDialog):
    """Install one prepared (extracted) archive: where its files go, whether
    it updates an installed mod, and exactly what changes in the game folder."""

    def __init__(self, prepared: staging.Prepared, games: list[DiscoveredGame], settings: Settings, *,
                 replace: ModRecord | None = None, current: str | None = None, can_install: bool = True,
                 why_not: str = "", fomod_wizard: fomod.Wizard | None = None,
                 run_dialog: Callable[[QDialog], bool] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: The FOMOD installer as last run (None: there is none, or it couldn't run).
        self.fomod_wizard = fomod_wizard
        self._fomod_plan = prepared.plan
        self._run_dialog = run_dialog or (lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted)
        self.prepared = prepared
        self.games = games
        self.settings = settings
        self.installed = [g.game_id for g in games]
        self.can_install = can_install
        self.choice: service.InstallChoice | None = None
        self._why_not = why_not
        self.setWindowTitle(f"Install {prepared.source.name}")
        self.resize(940, 720)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Name:", self.name_edit)
        self.mod_info = _label("")
        form.addRow("", self.mod_info)
        self.game_combo = QComboBox()
        for game in games:
            self.game_combo.addItem(game.name, game.game_id)
        form.addRow("Game:", self.game_combo)
        self.game_info = _label("")
        form.addRow("", self.game_info)
        self.placement_info = _label("", rich=True)
        form.addRow("Goes into:", self.placement_info)
        layout.addLayout(form)

        # Choosing what to install (FOMOD, unclear layouts, or the user's own choice).
        self.choose_box = QGroupBox("Choose what to install")
        choose = QVBoxLayout(self.choose_box)
        choose.addWidget(_label("Tick the folder(s) of the archive to install, and say where they go."))
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabels(["Folder", "Files"])
        self.folder_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.folder_tree.setMinimumHeight(160)
        choose.addWidget(self.folder_tree, 1)
        target_row = QHBoxLayout()
        self.target_group = QButtonGroup(self)
        for text, value in (("Detect", None), ("Data folder", Target.DATA), ("Game folder", Target.GAME_ROOT)):
            radio = QRadioButton(text)
            radio.setProperty("target", value)
            self.target_group.addButton(radio)
            target_row.addWidget(radio)
            if value is None:
                radio.setChecked(True)
        target_row.addStretch(1)
        self.apply_choice = QPushButton("Use this choice")
        target_row.addWidget(self.apply_choice)
        choose.addLayout(target_row)
        self.choice_error = _label("", rich=True)
        choose.addWidget(self.choice_error)
        self.choose_toggle = QCheckBox("Choose the folders myself")
        toggle_row = QHBoxLayout()
        toggle_row.addWidget(self.choose_toggle)
        toggle_row.addStretch(1)
        self.fomod_button = QPushButton("Run its installer again...")
        self.fomod_button.setVisible(fomod_wizard is not None)
        self.fomod_button.clicked.connect(self.run_installer_again)
        toggle_row.addWidget(self.fomod_button)
        layout.addLayout(toggle_row)
        layout.addWidget(self.choose_box)

        update_box = QGroupBox("New mod or update")
        update = QVBoxLayout(update_box)
        row = QHBoxLayout()
        self.as_new = QRadioButton("Install as a new mod")
        self.as_update = QRadioButton("Update the installed mod:")
        self.update_combo = QComboBox()
        self.update_combo.setMinimumWidth(260)
        row.addWidget(self.as_new)
        row.addWidget(self.as_update)
        row.addWidget(self.update_combo, 1)
        update.addLayout(row)
        self.disabled_check = QCheckBox("Add it disabled (nothing of it is linked until you enable it)")
        update.addWidget(self.disabled_check)
        self.update_info = _label("")
        update.addWidget(self.update_info)
        layout.addWidget(update_box)

        self.tabs = QTabWidget()
        self.files_tree = QTreeWidget()
        self.files_tree.setHeaderLabels(["File in the archive", "Goes to", "Note"])
        self.files_tree.setRootIsDecorated(False)
        self.files_tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.changes_text = QPlainTextEdit()
        self.changes_text.setReadOnly(True)
        self.changes_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.warnings_list = QListWidget()
        self.tabs.addTab(self.files_tree, "Files")
        self.tabs.addTab(self.changes_text, "Changes in the game folder")
        self.tabs.addTab(self.warnings_list, "Notes")
        layout.addWidget(self.tabs, 2)

        self.problem_label = _label("", rich=True)
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.install_button = buttons.addButton("Install", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_folders()
        detected = service.detected_game(prepared.plan, settings, self.installed)
        # The game of the mod to update, else the one the files are for, else
        # the one the window shows (*current*). A game that isn't installed is
        # never swapped for another one: the user picks the game (or cancels).
        wanted = replace.game_id if replace is not None else detected or current or settings.current_game
        index = self.game_combo.findData(wanted)
        if index < 0 and (detected is None or replace is not None) and len(games) == 1:
            index = 0
        self.game_combo.setCurrentIndex(index)
        self._detected = detected
        self._replace_wanted = replace
        self._name_touched = False
        # Ticks or target changed but not applied yet: Install waits for "Use this choice".
        self._choice_pending = False
        # The preview is redone once typing in the name pauses, not per key.
        self._name_timer = QTimer(self)
        self._name_timer.setSingleShot(True)
        self._name_timer.setInterval(300)
        self._name_timer.timeout.connect(self._refresh)
        self.name_edit.textEdited.connect(self._name_edited)
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        self.as_new.toggled.connect(self._options_changed)
        self.update_combo.currentIndexChanged.connect(self._options_changed)
        self.disabled_check.toggled.connect(self._options_changed)
        self.apply_choice.clicked.connect(self._apply_choice)
        self.folder_tree.itemChanged.connect(self._choice_edited)
        self.target_group.buttonToggled.connect(self._choice_edited)
        self.choose_toggle.toggled.connect(self._choose_toggled)
        problem = service.installable_problem(prepared.plan)
        self.choose_toggle.setChecked(problem is not None)
        self.choose_box.setVisible(problem is not None)
        self._game_changed()

    # ------------------------------------------------------------ state

    @property
    def game(self) -> DiscoveredGame | None:
        game_id = self.game_combo.currentData()
        return next((g for g in self.games if g.game_id == game_id), None)

    def _name_edited(self, _text: str) -> None:
        self._name_touched = True
        self._name_timer.start()

    def _choose_toggled(self, on: bool) -> None:
        self.choose_box.setVisible(on)
        if not on and self._choice_pending:  # back to the plan in effect
            self._choice_pending = False
            self.folder_tree.blockSignals(True)
            self._fill_folders()
            self.folder_tree.blockSignals(False)
            self._refresh()

    def _choice_edited(self, *_args) -> None:
        self._choice_pending = True
        self._show_pending()

    def _show_pending(self) -> None:
        self.choice = None
        self.install_button.setEnabled(False)
        self.problem_label.setText("<span style='color:#c0392b'>Click 'Use this choice' to see what the folders "
                                   "you ticked install.</span>")

    def _game_changed(self) -> None:
        game = self.game
        self.update_combo.blockSignals(True)
        self.update_combo.clear()
        if game is not None:
            with Manifest() as manifest:
                for mod in manifest.mods(game.game_id):
                    self.update_combo.addItem(mod.name, mod.id)
        self.update_combo.blockSignals(False)
        self._pick_update_target()
        self._refresh()

    def _pick_update_target(self) -> None:
        """Preselect what the update rules find (or the mod asked for)."""
        game = self.game
        existing = None
        if game is not None and self._replace_wanted is not None and self._replace_wanted.game_id == game.game_id:
            existing = self._replace_wanted
        elif game is not None and service.installable_problem(self.prepared.plan) is None:
            with Manifest() as manifest:
                existing = staging.find_existing(manifest, game.game_id, self.prepared)
        for widget in (self.as_new, self.as_update, self.update_combo):
            widget.blockSignals(True)
        if existing is not None:
            self.update_combo.setCurrentIndex(max(self.update_combo.findData(existing.id), 0))
            self.as_update.setChecked(True)
        else:
            self.as_new.setChecked(True)
        self.as_update.setEnabled(self.update_combo.count() > 0)
        for widget in (self.as_new, self.as_update, self.update_combo):
            widget.blockSignals(False)

    def _options_changed(self, *_args) -> None:
        self._refresh()

    def run_installer_again(self) -> None:
        """Change the choices made in the mod's FOMOD installer."""
        if self.fomod_wizard is None:
            return
        base = dataclasses.replace(self.prepared, plan=self._fomod_plan)
        ran, wizard, _note = run_fomod(base, self.game, self._run_dialog, wizard=self.fomod_wizard, parent=self)
        if ran is None:
            return
        self.prepared, self.fomod_wizard = ran, wizard
        if self.choose_toggle.isChecked():
            self.choose_toggle.setChecked(False)
        self._choice_pending = False
        self.folder_tree.blockSignals(True)  # refilled: no choice of the user's
        self._fill_folders()
        self.folder_tree.blockSignals(False)
        self._pick_update_target()
        self._refresh()

    def _fill_folders(self) -> None:
        """The archive's folder tree, each folder with its file count; the
        folders the plan installs are ticked."""
        self.folder_tree.clear()
        counts: dict[str, int] = {}
        for rel in self.prepared.files:
            parts = rel.split("/")[:-1]
            for i in range(len(parts) + 1):
                key = "/".join(parts[:i])
                counts[key] = counts.get(key, 0) + 1
        plan = self.prepared.plan
        ticked = {c.path for c in plan.candidate_roots} if service.installable_problem(plan) else {plan.root}
        items: dict[str, QTreeWidgetItem] = {}
        top = QTreeWidgetItem(self.folder_tree, ["(the whole archive)", str(counts.get("", 0))])
        top.setData(0, Qt.ItemDataRole.UserRole, "")
        top.setCheckState(0, Qt.CheckState.Checked if "" in ticked else Qt.CheckState.Unchecked)
        items[""] = top
        for key in sorted(k for k in counts if k):
            parent = items["/".join(key.split("/")[:-1])]
            item = QTreeWidgetItem(parent, [key.split("/")[-1], str(counts[key])])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setCheckState(0, Qt.CheckState.Checked if key in ticked else Qt.CheckState.Unchecked)
            items[key] = item
        self.folder_tree.expandToDepth(1)

    def _apply_choice(self) -> None:
        roots = []

        def walk(item: QTreeWidgetItem) -> None:
            if item.checkState(0) == Qt.CheckState.Checked:
                roots.append(item.data(0, Qt.ItemDataRole.UserRole))
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.folder_tree.topLevelItemCount()):
            walk(self.folder_tree.topLevelItem(i))
        target = self.target_group.checkedButton().property("target")
        try:
            self.prepared = staging.replan(self.prepared, roots or None, target, installed_games=self.installed)
        except ValueError as exc:
            self.choice_error.setText(f"<span style='color:#c0392b'>{_escape(str(exc))}</span>")
            self._choice_pending = True
            self._show_pending()
            return
        self.choice_error.setText("")
        self._choice_pending = False
        self._pick_update_target()
        self._refresh()

    # ------------------------------------------------------------ what it would do

    def _refresh(self) -> None:
        self._name_timer.stop()
        prepared, plan, game = self.prepared, self.prepared.plan, self.game
        if not self._name_touched:  # _preview shows the name an update keeps
            self.name_edit.setText(staging.suggested_name(prepared))
        bits = []
        if plan.nexus is not None and plan.nexus.is_nexus:
            if plan.nexus.version:
                bits.append(f"version {plan.nexus.version}")
            if plan.nexus.mod_id is not None:
                bits.append(f"Nexus mod {plan.nexus.mod_id}")
        self.mod_info.setText(", ".join(bits) or f"from {prepared.source}")
        guess = plan.game
        if guess.game_id:
            reasons = "; ".join(guess.reasons)
            info = (f"Looks like a {GAMES[guess.game_id].name} mod ({guess.confidence.name.lower()} confidence)"
                    f"{': ' + reasons if reasons else ''}")
        else:
            info = "The game can't be told from the files."
        if self._detected is not None and self._detected not in self.installed:
            info += f" {GAMES[self._detected].name} was not found; pick a game to install it into anyway."
        replace = self._replace_wanted
        if replace is not None and (game is None or game.game_id != replace.game_id):
            info += f" '{replace.name}' (the mod to update) is a {GAMES[replace.game_id].name} mod."
        self.game_info.setText(info)
        problem = service.installable_problem(plan)
        if problem is None:
            reasons = "; ".join(plan.reasons)
            self.placement_info.setText(_escape(f"{_TARGET_TEXT[plan.target]} ({plan.confidence.name.lower()} "
                                                f"confidence)" + (f" -- {reasons}" if reasons else "")))
        else:
            self.placement_info.setText(f"<span style='color:#c0392b'>{_escape(problem)}</span>")
        self.warnings_list.clear()
        for warning in [*plan.warnings, *prepared.warnings]:
            self.warnings_list.addItem(warning)
        if plan.skipped:
            self.warnings_list.addItem(f"{len(plan.skipped)} file(s) such as readmes are not installed: "
                                       + ", ".join(plan.skipped[:5]) + (" ..." if len(plan.skipped) > 5 else ""))
        self.tabs.setTabText(2, f"Notes ({self.warnings_list.count()})")
        self.choice = None
        self.files_tree.clear()
        self.changes_text.setPlainText("")
        why_not = problem or ("" if game is not None else "Pick the game to install it into.")
        if not why_not:
            try:
                why_not = self._preview(game)
            except Exception as exc:  # what the install itself would fail with
                why_not = str(exc)
        self.update_combo.setEnabled(self.as_update.isChecked())
        if not why_not and not self.can_install:
            why_not = self._why_not
        self.problem_label.setText(f"<span style='color:#c0392b'>{_escape(why_not)}</span>" if why_not else "")
        self.install_button.setEnabled(not why_not)
        self.install_button.setText("Update" if self.choice is not None and self.choice.existing else "Install")
        if self._choice_pending:
            self._show_pending()

    def _preview(self, game: DiscoveredGame) -> str:
        """Fill in the files and the planned changes; returns why it can't be
        installed ('' when it can)."""
        target = service.target_for(game, self.settings)
        with Manifest() as manifest:
            replace = None
            if self.as_update.isChecked() and self.update_combo.currentData() is not None:
                replace = manifest.mod(self.update_combo.currentData())
            # Only a name the user typed renames: an update keeps the mod's name.
            name = self.name_edit.text().strip() if self._name_touched else ""
            choice = service.choose_install(manifest, game.game_id, self.prepared, name=name or None,
                                            new=not self.as_update.isChecked(), replace=replace,
                                            disabled=self.disabled_check.isChecked())
            if not self._name_touched:
                self.name_edit.setText(choice.name)
            same_name, looks_same = choice.same_name, False
            if choice.existing is None and same_name is None:
                same_name = next((m.name for m in manifest.mods(game.game_id)
                                  if m.name.casefold() == choice.name.casefold()), None)
                # "Install as a new mod" picked although the update rules matched that mod.
                found = staging.find_existing(manifest, game.game_id, self.prepared) if same_name else None
                looks_same = found is not None and found.name.casefold() == same_name.casefold()
            owners = service.link_owners(game.game_id, manifest)
            resolved = service.resolve_against_game(self.prepared.plan, game, owners,
                                                    replacing=choice.existing.id if choice.existing else None)
            for r in resolved[:MAX_ROWS]:
                note = "" if r.status == "new" else r.status
                if r.case_adjusted:
                    note = f"{note}; {r.case_adjusted}" if note else r.case_adjusted
                QTreeWidgetItem(self.files_tree, [r.source, r.dest, note])
            if len(resolved) > MAX_ROWS:
                QTreeWidgetItem(self.files_tree, [f"... and {len(resolved) - MAX_ROWS} more file(s)", "", ""])
            for col in (0, 1):
                self.files_tree.resizeColumnToContents(col)
            self.tabs.setTabText(0, f"Files ({len(resolved)})")
            if choice.existing is not None:
                text = (f"This updates '{choice.existing.name}'"
                        + (f" {choice.existing.version}" if choice.existing.version else "")
                        + ", keeping its place in the list.")
                if choice.name != choice.existing.name:
                    text += f" It is renamed to '{choice.name}'."
                if choice.disabled:
                    text += " It becomes disabled: its files are removed from the game folder."
            elif choice.disabled:
                text = f"'{choice.name}' goes to the bottom of the list, disabled."
            else:
                text = f"'{choice.name}' goes to the bottom of the list, so it wins conflicts with mods installed."
                if same_name and looks_same:
                    text += (f" '{same_name}' looks like an earlier version of it; choose 'Update the installed "
                             "mod' to update it instead.")
                elif same_name:
                    text += (f" A mod called '{same_name}' is installed too, but it doesn't look like the same "
                             "mod; choose 'Update the installed mod' if this is its new version.")
            self.update_info.setText(text)
            result, mod_id = service.preview_install(manifest, target, self.prepared, choice)
            names = {m.id: m.name for m in manifest.mods(game.game_id)}
            names[mod_id] = choice.name
        if result is None:
            why = "the game is purged" if choice.purged else "the mod is added disabled"
            self.changes_text.setPlainText(f"Nothing is linked now: {why}.")
        else:
            done = describe.summary(describe.changes(result), dry_run=True)
            self.changes_text.setPlainText((f"In {result.plan.root}: {done}.\n\n" if done else "")
                                           + _ops_text(result, names))
        self.choice = choice
        return ""

    def _accept(self) -> None:
        if self._name_timer.isActive():  # the name just typed counts
            self._refresh()
        if self.choice is not None and self.install_button.isEnabled():
            self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        layout = QVBoxLayout(self)
        form = QFormLayout()
        row = QHBoxLayout()
        self.games_dir = QLineEdit(settings.games_dir)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.games_dir, 1)
        row.addWidget(browse)
        form.addRow("Lutris games folder:", row)
        self.scan_depth = QSpinBox()
        self.scan_depth.setRange(1, max(20, settings.scan_depth))  # the command line takes any depth
        self.scan_depth.setValue(settings.scan_depth)
        form.addRow("Folder levels to search:", self.scan_depth)
        self.sevenzip = QLineEdit(settings.sevenzip or "")
        self.sevenzip.setPlaceholderText("found on PATH")
        form.addRow("7z program:", self.sevenzip)
        self.dry_run = QCheckBox("Dry-run mode: show what would change, never change anything")
        self.dry_run.setChecked(settings.dry_run)
        form.addRow("", self.dry_run)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._shown = self._values()

    def _values(self) -> dict[str, object]:
        """The settings as the fields say."""
        return {"games_dir": self.games_dir.text().strip() or "~/Games", "scan_depth": self.scan_depth.value(),
                "sevenzip": self.sevenzip.text().strip() or None, "dry_run": self.dry_run.isChecked()}

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Lutris games folder", os.path.expanduser(self.games_dir.text()))
        if folder:
            self.games_dir.setText(folder)

    def apply(self, target: Settings | None = None) -> bool:
        """Copy the fields the user changed into *target* (the settings as
        saved now -- the command line may have changed others meanwhile;
        default: the ones shown). True when discovery must rescan."""
        s = target if target is not None else self.settings
        changed = {k: v for k, v in self._values().items() if v != self._shown[k]}
        for key, value in changed.items():
            setattr(s, key, value)
        return "games_dir" in changed or "scan_depth" in changed


class GameSettingsDialog(QDialog):
    """Manual paths and the link mode for one game."""

    def __init__(self, game_id: str, settings: Settings, game: DiscoveredGame | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.game_id = game_id
        self.settings = settings
        self.setWindowTitle(f"{GAMES[game_id].name}: game settings")
        current = settings.override(game_id)
        layout = QVBoxLayout(self)
        if game is not None:
            layout.addWidget(_label(f"Found at {game.root} (prefix {game.prefix or '?'}, store {game.store})."))
        form = QFormLayout()
        self.root = QLineEdit(current.root or "")
        self.root.setPlaceholderText("as found")
        form.addRow("Game folder:", self._with_browse(self.root))
        self.prefix = QLineEdit(current.prefix or "")
        self.prefix.setPlaceholderText("as found")
        form.addRow("Wine prefix:", self._with_browse(self.prefix))
        self.wine_user = QLineEdit(current.wine_user or "")
        self.wine_user.setPlaceholderText("as found")
        form.addRow("Wine user:", self.wine_user)
        self.link_mode = QComboBox()
        self.link_mode.addItems(list(LINK_MODES))
        self.link_mode.setCurrentText(current.link_mode)
        form.addRow("Link mode:", self.link_mode)
        self.ignore = QCheckBox("Hide this game")
        self.ignore.setChecked(current.ignore)
        form.addRow("", self.ignore)
        layout.addLayout(form)
        layout.addWidget(_label("Hardlinks are for a Wine that doesn't follow symlinks (e.g. Flatpak Lutris); they "
                                "need the game and ~/.local/share on the same disk. 'Check links' tells you which "
                                "your setup needs."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        layout.addWidget(buttons)
        self._shown = self._values()

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("Browse...")

        def pick() -> None:
            folder = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text() or str(Path.home()))
            if folder:
                edit.setText(folder)

        button.clicked.connect(pick)
        row.addWidget(button)
        return box

    def _defaults(self) -> None:
        for edit in (self.root, self.prefix, self.wine_user):
            edit.clear()
        self.link_mode.setCurrentText("symlink")
        self.ignore.setChecked(False)

    def _values(self) -> dict[str, object]:
        """This game's override as the fields say."""

        def folder(text: str) -> str | None:
            """As the settings read it: ~ is expanded, a relative path is in the home folder."""
            text = text.strip()
            return str(Path.home() / os.path.expanduser(text)) if text else None

        return {"root": folder(self.root.text()), "prefix": folder(self.prefix.text()),
                "wine_user": self.wine_user.text().strip() or None, "link_mode": self.link_mode.currentText(),
                "ignore": self.ignore.isChecked()}

    def apply(self, target: Settings | None = None) -> None:
        """Copy the fields the user changed into this game's override in
        *target* (the settings as saved now -- the command line may have
        changed others meanwhile; default: the ones shown)."""
        s = target if target is not None else self.settings
        o = dataclasses.replace(s.override(self.game_id))
        for key, value in self._values().items():
            if value != self._shown[key]:
                setattr(o, key, value)
        if o == GameOverride():
            s.game_overrides.pop(self.game_id, None)
        else:
            s.game_overrides[self.game_id] = o
