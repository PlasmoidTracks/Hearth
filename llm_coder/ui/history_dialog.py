"""
History panel: browse snapshot nodes, revert to a snapshot, or restore a single file.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QInputDialog
)
from datetime import datetime

from core.snapshots import SnapshotStore
from core.workspace import Workspace


class HistoryDialog(QDialog):
    def __init__(self, store: SnapshotStore, workspace: Workspace, parent=None):
        super().__init__(parent)
        self.store = store
        self.workspace = workspace
        self.setWindowTitle("Snapshot History")
        self.setMinimumSize(520, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Snapshots (most recent last). Select one to revert or restore a file from it."))

        self.list_widget = QListWidget()
        self._populate()
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        revert_btn = QPushButton("Revert to this snapshot")
        restore_file_btn = QPushButton("Restore single file...")
        close_btn = QPushButton("Close")
        revert_btn.clicked.connect(self._revert)
        restore_file_btn.clicked.connect(self._restore_file)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(revert_btn)
        btn_row.addWidget(restore_file_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _populate(self):
        self.list_widget.clear()
        for node in self.store.list_snapshots():
            ts = datetime.fromtimestamp(node["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
            files = ", ".join(f["path"] for f in node["files"])
            label = f"[{ts}] {node['prompt'][:60]!r} -> {files}"
            item = QListWidgetItem(label)
            item.setData(1000, node["id"])
            self.list_widget.addItem(item)

    def _selected_snapshot_id(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, "No selection", "Select a snapshot first.")
            return None
        return item.data(1000)

    def _revert(self):
        snap_id = self._selected_snapshot_id()
        if not snap_id:
            return
        confirm = QMessageBox.question(
            self, "Confirm revert",
            "This will restore files to their state before this snapshot. Continue?"
        )
        if confirm != QMessageBox.Yes:
            return
        touched = self.store.restore_snapshot(snap_id, self.workspace)
        QMessageBox.information(self, "Reverted", f"Restored: {', '.join(touched) or '(none)'}")
        self._populate()

    def _restore_file(self):
        snap_id = self._selected_snapshot_id()
        if not snap_id:
            return
        meta = self.store.get_meta(snap_id)
        paths = [f["path"] for f in meta["files"]]
        if not paths:
            QMessageBox.information(self, "No files", "This snapshot has no file records.")
            return
        path, ok = QInputDialog.getItem(self, "Restore file", "Choose file:", paths, editable=False)
        if not ok:
            return
        success = self.store.restore_single_file(snap_id, path, self.workspace)
        if success:
            QMessageBox.information(self, "Restored", f"Restored {path} from snapshot.")
        else:
            QMessageBox.warning(self, "Not found", f"No stored version of {path} in this snapshot (it may have been newly created).")
