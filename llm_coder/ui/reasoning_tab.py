"""
Reasoning tab: shows the model's parsed <think>...</think> content, if any,
for the most recent turn. Falls back to an activity log message if the model
does not emit separable reasoning.
"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextEdit, QComboBox, QHBoxLayout


class ReasoningTab(QWidget):
    def __init__(self, on_retention_changed, parent=None):
        super().__init__(parent)
        self.on_retention_changed = on_retention_changed
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Reasoning retention policy:"))
        self.combo = QComboBox()
        self.combo.addItems(["keep_all", "keep_last", "drop", "hide"])
        self.combo.currentTextChanged.connect(self.on_retention_changed)
        row.addWidget(self.combo)
        row.addStretch()
        layout.addLayout(row)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText("(No reasoning captured yet. If the model doesn't emit "
                                "separable <think> blocks, this tab will simply stay empty "
                                "and the activity log in chat is the best signal of progress.)")
        layout.addWidget(self.view)

    def set_retention_value(self, value: str):
        idx = self.combo.findText(value)
        if idx >= 0:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(idx)
            self.combo.blockSignals(False)

    def show_reasoning(self, text: str):
        if text.strip():
            self.view.setPlainText(text)
        else:
            self.view.setPlainText("(No reasoning block detected in the last response.)")
