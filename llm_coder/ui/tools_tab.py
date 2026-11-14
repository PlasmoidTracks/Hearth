"""
Tools tab: list available tools and toggle enable/disable.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QCheckBox, QComboBox, QHeaderView,
)
from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QFont, QColor

from core.tools import (
    TOOLS_CATEGORIES, ALL_TOOL_NAMES,
    SCRIPT_TOOL_NAMES, COMMAND_TOOL_NAMES, WRITE_TOOL_NAMES,
    SUBAGENT_TOOL_BLOCKLIST,
)


class StatsBridge(QObject):
    """Thread-safe relay: worker thread emits, GUI thread receives via Qt's queued connection."""
    changed = Signal()


class ToolsTab(QWidget):
    def __init__(self, executor_provider, on_toggle_changed,
                 on_subagent_toggle_changed=None, parent=None):
        super().__init__(parent)
        self.executor_provider = executor_provider
        self.on_toggle_changed = on_toggle_changed
        # Separate callback for the "Subagent" column so a subagent toggle never
        # writes into the main tool_toggles (and vice-versa). Optional: when
        # absent the column is simply hidden, keeping this tab usable for callers
        # that predate the subagent feature.
        self.on_subagent_toggle_changed = on_subagent_toggle_changed
        self._subscribed_executor = None
        self.bridge = StatsBridge()
        self.bridge.changed.connect(self.refresh)
        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top_row.addWidget(refresh_btn)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["On", "Sub", "Tool", "Calls"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 36)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 60)
        # Header tooltips clarify the two columns: "On" is the main agent,
        # "Sub" is what a deployed subagent may use (independent selection).
        self.table.horizontalHeaderItem(0).setToolTip("Enable this tool for the main agent")
        self.table.horizontalHeaderItem(1).setToolTip(
            "Enable this tool for deployed subagents (independent of the 'On' "
            "column). File-editing tools, run_command, web_search and ask_user "
            "are never available to subagents and stay greyed out.")
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setItemPrototype(QTableWidgetItem())
        layout.addWidget(self.table)

        self.refresh()

    def _ensure_subscribed(self, executor):
        if executor is not None and executor is not self._subscribed_executor:
            executor.stats.add_listener(self.bridge.changed.emit)
            self._subscribed_executor = executor

    def refresh(self):
        executor = self.executor_provider()
        self._ensure_subscribed(executor)
        self.table.setRowCount(0)
        if executor is None:
            return

        # Build lookup: tool_name -> total_calls
        all_rows = executor.stats.as_rows()
        calls_map = {}
        for row in all_rows:
            calls_map[row[0]] = row[1]

        # Build entries: category headers + tool rows
        entries = []
        for category_name, tool_names in TOOLS_CATEGORIES.items():
            entries.append(("category", category_name))
            for tool_name in tool_names:
                if tool_name in calls_map:
                    entries.append(("tool", tool_name))

        if not entries:
            return

        total_rows = len(entries)
        self.table.setRowCount(total_rows)

        for row_idx, entry in enumerate(entries):
            kind, name = entry
            if kind == "category":
                self._set_category_row(row_idx, name)
            else:
                self._set_tool_row(row_idx, name, executor, calls_map)

    def _set_category_row(self, row_idx, category_name):
        """Set a category header row spanning all columns."""
        item = QTableWidgetItem(f"▾ {category_name}")
        item.setForeground(QColor(80, 120, 180))
        font = QFont()
        font.setBold(True)
        font.setPointSize(font.pointSize() - 1)
        item.setFont(font)
        item.setBackground(QColor(235, 242, 255))
        self.table.setItem(row_idx, 2, item)

        # Set background on all cells in this row for visual separation
        for col in range(4):
            if col != 2:
                bg_item = QTableWidgetItem()
                bg_item.setBackground(QColor(235, 242, 255))
                bg_item.setFlags(Qt.NoItemFlags)
                self.table.setItem(row_idx, col, bg_item)

        self.table.item(row_idx, 2).setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)

    def _set_tool_row(self, row_idx, tool_name, executor, calls_map):
        """Set a tool row: main toggle, subagent toggle, name, and call count."""
        total_calls = calls_map.get(tool_name, 0)

        # Column 0: main-agent enable/disable checkbox
        checkbox = QCheckBox()
        checkbox.setChecked(executor.is_enabled(tool_name))
        checkbox.toggled.connect(lambda checked, n=tool_name: self._toggle(n, checked))
        self.table.setCellWidget(row_idx, 0, checkbox)

        # Column 1: subagent enable/disable checkbox. Blocklisted tools are shown
        # unchecked and disabled — the choice is not the user's to make, and a
        # greyed box says so better than an absent column would.
        blocked = tool_name in SUBAGENT_TOOL_BLOCKLIST
        sub_cb = QCheckBox()
        sub_cb.setChecked((not blocked) and executor.is_enabled_for_subagent(tool_name))
        sub_cb.setEnabled((not blocked) and self.on_subagent_toggle_changed is not None)
        if blocked:
            sub_cb.setToolTip("Never available to subagents")
        else:
            sub_cb.setToolTip("Available to deployed subagents")
        sub_cb.toggled.connect(lambda checked, n=tool_name: self._toggle_subagent(n, checked))
        self.table.setCellWidget(row_idx, 1, sub_cb)

        # Column 2: tool name
        item = QTableWidgetItem(tool_name)
        item.setFont(QFont(item.font()))
        self.table.setItem(row_idx, 2, item)

        # Column 3: total calls
        call_item = QTableWidgetItem(str(total_calls) if total_calls else "-")
        call_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row_idx, 3, call_item)

    def _toggle(self, name, checked):
        executor = self.executor_provider()
        if executor is None:
            return
        executor.enabled_tools[name] = checked
        self.on_toggle_changed(dict(executor.enabled_tools))

    def _toggle_subagent(self, name, checked):
        if self.on_subagent_toggle_changed is None:
            return
        executor = self.executor_provider()
        if executor is None:
            return
        # Never persist a True for a blocklisted tool, even if a signal slips
        # through before the widget is disabled.
        executor.subagent_enabled_tools[name] = checked and (name not in SUBAGENT_TOOL_BLOCKLIST)
        self.on_subagent_toggle_changed(dict(executor.subagent_enabled_tools))



