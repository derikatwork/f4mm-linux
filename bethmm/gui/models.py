"""The mod list as a Qt table model: checkboxes enable mods, dragging rows
reorders them (the lowest in the list wins conflicts). The model doesn't
change anything itself: it asks, and the window applies the change and
reloads the rows."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import QApplication

from bethmm.service import ModRow

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
                return "\n".join(lines)
            if col == COL_NAME:
                return f"{mod.name}\nfolder: {mod.staging_dir()}" + (
                    f"\nfrom: {mod.source_archive}" if mod.source_archive else "")
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
