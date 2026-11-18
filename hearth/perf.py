"""PerfMetrics: the one place generation speed is measured, stored and formatted.

Before this module existed, "tokens per second" in Hearth was computed independently in
``LlamaServerProvider._usage_event`` (completion tokens ÷ wall-clock duration) and
``orchestrator._merge_usage`` (accumulated across rounds), while llama.cpp's OWN numbers —
the ``timings`` object on the response and the ``print_timing`` lines in the server log —
were never read at all. The sibling harness made the same mistake and wrote it down: three
independent computations overwrote one another, and the least accurate of the three won.

Two sources feed this module, both authoritative and both from llama.cpp itself:
  * ``from_api_timings()``  — the per-response ``timings`` object (prompt_per_second /
    predicted_per_second / prompt_ms / predicted_ms)
  * ``parse_log_line()``    — the ``slot print_timing`` and perf-context log lines, which
    are the ONLY way to get a figure while a turn is still running

Naming discipline, because there are now several speeds on screen at once:
  * ``tokens_per_sec`` (existing, kept) — Hearth's own wall-clock estimate over the whole
    round, including tool-argument characters. It stays what it always was so persisted
    usage rows and the context bar keep working unchanged.
  * ``decode_tps`` / ``prefill_tps`` (new) — llama.cpp's measured rates, present only when
    the server actually reported them. The UI prefers these and marks an estimate as one;
    nothing downstream does arithmetic on either.

The last value is kept after a turn ends rather than cleared: "what was the speed of the
last generation" stays true until the next generation, and a blank label answers nothing.

Keep this module dependency-light (stdlib only) so it is importable and testable alone,
like ``hearth/errors.py`` and ``hearth/output_guard.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# `llama_perf_context: ... perf = 61.74 token/s` and the longer
# `prompt_per_second = / predicted_per_second =` fields that accompany it.
_PERF_RE = re.compile(r"perf\s*=\s*([\d.]+)\s*token/s")
_TIMING_FIELD_RE = re.compile(
    r"(prompt_per_second|predicted_per_second|prompt_per_token_ms|"
    r"predicted_per_token_ms|prompt_ms|predicted_ms)\s*=\s*([\d.]+)"
)
# `slot print_timing: id 0 | task 56 | prompt processing, n_tokens = 91424,
#  progress = 0.73, t = 131.16 s / 697.05 tokens per second`
_PRINT_TIMING_RE = re.compile(
    r"prompt processing\s*,\s*n_tokens\s*=\s*(\d+)\s*,\s*progress\s*=\s*([\d.]+)"
    r"(?:.*?(\d+\.?\d*)\s*tokens per second)?"
)
# `slot print_timing: id  3 | task 0 | n_gen =    262, tg =   2.76 t/s,
#  tg_3s =   2.76 t/s` — the live decode-rate line llama-server repeats while a slot
# generates (default every ~3 s). Two rates on one line: `tg` averages the whole generation
# so it lags hard after a fast start or a stall, while `tg_3s` is llama.cpp's own
# trailing-3-second sliding window — fresher, and what a live display should show. `tg` is
# only the fallback for builds that print no `tg_3s`. `\btg\s*=` cannot leak into `tg_3s`:
# after matching `tg` it requires `=`, and the next character there is `_`.
_DECODE_TIMING_RE = re.compile(r"\bn_gen\s*=\s*\d+.*?\btg\s*=\s*([\d.]+)\s*t/s")
_DECODE_3S_TIMING_RE = re.compile(r"\btg_3s\s*=\s*([\d.]+)\s*t/s")
# A prefill print_timing line can carry an n_gen field too; the decode parser must never
# claim one — its rate is prompt-eval speed, not token generation.
_PROMPT_PROCESSING_RE = re.compile(r"prompt processing", re.IGNORECASE)


def parse_prompt_progress(line: str) -> Optional[Tuple[int, float, Optional[float]]]:
    """(tokens processed so far, progress fraction, prefill rate t/s) from a live
    prompt-processing line, or None when the line is not one.

    The rate is llama.cpp's own number for THIS prompt as printed on this very line — the
    third field, `697.05 tokens per second`. It is None when the build prints no rate, and
    callers must treat that as "no estimate possible" rather than as zero.

    This is the one parser for that line: callers forward what it returns and nothing
    downstream re-derives a rate of its own."""
    m = _PRINT_TIMING_RE.search(line or "")
    if not m:
        return None
    try:
        rate = float(m.group(3)) if m.group(3) else None
        return (int(m.group(1)), float(m.group(2)), rate)
    except (TypeError, ValueError):
        return None


def format_seconds(secs: Optional[float]) -> str:
    """Human duration for a label: '8s', '1m 24s', '12m'.

    Sub-second values — including None and 0 — render as the empty string, so callers can
    write f"..., ≈{format_seconds(x)} left" and get no ETA at all rather than a misleading
    "0s left"."""
    if not secs:
        return ""
    secs = float(secs)
    if secs < 1:
        return ""
    secs = max(0, int(round(secs)))
    if secs < 60:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    return f"{mins}m {rem}s" if rem else f"{mins}m"


@dataclass
class PerfMetrics:
    """Latest known generation speed. None = never reported yet."""
    prefill_tps: Optional[float] = None
    decode_tps: Optional[float] = None
    prompt_ms: Optional[float] = None
    predicted_ms: Optional[float] = None
    source: str = ""          # "api timings" | "server log"

    def has_data(self) -> bool:
        return self.prefill_tps is not None or self.decode_tps is not None

    def as_dict(self) -> dict:
        """Wire form for SSE `usage` payloads. Only fields that carry a value, so a client
        can tell 'not measured' from 'measured zero' without guessing."""
        out = {}
        if self.prefill_tps is not None:
            out["prefill_tps"] = round(float(self.prefill_tps), 2)
        if self.decode_tps is not None:
            out["decode_tps"] = round(float(self.decode_tps), 2)
        if self.prompt_ms is not None:
            out["prompt_ms"] = round(float(self.prompt_ms), 1)
        if self.predicted_ms is not None:
            out["predicted_ms"] = round(float(self.predicted_ms), 1)
        if self.source:
            out["perf_source"] = self.source
        return out

    def summary(self) -> str:
        """One-line human text for a label. Empty when nothing is known."""
        parts = []
        if self.prefill_tps is not None:
            parts.append(f"prefill {self.prefill_tps:.0f} t/s")
        if self.decode_tps is not None:
            parts.append(f"decode {self.decode_tps:.1f} t/s")
        if not parts:
            return ""
        return "⚡ " + ", ".join(parts)


def parse_log_line(line: str) -> Optional[PerfMetrics]:
    """Extract metrics from one llama-server log line, or None if it has none.

    Handles the perf-context summary lines, the live ``print_timing`` prompt-processing
    lines, and the live decode-rate lines (``n_gen = ..., tg = ... t/s, tg_3s = ...``) —
    without those last ones, a decode number could only ever arrive from the end-of-turn
    API timings.
    """
    m = _PERF_RE.search(line or "")
    fields = {fm.group(1): float(fm.group(2)) for fm in _TIMING_FIELD_RE.finditer(line or "")}
    if m or fields:
        perf = PerfMetrics(source="server log")
        # `perf =` is the decode rate on a perf-context line.
        decode = fields.get("predicted_per_second")
        if decode is None and m:
            decode = float(m.group(1))
        perf.decode_tps = decode
        perf.prefill_tps = fields.get("prompt_per_second")
        perf.prompt_ms = fields.get("prompt_ms")
        perf.predicted_ms = fields.get("predicted_ms")
        if perf.has_data():
            return perf

    pm = _PRINT_TIMING_RE.search(line or "")
    if pm:
        rate = pm.group(3)
        if rate:
            perf = PerfMetrics(source="server log")
            perf.prefill_tps = float(rate)
            return perf

    # Live decode-rate line. Only reached for lines that are neither a perf-context summary
    # nor a prompt-processing line, so `tg` here is unambiguously token-generation speed.
    # The n_gen anchor keeps unrelated lines that merely mention a tg rate out of the
    # parser. tg_3s (llama.cpp's own trailing 3 s window) wins over the cumulative tg
    # whenever present: a live display wants the current rate, not an average that lags
    # long after a fast start or a stall.
    if not _PROMPT_PROCESSING_RE.search(line or ""):
        t3 = _DECODE_3S_TIMING_RE.search(line or "")
        dm = _DECODE_TIMING_RE.search(line or "") or (t3 if "n_gen" in (line or "") else None)
        if dm:
            try:
                decode = float(t3.group(1)) if t3 else float(dm.group(1))
            except (TypeError, ValueError):
                return None
            perf = PerfMetrics(source="server log")
            perf.decode_tps = decode
            return perf
    return None


def from_api_timings(timings: Optional[dict]) -> Optional[PerfMetrics]:
    """Build metrics from the ``timings`` object on a chat completion response.

    llama.cpp sends this alongside usage when it can; older builds send nothing, which is a
    legitimate 'no measurement' rather than an error."""
    if not timings:
        return None
    perf = PerfMetrics(source="api timings")
    perf.prefill_tps = timings.get("prompt_per_second")
    perf.decode_tps = timings.get("predicted_per_second")
    perf.prompt_ms = timings.get("prompt_ms")
    perf.predicted_ms = timings.get("predicted_ms")
    return perf if perf.has_data() else None
