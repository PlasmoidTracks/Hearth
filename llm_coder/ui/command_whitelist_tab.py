"""
Command whitelist tab: shows, adds, and removes allowed base commands
for the run_command tool.  Accessed from Settings -> Command Whitelist.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QInputDialog, QGroupBox, QFormLayout, QComboBox,
)
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt


class CommandWhitelistTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Mode selector ---
        mode_group = QGroupBox("Command approval mode")
        mode_form = QFormLayout(mode_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["notify (ask each time)", "always_allow (no prompts)"])
        mode_form.addRow("Mode:", self.mode_combo)
        layout.addWidget(mode_group)

        # --- Table ---
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Command", "Description", ""])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 60)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setItemPrototype(QTableWidgetItem())
        layout.addWidget(self.table)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton("+ Add Command")
        self.add_btn.clicked.connect(self._add_command)
        self.remove_btn = QPushButton("- Remove")
        self.remove_btn.clicked.connect(self._remove_command)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # --- Info ---
        info = QLabel(
            "<i>Commands in this list are auto-approved regardless of their arguments. "
            "For example, whitelisting 'git' will auto-approve 'git status', "
            "'git diff HEAD', 'git log --oneline', etc.</i>"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#8a8a8a; font-size:10px;")
        layout.addWidget(info)
        layout.addStretch()

    def set_policy(self, policy: dict):
        """Load policy into the UI."""
        mode_str = policy.get("mode", "notify")
        self.mode_combo.setCurrentIndex(0 if mode_str == "notify" else 1)
        self._policy = dict(policy)

        self.table.setRowCount(0)
        allowlist = self._policy.get("allowlist", [])
        for entry in allowlist:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cmd = entry.get("command", "")
            desc = entry.get("description", "")
            self.table.setItem(row, 0, QTableWidgetItem(cmd))
            self.table.setItem(row, 1, QTableWidgetItem(desc))
            # Remove button cell
            remove_action = QTableWidgetItem("🗑")
            remove_action.setTextAlignment(Qt.AlignCenter)
            remove_action.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(row, 2, remove_action)

    def get_policy(self) -> dict:
        """Return current policy dict from the UI."""
        entries = []
        for row in range(self.table.rowCount()):
            cmd_item = self.table.item(row, 0)
            desc_item = self.table.item(row, 1)
            if cmd_item:
                entries.append({
                    "command": cmd_item.text().strip(),
                    "description": desc_item.text().strip() if desc_item else "",
                })
        mode_idx = self.mode_combo.currentIndex()
        mode = "notify" if mode_idx == 0 else "always_allow"
        return {
            "mode": mode,
            "allowlist": entries,
        }

    def _add_command(self):
        cmd, ok1 = QInputDialog.getText(self, "Add Command", "Command name (e.g. 'git', 'tree', 'ls'):")
        if not ok1 or not cmd.strip():
            return
        cmd = cmd.strip()
        
        # Check if already exists
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text().strip().lower() == cmd.lower():
                QMessageBox.information(self, "Duplicate", f"'{cmd}' is already in the allowlist.")
                return

        desc, ok2 = QInputDialog.getText(self, "Add Command", "Description (optional):", text="")
        if not ok2:
            desc = ""
        
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(cmd))
        self.table.setItem(row, 1, QTableWidgetItem(desc))
        remove_action = QTableWidgetItem("🗑")
        remove_action.setTextAlignment(Qt.AlignCenter)
        remove_action.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.table.setItem(row, 2, remove_action)

    def _remove_command(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "No selection", "Select a command to remove, then press Remove.")
            return
        self.table.removeRow(current_row)
