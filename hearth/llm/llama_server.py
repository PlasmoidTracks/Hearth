"""LlamaServerProvider — talks to a running llama-server via its OpenAI-compatible
API (`/v1/chat/completions`) with streaming. Parses incremental tool calls from
`delta.tool_calls` and reports prefill/eval + tokens/sec from the final usage.

This is an HTTP client only; process launching lives in manager.py. If the server
is unreachable, health() reports not-ok and the manager falls back to stub.
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from typing import AsyncIterator

import httpx

from .base import Provider, StreamEvent, ToolRequest

# `"key": "value"` — used to pull the first string value (the file path in practice)
# out of a PARTIAL tool-call JSON fragment for the live progress chip. Tolerant by
# design: no json.loads on a fragment that is, by definition, unfinished.
_FIRST_STR_RE = re.compile(r'"[^"\\]{1,64}"\s*:\s*"((?:[^"\\]|\\.)*)"?')

# Internal signal (never surfaces to callers) meaning "the server 400'd specifically
# on parallel_tool_calls — retry once without it".
_RETRY_NO_PARALLEL = object()


class LlamaServerProvider(Provider):
    name = "llama-server"

    def __init__(self, base_url: str, model: str = "local"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def health(self) -> dict:
        """Liveness + slot info. `n_ctx` comes from /props when the build exposes it."""
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                r = await c.get(f"{self.base_url}/health")
                out = {"ok": r.status_code == 200, "provider": self.name,
                       "status": r.status_code}
                # /props is cheap and reports the real loaded context ("total_slots"
                # too), which is what the UI's context bar should divide by.
                try:
                    p = await c.get(f"{self.base_url}/props")
                    if p.status_code == 200:
                        props = p.json() or {}
                        nc = props.get("default_generation_settings", {}).get("n_ctx") \
                            or props.get("total_n_ctx")
                        if nc:
                            out["n_ctx"] = int(nc)
                        out["model"] = props.get("model_path", "")
                        # Some builds report the loaded projector; when present it is a
                        # truthful "vision is live" signal (vs. merely configured).
                        mp = props.get("mmproj_path") or props.get("mmpath") or ""
                        if mp:
                            out["mmproj"] = mp
                except Exception:  # noqa: BLE001 — /props is optional (older builds)
                    pass
                return out
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(e)[:200]}

    async def stream(self, messages, tools, params) -> AsyncIterator[StreamEvent]:
        """Run one model round-trip, yielding StreamEvents.

        Thin wrapper: builds the request body, delegates the HTTP exchange to
        _exchange(), and classifies failures so that OUR bugs are never reported as a
        llama-server disconnect (see the exception handlers at the bottom).
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": params.get("temperature", 0.7),
            "top_p": params.get("top_p", 0.95),
            "max_tokens": params.get("max_tokens", 2048),
        }
        # Ask for real token accounting. The OpenAI-compat protocol does NOT send
        # `usage` on a streamed response unless the client opts in — so without this
        # every prompt_tokens figure in Hearth was a chars/4 guess over stored message
        # text (which excludes the system prompt, workspace listing, tool-call JSON and
        # tool results), and the context bar barely moved. llama-server answers with one
        # final usage chunk. Same opt-in llm_coder uses on every request.
        # Opt-out for a server that rejects the field: Admin → Model → "Ask the server
        # for token usage" off (env fallback HEARTH_LLM_INCLUDE_USAGE=0).
        include_usage = params.get("include_usage", True)
        if isinstance(include_usage, str):
            include_usage = include_usage.strip().lower() not in ("0", "false", "no", "off", "")
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        # Repetition penalty (Admin → Generation). Omitted at the neutral 1.0 so an
        # unconfigured install sends exactly what it always did — and so a server build
        # that rejects the field can never break chat unless the admin asked for it.
        try:
            rp = float(params.get("repetition_penalty", 1.0) or 1.0)
        except (TypeError, ValueError):
            rp = 1.0
        if rp > 1.0:
            body["repetition_penalty"] = rp
        if tools:
            body["tools"] = tools
            # Ask for narration alongside tool calls: without content, a tool-only
            # turn shows the user nothing but chips. llama-server >=b4873 accepts
            # this; older builds 400 on it — handled by the retry below.
            body["parallel_tool_calls"] = True

        t0 = time.time()

        # The HTTP client MUST outlive the body read. A streamed response is readable
        # only while its client is open. The previous structure entered `async with
        # httpx.AsyncClient()` for the request alone and then LEAVED that block before
        # iterating the body (nothing yields inside it), closing the connection pool
        # under the still-pending body: the first aiter_lines() read then raised
        # ReadError('') in ~0.1s, which got reported as "llama-server dropped us" while
        # the server was perfectly healthy. _exchange() keeps the client open across
        # the entire read, which is what fixes it.
        try:
            async for ev in self._exchange(body, t0):
                if ev is _RETRY_NO_PARALLEL:
                    # Older llama-server builds reject parallel_tool_calls with a 400.
                    # Strip it once and retry; a second rejection surfaces plainly.
                    body.pop("parallel_tool_calls", None)
                    async for ev2 in self._exchange(body, t0):
                        if ev2 is _RETRY_NO_PARALLEL:
                            yield StreamEvent("error",
                                              text="llama-server rejected the request")
                            yield StreamEvent("done", data={"finish": "error"})
                            return
                        yield ev2
                    return
                yield ev
        except asyncio.CancelledError:
            # Client disconnected / turn aborted. Not a crash — let it propagate so the
            # orchestrator reports an abort rather than an error.
            raise
        except (httpx.TransportError, ConnectionError, OSError) as e:
            # Genuine transport-level failure. Not necessarily the server's fault: when
            # OUR side cancels the read (idle timeout in orchestrator._stream_checked,
            # client disconnect), httpx also surfaces it as ReadError — so the wording
            # shares both possibilities instead of blaming llama-server alone.
            msg = str(e).strip() or type(e).__name__
            yield StreamEvent("error", text=f"llama-server stream interrupted ({msg}) — the "
                                            f"connection was cancelled or dropped mid-response; "
                                            f"check llama-server health and HEARTH_STREAM_IDLE_TIMEOUT_S")
            yield StreamEvent("done", data={"finish": "error"})
            return
        except Exception as e:  # noqa: BLE001 — our own bug, NOT the server's fault
            # Deliberately loud and clearly attributed. Parsing/JSON/state faults here
            # must never be dressed up as a llama-server disconnect again; that
            # misdirection cost several debugging rounds against a healthy server.
            import traceback
            traceback.print_exc()
            yield StreamEvent("error", text=f"Hearth provider error while parsing the model "
                                            f"stream: {type(e).__name__}: {str(e)[:200]} "
                                            f"(this is a Hearth bug, not llama-server; "
                                            f"stack trace in the server console)")
            yield StreamEvent("done", data={"finish": "error"})
            return

    async def _exchange(self, body: dict, t0: float) -> AsyncIterator[StreamEvent]:
        """One HTTP request plus the full streamed read of its response body.

        The httpx client stays open for the whole body iteration (the fix). Yields
        StreamEvents, or _RETRY_NO_PARALLEL if the server 400'd specifically on
        parallel_tool_calls so stream() can retry once without that field.
        """
        # accumulate tool-call fragments across deltas (OpenAI streams them split up)
        tool_acc: dict[int, dict] = {}
        finish_reason = None
        completion_chars = 0
        # Real token counts from the server's usage chunk. `last_usage` is what we
        # report; `saw_real_usage` suppresses the chars/4 fallback at the end so a
        # measured number is never overwritten by an estimate.
        last_usage: dict = {}
        saw_real_usage = False
        # Per tool-call slot: {"n": chars at the last hint sent, "started": bool}.
        # NB: keyed by the OpenAI delta `index` (an int) — never mix in string keys
        # like "started" at the top level, or lookups for real slots break.
        last_prog: dict[int, dict] = {}

        def _prog_hints(idx: int, args: str, name: str, first: str) -> list[StreamEvent]:
            """Throttled tool_progress hints while a call's arguments are streaming.

            Without them the UI is silent for as long as the model composes a large
            file (tens of seconds for a big fs_write), which looks exactly like a hang.

            Two parts: an EARLY first hint (as soon as ~32 B exist AND the path is
            known, so "it started writing <file>" appears immediately even for a call
            that ends up tiny), then one per ≥256 B of new arguments — the original
            throttle, which keeps SSE chatter cheap. The client smooths between hints,
            so finer steps buy nothing visible (see chat.js growEstimate). No hint ever
            fires before the path has streamed in (see below).

            Returns a LIST and takes VALUES, not the slot dict: this is called from
            inside `async for`, where its body runs after the enclosing loop has moved
            on, so any variable captured from that loop would be rebound to the NEXT
            tool call by then (that late binding is what previously made hints silently
            stop). All state lives in `last_prog`, keyed by the delta `index` — an int;
            never mix string keys into it or per-slot lookups break.
            """
            n = len(args)
            if n == 0:
                return []
            # NO hint while the file path is still unknown. The client keys progress
            # chips by name|round and cannot rename a chip once drawn, so an early
            # hint fired before `"path": "…"` has streamed in creates a stray
            # "writing · 32 B" chip with no filename that never visibly resolves
            # (the real chip appears keyed by the path). Once `first` exists every
            # throttle step below fires normally.
            if not first:
                return []
            st = last_prog.setdefault(idx, {"n": 0, "started": False})
            # EARLY first hint so a short write still shows "writing <file>" at once.
            if not st["started"] and n >= 32:
                st["started"] = True
                st["n"] = n
                return [StreamEvent.tool_progress(name, args, first_name=first)]
            # Then one per ≥256 B of NEW arguments. Called once per delta, so at most
            # one hint fires here — the gap between this and the previous check is a
            # single delta, never several 256 B intervals (an earlier `while` loop here
            # was dead code). Consequence: the LAST hint can lag the finished size by up
            # to ~one throttle step; that is inherent to throttling and fine, because the
            # client eases the bar to completion anyway (chat.js growEstimate) and the
            # real byte count arrives with the tool_call event itself.
            if n - st["n"] >= 256:
                st["n"] = n
                return [StreamEvent.tool_progress(name, args, first_name=first)]
            return []

        # trust_env=False: never let ambient HTTP(S)_PROXY / ALL_PROXY reroute a
        # localhost inference call, nor impose a proxy's own read timeout on it.
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15),
                                     trust_env=False) as c:
            ctx = c.stream("POST", f"{self.base_url}/v1/chat/completions", json=body)
            try:
                r = await ctx.__aenter__()
                if r.status_code != 200:
                    txt = (await r.aread())[:400].decode("utf-8", "replace")
                    if ("parallel_tool_calls" in body and r.status_code == 400
                            and "parallel" in txt.lower()):
                        yield _RETRY_NO_PARALLEL
                        return
                    yield StreamEvent("error", text=f"llama-server {r.status_code}: {txt}")
                    yield StreamEvent("done", data={"finish": "error"})
                    return

                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Usage can arrive on ANY chunk. llama-server sends it on a final
                    # usage-only chunk (empty `choices`), but builds differ, and the
                    # previous code read it only in the no-choices branch — so real
                    # counts were routinely discarded. Read it first, unconditionally;
                    # `prompt_eval_count` is llama.cpp's own naming for prompt_tokens.
                    u = obj.get("usage")
                    if u:
                        pt = u.get("prompt_tokens") or u.get("prompt_eval_count")
                        ct = u.get("completion_tokens") or u.get("completion_eval_count")
                        if pt is not None or ct is not None:
                            last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
                            saw_real_usage = True
                            yield self._usage_event(u, t0, completion_chars)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    delta = ch.get("delta") or {}
                    finish_reason = ch.get("finish_reason") or finish_reason
                    content = delta.get("content")
                    if content:
                        completion_chars += len(content)
                        yield StreamEvent("token", text=content)
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": "",
                                                         "first": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            frag = fn["arguments"]
                            # The first string value in the partial JSON is nearly always
                            # the path, so the chip can name the file being written.
                            # Best-effort only: we never json.loads a fragment that is
                            # incomplete by design.
                            if not slot["first"]:
                                m = _FIRST_STR_RE.search(slot["args"] + frag)
                                if m:
                                    slot["first"] = m.group(1)[:64]
                            slot["args"] += frag
                            # Pass VALUES (not the slot dict) — see _prog_hints.
                            for hint in _prog_hints(idx, slot["args"], slot["name"],
                                                    slot["first"]):
                                yield hint
            finally:
                # Release the streaming response on every exit path (normal end, client
                # abort, exception). Safe because the body read is complete by then; the
                # client itself closes as the `async with` above exits.
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 — closing a dead stream is not our problem
                    pass

        if tool_acc:
            for slot in tool_acc.values():
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": slot["args"]}
                yield StreamEvent("tool_request",
                                  tool=ToolRequest(slot["name"], args, call_id=slot["id"],
                                                   args_raw=slot["args"]))
        # Final usage. If the server already sent real counts we restate them so a
        # tool-only round (which never reaches this point in older code paths) still
        # ends with measured numbers; otherwise fall back to the chars/4 estimate.
        yield self._usage_event(last_usage, t0, completion_chars, measured=saw_real_usage)
        yield StreamEvent("done", data={"finish": finish_reason or "stop"})

    def _usage_event(self, usage: dict, t0: float, chars: int, measured: bool = False) \
            -> StreamEvent:
        dur = max(0.001, time.time() - t0)
        ctok = usage.get("completion_tokens") or max(1, chars // 4)
        return StreamEvent("usage", data={
            "prompt_tokens": usage.get("prompt_tokens"),
            "eval_tokens": usage.get("eval_tokens") or ctok,
            "completion_tokens": ctok,
            "tokens_per_sec": round(ctok / dur, 1),
            "duration_s": round(dur, 2),
            # True when these came from the server, false when prompt size is unknown
            # and only completion is a chars/4 guess. The orchestrator uses it to keep a
            # measured figure from being displaced by an estimate (see _merge_usage).
            "measured": bool(measured or usage.get("prompt_tokens")),
        })
