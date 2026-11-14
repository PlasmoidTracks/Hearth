#!/usr/bin/env python3
"""Hearth self-test. Run from the project root:

    python scripts/selftest.py            # stdlib-only core checks (no web deps needed)
    HEARTH_RUN_HTTP=1 python scripts/selftest.py   # also run HTTP integration via TestClient

The core suite exercises security primitives, workspace jail, and settings using
ONLY the standard library + SQLAlchemy-free logic where possible, so it runs in a
bare environment. The HTTP suite needs fastapi/httpx/sqlalchemy installed and is
skipped (not failed) if those imports are missing.

Exit code 0 = all executed checks passed. Non-zero = failures listed.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Use a throwaway data dir so we never touch real data.
_tmp = tempfile.mkdtemp(prefix="hearth-selftest-")
os.environ["HEARTH_DATA_DIR"] = _tmp
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CoreTests(unittest.TestCase):
    """No web framework required."""

    def test_password_roundtrip(self):
        from hearth.security import hash_password, verify_password
        h = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertFalse(verify_password("", ""))

    def test_scrypt_within_openssl_memlimit(self):
        # Regression guard: scrypt params must hash under OpenSSL's default
        # maxmem without raising "memory limit exceeded".
        import hashlib
        from hearth.security import _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _scrypt_maxmem
        try:
            hashlib.scrypt(b"x", salt=b"0123456789abcdef", n=_SCRYPT_N, r=_SCRYPT_R,
                           p=_SCRYPT_P, dklen=32, maxmem=_scrypt_maxmem(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P))
        except ValueError as e:
            self.fail(f"scrypt params exceed memory limit: {e}")

    def test_signed_token(self):
        from hearth.security import sign_token, verify_token
        tok = sign_token({"sid": "abc", "exp": 9_999_999_999}, "secret1")
        self.assertEqual(verify_token(tok, "secret1")["sid"], "abc")
        self.assertIsNone(verify_token(tok, "different"))       # wrong key
        expired = sign_token({"sid": "x", "exp": 1}, "s")
        self.assertIsNone(verify_token(expired, "s"))           # expired

    def test_totp(self):
        from hearth.security import new_totp_secret, totp_code, totp_verify
        s = new_totp_secret()
        code = totp_code(s)
        self.assertTrue(totp_verify(s, code))
        self.assertFalse(totp_verify(s, "000001") and totp_code(s) != "000001")

    def test_i18n(self):
        from hearth import i18n
        self.assertEqual(i18n.normalize("de"), "de")
        self.assertEqual(i18n.normalize("fr"), "en")      # unsupported -> default
        self.assertEqual(i18n.t("chat.send", "de"), "Senden")
        self.assertEqual(i18n.t("chat.send", "en"), "Send")
        # every EN key has a DE translation (no silent English fallback for real strings)
        de, en = i18n.dict_for("de"), i18n.dict_for("en")
        missing = [k for k in en if k not in i18n._DE]
        self.assertEqual(missing, [], f"German translations missing: {missing}")

    def test_workspace_jail(self):
        from hearth import workspaces as ws
        uid = "jailuser"
        root = ws.root_for(uid)
        # write + read within jail
        rel = ws.write_text(uid, "notes/a.txt", "hello")
        self.assertEqual(ws.read_text(uid, "notes/a.txt"), "hello")
        # traversal blocked
        for bad in ["../escape", "/etc/passwd", "a/../../b"]:
            with self.assertRaises(ws.JailEscape):
                ws.resolve_in_jail(uid, bad)

    def test_workspace_symlink_escape(self):
        from hearth import workspaces as ws
        uid = "symuser"
        root = ws.root_for(uid)
        outside = Path(_tmp) / "outside_secret.txt"
        outside.write_text("secret")
        link = root / "evil"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unsupported here")
        with self.assertRaises(ws.JailEscape):
            ws.resolve_in_jail(uid, "evil")


class SettingsTests(unittest.TestCase):
    def test_defaults_and_coerce(self):
        from hearth.db import init_db
        from hearth import runtime_settings as rs
        init_db()
        vals = rs.get_all()
        self.assertEqual(vals["model.n_ctx"], 262144)          # DEC-008 default
        self.assertEqual(vals["model.backend"], "stub")
        self.assertFalse(vals["tools.exec"])                    # exec off by default
        rs.set_many({"model.temperature": "0.2", "tools.exec": "true"})
        vals = rs.get_all()
        self.assertAlmostEqual(vals["model.temperature"], 0.2)
        self.assertTrue(vals["tools.exec"])

    def test_choice_falls_back_on_bad_stored_value(self):
        # A key that migrated int/bool -> choice must not yield a value outside its options.
        from hearth import runtime_settings as rs
        spec = rs.SPEC_BY_KEY["model.flash_attn"]
        self.assertEqual(rs._coerce(spec, "False"), "auto")   # stale bool -> default
        self.assertEqual(rs._coerce(spec, "on"), "on")         # valid choice passes through

    def test_cookie_samesite_is_constrained(self):
        # LAN exposure (HEARTH_HOST=0.0.0.0) is supported, and SameSite=lax is the
        # documented relaxation for it; anything unrecognised must fall back to strict,
        # and NEVER to "none" — SameSite=None without Secure is both rejected by browsers
        # and no CSRF protection at all, so a typo in .env cannot silently remove it.
        # secret_key has a random default_factory: constructing BootSettings directly
        # must never depend on the ambient environment (a HEARTH_SECRET_KEY exported for
        # the real deployment would otherwise leak into this test).
        from hearth.config import BootSettings
        def mk(v):
            # `or ""` because pydantic (>=2.x) rejects None for a str field BEFORE the
            # property can normalize it; the property's own None-handling stays intact.
            return BootSettings(secret_key="test-key", cookie_samesite=v or ""
                                ).normalized_cookie_samesite
        self.assertEqual(BootSettings(secret_key="test-key").cookie_samesite, "strict")
        self.assertEqual(mk("lax"), "lax")
        self.assertEqual(mk("LAX"), "lax")
        self.assertEqual(mk("none"), "none")     # explicit is honoured (operator's choice)
        for bad in ("", None, "disabled", "no", "  ", "laxx"):
            self.assertEqual(mk(bad), "strict", f"{bad!r} must fall back to strict")


class ManagerArgvTests(unittest.TestCase):
    """build_argv is pure — assert the llama-server command line is correct."""

    class _BS:
        llama_port = 8081
        llama_jinja = True
        llama_extra_args = ""
        llama_server_bin = "/env/bin/llama-server"
        llama_model_path = "/env/model.gguf"
        llama_mmproj_path = ""

    def _argv(self, **over):
        from hearth.manager import ModelManager
        settings = {"model.n_ctx": 8192, "model.n_gpu_layers": "all", "model.n_parallel": 2,
                    "model.rope_scaling": "none", "model.rope_freq_base": 10000.0,
                    "model.rope_freq_scale": 1.0, "model.flash_attn": "on",
                    "model.threads": -1, "model.batch_size": 2048, "model.ubatch_size": 512,
                    "model.kv_cache_type": "q8_0", "model.device": "", "model.load_mode": "auto",
                    "model.reasoning": "auto"}
        # overrides are keyed WITHOUT the leading 'model.' for brevity; namespace them.
        settings.update({("model." + k if not k.startswith("model.") else k): v for k, v in over.items()})
        return ModelManager().build_argv(self._BS(), settings,
                                         bin_path="/b/llama-server", model_path="/m/model.gguf")

    def test_core_flags(self):
        a = self._argv()
        self.assertEqual(a[0], "/b/llama-server")
        self.assertIn(("--model", "/m/model.gguf"), list(zip(a, a[1:])))
        self.assertIn("--ctx-size", a)
        self.assertIn("8192", a)
        # n_gpu_layers 'all' maps to -1 (offload-everything). The literal word is
        # NOT valid for llama.cpp — passing it parses as -2 and aborts the fit.
        i = a.index("--n-gpu-layers"); self.assertEqual(a[i+1], "-1")
        # flash-attn uses the value form on this build
        i = a.index("--flash-attn"); self.assertEqual(a[i+1], "on")
        # kv quant sets both K and V
        self.assertIn("--cache-type-k", a); self.assertIn("q8_0", a)
        self.assertEqual(a.count("q8_0"), 2)

    def test_defaults_omit_auto_flags(self):
        a = self._argv(flash_attn="auto", kv_cache_type="f16", load_mode="auto", reasoning="auto")
        self.assertNotIn("--flash-attn", a)       # auto == binary default, skip
        self.assertNotIn("--cache-type-k", a)     # f16 == default, skip
        self.assertNotIn("--load-mode", a)
        self.assertNotIn("--reasoning", a)

    def test_ngl_auto_omits_flag(self):
        # The regression that made Hearth decode at CPU speed: 'auto' was passed as
        # the literal word --n-gpu-layers auto, which llama.cpp parses as -2 and its
        # fitter ABORTS ("n_gpu_layers already set by user to -2"), falling back to
        # CPU tensor overrides. 'auto' must omit the flag so llama.cpp auto-fits VRAM.
        a = self._argv(n_gpu_layers="auto")
        self.assertNotIn("--n-gpu-layers", a)

    def test_ngl_zero_is_cpu_only(self):
        # An explicit 0 is still honoured (CPU-only), distinct from 'auto'.
        a = self._argv(n_gpu_layers="0")
        i = a.index("--n-gpu-layers"); self.assertEqual(a[i+1], "0")

    def test_env_fallback_for_paths(self):
        from hearth.manager import ModelManager
        # build_argv with no explicit paths uses bs (env) values
        a = ModelManager().build_argv(self._BS(), {}, bin_path=None, model_path=None)
        self.assertEqual(a[0], "/env/bin/llama-server")

    def test_threads_only_when_set(self):
        a = self._argv(threads=8)
        i = a.index("--threads"); self.assertEqual(a[i+1], "8")

    def test_kv_unified_added_only_when_binary_supports_it(self):
        # --kv-unified prevents the n_ctx/parallel slot split on newer builds; on
        # older binaries it would abort startup, so argv must include it only when
        # the probe says the binary knows the flag.
        from hearth.manager import ModelManager
        self.assertFalse("--kv-unified" in self._argv())   # probe fails (no binary)

        class Probing(ModelManager):
            @staticmethod
            def _probe_flag(bin_path, flag): return flag == "--kv-unified"
        a = Probing().build_argv(self._BS(), {"model.n_parallel": 2},
                                 bin_path="/b/llama-server", model_path="/m/model.gguf")
        self.assertIn("--kv-unified", a)
        # With a single slot there is no split to fix — flag stays off.
        a1 = Probing().build_argv(self._BS(), {"model.n_parallel": 1},
                                  bin_path="/b/llama-server", model_path="/m/model.gguf")
        self.assertNotIn("--kv-unified", a1)

    def test_slot_n_ctx_math(self):
        from hearth.manager import ModelManager
        self.assertEqual(ModelManager._slot_n_ctx(262144, 2), 131072)
        self.assertEqual(ModelManager._slot_n_ctx(262144, 1), 262144)
        self.assertEqual(ModelManager._slot_n_ctx(8192, 0), 8192)      # garbage -> 1 slot

    def test_mmproj_added_only_when_set_and_supported(self):
        # --mmproj enables vision (image attachments). It must appear only when a
        # projector path is configured AND the binary knows the flag; a text-only
        # build must never get an unknown flag that aborts startup.
        from hearth.manager import ModelManager
        # No mmproj configured -> no flag (even though probe would fail anyway).
        self.assertNotIn("--mmproj", self._argv())

        class Probing(ModelManager):
            @staticmethod
            def _probe_flag(bin_path, flag): return flag in ("--mmproj", "--kv-unified")

        # Explicit mmproj path + supporting binary -> flag present with the value.
        a = Probing().build_argv(self._BS(), {}, bin_path="/b/llama-server",
                                 model_path="/m/model.gguf", mmproj_path="/m/mmproj.gguf")
        self.assertIn("--mmproj", a)
        i = a.index("--mmproj"); self.assertEqual(a[i+1], "/m/mmproj.gguf")

        # Settings value (admin UI) is used when no explicit path is passed.
        b = Probing().build_argv(self._BS(), {"model.mmproj_path": "/settings/mmproj.gguf"},
                                 bin_path="/b/llama-server", model_path="/m/model.gguf")
        i = b.index("--mmproj"); self.assertEqual(b[i+1], "/settings/mmproj.gguf")

        # Binary that does NOT know --mmproj -> flag omitted even when configured.
        class NoMmproj(ModelManager):
            @staticmethod
            def _probe_flag(bin_path, flag): return False
        c = NoMmproj().build_argv(self._BS(), {}, bin_path="/b/llama-server",
                                  model_path="/m/model.gguf", mmproj_path="/m/mmproj.gguf")
        self.assertNotIn("--mmproj", c)
        self.assertEqual(ModelManager._slot_n_ctx(None, "x"), 0)       # unset -> nothing to report

    def test_launch_command_preview(self):
        # The copy-launch-command feature must reflect build_argv() and be valid
        # shell (paths with spaces quoted), so running it by hand reproduces Hearth.
        from hearth.manager import ModelManager
        import shlex
        m = ModelManager()
        bs = self._BS()
        # Shared path resolver: admin settings win over env...
        b, mp, mm = m._resolve_launch_paths(
            {"model.server_bin": "/x/ls", "model.model_path": "/m/m.gguf"}, bs)
        self.assertEqual((b, mp), ("/x/ls", "/m/m.gguf"))
        # ...and env is the fallback when settings are blank.
        b2, mp2, _ = m._resolve_launch_paths({}, bs)
        self.assertEqual(b2, "/env/bin/llama-server")
        self.assertEqual(mp2, "/env/model.gguf")
        # A shell-quoted command from build_argv survives round-trip via shlex.split,
        # so the copied string is runnable even with spaces in paths.
        argv = m.build_argv(bs, {"model.n_ctx": 4096}, bin_path="/b/llama-server",
                            model_path="/m dir/model.gguf")
        cmd = shlex.join(argv)
        self.assertIn("'/m dir/model.gguf'", cmd)
        self.assertEqual(shlex.split(cmd), argv)


class ToolTests(unittest.TestCase):
    def test_fs_tool_jail(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, FsReadTool
        from hearth.tools import ToolContext
        ctx = ToolContext("tooluser", {})
        w = asyncio.run(FsWriteTool().run({"path": "x.txt", "content": "hi"}, ctx))
        self.assertTrue(w.ok)
        r = asyncio.run(FsReadTool().run({"path": "x.txt"}, ctx))
        self.assertEqual(r.content, "hi")
        bad = asyncio.run(FsReadTool().run({"path": "../nope"}, ctx))
        self.assertFalse(bad.ok)

    def test_exec_fail_closed_without_sandbox(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.exec_tool import ExecTool
        from hearth.tools import ToolContext
        # force stub backend so we assert refusal, not real exec
        ctx = ToolContext("execuser", {"sandbox.backend": "stub"})
        res = asyncio.run(ExecTool().run({"command": "echo pwned"}, ctx))
        self.assertFalse(res.ok)
        self.assertIn("Refusing", res.content)

    def test_write_creates_durable_artifact(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool
        from hearth.tools import ToolContext
        from hearth import artifacts
        ctx = ToolContext("artifactuser", {}, conversation_id="conv1")
        res = asyncio.run(FsWriteTool().run({"path": "report.txt", "content": "hello art"}, ctx))
        self.assertTrue(res.ok)
        # DEC-045: writing a file registers it but does NOT offer a download button —
        # only the `upload` tool does that. The file is still durable + listed.
        self.assertNotIn("download", res.meta or {})
        arts = artifacts.list_for("artifactuser", conversation_id="conv1")
        self.assertTrue(any(a["filename"] == "report.txt" for a in arts))
        # ownership enforced: another user cannot see it
        self.assertEqual(artifacts.list_for("someoneelse", conversation_id="conv1"), [])

    def test_upload_tool_returns_artifact_link(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, UploadTool
        from hearth.tools import ToolContext
        ctx = ToolContext("upluser", {}, conversation_id="c2")
        asyncio.run(FsWriteTool().run({"path": "out.md", "content": "# hi"}, ctx))
        res = asyncio.run(UploadTool().run({"path": "out.md"}, ctx))
        self.assertTrue(res.ok)
        self.assertIn("/api/artifact/", res.meta["download"]["url"])

    def test_artifact_for_owner_without_user_row(self):
        # Regression guard (DEC-019 fix): artifacts are keyed by owner-id string and MUST
        # be insertable even with no matching users row — workspaces are created lazily.
        # Re-adding a FK on Artifact.user_id would make this fail again.
        from hearth.db import init_db
        init_db()
        from hearth import artifacts, workspaces as ws
        uid = "ghostowner-no-such-user"
        ws.write_text(uid, "f.txt", "x")   # workspace can exist without an account
        art = artifacts.record(uid, "f.txt", conversation_id="cv", kind="produced")
        self.assertTrue(art["id"])
        self.assertEqual(artifacts.list_for(uid, conversation_id="cv")[0]["filename"], "f.txt")

    def test_write_then_upload_lists_file_once(self):
        # Operator bug: "create a hello world script then provide the download" made the
        # model call fs_write AND upload, and the file appeared TWICE in the Artifacts
        # panel (two rows for one workspace file). An artifact is identified by its
        # workspace path, so re-offering the same file refreshes the existing record
        # instead of adding a second one.
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, UploadTool
        from hearth.tools import ToolContext
        from hearth import artifacts
        ctx = ToolContext("dedupeuser", {}, conversation_id="dedupe-conv")
        asyncio.run(FsWriteTool().run({"path": "hello.py", "content": "print(1)\n"}, ctx))
        res = asyncio.run(UploadTool().run({"path": "hello.py"}, ctx))
        self.assertTrue(res.ok)
        arts = artifacts.list_for("dedupeuser", conversation_id="dedupe-conv")
        same = [a for a in arts if a["filename"] == "hello.py"]
        self.assertEqual(len(same), 1, f"file listed {len(same)} times: {arts}")

    def test_rewriting_a_file_updates_instead_of_duplicating(self):
        # Re-writing the same path must keep ONE record (same id, so links handed out
        # earlier stay valid), pick up the new size, AND order as the newest — which is
        # only guaranteed because created_at is a monotonic sequence, not whole seconds.
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, UploadTool
        from hearth.tools import ToolContext
        from hearth import artifacts
        ctx = ToolContext("rewriter", {}, conversation_id="rewrite-conv")
        asyncio.run(FsWriteTool().run({"path": "notes.md", "content": "short"}, ctx))
        # The id comes from the artifact store (fs_write no longer returns one; only
        # `upload` hands out a download reference — DEC-045).
        id1 = artifacts.list_for("rewriter", conversation_id="rewrite-conv")[0]["id"]
        asyncio.run(FsWriteTool().run({"path": "notes.md", "content": "much longer content"}, ctx))
        second = asyncio.run(UploadTool().run({"path": "notes.md"}, ctx))
        self.assertTrue(second.ok)
        arts = artifacts.list_for("rewriter", conversation_id="rewrite-conv")
        self.assertEqual(len([a for a in arts if a["filename"] == "notes.md"]), 1, arts)
        id2 = second.meta["download"]["artifact_id"]
        self.assertEqual(id1, id2, "rewriting a file must not mint a new artifact id")
        row = artifacts.get("rewriter", id1)
        self.assertEqual(row["size"], len("much longer content"))
        self.assertEqual(arts[0]["id"], id1, "the refreshed record must sort newest")

    def test_artifact_ordering_is_strict_within_one_second(self):
        # Regression guard for the reported duplicate: two registrations of DIFFERENT
        # files inside the same second must still have distinct, increasing created_at,
        # or "which row is newest" (used to pick the record to refresh) is a coin flip.
        from hearth.db import init_db
        init_db()
        from hearth import artifacts, workspaces as ws
        uid = "sequser"
        ids = []
        for i in range(5):
            ws.write_text(uid, f"f{i}.txt", "x" * i)
            ids.append(artifacts.record(uid, f"f{i}.txt", conversation_id="seq-conv")["id"])
        rows = artifacts.list_for(uid, conversation_id="seq-conv")
        stamps = [r["created_at"] for r in rows]
        self.assertEqual(len(set(stamps)), len(stamps), f"timestamps collide: {stamps}")
        # Newest first, and the last file registered is at the top.
        self.assertEqual(rows[0]["id"], ids[-1], [r["filename"] for r in rows])

    def test_upload_route_still_records_separate_rows(self):
        # Deliberate asymmetry: two uploads of the SAME filename are two transfers, each
        # with its own download link (the workspace path is overwritten, but the user
        # should still be able to fetch what they sent in that message). Only tool-side
        # registration dedupes — and it collapses onto the NEWEST row, because `record`
        # refreshes whichever record most recently touched the path.
        from hearth.db import init_db
        init_db()
        from hearth import artifacts, workspaces as ws
        uid = "twice-uploaded"
        ws.write_text(uid, "uploads/same.txt", "v1")
        a1 = artifacts.record(uid, "uploads/same.txt", conversation_id="c", kind="uploaded",
                              filename="same.txt", plain_size=2, new_row=True)
        ws.write_text(uid, "uploads/same.txt", "v22")
        a2 = artifacts.record(uid, "uploads/same.txt", conversation_id="c", kind="uploaded",
                              filename="same.txt", plain_size=3, new_row=True)
        self.assertNotEqual(a1["id"], a2["id"])
        # ...while the tool path (no new_row) collapses onto the NEWEST row: `record`
        # refreshes whichever record most recently touched the path, so re-offering the
        # file never adds a third entry and keeps a link that already exists valid.
        a3 = artifacts.record(uid, "uploads/same.txt", conversation_id="c", kind="produced")
        rows = [a for a in artifacts.list_for(uid, conversation_id="c") if a["filename"] == "same.txt"]
        self.assertEqual(len(rows), 2, f"expected the two upload rows only: {rows}")
        self.assertEqual(a3["id"], a2["id"], f"a3={a3['id']} a1={a1['id']} a2={a2['id']}")

    def test_attachment_content_is_base64_not_raw(self):
        # Images must become data-URL image_url parts (base64), never raw bytes in text.
        from hearth.db import init_db
        init_db()
        import base64
        from hearth import workspaces as ws, orchestrator as orch
        uid = "imguser"
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00\x01\x02\x03"   # tiny fake png
        ws.write_bytes(uid, "pic.png", png)
        att = [{"artifact_id": "x", "name": "pic.png", "mime": "image/png", "rel_path": "pic.png"}]
        content = orch._attachment_content(uid, att, "look at this")
        self.assertIsInstance(content, list)   # multimodal
        img = next(p for p in content if p["type"] == "image_url")
        url = img["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        # decodes back to the original bytes (round-trip integrity)
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), png)

    def test_stub_flattens_multimodal(self):
        from hearth.llm.stub import _flatten_content
        parts = [{"type": "text", "text": "list"}, {"type": "image_url", "image_url": {"url": "data:..."}}]
        flat = _flatten_content(parts)
        self.assertIn("list", flat)
        self.assertIn("[image]", flat)
        self.assertNotIn("base64", flat)   # never dumps raw data into the stub

    def test_workspace_context_lists_files(self):
        from hearth.db import init_db
        init_db()
        from hearth import workspaces as ws, orchestrator as orch
        uid = "ctxuser"
        ws.write_text(uid, "a.txt", "1")
        ctx = orch._workspace_context(uid)
        self.assertIn("a.txt", ctx)

    def test_workspace_context_shows_sizes_and_uploads(self):
        # The model needs an ls -l equivalent: file SIZES in the prompt listing,
        # and uploads/ marked as user attachments so it knows what was attached.
        from hearth.db import init_db
        init_db()
        from hearth import workspaces as ws, orchestrator as orch
        uid = "ctxuser2"
        ws.write_text(uid, "a.txt", "12345")          # 5 bytes
        ws.write_text(uid, "uploads/photo.png", "x")  # simulated user upload
        ctx = orch._workspace_context(uid)
        self.assertIn("a.txt (5 B)", ctx, f"size missing from listing: {ctx!r}")
        self.assertIn("uploads/", ctx)
        # The word "attachments" belongs to prompt.py's WORKSPACE_PREAMBLE (which
        # _workspace_context is appended to), while this listing says "the user attached"
        # — assert the meaning each place actually carries, not a guess about which.
        self.assertIn("attached", ctx.lower(), f"uploads not explained as user files: {ctx!r}")
        from hearth import prompt as prompt_mod
        self.assertIn("attachments", prompt_mod.WORKSPACE_PREAMBLE.lower())

    def test_tool_progress_event_shape(self):
        # StreamEvent.tool_progress must carry only name + char count — the raw
        # JSON fragment (which can contain arbitrary file content) never crosses
        # the SSE wire.
        from hearth.llm.base import StreamEvent
        ev = StreamEvent.tool_progress("fs_write", '{"path": "x.py", "content": "abc"}')
        self.assertEqual(ev.type, "tool_progress")
        self.assertEqual(ev.data["name"], "fs_write")
        self.assertEqual(ev.data["chars"], len('{"path": "x.py", "content": "abc"}'))
        self.assertNotIn("args_so_far", ev.data)

    def test_tool_progress_carries_path_hint_only(self):
        # The chip must be able to name the file being written ("which file is it?"),
        # but only from an already-complete string value in the partial JSON — never
        # from the content itself.
        from hearth.llm.base import StreamEvent
        ev = StreamEvent.tool_progress("fs_write", '{"path": "hello.py", "content": "secret"}',
                                       first_name="hello.py")
        self.assertEqual(ev.data["first_name"], "hello.py")
        # Absent when nothing usable has streamed in yet (chip shows name + size only).
        bare = StreamEvent.tool_progress("fs_write", '{"pa')
        self.assertEqual(bare.data.get("first_name", ""), "")

    def test_stub_write_and_upload_detection(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.llm.stub import StubProvider
        from hearth.tools import tool_specs
        specs = tool_specs({"tools.fs_read": True, "tools.fs_write": True})
        def first_tool(content):
            got = []
            async def collect():
                async for ev in StubProvider().stream([{"role":"user","content":content}], specs, {}):
                    if ev.type == "tool_request":
                        got.append((ev.tool.name, ev.tool.args))
            asyncio.run(collect()); return got
        w = first_tool("write notes.txt Meeting agenda")
        self.assertEqual(w[0][0], "fs_write", w)
        self.assertEqual(w[0][1]["path"], "notes.txt")
        self.assertIn("Meeting agenda", w[0][1]["content"])   # case preserved
        u = first_tool("upload notes.txt")
        self.assertEqual(u[0][0], "upload", u)


    def test_stub_streams_with_real_specs(self):
        # Regression guard: StubProvider must handle OpenAI-spec tools
        # ({"type":"function","function":{"name":...}}) and stream tokens, not crash.
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.llm.stub import StubProvider
        from hearth.tools import tool_specs
        specs = tool_specs({"tools.fs_read": True, "tools.fs_write": True})
        types = []
        async def collect():
            async for ev in StubProvider().stream(
                [{"role": "user", "content": "hello there"}], specs, {}):
                types.append(ev.type)
        asyncio.run(collect())
        assert "token" in types and "done" in types, types

    def test_stub_tool_detection(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.llm.stub import StubProvider
        from hearth.tools import tool_specs
        # fs_list: granular key present in the dict wins...
        specs = tool_specs({"tools.fs_read": True, "tools.fs_list": False})
        names = {t.get("function", {}).get("name") for t in specs}
        assert "fs_list" not in names, f"granular off ignored; got {names}"
        # ...otherwise legacy tools.fs_read still enables it (back-compat).
        # NOTE: fs_list is enabled via tools.fs_read fallback (see FsListTool).
        specs = tool_specs({"tools.fs_read": True})
        names = {t.get("function", {}).get("name") for t in specs}
        assert "fs_list" in names, f"specs missing fs_list; got {names}"
        got = []
        async def collect():
            async for ev in StubProvider().stream(
                [{"role": "user", "content": "list"}], specs, {}):
                if ev.type == "tool_request":
                    got.append(ev.tool.name)
        asyncio.run(collect())
        assert got == ["fs_list"], f"got {got}; names were {names}"

    def test_stub_does_not_reloop_after_tool_result(self):
        # Regression guard: after a tool result exists for the current turn,
        # the stub must NOT request the same tool again (prevents MAX_TOOL_LOOPS).
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.llm.stub import StubProvider
        from hearth.tools import tool_specs
        specs = tool_specs({"tools.fs_read": True})
        msgs = [{"role": "user", "content": "list"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "fs_list", "arguments": "{}"}}]},
                {"role": "tool", "content": "(empty)", "tool_call_id": "c1"}]
        requests = []
        async def collect():
            async for ev in StubProvider().stream(msgs, specs, {}):
                if ev.type == "tool_request":
                    requests.append(ev.tool.name)
        asyncio.run(collect())
        assert requests == [], f"stub re-requested tools after result: {requests}"


@unittest.skipUnless(os.environ.get("HEARTH_RUN_HTTP"), "set HEARTH_RUN_HTTP=1")
class HttpTests(unittest.TestCase):
    def test_login_and_chat_stream(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        app = create_app()
        with TestClient(app) as c:
            # bootstrap admin creds come from env or a printed password; use env for test
            r = c.post("/auth/login", data={"username": os.environ.get("HEARTH_BOOTSTRAP_ADMIN_USER", "admin"),
                                             "password": os.environ.get("BOOT_PW", "")})
            self.assertIn(r.status_code, (200, 401))

    def test_security_headers_present(self):
        # Regression guard for the CSP hardening (DEC-018): every response must carry
        # a strict script-src and the other baseline security headers.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            r = c.get("/login")
            self.assertEqual(r.status_code, 200)
            csp = r.headers.get("content-security-policy", "")
            self.assertIn("default-src 'self'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("cdn.tailwindcss.com", csp)   # no CDN in policy
            self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(r.headers.get("x-frame-options"), "DENY")

    def test_no_cdn_in_page_html(self):
        # The login page must not reference any third-party origin.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            html = c.get("/login").text
            for bad in ("cdn.tailwindcss.com", "unpkg.com", "cdnjs.cloudflare.com"):
                self.assertNotIn(bad, html)

    def test_upload_and_artifacts_flow(self):
        # Full path: login -> upload (CSRF) -> list artifacts -> download -> ownership.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        pw = os.environ.get("BOOT_PW", "testpass123")
        with TestClient(create_app()) as c:
            # ensure an admin exists with known pw (bootstrap may have created one already)
            login = c.post("/auth/login", data={"username": "admin", "password": pw})
            if login.status_code != 200:
                self.skipTest(f"no known admin pw; set BOOT_PW (login={login.status_code})")
            csrf = c.cookies.get("hearth_csrf", "")
            up = c.post("/api/upload", data={"conversation_id": "httptestconv"},
                        files=[("files", ("hello.txt", b"hi from test", "text/plain"))],
                        headers={"X-CSRF-Token": csrf})
            self.assertEqual(up.status_code, 200, up.text)
            arts = up.json()["artifacts"]
            self.assertEqual(len(arts), 1)
            aid = arts[0]["id"]
            # CSRF required: missing token rejected
            bad = c.post("/api/upload", data={"conversation_id": "x"},
                         files=[("files", ("evil.txt", b"x", "text/plain"))])
            self.assertEqual(bad.status_code, 403)
            # download works
            dl = c.get(f"/api/artifact/{aid}")
            self.assertEqual(dl.status_code, 200)
            self.assertEqual(dl.content, b"hi from test")

    def test_i18n_endpoint(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            d = c.get("/api/i18n").json()
            self.assertIn("strings", d)
            self.assertEqual(d["strings"]["chat.send"], "Send")  # default English

    def test_conversation_rename_delete_search(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        pw = os.environ.get("BOOT_PW", "testpass123")
        with TestClient(create_app()) as c:
            if c.post("/auth/login", data={"username": "admin", "password": pw}).status_code != 200:
                self.skipTest(f"set BOOT_PW (login failed)")
            csrf = c.cookies.get("hearth_csrf", "")
            H = {"X-CSRF-Token": csrf}
            cid = c.post("/api/chat/conversations", headers=H).json()["id"]
            # rename
            r = c.post(f"/api/chat/conversations/{cid}/rename", json={"title": "Groceries"}, headers=H)
            self.assertEqual(r.status_code, 200)
            titles = [x["title"] for x in c.get("/api/chat/conversations").json()]
            self.assertIn("Groceries", titles)
            # search by title
            res = c.get("/api/chat/search?q=Grocer").json()
            self.assertTrue(any(x["id"] == cid for x in res))
            # delete (keep artifacts default false)
            r = c.post(f"/api/chat/conversations/{cid}/delete", json={"delete_artifacts": False}, headers=H)
            self.assertEqual(r.status_code, 200)
            gone = [x["id"] for x in c.get("/api/chat/conversations").json()]
            self.assertNotIn(cid, gone)


class GranularToolTests(unittest.TestCase):
    """New harness-style tools + per-tool enable/approval granularity."""

    def test_enable_resolution_granular_and_legacy(self):
        from hearth.tools import enabled_tools, approval_mode, TOOLS_BY_NAME
        # nothing stored -> everything enabled (registry default)
        names = {t.name for t in enabled_tools({})}
        self.assertIn("fs_edit", names)
        # granular off wins
        names = {t.name for t in enabled_tools({"tools.fs_edit": False})}
        self.assertNotIn("fs_edit", names)
        # legacy coarse key still disables when granular absent from dict
        # (simulates an old install that only stored tools.fs_write=false)
        settings = {"tools.fs_write": False}   # fs_upload rides on it via fallback
        names = {t.name for t in enabled_tools(settings)}
        self.assertNotIn("upload", names)

    def test_approval_mode(self):
        from hearth.tools import approval_mode, TOOLS_BY_NAME
        s = {}
        self.assertEqual(approval_mode(TOOLS_BY_NAME["exec"], s), "ask")
        self.assertEqual(approval_mode(TOOLS_BY_NAME["fs_write"], s), "auto")
        self.assertEqual(approval_mode(TOOLS_BY_NAME["fs_delete"], s), "ask")
        # admin override both directions
        self.assertEqual(approval_mode(TOOLS_BY_NAME["fs_write"], {"tools.fs_write.approval": "ask"}), "ask")
        self.assertEqual(approval_mode(TOOLS_BY_NAME["exec"], {"tools.exec.approval": "auto"}), "auto")

    def test_read_line_ranges(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsReadTool
        from hearth.tools import ToolContext
        ctx = ToolContext("rangeuser", {})
        ws_mod = __import__("hearth.workspaces", fromlist=["x"])
        ws_mod.write_text("rangeuser", "r.txt", "l1\nl2\nl3\nl4")
        r = asyncio.run(FsReadTool().run({"path": "r.txt", "start_line": 2, "end_line": 3}, ctx))
        self.assertTrue(r.ok)
        # DEC-045: a range read is VERBATIM file text — no line-number prefixes. They
        # used to be added so the model could quote lines back, but fs_edit matches
        # literally and models quoted "2: l2" straight into old_text, which never
        # matched: re-read → same edit → loop until max tool steps.
        self.assertEqual(r.content, "l2\nl3")
        self.assertNotIn("l4", r.content)
        self.assertNotIn("2:", r.content)

    def test_edit_succeeds_on_text_read_from_a_range(self):
        # The exact reported failure: read a range, quote it back into fs_edit. With the
        # old numbered output this was always "old_text not found".
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, FsReadTool, FsEditTool
        from hearth.tools import ToolContext
        ctx = ToolContext("editloop", {}, conversation_id="cel")
        src = '#include <stdio.h>\n\nint main() {\n    printf("Hello World!\\n");\n    return 0;\n}\n'
        asyncio.run(FsWriteTool().run({"path": "hello.c", "content": src}, ctx))
        r = asyncio.run(FsReadTool().run({"path": "hello.c"}, ctx))
        self.assertEqual(r.content, src)   # verbatim, no decoration
        e = asyncio.run(FsEditTool().run(
            {"path": "hello.c", 'old_text': 'printf("Hello World!\\n");',
             "new_text": 'printf("%d\\n", rand());'}, ctx))
        self.assertTrue(e.ok, f"edit of quoted file text failed: {e.content}")

    def test_append_edit_delete_move(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import (FsWriteTool, FsAppendTool, FsEditTool,
                                           FsDeleteTool, FsMoveTool)
        from hearth.tools import ToolContext
        ctx = ToolContext("edituser", {}, conversation_id="ce")
        asyncio.run(FsWriteTool().run({"path": "e.txt", "content": "alpha\nbeta"}, ctx))
        a = asyncio.run(FsAppendTool().run({"path": "e.txt", "content": "\ngamma"}, ctx))
        self.assertTrue(a.ok)
        # unique match succeeds
        e = asyncio.run(FsEditTool().run({"path": "e.txt", "old_text": "beta", "new_text": "BETA"}, ctx))
        self.assertTrue(e.ok, e.content)
        # ambiguous match refused
        e2 = asyncio.run(FsEditTool().run({"path": "e.txt", "old_text": "a", "new_text": "@"}, ctx))
        self.assertFalse(e2.ok)
        self.assertIn("matches", e2.content)
        m = asyncio.run(FsMoveTool().run({"from_path": "e.txt", "to_path": "sub/e2.txt"}, ctx))
        self.assertTrue(m.ok, m.content)
        d = asyncio.run(FsDeleteTool().run({"path": "sub/e2.txt"}, ctx))
        self.assertTrue(d.ok, d.content)

    def test_zip_pack(self):
        import asyncio, io, zipfile
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, ZipPackTool
        from hearth.tools import ToolContext
        ctx = ToolContext("zipuser", {}, conversation_id="cz")
        asyncio.run(FsWriteTool().run({"path": "a.txt", "content": "A"}, ctx))
        asyncio.run(FsWriteTool().run({"path": "b.txt", "content": "B"}, ctx))
        res = asyncio.run(ZipPackTool().run({"paths": ["a.txt", "b.txt"], "zip_name": "pair"}, ctx))
        self.assertTrue(res.ok, res.content)
        from hearth import workspaces as ws
        blob = ws.read_bytes("zipuser", "pair.zip")
        zf = zipfile.ZipFile(io.BytesIO(blob))
        self.assertEqual(sorted(zf.namelist()), ["a.txt", "b.txt"])

    def test_search_contents(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, FsSearchTool
        from hearth.tools import ToolContext
        ctx = ToolContext("searchuser", {})
        asyncio.run(FsWriteTool().run({"path": "notes/today.md", "content": "buy MILK now"}, ctx))
        r = asyncio.run(FsSearchTool().run({"pattern": "milk", "glob": "*.md"}, ctx))
        self.assertTrue(r.ok)
        self.assertIn("notes/today.md:1:", r.content)


class WebToolTests(unittest.TestCase):
    """Web access modes (llm_coder-style policy dispatch) + source-link meta."""

    def test_policy_resolution_and_domain_rules(self):
        from hearth.tools import web_policy as wp
        p = wp.resolve({})
        self.assertEqual(p.mode, "search_and_fetch_safe")
        self.assertTrue(p.enabled)
        self.assertEqual(wp.check_url("https://ok.example.com/x", p), "")
        self.assertIn("http(s)", wp.check_url("ftp://example.com", p))
        # unknown/legacy stored values fall back to the safe default mode
        self.assertEqual(wp.resolve({"web.mode": "garbage"}).mode, "search_and_fetch_safe")
        # blocklist matches the domain AND subdomains, in every live mode
        pb = wp.resolve({"web.block_domains": "evil.com, spam.org"})
        self.assertIn("blocked", wp.check_url("https://a.evil.com/x", pb))
        self.assertIn("blocked", wp.check_url("https://spam.org/", pb))
        self.assertEqual(wp.check_url("https://good.example/", pb), "")
        # allowlist (when set) restricts fetch targets; subdomains allowed
        pa = wp.resolve({"web.allow_domains": "wikipedia.org"})
        self.assertEqual(wp.check_url("https://de.wikipedia.org/wiki/x", pa), "")
        self.assertIn("allowlist", wp.check_url("https://ex.com/", pa))
        # off refuses everything, with an actionable message
        po = wp.resolve({"web.mode": "off"})
        self.assertFalse(po.enabled)
        self.assertIn("disabled", wp.check_url("https://x.de", po))
        self.assertIn("disabled", wp.check_search(po, "duckduckgo"))
        # tor proxy default uses socks5h (remote DNS → no local resolver leak)
        self.assertTrue(wp.resolve({}).tor_proxy.startswith("socks5h://"))

    def test_mode_refusals_never_touch_network(self):
        import asyncio
        from hearth.tools.web_search import WebSearchTool
        from hearth.tools.web_fetch import WebFetchTool
        from hearth.tools import ToolContext
        # mode off → both refuse with a pointer to the admin setting
        ctx = ToolContext("webtest", {"web.mode": "off"})
        r = asyncio.run(WebSearchTool().run({"query": "anything"}, ctx))
        self.assertFalse(r.ok)
        self.assertIn("disabled", r.content)
        r = asyncio.run(WebFetchTool().run({"url": "https://example.com"}, ctx))
        self.assertFalse(r.ok)
        # blocklist enforced pre-flight in web_fetch
        ctx = ToolContext("webtest", {"web.block_domains": "bank.example"})
        r = asyncio.run(WebFetchTool().run({"url": "https://online.bank.example/"}, ctx))
        self.assertFalse(r.ok)
        self.assertIn("blocked", r.content)

    def test_ssrf_guard_refuses_private_addresses(self):
        from hearth.tools import web_policy as wp
        p = wp.resolve({})   # default: guard ON
        for url in ("http://127.0.0.1:8080/admin", "http://localhost/x",
                    "http://169.254.169.254/latest/meta-data",
                    "http://192.168.1.1/", "http://[::1]/"):
            self.assertIn("loopback/private", wp.check_url(url, p), url)
        self.assertEqual(wp.check_url("https://87.10.20.30/page", p), "")
        # admin opt-in lifts it
        p_on = wp.resolve({"web.http_allow_private": True})
        self.assertEqual(wp.check_url("http://192.168.1.10/wiki", p_on), "")
        # open_browser is exempt by design (operator's browser navigates)
        p_ob = wp.resolve({"web.mode": "open_browser"})
        self.assertEqual(wp.check_url("http://192.168.1.10/wiki", p_ob), "")

    def test_open_browser_mode_without_selenium_is_actionable(self):
        # open_browser must degrade to an actionable message (llm_coder behaviour),
        # never a traceback, whether or not selenium happens to be installed.
        import asyncio
        from hearth.tools.web_search import WebSearchTool
        from hearth.tools.web_fetch import WebFetchTool
        from hearth.tools import ToolContext
        try:
            import selenium  # noqa: F401
            self.skipTest("selenium installed — refusal path not exercised here")
        except ImportError:
            pass
        ctx = ToolContext("webtest", {"web.mode": "open_browser"})
        r = asyncio.run(WebFetchTool().run({"url": "https://example.com"}, ctx))
        self.assertFalse(r.ok)
        self.assertIn("Selenium", r.content)
        r = asyncio.run(WebSearchTool().run({"query": "x y"}, ctx))
        self.assertFalse(r.ok)
        self.assertIn("Selenium", r.content)

    def test_ddg_parse_and_search_meta_for_ui(self):
        # Offline: stub ddg_search, assert the model-facing text AND the structured
        # meta the chat UI turns into clickable source links.
        import asyncio
        from hearth.tools import web_search as ws_mod
        from hearth.tools import ToolContext
        results = [{"title": "Example Post", "url": "https://example.org/post",
                    "snippet": "A useful snippet."}]
        orig = ws_mod.ddg_search

        async def fake(query, max_results=6, proxies=None):
            return list(results)
        ws_mod.ddg_search = fake
        try:
            ctx = ToolContext("webtest", {"web.mode": "search_and_fetch_safe"})
            r = asyncio.run(ws_mod.WebSearchTool().run({"query": "example post"}, ctx))
            self.assertTrue(r.ok, r.content)
            self.assertIn("https://example.org/post", r.content)
            self.assertEqual(r.meta["results"], results)
            self.assertEqual(r.meta["engine"], "duckduckgo")
            self.assertEqual(r.meta["query"], "example post")
            # blocklisted results are dropped even when the engine returned them
            ctx2 = ToolContext("webtest", {"web.block_domains": "example.org"})
            r2 = asyncio.run(ws_mod.WebSearchTool().run({"query": "example post"}, ctx2))
            self.assertTrue(r2.ok)
            self.assertNotIn("example.org", r2.content)
            self.assertIn("No results", r2.content)   # actionable no-results hint
        finally:
            ws_mod.ddg_search = orig

    def test_fetch_success_meta_carries_url_and_backend(self):
        # _success shaping is network-free and defines the UI contract.
        from hearth.tools.web_fetch import WebFetchTool
        res = WebFetchTool()._success("https://x.example/page", "Body.",
                                      [{"text": "L1", "url": "https://l1.example"}], "http")
        self.assertTrue(res.ok)
        self.assertEqual(res.meta["url"], "https://x.example/page")
        self.assertEqual(res.meta["backend"], "http")
        self.assertIn("Links", res.content)

    def test_extract_text_and_links_strips_clutter(self):
        from hearth.tools.browser_connector import extract_text_and_links
        page = ('<html><head><script>var x=1;</script><style>p{}</style></head>'
                '<body><nav>menu junk</nav><main><h1>T</h1><p>Hello world.</p>'
                '<a href="https://a.example/x">Link A</a></main>'
                '<footer>foot</footer></body></html>')
        text, links = extract_text_and_links(page, 5000, 10)
        self.assertIn("Hello world.", text)
        for junk in ("menu junk", "var x", "foot", "{}"):
            self.assertNotIn(junk, text)
        self.assertEqual(links, [{"text": "Link A", "url": "https://a.example/x"}])

    def test_web_meta_persisted_on_tool_rows(self):
        # The chat UI re-renders sources from meta.web — so the orchestrator must
        # persist it (not drop it) when writing tool rows, and pass it live too.
        from hearth import orchestrator as orch
        from hearth.tools.base import ToolResult
        res = ToolResult(True, "1. T\n   https://a.example",
                         meta={"results": [{"title": "T", "url": "https://a.example",
                                            "snippet": "s"}],
                               "engine": "duckduckgo", "backend": "duckduckgo-html",
                               "query": "q"})
        row = orch._tool_row_meta("web_search", res, "call_1")
        self.assertEqual(row["tool"], "web_search")
        self.assertEqual(row["tool_call_id"], "call_1")
        self.assertEqual(row["web"]["results"][0]["url"], "https://a.example")
        ui = orch._ui_meta("web_search", res)
        self.assertEqual(ui["web"]["backend"], "duckduckgo-html")
        # non-web tools keep the plain row shape (no web key at all)
        plain = ToolResult(True, "deleted", meta=None)
        self.assertEqual(orch._tool_row_meta("fs_delete", plain, "c2"),
                         {"tool": "fs_delete", "tool_call_id": "c2"})

    def test_web_specs_advertise_modes(self):
        # The model must be told the backend follows the admin mode (llm_coder's
        # description pattern) and that short queries work best.
        from hearth.tools import TOOLS_BY_NAME
        d = TOOLS_BY_NAME["web_search"].spec()["function"]["description"]
        self.assertIn("web-access mode", d)
        self.assertIn("2-6 keywords", d)
        params = TOOLS_BY_NAME["web_search"].spec()["function"]["parameters"]["properties"]
        self.assertIn("engine", params)
        self.assertIn("open_browser",
                      TOOLS_BY_NAME["web_fetch"].spec()["function"]["description"])

    def test_ddg_challenge_is_reported_distinctly(self):
        # Observed live: html.duckduckgo.com answers datacenter IPs with a 202 +
        # anomaly page. That must read as "bot challenge", never as "no results"
        # (which sends the model off shortening a perfectly good query).
        import asyncio
        from hearth.tools import web_search as ws_mod
        from hearth.tools import ToolContext
        challenge_html = '<div id="anomaly-modal"><form id="challenge-form"></form></div>'

        async def fake_ddg(query, max_results=6, proxies=None):
            if ws_mod.looks_challenged(challenge_html):
                raise ws_mod.DDGChallengeError()
            return []
        orig = ws_mod.ddg_search
        ws_mod.ddg_search = fake_ddg
        try:
            ctx = ToolContext("webtest", {"web.mode": "search_and_fetch_safe"})
            r = asyncio.run(ws_mod.WebSearchTool().run({"query": "fine query"}, ctx))
            self.assertFalse(r.ok)
            self.assertIn("CAPTCHA", r.content)
            self.assertTrue(ws_mod.looks_challenged(challenge_html))
            self.assertFalse(ws_mod.looks_challenged("<div>normal results here</div>"))
        finally:
            ws_mod.ddg_search = orig


def _fake_llama_client(lines):
    """Shared httpx stand-in for LlamaServerProvider.stream() tests.

    The provider does `async with AsyncClient()` and then enters `c.stream(...)` via its
    async-context-manager protocol, so the fake must implement BOTH: an object that only
    defines __aenter__/__aexit__ silently fails the outer `async with`, and because the
    provider used to wrap the whole parse loop in one try/except, that failure surfaced as
    "no tool_progress events" rather than as an error. Kept here (not copy-pasted per
    test) so all three tests exercise the same interface and cannot drift again.
    """
    import httpx

    async def _aiter(items):
        for it in items:
            yield it

    class FakeResponse:
        status_code = 200

        async def aread(self): return b""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def aiter_lines(self): return _aiter(lines)

    class FakeClient:
        def stream(self, method, url, json=None): return FakeResponse()
        async def __aenter__(self): return self          # <- required by `async with`
        async def __aexit__(self, *a): return False

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: FakeClient()
    return orig


class CryptoTests(unittest.TestCase):
    """Codec + provider-stream behaviour (tool-call parsing, progress hints)."""

    def test_roundtrip_and_passthrough(self):
        # Unit-test the codec without touching global boot settings.
        from hearth import crypto

        class FakeCipher:
            def encrypt(self, nonce, data, aad): return b"CT:" + data[::-1]
            def decrypt(self, nonce, ct, aad): return ct[3:][::-1]

        orig_en, orig_cip = crypto.encryption_enabled, crypto._cipher
        crypto.encryption_enabled = lambda: True
        crypto._cipher = lambda: FakeCipher()
        try:
            blob = crypto.encrypt_bytes(b"hello")
            self.assertTrue(blob.startswith(crypto.MAGIC))
            self.assertNotIn(b"hello", blob)
            self.assertEqual(crypto.decrypt_bytes(blob), b"hello")
            # unframed data passes through untouched (mid-life enablement safety)
            self.assertEqual(crypto.decrypt_bytes(b"plain old file"), b"plain old file")
        finally:
            crypto.encryption_enabled, crypto._cipher = orig_en, orig_cip



    """Abort / max-tool-steps / retry / context-usage / device-grant (session 5)."""

    def test_abort_registry_scoping_and_staleness(self):
        from hearth import cancel as c
        h = c.begin("convA", "u1")
        self.assertTrue(c.is_running("convA", "u1"))
        self.assertFalse(c.is_running("convA", "u2"), "other user must not see/abort it")
        # A new turn replaces the handle; aborting then ending the OLD handle must
        # not unregister the new one (that would wedge the chat as un-abortable).
        h2 = c.begin("convA", "u1")
        self.assertTrue(c.abort_conversation("convA", "u1"))
        self.assertTrue(h2.cancelled)
        c.end("convA", h)
        self.assertTrue(c.is_running("convA", "u1"))
        c.end("convA", h2)
        self.assertFalse(c.is_running("convA", "u1"))
        # Aborting something not running is a no-op, not an error.
        self.assertFalse(c.abort_conversation("conv-nope", "u1"))

    def test_max_tool_steps_setting_bounds_loop(self):
        from hearth import orchestrator as orch
        # Default matches the historical cap; explicit values are respected and
        # garbage falls back rather than disabling the safety net entirely.
        self.assertEqual(orch._max_steps({}), orch.MAX_TOOL_LOOPS)
        self.assertEqual(orch._max_steps({"agent.max_tool_steps": 2}), 2)
        self.assertEqual(orch._max_steps({"agent.max_tool_steps": "0"}), 1)
        self.assertEqual(orch._max_steps({"agent.max_tool_steps": "nonsense"}),
                         orch.MAX_TOOL_LOOPS)
        spec = next(s for s in _specs() if s.key == "agent.max_tool_steps")
        self.assertEqual((spec.min, spec.max), (1, 50))

    def _make_conv(self, conv_id: str, user_id: str = "u") -> None:
        """Create the user + conversation a turn needs.

        Turns write real Message rows (FK to conversations), so tests that drive
        run_turn must set up the row rather than inventing an id — otherwise the
        first append trips a FOREIGN KEY constraint.
        """
        from hearth.db import init_db, session_scope
        from hearth.models import Conversation, User
        init_db()
        with session_scope() as s:
            if s.get(User, user_id) is None:
                s.add(User(id=user_id, username=f"user-{user_id}", pw_hash="x"))
            if s.get(Conversation, conv_id) is None:
                s.add(Conversation(id=conv_id, user_id=user_id, title="test"))

    def test_abort_ends_turn_with_aborted_event(self):
        # End-to-end through run_turn with a provider that streams forever: the
        # handle must interrupt it and surface exactly one `aborted` event.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent

        conv = "abort-conv"
        self._make_conv(conv)

        class Chatty:
            async def stream(self, messages, tools, params):
                for _ in range(10_000):
                    yield StreamEvent("token", text="blah ")

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                h.cancel()   # aborted before the first event: loop must stop immediately
                return [ev async for ev in orch.run_turn("u", conv, Chatty(), {}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        types = [e["type"] for e in asyncio.run(go())]
        self.assertEqual(types, ["aborted"], f"expected a single aborted event, got {types}")

    def test_tool_progress_streams_to_client_without_raw_args(self):
        # While the model composes a tool call (args streaming in), the user must
        # see live progress. A provider that emits tool_progress hints mid-call
        # must surface them as SSE dicts — carrying only name + char count, never
        # the raw argument fragment (it can contain whole file contents).
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent

        conv = "prog-conv"
        self._make_conv(conv)

        class SlowWriter:
            async def stream(self, messages, tools, params):
                for i in (100, 500, 900):
                    yield StreamEvent.tool_progress("fs_write", "x" * i)
                from hearth.llm.base import ToolRequest
                yield StreamEvent("tool_request", tool=ToolRequest(
                    "fs_write", {"path": "a.txt", "content": "done"}, call_id="c1"))
                yield StreamEvent("done", data={"finish": "tool"})

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                return [ev async for ev in orch.run_turn("u", conv, SlowWriter(), {}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        events = asyncio.run(go())
        # NB: one provider stream per AGENT LOOP. This fake never sees a `tool` result,
        # so the loop re-streams until the tool-step budget runs out and each round's
        # hints appear — hence N rounds of (100, 500, 900), not one flat list. Assert
        # the shape of one round plus the total count; the leak check below is what
        # actually guards the wire format.
        progs = [e for e in events if e["type"] == "tool_progress"]
        rounds = len(progs) // 3
        self.assertGreaterEqual(rounds, 1, "no tool_progress forwarded at all")
        self.assertEqual(len(progs), rounds * 3, f"unexpected hint stream: {progs}")
        for r in range(rounds):
            self.assertEqual([p["chars"] for p in progs[r * 3:r * 3 + 3]], [100, 500, 900])
        self.assertTrue(all(p["name"] == "fs_write" for p in progs))
        # The raw fragment must never reach the client on any event.
        blob = str(events)
        self.assertNotIn("x" * 100, blob, "raw args fragment leaked into SSE events")

    def test_tool_progress_forwards_path_hint(self):
        # The client chip needs to say WHICH file is being written; the server forwards
        # only the path hint, never the argument fragment.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent

        conv = "prog-conv-hint"
        self._make_conv(conv)

        class Writer:
            async def stream(self, messages, tools, params):
                yield StreamEvent.tool_progress("fs_write", '{"path": "hello.py"', first_name="hello.py")
                from hearth.llm.base import ToolRequest
                yield StreamEvent("tool_request", tool=ToolRequest(
                    "fs_write", {"path": "hello.py", "content": "print('hi')"}, call_id="c1"))
                yield StreamEvent("done", data={"finish": "tool"})

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                return [ev async for ev in orch.run_turn("u", conv, Writer(), {}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        events = asyncio.run(go())
        progs = [e for e in events if e["type"] == "tool_progress"]
        self.assertTrue(progs, "no tool_progress forwarded")
        self.assertEqual(progs[0]["path_hint"], "hello.py")
        # Content still never crosses the wire.
        self.assertNotIn("print('hi')", str(progs))

    def test_llama_server_emits_throttled_tool_progress(self):
        # llama-server streams tool-call ARGUMENTS as JSON fragments across many
        # deltas. The provider must emit tool_progress hints while they arrive —
        # throttled (not per-delta) — and still parse the final call correctly.
        import asyncio, json
        from hearth.llm.llama_server import LlamaServerProvider

        content = "print('hi')" * 200          # ~1.8 KB of file content
        args_full = json.dumps({"path": "cool.py", "content": content})
        chunks = [args_full[i:i + 64] for i in range(0, len(args_full), 64)]
        # Sanity: the hint-count bound below is expressed as a fraction of delta count,
        # so it only means something if this fixture really is many small deltas. Guard
        # the relationship instead of assuming ~1.8 KB / 64 B holds forever.
        self.assertGreater(len(chunks), 20,
                           f"fixture too small to test throttling: {len(chunks)} deltas")

        def sse(obj):
            return f"data: {json.dumps(obj)}\n"

        # Build the first tool-call delta explicitly (same shape as before, just readable).
        first_delta = {"tool_calls": [{"index": 0, "id": "c1",
                                       "function": {"name": "fs_write", "arguments": ""}}]}
        lines = [sse({"choices": [{"delta": first_delta}]})]
        for i, ch in enumerate(chunks):
            delta = {"tool_calls": [{"index": 0}]}
            if i == 0:
                delta["tool_calls"][0]["id"] = "c1"
                delta["tool_calls"][0]["function"] = {"name": "fs_write", "arguments": ch}
            else:
                delta["tool_calls"][0]["function"] = {"arguments": ch}
            lines.append(sse({"choices": [{"delta": delta}]}))
        lines.append(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        lines.append("data: [DONE]\n")

        import httpx
        orig = _fake_llama_client(lines)
        try:
            events = []
            async def go():
                async for ev in LlamaServerProvider("http://127.0.0.1:9").stream(
                        [{"role": "user", "content": "x"}], [], {}):
                    events.append(ev)
            asyncio.run(go())
        finally:
            httpx.AsyncClient = orig

        progs = [e for e in events if e.type == "tool_progress"]
        # Throttled: far fewer hints than 64-byte deltas, but at least one.
        self.assertGreater(len(progs), 0, "no tool_progress emitted")
        self.assertLess(len(progs), len(chunks) // 2, f"not throttled: {len(progs)} hints for {len(chunks)} chunks")
        # The last hint tracks the full argument size to within ONE THROTTLE STEP (256 B),
        # not one delta. This assertion previously used `- 64` (a delta), which the throttle
        # can never guarantee: by design a hint only fires after 256 new bytes, so the final
        # ~255 B legitimately arrive un-reported and the client eases the bar to completion.
        self.assertGreaterEqual(progs[-1].data["chars"], len(args_full) - 256,
                                f"last hint {progs[-1].data['chars']} too far below {len(args_full)}")
        # And it never over-reports — a chip must not claim more bytes than exist.
        self.assertTrue(all(e.data["chars"] <= len(args_full) for e in progs),
                        [e.data["chars"] for e in progs])
        self.assertTrue(all(e.data["name"] == "fs_write" for e in progs))
        # The chip's file name comes from the partial JSON: `"path": "cool.py"` must
        # surface as first_name long before the whole call has streamed in.
        self.assertTrue(all(e.data.get("first_name") == "cool.py" for e in progs),
                        [e.data for e in progs])

    def test_tool_request_carries_raw_args_for_usage(self):
        # The orchestrator sizes a tool round's output from the RAW argument string, so
        # the provider must hand it over — otherwise the usage estimate for an fs_write
        # counts nothing and the context bar under-reports every write.
        import json as _json
        from hearth.llm.base import ToolRequest
        self.assertEqual(getattr(ToolRequest("fs_write", {"path": "a"}, call_id="c"),
                                 "args_raw", None), "")
        raw = _json.dumps({"path": "a.txt", "content": "hi"})
        req = ToolRequest("fs_write", {"path": "a.txt", "content": "hi"}, call_id="c", args_raw=raw)
        self.assertEqual(req.args_raw, raw)

    def test_agent_loop_reports_usage_for_tool_rounds(self):
        # Regression: usage was emitted only when a stream ended WITHOUT tool calls, and
        # llama-server puts usage on the final chunk we never wait for. A tool round must
        # still produce a `usage` event (estimated) or the context bar freezes and the
        # tok/s badge goes blank on exactly the turns that generate the most output.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent, ToolRequest

        conv = "usage-tool-conv"
        self._make_conv(conv)

        class Writer:
            async def stream(self, messages, tools, params):
                yield StreamEvent("tool_request", tool=ToolRequest(
                    "fs_write", {"path": "a.txt", "content": "x" * 400}, call_id="c1",
                    args_raw='{"path":"a.txt","content":"' + "x" * 400 + '"}'))
                yield StreamEvent("done", data={"finish": "tool"})

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                return [ev async for ev in orch.run_turn(
                    "u", conv, Writer(), {"agent.max_tool_steps": 1}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        events = asyncio.run(go())
        usages = [e for e in events if e["type"] == "usage"]
        self.assertTrue(usages, "no usage event on a tool-only turn")
        u = usages[0]["data"]
        self.assertTrue(u.get("estimated"), f"tool-round usage should be flagged: {u}")
        self.assertTrue(u.get("prompt_tokens"), f"prompt size missing: {u}")
        # ~4 chars/token over the generated arguments, not zero.
        self.assertGreater(u.get("completion_tokens") or 0, 50, u)

    def test_stub_reports_usage_on_tool_rounds(self):
        # The stub reports its own per-round usage (it has no final chunk either), which
        # also proves the orchestrator does NOT double-count it with an estimate.
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.llm.stub import StubProvider
        from hearth.tools import tool_specs
        specs = tool_specs({"tools.fs_read": True})
        usage = []
        async def collect():
            async for ev in StubProvider().stream([{"role": "user", "content": "list"}], specs, {}):
                if ev.type == "usage":
                    usage.append(ev.data)
        asyncio.run(collect())
        self.assertTrue(usage, "stub emitted no usage on a tool round")
        self.assertTrue(usage[0].get("prompt_tokens"), usage[0])

    def test_llama_server_path_hint_never_leaks_content(self):
        # The path hint is scraped from the PARTIAL JSON, so a file whose content also
        # looks like `"key": "value"` must not have that content end up as the name —
        # and nothing of the fragment may cross the wire.
        import asyncio, json
        from hearth.llm.llama_server import LlamaServerProvider

        content = 'x = {"secret_key": "hunter2"}\n' * 40
        args_full = json.dumps({"path": "cfg.py", "content": content})
        chunks = [args_full[i:i + 32] for i in range(0, len(args_full), 32)]

        def sse(obj):
            return f"data: {json.dumps(obj)}\n"

        lines = []
        for i, ch in enumerate(chunks):
            delta = {"tool_calls": [{"index": 0,
                                     "function": {"arguments": ch},
                                     **({"id": "c1"} if i == 0 else {})}]}
            if i == 0:
                delta["tool_calls"][0]["function"]["name"] = "fs_write"
            lines.append(sse({"choices": [{"delta": delta}]}))
        lines.append("data: [DONE]\n")

        import httpx
        orig = _fake_llama_client(lines)
        try:
            events = []
            async def go():
                async for ev in LlamaServerProvider("http://127.0.0.1:9").stream(
                        [{"role": "user", "content": "x"}], [], {}):
                    events.append(ev)
            asyncio.run(go())
        finally:
            httpx.AsyncClient = orig

        progs = [e for e in events if e.type == "tool_progress"]
        self.assertGreaterEqual(len(progs), 2, f"{len(progs)} hints — too coarse")
        # A swallowed parse error would also leave a `done{finish:"error"}` behind.
        self.assertFalse([e for e in events if e.type == "error"],
                         f"provider raised mid-stream: {[e.data for e in events if e.type=='error']}")
        for e in progs:
            fn = e.data.get("first_name", "")
            self.assertNotIn("hunter2", fn, f"content leaked into the path hint: {fn!r}")
            self.assertLessEqual(len(fn), 64)

    def test_llama_server_progress_hints_start_early(self):
        # A small file (~1 KB of args) must still show visible progress: with a coarse
        # throttle such a call produced one hint at the very end, which looked like a
        # hang for the whole time the model was composing it.
        import asyncio, json
        from hearth.llm.llama_server import LlamaServerProvider

        args_full = json.dumps({"path": "small.py", "content": "print('hi')\n" * 40})
        chunks = [args_full[i:i + 24] for i in range(0, len(args_full), 24)]

        def sse(obj):
            return f"data: {json.dumps(obj)}\n"

        lines = []
        for i, ch in enumerate(chunks):
            delta = {"tool_calls": [{"index": 0, "function": {"arguments": ch}}]}
            if i == 0:
                delta["tool_calls"][0]["function"]["name"] = "fs_write"
                delta["tool_calls"][0]["id"] = "c1"
            lines.append(sse({"choices": [{"delta": delta}]}))
        lines.append("data: [DONE]\n")

        import httpx
        orig = _fake_llama_client(lines)
        try:
            events = []
            async def go():
                async for ev in LlamaServerProvider("http://127.0.0.1:9").stream(
                        [{"role": "user", "content": "x"}], [], {}):
                    events.append(ev)
            asyncio.run(go())
        finally:
            httpx.AsyncClient = orig

        progs = [e for e in events if e.type == "tool_progress"]
        self.assertGreaterEqual(len(progs), 2,
                                f"{len(progs)} hints for a {len(args_full)} B call — too coarse")
        # At least one hint must arrive EARLY (the first ~100 bytes), so the chip shows
        # "writing small.py" as soon as the model starts composing it rather than only
        # when the call is nearly complete.
        self.assertLessEqual(progs[0].data["chars"], 96)
        # ...and at least one later hint reflecting real growth.
        self.assertGreater(progs[-1].data["chars"], len(args_full) * 0.5)
        self.assertTrue(all(e.data.get("first_name") == "small.py" for e in progs),
                        [e.data for e in progs])

    def test_repetition_penalty_sent_only_when_raised(self):
        # Admin → Generation → Repetition penalty must reach llama-server when the admin
        # asks for it, and be ABSENT at the neutral 1.0 (so an unconfigured install sends
        # exactly what it always did, and a build that rejects the field cannot break).
        # NB: `import httpx` is required here — this test replaces httpx.AsyncClient, so
        # referencing it without importing raises NameError (which is exactly how this
        # test first failed for the operator). Other provider tests get httpx from an
        # earlier import in their own body; do not rely on that.
        import asyncio
        import httpx
        from hearth.llm.llama_server import LlamaServerProvider

        captured = {}

        class _Resp:
            status_code = 200
            async def aiter_lines(self):
                yield "data: [DONE]"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def stream(self, method, url, json=None):
                captured["body"] = json or {}
                return _Resp()

        orig = httpx.AsyncClient
        try:
            httpx.AsyncClient = _Client
            async def go(params):
                async for _ in LlamaServerProvider("http://127.0.0.1:9").stream(
                        [{"role": "user", "content": "x"}], [], params):
                    pass
            asyncio.run(go({"temperature": 0.7}))
            self.assertNotIn("repetition_penalty", captured["body"], captured["body"])
            asyncio.run(go({"repetition_penalty": 1.12}))
            self.assertEqual(captured["body"].get("repetition_penalty"), 1.12)
        finally:
            httpx.AsyncClient = orig

    def test_repeated_tool_call_is_not_executed_twice(self):
        # The reported loop: fs_edit fails, the model re-reads and re-issues the SAME
        # edit until max tool steps. A provider that keeps asking for the identical call
        # must not have the tool executed again — it gets the recorded result back with a
        # "do not repeat" note, and the file is only touched once.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.base import StreamEvent, ToolRequest

        conv = "repeat-conv"
        self._make_conv(conv)
        runs = []

        class Looper:
            async def stream(self, messages, tools, params):
                runs.append(1)
                yield StreamEvent("tool_request", tool=ToolRequest(
                    "fs_append", {"path": "loop.txt", "content": "X"}, call_id=f"c{len(runs)}"))
                yield StreamEvent("done", data={"finish": "tool"})

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                return [ev async for ev in orch.run_turn(
                    "u", conv, Looper(), {"agent.max_tool_steps": 4}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        events = asyncio.run(go())
        from hearth import workspaces as ws
        # One append per round is what the guard prevents: the file holds a single X.
        self.assertEqual(ws.read_text("u", "loop.txt"), "X")
        results = [e for e in events if e["type"] == "tool_result" and e["name"] == "fs_append"]
        self.assertEqual(len(results), 4, f"expected one result per round: {len(results)}")
        suppressed = [e for e in results if "do not repeat" in (e["preview"] or "")]
        self.assertTrue(suppressed, "no suppression note on the repeated calls")
        self.assertFalse([e for e in results if e.get("meta", {}).get("download")],
                         "repeated call must not hand out a download link")

    def test_done_note_is_stripped_from_replayed_tool_calls(self):
        # The harness note written into assistant.tool_calls must never accumulate or
        # reach the model as part of the arguments it asked for.
        import asyncio
        from hearth import orchestrator as orch
        tcs = [{"id": "c1", "type": "function",
                "function": {"name": "fs_read",
                             "arguments": '{"path":"a"}' + orch._DONE_NOTE.format(done="fs_read x")}}]
        clean = orch._clean_tool_calls(tcs)
        self.assertEqual(clean[0]["function"]["arguments"], '{"path":"a"}')
        self.assertEqual(clean[0]["id"], "c1")
        # Idempotent on plain arguments, and the estimate ignores the note too. Compare
        # the SAME call with and without the note — comparing against a different name /
        # shorter arguments measures the fixture, not the stripping (that is what made
        # this assert 12 != 8).
        msg = [{"role": "assistant", "content": "", "tool_calls": tcs}]
        bare = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "fs_read",
                                                          "arguments": '{"path":"a"}'}}]}]
        self.assertEqual(orch._estimate_prompt_tokens(msg),
                         orch._estimate_prompt_tokens(bare))
        plain = [{"id": "c2", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
        self.assertEqual(orch._clean_tool_calls(plain), plain)

    def test_done_note_does_not_accumulate_across_turns(self):
        # Two turns where the model repeats a call: the note is written per round and
        # stripped on replay, so it must never compound in the stored rows.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.db import session_scope
        from hearth.models import Message
        from hearth.llm.base import StreamEvent, ToolRequest

        conv = "note-conv"
        self._make_conv(conv)
        seen = []

        class Looper:
            async def stream(self, messages, tools, params):
                seen.append([m.get("tool_calls") for m in messages if m.get("tool_calls")])
                yield StreamEvent("tool_request", tool=ToolRequest(
                    "fs_list", {"path": "."}, call_id=f"c{len(seen)}"))
                yield StreamEvent("done", data={"finish": "tool"})

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                for _ in range(2):   # two turns, each with several repeat rounds
                    [ev async for ev in orch.run_turn(
                        "u", conv, Looper(), {"agent.max_tool_steps": 3}, {}, cancel=h)]
            finally:
                cancel_mod.end(conv, h)

        asyncio.run(go())
        marks = 0
        with session_scope() as s:
            for m in s.query(Message).filter(Message.conversation_id == conv).all():
                marks += str(m.meta or "").count(orch._DONE_MARK)
        self.assertGreater(marks, 0, "expected the note to be written at least once")
        # What the model is shown must contain at most one note per tool call.
        for calls in [c for rnd in seen for c in rnd]:
            for tc in calls:
                self.assertEqual(str(tc["function"]["arguments"]).count(orch._DONE_MARK), 0,
                                 f"note leaked into the replayed message: {tc}")

    def test_no_tools_turn_ignores_tool_requests(self):
        # "Force respond": with specs=[] a well-behaved provider emits no tool calls,
        # so the turn ends in one pass. The stub honours an empty spec list.
        import asyncio
        from hearth import orchestrator as orch
        from hearth.llm.stub import StubProvider

        conv = "notools-conv"
        self._make_conv(conv)

        async def go():
            from hearth import cancel as c
            h = c.begin(conv, "u")
            try:
                return [ev async for ev in orch.run_turn("u", conv, StubProvider(), {}, {},
                                                         gen={"no_tools": True}, cancel=h)]
            finally:
                c.end(conv, h)

        types = [e["type"] for e in asyncio.run(go())]
        self.assertNotIn("tool_call", types, f"no_tools turn still used tools: {types}")
        self.assertIn("token", types)
        self.assertEqual(types[-1], "done")

    def test_turn_on_deleted_conversation_ends_cleanly(self):
        # Regression for the FK crash this suite surfaced: a turn can outlive its
        # conversation (deleted while a cooperative abort lets a tool finish). It
        # must end quietly, not raise IntegrityError out of the SSE generator.
        import asyncio
        from hearth.db import session_scope
        from hearth.models import Conversation, Message
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod
        from hearth.llm.stub import StubProvider

        conv = "gone-conv"
        self._make_conv(conv)

        async def go():
            h = cancel_mod.begin(conv, "u")
            try:
                # First turn writes normally...
                first = [ev async for ev in orch.run_turn("u", conv, StubProvider(), {}, {},
                                                           gen={"no_tools": True}, cancel=h)]
                # ...then the chat is deleted and another turn streams into it.
                with session_scope() as s:
                    s.query(Message).filter(Message.conversation_id == conv) \
                        .delete(synchronize_session=False)
                    s.query(Conversation).filter(Conversation.id == conv) \
                        .delete(synchronize_session=False)
                second = [ev async for ev in orch.run_turn("u", conv, StubProvider(), {}, {},
                                                           gen={"no_tools": True}, cancel=h)]
                return first, second
            finally:
                cancel_mod.end(conv, h)

        first, second = asyncio.run(go())   # must not raise
        self.assertEqual(first[-1]["type"], "done")
        self.assertTrue(any(e.get("conversation_gone") for e in second),
                        f"expected a conversation_gone done event, got {[e['type'] for e in second]}")
        with session_scope() as s:
            orphans = s.query(Message).filter(Message.conversation_id == conv).count()
        self.assertEqual(orphans, 0, "deleted conversation must not accumulate orphan messages")

    def test_args_summary_summarizes_without_dumping_content(self):
        from hearth import orchestrator as orch
        from hearth.tools.fs_tools import FsWriteTool, FsEditTool
        s = orch._args_summary(FsWriteTool(), {"path": "notes/a.txt", "content": "x" * 5000})
        self.assertIn("notes/a.txt", s)
        self.assertIn("5000 chars", s)
        self.assertNotIn("xxxxxx", s, "file contents must never be dumped into the chip")
        e = orch._args_summary(FsEditTool(), {"path": "a.md", "old_text": "foo", "new_text": "bar"})
        self.assertIn("foo", e)
        self.assertIn("bar", e)

    def test_device_tools_refuse_without_grant(self):
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools import ToolContext
        from hearth.tools.device_tools import DeviceListTool, DeviceScreenshotTool

        # No grant + requirement on -> refused, and the message says why.
        ctx = ToolContext("devuser", {"agent.require_device_grant": True})
        r = asyncio.run(DeviceListTool().run({"path": "."}, ctx))
        self.assertFalse(r.ok)
        self.assertTrue("helper" in r.content.lower() or "allow" in r.content.lower())
        s = asyncio.run(DeviceScreenshotTool().run({}, ctx))
        self.assertFalse(s.ok)

        # Requirement off -> the helper (not the grant) is the blocker.
        ctx2 = ToolContext("devuser", {"agent.require_device_grant": False})
        r2 = asyncio.run(DeviceListTool().run({"path": "."}, ctx2))
        self.assertFalse(r2.ok)
        self.assertIn("helper", r2.content.lower())

    def test_tool_context_reads_grant_from_settings(self):
        from hearth.tools import ToolContext
        off = ToolContext("u", {"agent.require_device_grant": True})
        on = ToolContext("u", {"agent.require_device_grant": True, "_device.granted": True})
        self.assertFalse(off.device_granted())
        self.assertTrue(on.device_granted())
        self.assertTrue(on.require_device_grant())
        # No helper configured anywhere -> nothing is paired.
        os.environ.pop("HEARTH_DEVICE_HELPER_URL", None)
        self.assertFalse(on.device_helper_present())

    def test_context_bar_computes_percentage(self):
        # The /api/chat/context-usage maths lives in routes_chat but must not need
        # FastAPI to be checked, so it is factored into context_usage_payload().
        from hearth.routes_chat import context_usage_payload

        # Estimate path: ~4 chars/token PLUS a 16-token overhead constant (system
        # prompt), so 4000 chars -> 1016 tokens, not a naive 1000. Assert the real
        # contract (estimate flag + overhead included) rather than round numbers.
        d = context_usage_payload(total_chars=4000, usage=None, n_ctx=10_000)
        self.assertEqual(d["source"], "estimate")
        self.assertEqual(d["n_ctx"], 10_000)
        self.assertEqual(d["prompt_tokens"], 4000 // 4 + 16)
        self.assertAlmostEqual(d["pct"], (1016 / 10_000), places=4)
        self.assertFalse(d["warning"])

        # Measured path wins over any character count.
        d = context_usage_payload(total_chars=50_000, usage={"prompt_tokens": 9500}, n_ctx=10_000)
        self.assertEqual(d["source"], "usage")
        self.assertEqual(d["prompt_tokens"], 9500)
        self.assertAlmostEqual(d["pct"], 0.95, places=4)
        self.assertTrue(d["warning"], ">=80% should warn the user")

        # Boundary: exactly at the warning threshold, and clamping past the window.
        self.assertTrue(context_usage_payload(0, {"prompt_tokens": 8000}, 10_000)["warning"])
        self.assertFalse(context_usage_payload(0, {"prompt_tokens": 7999}, 10_000)["warning"])
        self.assertEqual(context_usage_payload(0, {"prompt_tokens": 40_000}, 10_000)["pct"], 1.0,
                         "usage above the window must clamp to 1.0, not report 400%")

        # No window configured -> no percentage rather than a divide-by-zero;
        # nothing stored at all -> source "none" and zero usage shown.
        d = context_usage_payload(total_chars=100, usage=None, n_ctx=0)
        self.assertIsNone(d["pct"])
        self.assertFalse(d["warning"])
        d = context_usage_payload(total_chars=0, usage=None, n_ctx=8192)
        self.assertEqual(d["source"], "none")
        self.assertIsNone(d["prompt_tokens"])

    def test_turn_options_clamp_and_flags(self):
        from hearth.routes_chat import _turn_options
        settings = {"model.max_tokens": 2048}
        self.assertEqual(_turn_options({"no_tools": True}, settings), {"no_tools": True})
        self.assertEqual(_turn_options({}, settings), {})
        # Clamped to the admin cap from BOTH sides.
        self.assertEqual(_turn_options({"max_tokens": 999999}, settings)["max_tokens"], 2048)
        self.assertEqual(_turn_options({"max_tokens": 1}, settings)["max_tokens"], 16)
        self.assertEqual(_turn_options({"max_tokens": "junk"}, settings), {})

    def test_model_start_stop_exist(self):
        from hearth.manager import get_manager
        m = get_manager()
        for name in ("start", "stop_server", "ensure_server", "status"):
            self.assertTrue(callable(getattr(m, name)), f"manager.{name} missing")

    def test_healthz_never_launches_llama_server(self):
        # DEC-048: opening the chat page hit /healthz, which called status() and —
        # through get_provider()/ensure_server() — started llama-server. The badge
        # endpoint must stay a pure probe.
        import asyncio
        from unittest import mock
        from hearth.manager import get_manager
        m = get_manager()
        calls = []

        async def _boom(*a, **k):
            calls.append((a, k))
            return ""

        with mock.patch.object(m, "ensure_server", _boom), \
             mock.patch.object(m, "_reachable", lambda u: asyncio.sleep(0, result=False)), \
             mock.patch.object(m, "_is_llama_server", lambda u: asyncio.sleep(0, result=False)):
            probe = asyncio.run(m.probe())
            self.assertEqual(calls, [], "probe() must not call ensure_server()")

            # status() is the admin-facing view; it must stay launch-free too.
            st = asyncio.run(m.status())
            self.assertEqual(calls, [], "status() must not call ensure_server()")
        self.assertFalse(probe["server_reachable"])
        self.assertIsNone(probe.get("n_ctx"))
        self.assertNotIn("effective", probe, "probe() must not report a provider choice")

    def test_new_settings_specs_registered(self):
        from hearth import runtime_settings as rs
        for key in ("agent.max_tool_steps", "agent.require_device_grant", "device.helper_url",
                    "model.repetition_penalty"):
            self.assertIn(key, rs.SPEC_BY_KEY, f"{key} not registered")
        steps = rs.SPEC_BY_KEY["agent.max_tool_steps"]
        self.assertEqual((steps.type, steps.min, steps.max), ("int", 1, 50))
        # Defaults must survive an empty DB (get_all falls back to spec defaults).
        vals = rs.get_all()
        self.assertEqual(vals["agent.max_tool_steps"], 6)
        self.assertTrue(vals["agent.require_device_grant"])
        # Repetition penalty defaults to OFF (1.0) and is a generation-time setting, so
        # it must not claim to need a model reload (same rule as temperature/top_p).
        rp = rs.SPEC_BY_KEY["model.repetition_penalty"]
        self.assertEqual((rp.type, rp.default, rp.min, rp.max), ("float", 1.0, 1.0, 2.0))
        self.assertFalse(rp.requires_reload)
        self.assertEqual(float(vals["model.repetition_penalty"]), 1.0)

    def test_new_i18n_keys_have_german(self):
        from hearth import i18n
        for k in ("chat.abort", "chat.retry", "chat.force_respond", "chat.agent_mode_on",
                  "chat.chat_only", "chat.aborted", "chat.ctx_title", "chat.device_note",
                  "chat.await_approval", "chat.still_running", "chat.generating",
                  "chat.writing", "chat.composing_cmd",
                  "chat.intro_head", "chat.intro_body", "chat.intro_dismiss",
                  # Sidebar redesign: rail categories, prompt editor, skills panel.
                  "nav.chats", "nav.files", "nav.prompt", "nav.skills",
                  "prompt.title", "prompt.save", "prompt.saved", "prompt.hint",
                  "skills.title", "skills.hint", "settings.debug_hint",
                  "chat.device_hint"):
            self.assertIn(k, i18n._EN, f"missing EN {k}")
            self.assertIn(k, i18n._DE, f"missing DE {k}")

    def test_system_prompt_column_and_api(self):
        """Per-user system prompt: ORM column + migration + resolution rules."""
        from hearth.db import init_db, session_scope
        init_db()
        from hearth.models import User, Conversation, Doc
        self.assertIn("system_prompt", User.__table__.columns)
        from sqlalchemy import inspect
        from hearth.db import get_engine
        insp = inspect(get_engine())
        cols = {c["name"] for c in insp.get_columns("users")}
        self.assertIn("system_prompt", cols)

        uid = "sysprompt-tester"
        with session_scope() as s:
            u = s.query(User).filter(User.username == uid).one_or_none()
            if u is None:
                u = User(username=uid, pw_hash="x")
                s.add(u)
            u.system_prompt = "Antworte auf Deutsch."
            # NB: flush FIRST — the User PK default only applies at flush time, and
            # conversations.user_id is a real FK (a dangling id fails immediately).
            s.flush()
            uid = u.id
            conv = Conversation(user_id=uid, title="t")
            s.add(conv)
            s.flush()
            cid = conv.id
        from hearth.orchestrator import build_messages
        # No conversation doc -> the personal prompt is used.
        msgs = build_messages(cid, uid)
        self.assertIn("Antworte auf Deutsch", msgs[0]["content"])
        # An explicit admin doc on the conversation wins over the personal prompt.
        with session_scope() as s:
            doc = Doc(kind="system_prompt", name="d", content="DOC-PROMPT")
            s.add(doc)
            s.flush()
            c = s.get(Conversation, cid)
            c.system_prompt_doc_id = doc.id
        msgs = build_messages(cid, uid)
        self.assertIn("DOC-PROMPT", msgs[0]["content"])
        self.assertNotIn("Antworte auf Deutsch", msgs[0]["content"])

    def test_prefs_reject_removed_agent_mode_key(self):
        """agent_mode_default was removed with the Agent-mode button: unknown keys
        must not be stored (the whitelist silently drops them)."""
        from hearth.routes_chat import _PREF_KEYS
        self.assertNotIn("agent_mode_default", _PREF_KEYS)
        self.assertEqual(_PREF_KEYS, {"enter_sends", "auto_scroll", "show_timestamps"})

    def test_conversation_device_granted_column(self):
        # The grant lives on the conversation row; verify the ORM column + that the
        # migration helper adds it to a pre-existing table (idempotently).
        from hearth.db import init_db
        init_db()
        from hearth.models import Conversation
        self.assertIn("device_granted", Conversation.__table__.columns)
        from sqlalchemy import inspect
        from hearth.db import get_engine
        insp = inspect(get_engine())
        cols = {c["name"] for c in insp.get_columns("conversations")}
        self.assertIn("device_granted", cols)


def _specs():
    """ALL_SPECS, fetched lazily so the class body needs no imports."""
    from hearth import runtime_settings as rs
    return rs.ALL_SPECS


class ProviderStreamLifetimeTests(unittest.TestCase):
    """Regression guard for the client-lifetime bug in LlamaServerProvider.

    The provider used to open its httpx client with `async with AsyncClient() ...` and
    then LEAVE that block before iterating the streamed body (nothing yielded inside
    it). Against real httpx that closes the connection pool under the still-pending
    body, so the first aiter_lines() read raised ReadError('') almost immediately —
    reported to users as "llama-server stream interrupted" while the server was fine.

    The existing `_fake_llama_client` cannot catch this: its client is inert on close,
    so reading after `__aexit__` silently works. These tests use a STRICT fake that
    mimics real httpx — once the client has exited, reading the body raises ReadError.
    That makes the failure self-reporting instead of invisible (the lesson of DEC-044).
    """

    @staticmethod
    def _strict_client():
        """httpx.AsyncClient stand-in whose reads fail after the client closes.

        This is the property the old code violated and real httpx enforces: a streamed
        response is only readable while its client is open. Returns (factory, orig) —
        restore httpx.AsyncClient = orig in a finally.
        """
        import httpx

        def sse(obj):
            return "data: " + json.dumps(obj) + "\n"

        lines = [
            sse({"choices": [{"delta": {"role": "assistant"}}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1",
                 "function": {"name": "fs_read", "arguments": '{"path":"notes.md"}'}}]}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "data: [DONE]\n",
        ]

        class StrictResponse:
            status_code = 200

            def __init__(self, client):
                self._client = client

            async def aread(self):
                return b""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aiter_lines(self):
                # The whole point: reading after the client closed is an error, exactly
                # like real httpx. If the provider leaves `async with AsyncClient()`
                # before this finishes, we raise instead of yielding.
                for ln in lines:
                    if self._client.closed:
                        raise httpx.ReadError("read on a closed client")
                    yield ln

        class StrictClient:
            def __init__(self):
                self.closed = False

            def stream(self, method, url, json=None):
                return StrictResponse(self)

            async def __aenter__(self):
                self.closed = False
                return self

            async def __aexit__(self, *a):
                self.closed = True
                return False

        holder = {}

        def factory(*a, **k):
            # One client instance per AsyncClient() call; remember it so the test can
            # assert it was actually closed (no leak) after the stream finished.
            c = StrictClient()
            holder["client"] = c
            return c

        orig = httpx.AsyncClient
        httpx.AsyncClient = factory
        return holder, orig

    def test_stream_reads_body_while_client_open(self):
        # The core regression: streaming must yield the tool call, not an error. Under
        # the old structure this produced an `error` event (ReadError) and no tool.
        from hearth.llm.llama_server import LlamaServerProvider
        holder, orig = self._strict_client()
        try:
            events = []

            async def go():
                p = LlamaServerProvider("http://127.0.0.1:9")
                async for ev in p.stream([{"role": "user", "content": "x"}],
                                         [{"type": "function", "function":
                                             {"name": "fs_read", "parameters": {}}}], {}):
                    events.append(ev)
            asyncio.run(go())
        finally:
            import httpx
            httpx.AsyncClient = orig

        errs = [e for e in events if e.type == "error"]
        self.assertEqual(errs, [], f"unexpected error event(s): {[e.text for e in errs]}")
        tools = [e for e in events if e.type == "tool_request"]
        self.assertEqual(len(tools), 1, f"expected exactly one tool_request, got {tools}")
        self.assertEqual(tools[0].tool.name, "fs_read")
        self.assertEqual(tools[0].tool.args, {"path": "notes.md"})
        self.assertTrue(any(e.type == "done" for e in events), "no done event")

    def test_client_is_closed_after_stream(self):
        # No connection leak: the provider must release the client when it finishes.
        from hearth.llm.llama_server import LlamaServerProvider
        holder, orig = self._strict_client()
        try:
            async def go():
                p = LlamaServerProvider("http://127.0.0.1:9")
                out = []
                async for ev in p.stream([{"role": "user", "content": "x"}], [], {}):
                    out.append(ev)
            asyncio.run(go())
        finally:
            import httpx
            httpx.AsyncClient = orig
        client = holder.get("client")
        self.assertIsNotNone(client, "provider never created a client")
        self.assertTrue(client.closed, "provider leaked an open httpx client")

    def test_internal_bug_is_not_reported_as_server_disconnect(self):
        # Hardening: if OUR parsing code raises, the message must say it's a Hearth bug,
        # not blame llama-server. A ReadError from the transport is still phrased as a
        # possible disconnect; anything else (our fault) must be attributed to us.
        from hearth.llm import llama_server

        async def boom_exchange(self, body, t0):
            raise ValueError("simulated provider bug")
            yield  # make it an async generator

        orig_exchange = llama_server.LlamaServerProvider._exchange
        llama_server.LlamaServerProvider._exchange = boom_exchange
        try:
            events = []

            async def go():
                p = llama_server.LlamaServerProvider("http://127.0.0.1:9")
                async for ev in p.stream([{"role": "user", "content": "x"}], [], {}):
                    events.append(ev)
            asyncio.run(go())
        finally:
            llama_server.LlamaServerProvider._exchange = orig_exchange

        errs = [e for e in events if e.type == "error"]
        self.assertEqual(len(errs), 1, f"expected one error event, got {errs}")
        text = errs[0].text
        self.assertIn("Hearth provider error", text,
                      f"internal bug mislabelled: {text!r}")
        self.assertNotIn("check llama-server health", text,
                         "our own bug must not tell the user to debug llama-server")


if __name__ == "__main__":
    print(f"[selftest] data dir = {_tmp}")
    unittest.main(verbosity=2)
