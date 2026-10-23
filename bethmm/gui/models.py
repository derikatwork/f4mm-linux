"""The mod list as a Qt table model: checkboxes enable mods, dragging rows
reorders them (the lowest in the list wins conflicts). The model doesn't
change anything itself: it asks, and the window applies the change and
reloads the rows."""

from __future__ import annotations

import html

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import QApplication

from bethmm.service import ModRow


def _tooltip(text: str) -> str:
    """*text* as a tooltip that shows it as it is: Qt renders a tooltip that
    looks like markup (mod, archive and plugin names can) as HTML."""
    return f"<p style='white-space:pre'>{html.escape(text, quote=False)}</p>"

MIME_TYPE = "application/x-bethmm-mod-id"

COL_ENABLED, COL_POS, COL_NAME, COL_VERSION, COL_FILES, COL_CONFLICTS = range(6)
HEADERS = ["", "#", "Mod", "Version", "Files", "Conflicts"]

Index = QModelIndex | QPersistentModelIndex


class ModListModel(QAbstractTableModel):
    #: mod id, enabled
    enableRequested = Signal(int, bool)
    #: mod id, new 0-based position
    moveRequested = Signal(int, int)
    #: archives or folders dropped onto the list (absolute paths)
    filesDropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ModRow] = []

    def set_rows(self, rows: list[ModRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def row_of(self, mod_id: int) -> int | None:
        return next((i for i, r in enumerate(self.rows) if r.mod.id == mod_id), None)

    # ------------------------------------------------------------ table

    def rowCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return {COL_ENABLED: "Enabled: linked into the game folder",
                    COL_POS: "Priority: lower in the list wins conflicts"}.get(section)
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        mod, col = row.mod, index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_ENABLED:
            return Qt.CheckState.Checked if mod.enabled else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_POS:
                return row.position
            if col == COL_NAME:
                return mod.name
            if col == COL_VERSION:
                return mod.version or ""
            if col == COL_FILES:
                return row.files
            if col == COL_CONFLICTS:
                parts = []
                if row.overwrites:
                    parts.append(f"wins over {len(row.overwrites)}")
                if row.overwritten_by:
                    parts.append(f"loses to {len(row.overwritten_by)}")
                return ", ".join(parts)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_CONFLICTS and (row.overwrites or row.overwritten_by):
                lines = []
                if row.overwrites:
                    lines.append("Overwrites files of: " + ", ".join(row.overwrites))
                if row.overwritten_by:
                    lines.append("Overwritten by: " + ", ".join(row.overwritten_by))
                return _tooltip("\n".join(lines))
            if col == COL_NAME:
                return _tooltip(f"{mod.name}\nfolder: {mod.staging_dir()}" + (
                    f"\nfrom: {mod.source_archive}" if mod.source_archive else ""))
        if role == Qt.ItemDataRole.ForegroundRole and not mod.enabled:
            return QBrush(QApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_POS, COL_FILES):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return mod.id
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        # Rows take no drops themselves, so the view drops above or below the
        # row under the cursor (dragging a mod onto the last row makes it last).
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if index.column() == COL_ENABLED:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: Index, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == COL_ENABLED:
            checked = Qt.CheckState(value) == Qt.CheckState.Checked if not isinstance(value, bool) else value
            mod = self.rows[index.row()].mod
            if checked != mod.enabled:
                self.enableRequested.emit(mod.id, checked)
            return False  # applied by the window, which reloads the rows
        return False

    # ------------------------------------------------------------ drag and drop

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [MIME_TYPE, "text/uri-list"]

    def mimeData(self, indexes) -> QMimeData:
        data = QMimeData()
        rows = sorted({i.row() for i in indexes if i.isValid()})
        if rows:
            data.setData(MIME_TYPE, str(self.rows[rows[0]].mod.id).encode())
        return data

    def canDropMimeData(self, data: QMimeData, action, row: int, column: int, parent: Index) -> bool:
        return data.hasFormat(MIME_TYPE) or data.hasUrls()

    def dropMimeData(self, data: QMimeData, action, row: int, column: int, parent: Index) -> bool:
        if data.hasUrls() and not data.hasFormat(MIME_TYPE):
            dropped = [u.toLocalFile() for u in data.urls() if u.isLocalFile()]
            if dropped:
                self.filesDropped.emit(dropped)
            return False
        if not data.hasFormat(MIME_TYPE):
            return False
        try:
            mod_id = int(bytes(data.data(MIME_TYPE)).decode())
        except ValueError:
            return False
        source = self.row_of(mod_id)
        if source is None:
            return False
        if row < 0:  # dropped onto a row: before it; onto empty space: at the end
            row = parent.row() if parent.isValid() else len(self.rows)
        new = row - 1 if source < row else row
        if new != source:
            self.moveRequested.emit(mod_id, new)
        return False  # nothing to remove: the window reloads the rows


# ---------------------------------------------------------------- plugins

PLUGIN_MIME_TYPE = "application/x-bethmm-plugin"

PCOL_ACTIVE, PCOL_POS, PCOL_NAME, PCOL_MOD, PCOL_FLAGS, PCOL_NOTES = range(6)
PLUGIN_HEADERS = ["", "#", "Plugin", "From mod", "Kind", "Notes"]


class PluginListModel(QAbstractTableModel):
    """The load order: tick boxes switch plugins on or off, dragging moves
    them (later ones win). Like the mod list, it asks and the window applies."""

    #: plugin name, active
    activeRequested = Signal(str, bool)
    #: plugin name, new 0-based position in the load order
    moveRequested = Signal(str, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list = []  # plugins.PluginInfo
        self.mod_names: dict[int, str] = {}

    def set_rows(self, rows: list, mod_names: dict[int, str]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.mod_names = dict(mod_names)
        self.endResetModel()

    def row_of(self, name: str) -> int | None:
        return next((i for i, p in enumerate(self.rows) if p.name.lower() == name.lower()), None)

    def rowCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(PLUGIN_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return PLUGIN_HEADERS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return {PCOL_ACTIVE: "Active: the game loads it", PCOL_POS: "Load order: later plugins win"}.get(section)
        return None

    def notes(self, plugin) -> str:
        notes = []
        if plugin.implicit:
            notes.append("the game loads it itself")
        if not plugin.present:
            notes.append("not in Data")
        if plugin.missing:
            notes.append("needs " + ", ".join(plugin.missing))
        if plugin.late:
            notes.append("loads before its master " + ", ".join(plugin.late))
        return "; ".join(notes)

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        plugin, col = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == PCOL_ACTIVE:
            return Qt.CheckState.Checked if plugin.active else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == PCOL_POS:
                return index.row() + 1
            if col == PCOL_NAME:
                return plugin.name
            if col == PCOL_MOD:
                return self.mod_names.get(plugin.mod_id, "?") if plugin.mod_id is not None else ""
            if col == PCOL_FLAGS:
                return " ".join(w for w, on in (("master", plugin.master), ("light", plugin.light)) if on)
            if col == PCOL_NOTES:
                return self.notes(plugin)
        if role == Qt.ItemDataRole.ToolTipRole and col == PCOL_NAME and plugin.masters:
            return _tooltip("Masters: " + ", ".join(plugin.masters))
        if role == Qt.ItemDataRole.ForegroundRole:
            if plugin.missing or plugin.late or not plugin.present:
                return QBrush(Qt.GlobalColor.red)
            if not plugin.active:
                return QBrush(QApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
        if role == Qt.ItemDataRole.TextAlignmentRole and col == PCOL_POS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return plugin.name
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        plugin = self.rows[index.row()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if plugin.movable:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if index.column() == PCOL_ACTIVE and not plugin.implicit:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: Index, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == PCOL_ACTIVE:
            checked = Qt.CheckState(value) == Qt.CheckState.Checked if not isinstance(value, bool) else value
            plugin = self.rows[index.row()]
            if checked != plugin.active and not plugin.implicit:
                self.activeRequested.emit(plugin.name, checked)
        return False

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [PLUGIN_MIME_TYPE]

    def mimeData(self, indexes) -> QMimeData:
        data = QMimeData()
        rows = sorted({i.row() for i in indexes if i.isValid()})
        if rows:
            data.setData(PLUGIN_MIME_TYPE, self.rows[rows[0]].name.encode("utf-8"))
        return data

    def canDropMimeData(self, data: QMimeData, action, row: int, column: int, parent: Index) -> bool:
        return data.hasFormat(PLUGIN_MIME_TYPE)

    def dropMimeData(self, data: QMimeData, action, row: int, column: int, parent: Index) -> bool:
        if not data.hasFormat(PLUGIN_MIME_TYPE):
            return False
        name = bytes(data.data(PLUGIN_MIME_TYPE)).decode("utf-8", errors="replace")
        source = self.row_of(name)
        if source is None:
            return False
        if row < 0:
            row = parent.row() if parent.isValid() else len(self.rows)
        new = row - 1 if source < row else row
        if new != source:
            self.moveRequested.emit(self.rows[source].name, new)
        return False
