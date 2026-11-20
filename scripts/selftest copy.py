
class StreamLoopReentryTests(unittest.TestCase):
    """The idle-progress poller must not re-enter or time out the provider read.

    Two bugs shared one root: `_stream_checked()` treated "only the log poll fired" as a
    first-class outcome and retired/re-armed the provider `__anext__()` around it. An async
    generator allows only ONE read in flight, and cancelling a read task does not unwind its
    frame synchronously — so the next iteration re-entered a still-RUNNING generator and the
    SSE route died with "anext(): asynchronous generator is already running". The same
    fall-through also ended every prefill at ~_LOG_POLL_S with a bogus "model produced no
    output for 300s", because the idle deadline was recomputed per iteration and the poll
    kept restarting the countdown.
    """

    def _idle(self, seconds: float) -> None:
        from hearth.config import get_boot_settings
        bs = get_boot_settings()
        bs.stream_idle_timeout_s = seconds
        bs.stream_idle_guard = True

    def test_prefill_survives_many_poll_wakeups(self):
        # A provider silent for several poll intervals (what llama-server does during
        # prefill) must stream normally, delivering progress in between.
        import asyncio
        from hearth import orchestrator as orch
        from hearth.llm.base import StreamEvent

        polls = {"n": 0}

        def log_src():
            polls["n"] += 1
            return ([{"kind": "prefill", "n_tokens": polls["n"] * 200,
                      "progress": min(0.9, 0.1 * polls["n"]), "rate_tps": 800.0}],
                    {"offset": 1})

        class Prefill:
            name = "llama-server"
            async def stream(self, messages, tools, params):
                await asyncio.sleep(orch._LOG_POLL_S * 3 + 0.2)
                yield StreamEvent("token", text="hi")
                yield StreamEvent("done")

        self._idle(30)

        async def go():
            return [ev async for ev in orch._stream_checked(
                Prefill(), [], [], {}, None, log_src=log_src)]

        evs = asyncio.run(go())
        kinds = [e.type for e in evs]
        self.assertGreaterEqual(polls["n"], 2, "poller should have fired several times")
        self.assertIn("progress", kinds, "prefill progress must reach the caller")
        self.assertEqual(kinds[-2:], ["token", "done"],
                         f"prefill must complete, not be cut off: {kinds}")
        self.assertFalse([e for e in evs if e.type == "error"],
                         "a healthy prefill must never yield an error event")

    def test_wedged_stream_times_out_despite_poller(self):
        # The mirror case: with a poller armed, a stream that NEVER produces anything must
        # still hit the idle guard. Per-iteration deadline math made this unreachable —
        # the poll restarted the countdown forever and a hung request hung forever.
        import asyncio
        import time
        from hearth import orchestrator as orch

        def log_src():
            return [], {"offset": 1}

        class Wedged:
            name = "llama-server"
            async def stream(self, messages, tools, params):
                while True:
                    await asyncio.sleep(0.05)
                yield   # pragma: no cover - unreachable, keeps this an async generator

        self._idle(1.0)

        async def go():
            out = []
            t0 = time.monotonic()
            agen = orch._stream_checked(Wedged(), [], [], {}, None, log_src=log_src)
            async for ev in agen:
                out.append(ev.type)
                if len(out) > 200:      # a runaway poll loop must not spin forever
                    break
            return out, time.monotonic() - t0

        kinds, elapsed = asyncio.run(go())
        self.assertEqual(kinds[-2:], ["error", "done"],
                         f"expected timeout error+done, got {kinds}")
        self.assertLess(elapsed, 5.0,
                        f"idle guard did not fire (took {elapsed:.1f}s of polling)")

    def test_abort_and_disconnect_close_the_stream_quietly(self):
        # Both unwind paths must release the provider generator without surfacing
        # "already running" — the abort path leaves the read pending for aclose() to
        # drive out, the disconnect path runs `finally` while a read owns the frame.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent

        self._idle(30)

        class Blocks:
            name = "llama-server"
            async def stream(self, messages, tools, params):
                await asyncio.sleep(30)
                yield StreamEvent("token", text="never")

        def log_src():
            return [], {"offset": 1}

        async def abort_case():
            h = cancel_mod.begin("reentry-abort", "u")
            try:
                agen = orch._stream_checked(Blocks(), [], [], {}, h, log_src=log_src)
                task = asyncio.ensure_future(agen.__aiter__().__anext__())
                await asyncio.sleep(0.05)
                h.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except orch.TurnAborted:
                    pass
            finally:
                cancel_mod.end("reentry-abort", h)

        async def disconnect_case():
            agen = orch._stream_checked(Blocks(), [], [], {}, None, log_src=log_src)
            it = agen.__aiter__()
            task = asyncio.ensure_future(it.__anext__())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass

        for label, coro in (("abort", abort_case), ("disconnect", disconnect_case)):
            try:
                asyncio.run(coro())
            except RuntimeError as e:
                self.fail(f"{label}: teardown must not surface 'already running': {e}")
            except (asyncio.CancelledError, StopAsyncIteration):
                pass


if __name__ == "__main__":
    print(f"[selftest] data dir = {_tmp}")
    unittest.main(verbosity=2)
