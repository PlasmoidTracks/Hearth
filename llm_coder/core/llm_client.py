"""
LLM client for the local llama-server OpenAI-compatible API.
Handles streaming, tool calls, UTF-8 decoding, and retries malformed
tool-call JSON emitted by the model (a known local-model failure mode
when a tool argument contains a large code string with an unescaped
quote or newline).
"""
import json
import re
import requests
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Callable

MALFORMED_TOOLCALL_RE = re.compile(r"Failed to parse tool call arguments as JSON", re.IGNORECASE)

# llama-server refuses to prefill an assistant message when the conversation
# cannot be continued as an open prefix — in particular when the last assistant
# turn still carries tool calls (HTTP 400 "Cannot continue an assistant message
# that contains tool calls."). Which shapes a given build accepts varies, so
# the caller cannot predict it: it has to ask and find out. Matching on the
# message (not just the status code) keeps genuine 400s — bad payloads, context
# overflow — surfacing as real errors instead of being silently retried.
PREFILL_REJECTED_RE = re.compile(
    r"cannot continue an assistant message|assistant.{0,40}prefill|add_generation_prompt",
    re.IGNORECASE | re.DOTALL,
)


class PrefillRejectedError(RuntimeError):
    """The server refused to prefill an assistant message (HTTP 400).

    Carries the server's own message so the caller can explain *why* Continue
    could not extend the message, rather than showing a raw 400 or quietly
    retrying with an injected instruction.
    """


def is_prefill_rejection(exc: Exception) -> bool:
    """True when ``exc`` is the server refusing to prefill an assistant message.

    Used to turn that specific 400 into a clear message about the server or
    model not supporting assistant prefill.
    """
    return bool(PREFILL_REJECTED_RE.search(str(exc)))


def _raise_for_status(resp: requests.Response, assistant_prefix: bool):

    """raise_for_status, but with the server's error text in the message.

    requests' own HTTPError only says "400 Client Error: Bad Request for url:
    ...", which makes it impossible to tell *why* the server rejected the call.
    llama-server always answers errors with a JSON body that does say, so for
    the one case we can recover from — a refused prefill — pull it out and
    raise a typed error. Anything else keeps the normal raise_for_status path.
    """
    if resp.status_code < 400:
        resp.raise_for_status()
        return
    if assistant_prefix:
        try:
            body = resp.json()
        except Exception:
            body = None
        detail = ""
        if isinstance(body, dict):
            detail = str((body.get("error") or {}).get("message") or "")
        if is_prefill_rejection(Exception(detail or resp.text)):
            raise PrefillRejectedError(detail or resp.text)
    resp.raise_for_status()


def _build_chat_template_kwargs(enable_thinking=None, preserve_thinking=None) -> Optional[dict]:
    """Merge every chat-template variable this client ever sends into one dict.

    Returns None when there is nothing to send, so callers can omit the key
    entirely rather than transmit an empty object.

    Note what is deliberately NOT here: ``add_generation_prompt``. It used to be
    sent as ``False`` for a "Continue" turn on the theory that this suppresses
    the trailing ``assistant`` header and therefore leaves the last assistant
    message open. That theory is wrong, and it is what broke the button. In
    llama.cpp, emitting the generation header and prefilling the assistant text
    are the SAME decision, made once by ``add_generation_prompt``: when it is
    true the server writes ``assistant`` *and* the text of a
    trailing assistant message after it. Forcing it to False therefore does not
    open the assistant turn -- it removes it, leaving the prompt ending on a
    user turn, where the model starts a brand-new reply instead of continuing.

    Assistant prefill is already the server default (``--prefill-assistant`` is
    enabled unless explicitly turned off). The way to continue an assistant
    message is simply to make it the last message in ``messages`` and send the
    request normally -- see ``Agent.prepare_continue``.

    IMPORTANT: for prefill to work, this function must return None (i.e. the
    caller must pass enable_thinking=None and preserve_thinking=None).  Any
    chat_template_kwargs changes how the Jinja template renders the assistant
    turn and breaks the open-prefix structure that llama.cpp needs.
    """
    kwargs = {}
    if enable_thinking is not None:
        kwargs["enable_thinking"] = enable_thinking
    if preserve_thinking is not None:
        kwargs["preserve_thinking"] = preserve_thinking
    return kwargs or None


@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: Optional[List[dict]] = None
    raw: Optional[dict] = None
    prompt_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reasoning_content: Optional[str] = None
    # Timing fields populated from llama-server's "timings" object
    prompt_per_second: Optional[float] = None
    predicted_per_second: Optional[float] = None
    prompt_ms: Optional[float] = None
    predicted_ms: Optional[float] = None
    prompt_per_token_ms: Optional[float] = None
    predicted_per_token_ms: Optional[float] = None

    def get(self, key, default=None):
        return getattr(self, key, default)


class LLMClient:
    def __init__(self, base_url: str):
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")]
        self.base_url = base_url

    def _endpoint(self):
        return f"{self.base_url}/v1/chat/completions"

    def _force_utf8(self, resp: requests.Response):
        # requests sometimes guesses an 8-bit encoding from headers/content-sniffing.
        # llama-server always emits UTF-8 JSON; force it explicitly so multi-byte
        # emoji sequences don't get mangled into "Ã°Å¸..." style garbage.
        resp.encoding = "utf-8"

    def chat(self, messages: List[Dict[str, Any]], tools=None, _retry_note: str = None,
              enable_thinking: bool = None, preserve_thinking: bool = None,
              assistant_prefix: bool = False) -> LLMResponse:
        payload = {"messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        if _retry_note:
            payload["messages"] = messages + [{"role": "user", "content": _retry_note}]
        chat_kwargs = _build_chat_template_kwargs(
            enable_thinking, preserve_thinking)
        if chat_kwargs:
            payload["chat_template_kwargs"] = chat_kwargs

        resp = requests.post(self._endpoint(), json=payload, timeout=600)
        self._force_utf8(resp)

        if resp.status_code >= 500:
            body_text = resp.text
            if MALFORMED_TOOLCALL_RE.search(body_text) and not _retry_note:
                # Model produced a tool call with broken JSON (usually an
                # unescaped quote/newline inside a large code string argument).
                # Retry once with a corrective nudge instead of surfacing a raw 500.
                nudge = (
                    "Your previous tool call had malformed JSON arguments "
                    "(likely an unescaped quote or newline inside a string value). "
                    "Resend the same tool call with valid JSON. If the file content is "
                    "large, split it into multiple smaller write_file/replace_in_file/append_to_file/prepend_to_file "
                    "calls instead of one very large string."
                )
                return self.chat(messages, tools=tools, _retry_note=nudge,
                                 enable_thinking=enable_thinking,
                                 preserve_thinking=preserve_thinking,
                                 assistant_prefix=assistant_prefix)
            _raise_for_status(resp, assistant_prefix)
        _raise_for_status(resp, assistant_prefix)

        data = resp.json()
        choice = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        timings = data.get("timings") or {}
        return LLMResponse(
            content=choice.get("content"),
            tool_calls=choice.get("tool_calls"),
            raw=data,
            prompt_tokens=usage.get("prompt_tokens"),
            total_tokens=usage.get("total_tokens"),
            reasoning_content=choice.get("reasoning_content"),
            # Timing fields from llama-server
            prompt_per_second=timings.get("prompt_per_second"),
            predicted_per_second=timings.get("predicted_per_second"),
            prompt_ms=timings.get("prompt_ms"),
            predicted_ms=timings.get("predicted_ms"),
            prompt_per_token_ms=timings.get("prompt_per_token_ms"),
            predicted_per_token_ms=timings.get("predicted_per_token_ms"),
        )

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools=None,
        on_delta: Callable[[str, str, str, Optional[int]], None] = None,
        cancel_check: Callable[[], bool] = None,
        _retry_note: str = None,
        enable_thinking: bool = None,
        preserve_thinking: bool = None,
        assistant_prefix: bool = False,
    ) -> LLMResponse:
        payload = {"messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        if _retry_note:
            payload["messages"] = messages + [{"role": "user", "content": _retry_note}]
        chat_kwargs = _build_chat_template_kwargs(
            enable_thinking, preserve_thinking)
        if chat_kwargs:
            payload["chat_template_kwargs"] = chat_kwargs

        # ---- DEBUG: log the outgoing request for continuation diagnostics ----
        if assistant_prefix:
            import logging as _logging
            _dbg = _logging.getLogger("llm_client.prefill")
            if not _dbg.handlers:
                import sys as _sys
                _h = _logging.StreamHandler(_sys.stderr)
                _h.setFormatter(_logging.Formatter("[PREFILL-DBG] %(message)s"))
                _dbg.addHandler(_h)
            _dbg.setLevel(_logging.DEBUG)
            last_msg = payload["messages"][-1] if payload["messages"] else {}
            _dbg.warning(
                "REQUEST: last_msg_role=%s last_msg_content=%.120r "
                "n_messages=%d tools=%s chat_kwargs=%s",
                last_msg.get("role"), last_msg.get("content"),
                len(payload["messages"]),
                "YES" if payload.get("tools") else "none",
                payload.get("chat_template_kwargs", "none"),
            )
            # Log every message role + first 60 chars of content
            for _i, _m in enumerate(payload["messages"]):
                _c = _m.get("content", "")
                if isinstance(_c, list):
                    _c = " ".join(p.get("text", "") for p in _c if isinstance(p, dict))
                _dbg.warning("  msg[%d] role=%s content=%.60r", _i, _m.get("role"), _c)
        # ---- END DEBUG ----

        content_parts = []
        reasoning_parts = []
        tool_calls_acc: Dict[int, dict] = {}
        stream_usage: Dict[str, Optional[int]] = {"total_tokens": None, "prompt_tokens": None}
        stream_timings: Dict[str, Optional[float]] = {
            "prompt_per_second": None,
            "predicted_per_second": None,
            "prompt_ms": None,
            "predicted_ms": None,
            "prompt_per_token_ms": None,
            "predicted_per_token_ms": None,
        }

        with requests.post(self._endpoint(), json=payload, stream=True, timeout=600) as resp:
            self._force_utf8(resp)

            if resp.status_code >= 500:
                body_text = resp.text
                if MALFORMED_TOOLCALL_RE.search(body_text) and not _retry_note:
                    nudge = (
                        "Your previous tool call had malformed JSON arguments "
                        "(likely an unescaped quote or newline inside a string value). "
                        "Resend the same tool call with valid JSON. If the file content "
                        "is large, split it into multiple smaller write_file/replace_in_file/append_to_file/prepend_to_file "
                        "calls instead of one very large string."
                    )
                    return self.chat_stream(
                        messages, tools=tools, on_delta=on_delta,
                        cancel_check=cancel_check, _retry_note=nudge,
                        enable_thinking=enable_thinking,
                        preserve_thinking=preserve_thinking,
                        assistant_prefix=assistant_prefix,
                    )
                _raise_for_status(resp, assistant_prefix)
            _raise_for_status(resp, assistant_prefix)

            for raw_line in resp.iter_lines(decode_unicode=True):
                if cancel_check and cancel_check():
                    break
                if not raw_line:
                    continue
                line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                usage_chunk = chunk.get("usage")
                timings_chunk = chunk.get("timings")
                if usage_chunk or timings_chunk:
                    if usage_chunk:
                        stream_usage["total_tokens"] = usage_chunk.get("total_tokens")
                        stream_usage["prompt_tokens"] = usage_chunk.get("prompt_tokens")
                        # Forward real token count to the UI via on_delta so it can
                        # display actual tokens instead of chars during/after streaming.
                        if on_delta and stream_usage["total_tokens"] is not None:
                            completion_tokens = max(
                                0, stream_usage["total_tokens"] - stream_usage["prompt_tokens"]
                            )
                            on_delta("", "usage", "", completion_tokens)
                            # Also forward total_tokens (separate phase) so components
                            # like the context-meter can update live during inference,
                            # not only after the stream finishes.
                            on_delta("", "usage_total", "", stream_usage["total_tokens"])

                    # Extract timing info from llama-server's "timings" object
                    if timings_chunk:
                        stream_timings["prompt_per_second"] = timings_chunk.get("prompt_per_second")
                        stream_timings["predicted_per_second"] = timings_chunk.get("predicted_per_second")
                        stream_timings["prompt_ms"] = timings_chunk.get("prompt_ms")
                        stream_timings["predicted_ms"] = timings_chunk.get("predicted_ms")
                        stream_timings["prompt_per_token_ms"] = timings_chunk.get("prompt_per_token_ms")
                        stream_timings["predicted_per_token_ms"] = timings_chunk.get("predicted_per_token_ms")
                        if on_delta:
                            on_delta(json.dumps(stream_timings), "timings", "", None)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    # ---- DEBUG: log first content pieces for prefill diagnostics ----
                    if assistant_prefix and len(content_parts) <= 10:
                        import logging as _logging
                        _dbg = _logging.getLogger("llm_client.prefill")
                        _dbg.warning(
                            "OUTPUT[%d]: %.80r", len(content_parts), piece
                        )
                    # ---- END DEBUG ----
                    if on_delta:
                        on_delta(piece, "content", "")
                reasoning_piece = delta.get("reasoning_content")
                if reasoning_piece:
                    reasoning_parts.append(reasoning_piece)
                    if on_delta:
                        on_delta(reasoning_piece, "reasoning", "")

                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    acc = tool_calls_acc.setdefault(idx, {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
                        if on_delta:
                            on_delta(fn["arguments"], "tool_args", acc["function"]["name"])

        tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)] if tool_calls_acc else None
        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            prompt_tokens=stream_usage["prompt_tokens"],
            total_tokens=stream_usage["total_tokens"],
            reasoning_content="".join(reasoning_parts) or None,
            # Timing fields from llama-server
            prompt_per_second=stream_timings["prompt_per_second"],
            predicted_per_second=stream_timings["predicted_per_second"],
            prompt_ms=stream_timings["prompt_ms"],
            predicted_ms=stream_timings["predicted_ms"],
            prompt_per_token_ms=stream_timings["prompt_per_token_ms"],
            predicted_per_token_ms=stream_timings["predicted_per_token_ms"],
        )