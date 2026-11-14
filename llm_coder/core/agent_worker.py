"""
QThread worker that runs Agent.step() off the UI thread, forwarding streaming
activity, deltas, and the final result back via signals.
"""
from PySide6.QtCore import QThread, Signal


class AgentWorker(QThread):
    delta_received = Signal(str, str, str, object, int)  # piece, phase, tool_name, tokens, round_num
    activity_changed = Signal(str)
    tool_event = Signal(str, str, dict)
    finished_step = Signal(object)
    web_fetch_truncated = Signal(dict)  # {url, truncated_reasons}
    failed = Signal(str)

    def __init__(self, agent, use_streaming: bool = True, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.use_streaming = use_streaming

    def run(self):
        previous_cb = getattr(self.agent, "activity_callback", None)

        def _on_delta_wrapper(piece, phase, tool_name="", tokens=None, round_num=0):
            """Forward delta to UI + capture real token counts for live context-meter updates."""
            if tokens is not None and phase == "usage_total":
                self.agent.last_known_real_tokens = tokens
            self.delta_received.emit(piece, phase, tool_name or "", tokens, round_num)

        try:
            self.agent.activity_callback = lambda state: self.activity_changed.emit(state)
            self.agent.tool_event_callback = lambda phase, name, payload: self.tool_event.emit(phase, name, payload)
            result = self.agent.step(
                use_streaming=self.use_streaming,
                on_delta=_on_delta_wrapper,
            )
            # Emit web fetch truncation signal before the generic finished_step
            if result.web_fetch_truncation:
                self.web_fetch_truncated.emit(result.web_fetch_truncation)
            self.finished_step.emit(result)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            try:
                self.agent.activity_callback = previous_cb or (lambda state: None)
            except Exception:
                pass

    def cancel(self):
        self.agent.cancel()

class CompactionWorker(QThread):
    finished_compaction = Signal(str)
    failed = Signal(str)

    def __init__(self, agent, strategy=None, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.strategy = strategy

    def run(self):
        try:
            summary = self.agent.compact_history(strategy=self.strategy)
            self.finished_compaction.emit(summary)
        except Exception as e:
            self.failed.emit(str(e))
