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
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
        ctx, capped = orch._workspace_context(uid)
        self.assertIn("a.txt", ctx)
        self.assertFalse(capped)

    def test_workspace_context_shows_sizes_and_uploads(self):
        # The model needs an ls -l equivalent: file SIZES in the prompt listing,
        # and uploads/ marked as user attachments so it knows what was attached.
        from hearth.db import init_db
        init_db()
        from hearth import workspaces as ws, orchestrator as orch
        uid = "ctxuser2"
        ws.write_text(uid, "a.txt", "12345")          # 5 bytes
        ws.write_text(uid, "uploads/photo.png", "x")  # simulated user upload
        ctx, capped = orch._workspace_context(uid)
        self.assertIn("a.txt (5 B)", ctx, f"size missing from listing: {ctx!r}")
        self.assertIn("uploads/", ctx)
        # The word "attachments" belongs to prompt.py's WORKSPACE_PREAMBLE (which
        # _workspace_context is appended to), while this listing says "the user attached"
        # — assert the meaning each place actually carries, not a guess about which.
        self.assertIn("attached", ctx.lower(), f"uploads not explained as user files: {ctx!r}")
        from hearth import prompt as prompt_mod
        self.assertIn("attachments", prompt_mod.WORKSPACE_PREAMBLE.lower())

    def test_workspace_listing_cap_is_settings_driven_and_announced(self):
        """AUDIT #1 / B3 — the prompt listing used to cut at a hard-coded 80 with no
        marker, while the preamble told the model it showed 'every file'."""
        from hearth.db import init_db
        init_db()
        from hearth import workspaces as ws, orchestrator as orch, prompt as prompt_mod
        uid = "ctxcap"
        for i in range(12):
            ws.write_text(uid, f"f{i}.txt", "x")
        s_small = {"agent.workspace_listing_entries": 5}
        ctx, capped = orch._workspace_context(uid, s_small)
        self.assertTrue(capped, "a listing that hit the cap must report it")
        self.assertIn("…and more", ctx)
        # A truncated listing must NEVER state a number of omitted files: a capped read
        # cannot know the total, and an earlier version of this fix invented one.
        self.assertNotIn("1 more (use fs_list", ctx)
        self.assertNotIn("every file", prompt_mod.build_workspace_preamble(s_small, True))
        self.assertIn("INCOMPLETE", prompt_mod.build_workspace_preamble(s_small, True))
        # Uncapped: no marker appears, and the wording never claims a limit was hit.
        s_big = {"agent.workspace_listing_entries": 500}
        ctx2, capped2 = orch._workspace_context(uid, s_big)
        self.assertFalse(capped2)
        self.assertNotIn("…and more", ctx2)
        pre_big = prompt_mod.build_workspace_preamble(s_big, False)
        self.assertIn("up to 500 entries", pre_big)
        self.assertNotIn("INCOMPLETE", pre_big)
        # No settings at all -> the original "every file" text, unchanged.
        self.assertIn("every file", prompt_mod.build_workspace_preamble())

    def test_workspace_listing_failure_says_failed_not_empty(self):
        """AUDIT #5 — an exception used to return '', which the model read as an empty
        workspace and reported to the user as 'your files are gone'."""
        from hearth.db import init_db
        init_db()
        from hearth import orchestrator as orch, workspaces as ws
        uid = "ctxboom"
        orig = ws.list_tree
        ws.list_tree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jail exploded"))
        try:
            ctx, capped = orch._workspace_context(uid, {})
        finally:
            ws.list_tree = orig
        self.assertIn("FAILED", ctx)
        self.assertIn("fs_list", ctx)
        self.assertNotIn("currently empty", ctx)

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
    """HTTP integration tests (need fastapi/httpx/sqlalchemy).

    Login used to be `if login != 200: skipTest("set BOOT_PW")`, which meant these tests
    silently skipped in most runs: main._bootstrap_admin() only creates an admin when the DB
    is empty, so once any user exists the password is unknown and every dependent test was a
    no-op that still reported green. _login_admin() guarantees a known-password admin instead
    (see hearth.main.ensure_bootstrap_admin). BOOT_PW still wins if set, so the documented
    manual invocation keeps working.
    """

    def _login_admin(self, c):
        """Sign a TestClient in as an admin with a known password. Returns CSRF header."""
        from hearth.main import ensure_bootstrap_admin
        pw = os.environ.get("BOOT_PW") or ensure_bootstrap_admin()
        r = c.post("/auth/login", data={"username": "admin", "password": pw})
        self.assertEqual(r.status_code, 200, f"admin login must work in tests: {r.status_code}")
        return {"X-CSRF-Token": c.cookies.get("hearth_csrf", "")}

    def _admin_uid(self):
        """The id of the admin account _login_admin() signs in as. Workspace/artifact
        helpers below take a raw user id, and routes derive it server-side — tests need
        the same value to check jail contents directly."""
        from hearth.db import session_scope
        from hearth.models import User
        with session_scope() as s:
            u = s.query(User).filter(User.username == "admin").first()
            return u.id if u else None

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
            # DEC-077: display_media plays same-origin audio/video, and some browsers
            # re-check a fullscreened element's loads against the top-level policy. The
            # directive must stay origin-limited — assert what it allows AND that it did
            # not become a wildcard while somebody was "fixing" a black video.
            self.assertIn("media-src 'self' blob:;", csp)
            self.assertNotRegex(csp, r"media-src[^;]*\*")
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
        with TestClient(create_app()) as c:
            csrf = self._login_admin(c)["X-CSRF-Token"]
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

    def test_artifact_delete_purges_file(self):
        # The Files panel's ⋮ → Delete. The endpoint used to drop only the DB row, which
        # left the bytes in the workspace with nothing left to list or fetch them; now
        # purge:true removes the file too, and the default (no body) keeps the old
        # row-only behaviour for any other caller. CSRF is required like every mutation.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        from hearth import artifacts, workspaces as ws
        with TestClient(create_app()) as c:
            csrf = self._login_admin(c)

            # CSRF still guards the mutation.
            nocfg = c.post("/api/artifact/nope/delete")
            self.assertEqual(nocfg.status_code, 403)

            def _upload(name, blob):
                up = c.post("/api/upload", data={"conversation_id": "delconv"},
                            files=[("files", (name, blob, "text/plain"))], headers=csrf)
                self.assertEqual(up.status_code, 200, up.text)
                return up.json()["artifacts"][0]

            # Row-only delete: entry gone, bytes kept.
            a1 = _upload("keepme.txt", b"keep")
            r = c.post(f"/api/artifact/{a1['id']}/delete", json={}, headers=csrf)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            self.assertFalse(r.json()["file_deleted"])
            self.assertIsNone(artifacts.get(self._admin_uid(), a1["id"]))
            self.assertEqual(ws.read_bytes(self._admin_uid(), a1["rel_path"]), b"keep")

            # purge:true: entry gone AND file gone.
            a2 = _upload("bye.txt", b"bye")
            r = c.post(f"/api/artifact/{a2['id']}/delete", json={"purge": True}, headers=csrf)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            self.assertTrue(r.json()["file_deleted"])
            self.assertIsNone(artifacts.get(self._admin_uid(), a2["id"]))
            with self.assertRaises((FileNotFoundError, ws.JailEscape)):
                ws.read_bytes(self._admin_uid(), a2["rel_path"])

            # Unknown id reports ok:false rather than raising.
            r = c.post("/api/artifact/does-not-exist/delete", json={"purge": True}, headers=csrf)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["ok"])

    def test_project_link_admin_flow(self):
        # DEC-049 admin path: link -> import -> status shows diff -> revert, with CSRF.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        proj = tempfile.mkdtemp(prefix="pl-http-")
        (Path(proj) / "a.py").write_text("one\n", encoding="utf-8")
        with TestClient(create_app()) as c:
            H = self._login_admin(c)

            # CSRF is required, like every other mutation.
            nocfg = c.post("/api/admin/project/link", json={"target": proj})
            self.assertEqual(nocfg.status_code, 403)

            bad = c.post("/api/admin/project/link", json={"target": "relative/nope"}, headers=H)
            self.assertEqual(bad.status_code, 400)

            ok = c.post("/api/admin/project/link", json={"target": proj}, headers=H)
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertTrue(ok.json()["linked"])

            imp = c.post("/api/admin/project/import", json={}, headers=H)
            self.assertEqual(imp.status_code, 200, imp.text)
            self.assertEqual(imp.json()["copied"], 1)

            st = c.get("/api/admin/project", headers=H).json()
            self.assertTrue(st["linked"])
            self.assertEqual(st["files"], 1)
            self.assertEqual(st["added"] + st["modified"] + st["conflicts"], 0)

            # Revert with a bogus id is refused rather than crashing.
            r = c.post("/api/admin/project/revert", json={"backup_id": "nope"}, headers=H)
            self.assertEqual(r.status_code, 400)

            unl = c.post("/api/admin/project/unlink", json={"delete_copy": True}, headers=H)
            self.assertEqual(unl.status_code, 200)
            self.assertFalse(c.get("/api/admin/project", headers=H).json()["linked"])

    def test_project_chat_side_routes_and_upload_prefix(self):
        """DEC-049 chat side: status/summary/apply on the user's own link + upload prefix."""
        from fastapi.testclient import TestClient
        from hearth.main import create_app, ensure_bootstrap_admin
        proj = tempfile.mkdtemp(prefix="pl-chat-")
        (Path(proj) / "a.py").write_text("one\n", encoding="utf-8")
        with TestClient(create_app()) as c:
            H = self._login_admin(c)
            # No link yet -> chip has nothing to show.
            self.assertFalse(c.get("/api/project/status").json()["linked"])
            self.assertFalse(c.get("/api/project/summary").json()["linked"])

            c.post("/api/admin/project/link", json={"target": proj}, headers=H)
            c.post("/api/admin/project/import", json={}, headers=H)
            st = c.get("/api/project/status").json()
            self.assertTrue(st["linked"])
            self.assertEqual(st["prefix"], "project")

            # Upload into the project copy (item 4).
            up = c.post("/api/upload", data={"conversation_id": "pc", "prefix": "project"},
                        files=[("files", ("added.txt", b"from browser\n", "text/plain"))],
                        headers=H)
            self.assertEqual(up.status_code, 200, up.text)
            self.assertEqual(up.json()["artifacts"][0]["rel_path"], "project/added.txt")

            # Summary reports it as pending (item 2).
            s = c.get("/api/project/summary").json()
            self.assertEqual(s["added"], 1)
            self.assertIn("added.txt", s["will_write"])
            self.assertTrue(s["apply_enabled"])

            # User-initiated apply, no model involved (item 3).
            r = c.post("/api/project/apply", headers=H)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertTrue((Path(proj) / "added.txt").is_file())

            # CSRF still required on the write paths.
            self.assertEqual(c.post("/api/project/apply").status_code, 403)
            self.assertEqual(c.post("/api/project/revert", json={"backup_id": "x"}).status_code, 403)

            # A bogus prefix is refused; it is never treated as a path.
            for bad in ("../../etc", "secret", "uploads/../x"):
                r2 = c.post("/api/upload", data={"conversation_id": "pc", "prefix": bad},
                            files=[("files", ("f.txt", b"x", "text/plain"))], headers=H)
                self.assertEqual(r2.status_code, 400, f"{bad} must be rejected")

    def test_project_prompt_context_only_when_linked(self):
        """Item 1: the model is told about project/ only while a link exists, and cheaply."""
        from hearth import orchestrator as orch, project_link as pl
        uid = "plctx"
        self.assertEqual(orch._project_context(uid), "")
        proj = tempfile.mkdtemp(prefix="pl-ctx-")
        (Path(proj) / "a.py").write_text("x\n", encoding="utf-8")
        link = pl.set_link(uid, proj)
        pl.import_project(uid, {}, link)
        ctx = orch._project_context(uid)
        self.assertIn("sync_preview", ctx)
        self.assertIn("sync_apply", ctx)
        self.assertIn("project/", ctx)
        # Cost guard: prompt building must never hash the tree.
        real = pl.diff_project
        try:
            pl.diff_project = lambda *a, **k: (_ for _ in ()).throw(AssertionError("diff in prompt"))
            orch._project_context(uid)
        finally:
            pl.diff_project = real
        pl.clear_link(uid, delete_copy=True)
        self.assertEqual(orch._project_context(uid), "")

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
        with TestClient(create_app()) as c:
            csrf = self._login_admin(c)["X-CSRF-Token"]
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


def _fake_caption_client(chunks, capture=None):
    """Shared httpx stand-in for Captioner._run_server streaming tests.

    _run_server does `with httpx.Client(...)` then `with c.stream("POST", ...)` and reads
    SSE via `iter_lines()`, so the fake implements the sync context-manager protocol on both
    the client and the response (the async fake above is for the chat provider and would fail
    here). Each entry in `chunks` becomes one `data: {json}` line; a literal "[DONE]" sentinel
    is appended, mirroring llama-server. `capture` (a dict) records the request body so a test
    can assert stream:true was actually sent. Kept here so all streaming tests share one
    interface and cannot drift.
    """
    import httpx

    def _aiter():
        for c in chunks:
            if c == "[DONE]":
                yield "data: [DONE]"
            else:
                yield "data: " + json.dumps({"choices": [{"delta": c}]})

    class FakeResponse:
        status_code = 200

        def iter_lines(self): return _aiter()
        def read(self): return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeClient:
        def stream(self, method, url, json=None):
            if capture is not None:
                capture["body"] = json
                capture["stream_flag"] = (json or {}).get("stream")
            return FakeResponse()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = httpx.Client
    httpx.Client = lambda *a, **k: FakeClient()
    return orig


class CaptionerStreamTests(unittest.TestCase):
    """Streaming the captioner's response + Stop reaching the audio model (DEC-057)."""

    def _settings(self, **over):
        from hearth import runtime_settings as rs
        base = {s.key: s.default for s in rs.ALL_SPECS}
        base.update({"captioner.enabled": True, "captioner.auto_route": True,
                     "captioner.chunk_seconds": 0})
        base.update(over)
        return base

    def _wav(self):
        """A real (tiny) wav on disk — _run_server reads the chunk bytes to base64 them, so a
        made-up path raises before any HTTP happens."""
        import tempfile as _tf
        fd, path = _tf.mkstemp(suffix=".wav")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"RIFF....WAVEfmt ")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def _external_server(self):
        """Point the captioner at an (unlaunched) external base_url for these tests.

        Without it, _run_server sees no owned process and returns None before any HTTP — the
        documented 'bring one up' path, which would launch a real server. An external
        HEARTH_CAPTIONER_BASE_URL makes _owned() irrelevant (the request goes straight out) and
        is never launched or killed by Hearth, so the fake httpx client is what answers.
        """
        prev = os.environ.get("HEARTH_CAPTIONER_BASE_URL")
        os.environ["HEARTH_CAPTIONER_BASE_URL"] = "http://127.0.0.1:9"

        def _restore():
            if prev is None:
                os.environ.pop("HEARTH_CAPTIONER_BASE_URL", None)
            else:
                os.environ["HEARTH_CAPTIONER_BASE_URL"] = prev
        self.addCleanup(_restore)

    def test_stream_setting_defaults_on(self):
        from hearth import runtime_settings as rs
        self.assertIs(rs.SPEC_BY_KEY["captioner.stream"].default, True)

    def test_run_server_streams_deltas_in_order(self):
        import httpx
        from hearth import captioner as cap
        self._external_server()
        c = cap.Captioner()
        got = []
        cap_lines = [{"content": "Two "}, {"reasoning_content": "[think] "},
                     {"content": "people talk."}, "[DONE]"]
        orig = _fake_caption_client(cap_lines)
        try:
            text = c._run_server(self._wav(), self._settings(),
                                 cap.resolve_paths(self._settings()), "",
                                 on_delta=got.append)
        finally:
            httpx.Client = orig
        # content AND reasoning_content are surfaced; the return value is their concatenation.
        self.assertEqual("".join(got), text)
        self.assertIn("[think] ", got)
        self.assertEqual(text, "Two [think] people talk.")

    def test_run_server_requests_streaming_when_enabled(self):
        import httpx
        from hearth import captioner as cap
        self._external_server()
        c = cap.Captioner()
        cap_ = {}
        orig = _fake_caption_client([{"content": "ok"}, "[DONE]"], capture=cap_)
        try:
            c._run_server(self._wav(), self._settings(),
                          cap.resolve_paths(self._settings()), "", on_delta=lambda t: None)
        finally:
            httpx.Client = orig
        self.assertIs(cap_.get("stream_flag"), True)

    def test_run_server_no_stream_when_disabled(self):
        # captioner.stream=false must NOT ask the server for a streamed response.
        import httpx
        from hearth import captioner as cap
        self._external_server()
        c = cap.Captioner()
        seen = {}

        class Resp:
            status_code = 200
            text = '{"choices":[{"message":{"content":"whole"}}]}'
            def json(self): return {"choices": [{"message": {"content": "whole"}}]}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Client:
            def post(self, url, json=None):
                seen["stream"] = (json or {}).get("stream")
                return Resp()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        orig = httpx.Client
        httpx.Client = lambda *a, **k: Client()
        try:
            text = c._run_server(self._wav(), self._settings(**{"captioner.stream": False}),
                                 cap.resolve_paths(self._settings()), "")
        finally:
            httpx.Client = orig
        self.assertIs(seen.get("stream"), False)
        self.assertEqual(text, "whole")

    def test_run_server_honours_cancel_event(self):
        import threading
        import httpx
        from hearth import captioner as cap
        self._external_server()
        c = cap.Captioner()
        # A cancel flag that flips on after the first chunk: the loop must raise rather than
        # finish listening. This is what makes Stop abandon a recording mid-caption.
        ev = threading.Event()

        def _aiter():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "start"}}]})
            ev.set()
            for _ in range(50):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": "more"}}]})

        class Resp:
            status_code = 200
            def iter_lines(self): return _aiter()
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Client:
            def stream(self, method, url, json=None): return Resp()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        orig = httpx.Client
        httpx.Client = lambda *a, **k: Client()
        try:
            with self.assertRaises(cap.CaptionError) as cm:
                c._run_server(self._wav(), self._settings(),
                              cap.resolve_paths(self._settings()), "", cancel=ev)
        finally:
            httpx.Client = orig
        self.assertIn("stopped", str(cm.exception).lower())

    def test_prepare_captions_forwards_stream_chunks_and_cancel(self):
        import asyncio
        import threading
        from hearth import orchestrator as orch, captioner as cap
        att = [{"name": "voice.mp3", "mime": "audio/mpeg", "rel_path": "uploads/vo-stream.mp3"}]
        seen_events = []

        async def fake_describe(user_id, rel, extra_prompt="", settings=None,
                                on_delta=None, cancel=None):
            if on_delta:
                on_delta("hello ")
                on_delta("world")
            return {"caption": "hello world", "chunks": 1, "duration_s": 3.0,
                    "backend": "server", "notes": []}

        orig = cap.describe_file
        cap.describe_file = fake_describe
        try:
            out = asyncio.run(orch.prepare_captions("u-stream", "conv-s", att, self._settings(),
                                                    emit=seen_events.append))
        finally:
            cap.describe_file = orig
        kinds = [e.get("kind") for e in seen_events]
        self.assertIn("caption_chunk", kinds)
        chunk_text = "".join(e["text"] for e in seen_events if e.get("kind") == "caption_chunk")
        self.assertEqual(chunk_text, "hello world")
        self.assertIn("uploads/vo-stream.mp3", out)

    def test_prepare_captions_skips_when_already_cancelled(self):
        import asyncio
        from hearth import orchestrator as orch, captioner as cap
        att = [{"name": "a.mp3", "mime": "audio/mpeg", "rel_path": "uploads/a-cancel.mp3"}]

        class H:
            cancelled = True

        called = []
        orig = cap.describe_file
        cap.describe_file = lambda *a, **k: called.append(1)
        try:
            out = asyncio.run(orch.prepare_captions("u-c", "conv-c", att, self._settings(),
                                                    cancel=H()))
        finally:
            cap.describe_file = orig
        self.assertEqual(called, [], "must not start listening after a Stop")
        self.assertEqual(out, {})

    def test_tool_context_cancel_adapter(self):
        from hearth.tools import ToolContext
        ctx = ToolContext("u", {}, {})
        # No handle attached -> nothing to poll (a tool must not crash checking it).
        self.assertIsNone(ctx.cancel_event())

        class H:
            cancelled = False
        ctx.attach_cancel(H())
        ce = ctx.cancel_event()
        self.assertFalse(ce.is_set())
        H.cancelled = True
        self.assertTrue(ce.is_set())

    def test_cancel_handle_is_thread_interface_compatible(self):
        # The regression: prepare_captions passes the RAW asyncio CancelHandle to
        # captioner.describe_file for a directly-attached recording, while the describe_audio
        # tool passes a threading.Event-shaped adapter. caption_sync polled only `.is_set()`,
        # so every direct mp3 attachment died with AttributeError before describing anything.
        import asyncio
        from hearth.cancel import CancelHandle
        h = CancelHandle(user_id="u")
        self.assertFalse(h.is_set())
        self.assertFalse(h.cancelled)
        h.cancel()
        self.assertTrue(h.is_set())
        self.assertTrue(h.cancelled)

    def test_captioner_accepts_both_cancel_shapes(self):
        import threading
        from hearth import captioner as cap
        from hearth.cancel import CancelHandle
        # Unset / set for each shape, plus None and an unknown object: no AttributeError ever.
        # NOTE: build the SET instances inside each case list — a CancelHandle created up
        # front and cancelled before the "off" assertions run would report itself set there.
        cases_off = [None, CancelHandle(user_id="u"), threading.Event(), object()]
        h = CancelHandle(user_id="u")
        ev = threading.Event()
        h.cancel()
        ev.set()
        cases_on = [h, ev]

        class OnlyCancelled:
            cancelled = True

        for c in cases_off:
            self.assertFalse(cap._cancel_requested(c), f"{c!r} read as cancelled")
        for c in cases_on + [OnlyCancelled()]:
            self.assertTrue(cap._cancel_requested(c), f"{c!r} not read as cancelled")

    def test_caption_sync_with_raw_cancel_handle(self):
        # End-to-end through caption_sync (the path an attachment takes), with the real
        # CancelHandle rather than the tool's adapter.
        import json
        import tempfile as _tf
        import httpx
        from hearth import captioner as cap
        from hearth.cancel import CancelHandle
        self._external_server()
        orig_chunks = cap.prepare_chunks
        orig_info = cap.audio_info
        cap.prepare_chunks = (lambda path, tmp, resample=True, chunk_seconds=30,
                              window=None, log=lambda m: None: [(path, 0.0)])
        cap.audio_info = lambda p: {"duration_s": 1.0, "audio_streams": 1}
        self.addCleanup(setattr, cap, "prepare_chunks", orig_chunks)
        self.addCleanup(setattr, cap, "audio_info", orig_info)

        c = cap.Captioner()
        # A real model file on disk: caption_sync runs preflight first, and preflight checks
        # that the configured model path actually exists.
        with _tf.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            with open(model, "wb") as fh:
                fh.write(b"x")
            settings = self._settings(**{"captioner.model_path": model,
                                         # preflight also wants a launchable server binary;
                                         # no process is started here (external base_url).
                                         "captioner.server_bin": sys.executable})

            # Not cancelled -> a normal caption.
            h = CancelHandle(user_id="u")
            orig = _fake_caption_client([{"content": "a voice"}, "[DONE]"])
            try:
                res = c.caption_sync(self._wav(), settings, cancel=h)
            finally:
                httpx.Client = orig
            self.assertEqual(res["caption"], "a voice")

            # Cancelled -> CaptionError saying it stopped, not AttributeError.
            h2 = CancelHandle(user_id="u")
            h2.cancel()
            orig = _fake_caption_client([{"content": "a voice"}, "[DONE]"])
            try:
                with self.assertRaises(cap.CaptionError) as cm:
                    c.caption_sync(self._wav(), settings, cancel=h2)
            finally:
                httpx.Client = orig
            self.assertIn("stopped", str(cm.exception).lower())


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

    def test_context_usage_never_launches_llama_server(self):
        # The DEC-048 invariant has two halves: never launch on a passive page view, AND
        # still take the live window from /props when a server IS up. Both were broken for
        # /api/chat/context-usage — it called get_provider(), which reaches ensure_server(),
        # so opening a conversation (chat.js loadContextUsage) restarted llama-server right
        # after an admin had stopped it. The DEC-048 guard above only covers probe()/status()
        # and the payload tests only cover the pure maths, so nothing exercised this route.
        # NB: must pass a REAL conversation_id — the route returns before reaching the
        # manager when it is empty, and an empty-id test passes against the buggy code too.
        import asyncio
        from unittest import mock
        from hearth.db import init_db, session_scope
        from hearth.models import Conversation, User
        from hearth.manager import get_manager
        from hearth.routes_chat import chat_context_usage

        init_db()
        uid, cid = "ctx-guard-user", "ctx-guard-conv"
        with session_scope() as s:
            if s.get(User, uid) is None:
                s.add(User(id=uid, username=f"u-{uid}", pw_hash="x"))
            if s.get(Conversation, cid) is None:
                s.add(Conversation(id=cid, user_id=uid, title="ctx guard"))

        class _U:
            id = uid

        m = get_manager()
        launches = []

        async def _boom(*a, **k):
            launches.append(("ensure_server", a, k))
            return ""

        async def _never_provider(*a, **k):
            launches.append(("get_provider", a, k))
            raise AssertionError("context-usage must not resolve a provider")

        async def _unreachable(*a, **k):
            raise RuntimeError("no server here")

        # Phase 1 — nothing answering. ensure_server is trapped; get_provider and probe()'s
        # own probes are made to raise, so reaching ANY launch-capable path fails loudly
        # instead of silently probing a llama-server that happens to run on this machine.
        with mock.patch.object(m, "ensure_server", _boom), \
             mock.patch.object(m, "get_provider", _never_provider), \
             mock.patch.object(m, "_reachable", _unreachable), \
             mock.patch.object(m, "_is_llama_server", _unreachable):
            d = json.loads(asyncio.run(chat_context_usage(None, cid, user=_U())).body)
        self.assertEqual(
            launches, [],
            "context-usage must not launch llama-server — it runs on every conversation "
            "open (DEC-048)")
        # Nothing answering => the configured window is still reported, so the bar keeps
        # working instead of going blank.
        self.assertGreater(d["n_ctx"], 0, "must fall back to the configured model.n_ctx")

        # Phase 2 — a server IS answering: its live /props window must win over the
        # setting, which is the whole reason this call exists. Patched on probe() itself so
        # it also pins the route to the launch-free path (ensure_server stays trapped).
        with mock.patch.object(m, "ensure_server", _boom), \
             mock.patch.object(m, "probe",
                               lambda: asyncio.sleep(0, result={"server_reachable": True,
                                                                "base_url": "http://x",
                                                                "n_ctx": 4242})):
            d = json.loads(asyncio.run(chat_context_usage(None, cid, user=_U())).body)
        self.assertEqual(launches, [], "context-usage must not launch llama-server")
        self.assertEqual(d["n_ctx"], 4242, "live /props n_ctx must win over the setting")

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

    def test_stream_checked_releases_provider_generator_on_disconnect(self):
        # Regression: cancelling the *route task* (what Starlette does when a client
        # disconnects) unwinds _stream_checked through its `finally` while the provider
        # generator is still suspended mid-read. CPython refuses to aclose() an async
        # generator that is running, so the old bare `await agen.aclose()` raised
        # "anext(): asynchronous generator is already running" out of the SSE route and
        # leaked the stream. _stream_checked must swallow it and unwind quietly.
        import asyncio
        from hearth import orchestrator as orch
        from hearth import cancel as cancel_mod

        class Blocks:
            name = "stub-blocker"          # not llama-server -> no log poller
            async def stream(self, messages, tools, params):
                if False:
                    yield
                await asyncio.sleep(30)    # stands in for a blocked httpx body read
                yield StreamEvent("token", text="never")

        async def go():
            conv = "disconnect-conv"
            self._make_conv(conv)
            h = cancel_mod.begin(conv, "u")
            try:
                agen = orch._stream_checked(Blocks(), [], [], {}, h)
                task = asyncio.ensure_future(agen.__aiter__().__anext__())
                await asyncio.sleep(0.05)  # let it reach the blocked read
                task.cancel()              # client disconnects mid-stream
                try:
                    await task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                # The wrapper generator is torn down here; before the fix its finally
                # raised RuntimeError instead of unwinding.
                await asyncio.wait_for(agen.aclose(), timeout=5)
            finally:
                cancel_mod.end(conv, h)

        try:
            asyncio.run(go())
        except RuntimeError as e:
            self.fail(f"aclose() must not surface 'already running': {e}")
        except (asyncio.CancelledError, StopAsyncIteration):
            pass    # both are legitimate teardown outcomes


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


class ProjectLinkTests(unittest.TestCase):
    """Linked project folder (DEC-049 / CONTRACT v1.3 REQ-041..048).

    Uses real temp directories as the "real project" so hashing, conflict detection and
    backups are exercised against actual files rather than mocks.
    """

    def _proj(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="pl-proj-")
        for rel, content in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return d

    def _linked(self, uid: str, files: dict):
        from hearth import project_link as pl
        proj = self._proj(files)
        link = pl.set_link(uid, proj)
        pl.import_project(uid, {}, link)
        return proj, link

    def test_link_requires_absolute_existing_dir(self):
        # REQ-041: validation of the target, and no link created on failure.
        from hearth import project_link as pl
        for bad in ["", "relative/path", "/definitely/not/here"]:
            with self.assertRaises(pl.LinkError):
                pl.set_link("plink1", bad)
        self.assertIsNone(pl.get_link("plink1"))

    def test_cannot_link_inside_own_workspace(self):
        from hearth import project_link as pl, workspaces as ws
        inside = Path(ws.root_for("plink2")) / "sub"
        inside.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(pl.LinkError):
            pl.set_link("plink2", str(inside))

    def test_import_copies_and_records_baseline(self):
        # REQ-042: copy + manifest; noise dirs skipped.
        from hearth import project_link as pl, workspaces as ws
        proj, link = self._linked("plimp", {"README.md": "hello\n", "src/a.py": "x = 1\n",
                                            "node_modules/junk.js": "nope\n"})
        mf = pl.load_manifest("plimp", link)
        self.assertEqual(sorted(mf["files"]), ["README.md", "src/a.py"])
        self.assertEqual(ws.read_text("plimp", "project/src/a.py"), "x = 1\n")
        self.assertFalse((Path(ws.root_for("plimp")) / "project" / "node_modules").exists())

    def test_clean_diff_is_empty(self):
        from hearth import project_link as pl
        proj, link = self._linked("plclean", {"a.py": "one\n"})
        cs = pl.diff_project("plclean", link)
        self.assertEqual(cs.applyable, [])
        self.assertEqual(cs.conflicts, [])
        self.assertEqual(cs.unchanged, 1)

    def test_apply_writes_modified_and_new_files(self):
        # REQ-044/045 happy path + backup id reported.
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("plapply", {"a.py": "one\n"})
        ws.edit_text("plapply", "project/a.py", "one", "TWO")
        ws.write_text("plapply", "project/new.md", "created\n")
        cs = pl.diff_project("plapply", link)
        self.assertEqual(cs.modified, ["a.py"])
        self.assertEqual(cs.added, ["new.md"])

        res = pa.apply_changes("plapply", {}, link)
        self.assertTrue(res.ok, res.note)
        self.assertEqual((Path(proj) / "a.py").read_text(), "TWO\n")
        self.assertEqual((Path(proj) / "new.md").read_text(), "created\n")
        self.assertTrue(res.backup_id)
        # Baseline refreshed: a second preview must be clean.
        self.assertEqual(pl.diff_project("plapply", link).applyable, [])

    def test_revert_restores_previous_state(self):
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("plrev", {"a.py": "one\n"})
        ws.edit_text("plrev", "project/a.py", "one", "TWO")
        res = pa.apply_changes("plrev", {}, link)
        self.assertEqual((Path(proj) / "a.py").read_text(), "TWO\n")
        rv = pa.revert("plrev", res.backup_id, link)
        self.assertTrue(rv.ok, rv.note)
        self.assertEqual((Path(proj) / "a.py").read_text(), "one\n")

    def test_conflict_refuses_and_preserves_user_edit(self):
        # REQ-044: a target edited since import is refused, never merged. All-or-nothing.
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("plconf", {"a.py": "one\n", "b.py": "bee\n"})
        ws.edit_text("plconf", "project/a.py", "one", "TWO")
        ws.edit_text("plconf", "project/b.py", "bee", "BEE")
        (Path(proj) / "a.py").write_text("user's own edit\n")   # concurrent user edit

        cs = pl.diff_project("plconf", link)
        self.assertEqual(len(cs.conflicts), 1, cs.conflicts)
        res = pa.apply_changes("plconf", {}, link)
        self.assertFalse(res.ok)
        self.assertEqual(res.written, [], "a conflict must block the whole apply")
        self.assertEqual((Path(proj) / "a.py").read_text(), "user's own edit\n")
        self.assertEqual((Path(proj) / "b.py").read_text(), "bee\n")

    def test_new_file_colliding_with_target_is_a_conflict(self):
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("plnewc", {"a.py": "one\n"})
        ws.write_text("plnewc", "project/both.txt", "agent\n")
        (Path(proj) / "both.txt").write_text("user\n")
        cs = pl.diff_project("plnewc", link)
        self.assertTrue(any("both.txt" in c for c in cs.conflicts), cs.conflicts)
        res = pa.apply_changes("plnewc", {}, link)
        self.assertFalse(res.ok)
        self.assertEqual((Path(proj) / "both.txt").read_text(), "user\n")

    def test_import_does_not_follow_symlinks(self):
        from hearth import project_link as pl, workspaces as ws
        proj = self._proj({"a.py": "x\n"})
        try:
            (Path(proj) / "sneaky").symlink_to("/etc/passwd")
        except OSError:
            self.skipTest("symlinks unsupported here")
        link = pl.set_link("plsym", proj)
        pl.import_project("plsym", {}, link)
        self.assertEqual(sorted(pl.load_manifest("plsym", link)["files"]), ["a.py"])
        self.assertFalse((Path(ws.root_for("plsym")) / "project" / "sneaky").exists())

    def test_deletions_are_reported_but_never_applied(self):
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("pldel", {"a.py": "one\n", "gone.py": "bye\n"})
        ws.delete_path("pldel", "project/gone.py")
        cs = pl.diff_project("pldel", link)
        self.assertEqual(cs.deleted, ["gone.py"])
        pa.apply_changes("pldel", {}, link)
        self.assertTrue((Path(proj) / "gone.py").exists(), "the tool must not delete real files")

    def test_stub_backend_refuses_without_writing(self):
        # REQ-046: unsupported backend -> refusal, no disk change.
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        proj, link = self._linked("plstub", {"a.py": "one\n"})
        ws.edit_text("plstub", "project/a.py", "one", "TWO")
        res = pa.apply_changes("plstub", {"project.apply_backend": "stub"}, link)
        self.assertFalse(res.ok)
        self.assertEqual(res.backend, "stub")
        self.assertIn("not supported", res.note)
        self.assertEqual((Path(proj) / "a.py").read_text(), "one\n")

    def test_encryption_at_rest_blocks_apply(self):
        # REQ-047: never write encrypted-at-rest workspace bytes out to a real project.
        from hearth import project_link as pl, project_apply as pa, workspaces as ws
        import hearth.crypto as crypto
        proj, link = self._linked("plenc", {"a.py": "one\n"})
        ws.edit_text("plenc", "project/a.py", "one", "TWO")
        real = crypto.encryption_enabled
        try:
            crypto.encryption_enabled = lambda: True
            res = pa.apply_changes("plenc", {}, link)
        finally:
            crypto.encryption_enabled = real
        self.assertFalse(res.ok)
        self.assertIn("encryption", res.note)
        self.assertEqual((Path(proj) / "a.py").read_text(), "one\n")

    def test_apply_is_gated_and_preview_is_not(self):
        # The write-back tool must always interrupt for approval.
        from hearth.tools import TOOLS_BY_NAME, approval_mode
        pv, ap = TOOLS_BY_NAME["sync_preview"], TOOLS_BY_NAME["sync_apply"]
        self.assertTrue(ap.gated)
        self.assertEqual(approval_mode(ap, {}), "ask")
        self.assertEqual(approval_mode(pv, {}), "auto")

    def test_link_settings_exist_with_safe_defaults(self):
        from hearth import runtime_settings as rs
        specs = rs.SPEC_BY_KEY
        self.assertTrue(specs["tools.sync_apply"].default)
        self.assertEqual(specs["tools.sync_apply.approval"].default, "ask")
        self.assertEqual(specs["project.apply_backend"].choices, ["auto", "local", "stub"])

    def test_unlink_disables_and_optionally_removes_copy(self):
        from hearth import project_link as pl, workspaces as ws
        proj, link = self._linked("plunl", {"a.py": "one\n"})
        self.assertTrue(pl.clear_link("plunl"))
        self.assertIsNone(pl.get_link("plunl"))
        self.assertTrue((Path(ws.root_for("plunl")) / "project" / "a.py").exists())
        pl.set_link("plunl", proj)
        pl.import_project("plunl", {}, pl.get_link("plunl"))
        pl.clear_link("plunl", delete_copy=True)
        self.assertFalse((Path(ws.root_for("plunl")) / "project" / "a.py").exists())


class MediaToolTests(unittest.TestCase):
    """PDF/video tools + how their images reach the model.

    The rendering backends are external (pymupdf / poppler / ffmpeg), so each check
    skips itself when the machine lacks one — a missing dependency must never look
    like a broken tool. The parts that ARE ours and always testable: the images field
    on ToolResult, its persistence on the tool row, and the multimodal replay shape.
    """

    def _conv(self, uid: str, cid: str):
        from hearth.db import init_db, session_scope
        from hearth.models import Conversation, User
        init_db()
        with session_scope() as s:
            if s.get(User, uid) is None:
                s.add(User(id=uid, username=f"u-{uid}", pw_hash="x"))
            if s.get(Conversation, cid) is None:
                s.add(Conversation(id=cid, user_id=uid, title="media"))

    def test_toolresult_images_field_is_optional(self):
        from hearth.tools.base import ToolResult
        self.assertIsNone(ToolResult(True, "text").images)
        r = ToolResult(True, "t", images=[{"path": "a.png", "mime": "image/png"}])
        self.assertEqual(r.images[0]["path"], "a.png")

    def test_media_tools_registered_with_settings_and_specs(self):
        from hearth.tools import TOOLS_BY_NAME, tool_specs
        names = ["pdf_info", "pdf_read", "video_info", "view_video", "context_usage"]
        for n in names:
            self.assertIn(n, TOOLS_BY_NAME, f"{n} not registered")
            self.assertEqual(TOOLS_BY_NAME[n].enable_key, f"tools.{n}")
        specs = {s["function"]["name"]: s for s in tool_specs({})}
        for n in names:
            self.assertIn(n, specs, f"{n} missing from tool specs")
            self.assertTrue(specs[n]["function"]["description"].strip())
        # Admin toggles must exist so an operator can turn each one off.
        from hearth import runtime_settings as rs
        for n in names:
            self.assertIn(f"tools.{n}", rs.SPEC_BY_KEY)
            self.assertTrue(rs.SPEC_BY_KEY[f"tools.{n}"].default)
        self.assertEqual(rs.SPEC_BY_KEY["media.max_image_edge"].default, 1280)

    def test_optimise_png_keeps_colour_and_caps_the_long_edge(self):
        """Colour is preserved; only the long edge is capped (aspect kept, never upscaled).

        Grayscaling used to be the default and was removed: it saved bytes while costing
        nothing back — vision tokens depend on pixel dimensions, identical grey or colour.
        Skips itself when Pillow is absent, like the other media checks.
        """
        import io
        from hearth.tools import media_render as mr
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001 — Pillow is optional by design
            self.skipTest("Pillow not installed")
        buf = io.BytesIO()
        # Deliberately a strongly COLOURED image: a red/green split. If anything converts
        # to greyscale, R and G collapse to different luminances only after the fact, so
        # assert mode "RGB" is what actually catches a regression.
        im = Image.new("RGB", (1920, 1080))
        for x in range(1920):
            for y in (0, 1):
                im.putpixel((x, y), (220, 30, 30) if x % 2 else (30, 200, 30))
        im.save(buf, format="PNG")
        out = mr.optimise_png(buf.getvalue(), max_edge=1280)
        got = Image.open(io.BytesIO(out))
        self.assertEqual(got.mode, "RGB", "colour must survive optimise_png() by default")
        self.assertEqual(max(got.size), 1280)
        self.assertEqual(got.size, (1280, 720), "aspect ratio must be preserved")
        # An explicit grayscale=True still works — the parameter is kept, just not default.
        grey = Image.open(io.BytesIO(mr.optimise_png(buf.getvalue(), max_edge=1280,
                                                    grayscale=True)))
        self.assertEqual(grey.mode, "L")
        # Never upscaled: an image already under the cap keeps its dimensions.
        small = io.BytesIO()
        Image.new("RGB", (640, 480), (10, 200, 90)).save(small, format="PNG")
        kept = Image.open(io.BytesIO(mr.optimise_png(small.getvalue(), max_edge=1280)))
        self.assertEqual(kept.size, (640, 480))

    def test_images_persist_on_tool_row_and_replay_as_multimodal(self):
        # The core of DEC-052: images ride on the tool row's meta as PATHS (never
        # base64 in the DB), and build_messages turns them into a user-role multimodal
        # message — the one shape proven to reach an mmproj projector.
        import json
        from hearth import orchestrator as orch, workspaces as ws
        uid, cid = "media-replay", "media-replay-conv"
        self._conv(uid, cid)
        png = ws.write_bytes(uid, "media/x_p1.png", b"\x89PNG\r\n\x1a\n" + b"0" * 200)

        class Res:
            meta = {"pages": [1]}
            images = [{"path": png, "mime": "image/png"}]
            content = "Rendered 1 page as an image."

        meta = orch._tool_row_meta("pdf_read", Res(), "call_9")
        self.assertEqual(meta["images"], [{"path": png, "mime": "image/png"}])
        self.assertNotIn("base64", json.dumps(meta), "DB meta must hold paths, not pixels")

        orch.append_message(cid, "assistant", "", meta=json.dumps({"tool_calls": [
            {"id": "call_9", "type": "function",
             "function": {"name": "pdf_read", "arguments": "{}"}}]}))
        orch.append_message(cid, "tool", Res.content, meta=json.dumps(meta))
        msgs = orch.build_messages(cid, uid)
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles[-1], "user", "images must follow the tool message")
        parts = msgs[-1]["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual([p["type"] for p in parts][0], "text")
        self.assertEqual([p["type"] for p in parts][1], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        # The tool message itself stays plain text (option B: no multimodal tool role).
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        self.assertIsInstance(tool_msg["content"], str)

    def test_image_parts_survive_missing_files_and_no_conversation(self):
        from hearth import orchestrator as orch
        self.assertEqual(orch._image_parts("nobody", [{"path": "nope.png"}]), [])
        self.assertEqual(orch._image_parts("u", []), [])
        self.assertEqual(orch._image_parts("u", [{}]), [])


class DisplayMediaToolTests(unittest.TestCase):
    """display_media: showing a workspace file to the USER, not to the model.

    The interesting risks are all in which bytes go where: a video must never be
    base64'd into the model's prompt as if it were a picture, and the UI list must
    survive persistence so a refresh does not silently un-display what was shown.
    """

    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    def _conv(self, uid: str, cid: str):
        from hearth.db import init_db, session_scope
        from hearth.models import Conversation, User
        init_db()
        with session_scope() as s:
            if s.get(User, uid) is None:
                s.add(User(id=uid, username=f"u-{uid}", pw_hash="x"))
            if s.get(Conversation, cid) is None:
                s.add(Conversation(id=cid, user_id=uid, title="display"))

    def _tool(self):
        from hearth.tools import TOOLS_BY_NAME
        return TOOLS_BY_NAME["display_media"]

    def test_registered_enabled_and_not_gated(self):
        from hearth.tools import tool_specs, approval_mode
        from hearth import runtime_settings as rs
        t = self._tool()
        self.assertEqual(t.enable_key, "tools.display_media")
        self.assertFalse(t.gated, "showing a file the user can already download is not "
                                  "an approval-worthy act")
        self.assertEqual(approval_mode(t, {}), "auto")
        self.assertIn("tools.display_media", rs.SPEC_BY_KEY)
        self.assertTrue(rs.SPEC_BY_KEY["tools.display_media"].default)
        self.assertIn("display_media", {s["function"]["name"] for s in tool_specs({})})

    def test_kind_detection(self):
        from hearth.tools.display_media import media_kind
        self.assertEqual(media_kind("a/b/photo.PNG"), "image")
        self.assertEqual(media_kind("clip.mp4"), "video")
        self.assertEqual(media_kind("voice.wav"), "audio")
        # Not presentational: no element would render these, so the tool refuses instead.
        for p in ("doc.pdf", "notes.md", "archive.zip", "no_extension"):
            self.assertEqual(media_kind(p), "", f"{p} must not be treated as displayable")

    def test_shows_media_and_refuses_the_rest_by_name(self):
        from hearth import workspaces as ws
        from hearth.tools import ToolContext
        uid, cid = "dm-show", "dm-show-conv"
        self._conv(uid, cid)
        ws.write_bytes(uid, "shot.png", self.PNG)
        ws.write_bytes(uid, "clip.mp4", b"\x00" * 512)
        ws.write_bytes(uid, "notes.md", b"hello")
        res = asyncio.run(self._tool().run(
            {"paths": ["shot.png", "clip.mp4", "notes.md", "gone.png", "../../etc/passwd"],
             "caption": "look"}, ToolContext(uid, {}, conversation_id=cid)))
        self.assertTrue(res.ok)
        kinds = {m["path"]: m["kind"] for m in res.meta["media"]}
        self.assertEqual(kinds, {"shot.png": "image", "clip.mp4": "video"})
        # Every refusal is named rather than silently dropped — a model that cannot tell
        # which files landed will tell the user it showed them.
        self.assertIn("notes.md", res.content)
        self.assertIn("gone.png", res.content)
        self.assertIn("../../etc/passwd", res.content)
        self.assertIn("look", res.content)
        for m in res.meta["media"]:
            self.assertTrue(m["url"].startswith("/api/artifact/"), "must use the owned route")
            self.assertTrue(m["artifact_id"])

    def test_nothing_displayable_is_not_a_silent_success(self):
        from hearth import workspaces as ws
        from hearth.tools import ToolContext
        uid, cid = "dm-none", "dm-none-conv"
        self._conv(uid, cid)
        ws.write_bytes(uid, "notes.md", b"x")
        res = asyncio.run(self._tool().run({"paths": ["notes.md"]},
                                           ToolContext(uid, {}, conversation_id=cid)))
        self.assertFalse(res.ok)
        self.assertIn("upload", res.content, "should point at the tool that does work here")
        empty = asyncio.run(self._tool().run({"paths": []},
                                             ToolContext(uid, {}, conversation_id=cid)))
        self.assertFalse(empty.ok)

    def test_video_never_enters_the_model_image_channel(self):
        """The core safety property: `images` is base64'd as image_url for every later turn.

        A clip listed there would cost a huge prompt and be handed to an IMAGE projector,
        so only genuine images may ride that channel; audio/video go to the UI only.
        """
        from hearth import workspaces as ws, orchestrator as orch
        from hearth.tools import ToolContext
        uid, cid = "dm-noleak", "dm-noleak-conv"
        self._conv(uid, cid)
        ws.write_bytes(uid, "shot.png", self.PNG)
        ws.write_bytes(uid, "clip.mp4", b"\x00" * 512)
        res = asyncio.run(self._tool().run(
            {"paths": ["shot.png", "clip.mp4"]}, ToolContext(uid, {}, conversation_id=cid)))
        # Nothing goes into the model's image channel directly: build_messages derives what
        # the model may see from meta.display, and filling `images` too made replay encode
        # each displayed picture twice.
        self.assertIsNone(res.images)
        row = orch._tool_row_meta("display_media", res, "c1")
        self.assertNotIn("images", row)
        self.assertEqual({d["path"] for d in row["display"]}, {"shot.png", "clip.mp4"})
        # No caption was passed, so no caption key — the UI must not see an empty label.
        self.assertNotIn("display_caption", row)

    def test_display_persists_and_replays_without_leaking_bytes(self):
        from hearth import workspaces as ws, orchestrator as orch
        from hearth.tools import ToolContext
        uid, cid = "dm-replay", "dm-replay-conv"
        self._conv(uid, cid)
        ws.write_bytes(uid, "shot.png", self.PNG)
        ws.write_bytes(uid, "clip.mp4", b"\x00" * 512)
        res = asyncio.run(self._tool().run(
            {"paths": ["shot.png", "clip.mp4"], "caption": "the set"},
            ToolContext(uid, {}, conversation_id=cid)))
        row = orch._tool_row_meta("display_media", res, "c9")
        self.assertEqual(row["display_caption"], "the set")
        orch.append_message(cid, "assistant", "", meta=json.dumps({"tool_calls": [
            {"id": "c9", "type": "function",
             "function": {"name": "display_media", "arguments": "{}"}}]}))
        orch.append_message(cid, "tool", res.content, meta=json.dumps(row))
        msgs = orch.build_messages(cid, uid)
        blob = json.dumps(msgs)
        # Paths travel; pixels never do (until a message is actually encoded for the model).
        self.assertNotIn("base64\",", blob.replace("image/png;base64", ""))
        texts = [m["content"] for m in msgs if m["role"] == "user" and isinstance(m["content"], str)]
        self.assertTrue(any("clip.mp4" in t and "cannot see or hear" in t for t in texts),
                        "the model must be told the clip is on screen but not perceivable")
        multimodal = [m for m in msgs if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertTrue(multimodal, "the displayed image should reach the model")
        urls = [p["image_url"]["url"] for m in multimodal for p in m["content"]
                if p.get("type") == "image_url"]
        self.assertEqual(len(urls), 1, "exactly one image part — the PNG, never the MP4")
        self.assertTrue(all(u.startswith("data:image/png;base64,") for u in urls),
                        "no non-image mime may be encoded as an image_url part")

    def test_video_only_display_still_reaches_the_model_as_text(self):
        """A display call with no images must not vanish from the model's view."""
        from hearth import workspaces as ws, orchestrator as orch
        uid, cid = "dm-vidonly", "dm-vidonly-conv"
        self._conv(uid, cid)
        row = {"tool": "display_media", "tool_call_id": "cv1",
               "display": [{"path": "clip.mp4", "kind": "video", "url": "/api/artifact/x",
                            "artifact_id": "x", "name": "clip.mp4"}]}
        ws.write_bytes(uid, "clip.mp4", b"\x00" * 128)
        orch.append_message(cid, "assistant", "", meta=json.dumps({"tool_calls": [
            {"id": "cv1", "type": "function",
             "function": {"name": "display_media", "arguments": "{}"}}]}))
        orch.append_message(cid, "tool", "Showing 1 file(s)", meta=json.dumps(row))
        msgs = orch.build_messages(cid, uid)
        texts = [m["content"] for m in msgs if m["role"] == "user" and isinstance(m["content"], str)]
        self.assertTrue(any("clip.mp4" in t for t in texts))

    def test_display_replay_ignores_files_that_no_longer_exist(self):
        from hearth import orchestrator as orch, workspaces as ws
        uid = "dm-gone"
        self._conv(uid, "dm-gone-conv")
        ws.write_bytes(uid, "here.png", self.PNG)
        ws.write_bytes(uid, "song.mp3", self.PNG)   # exists, but is not an image
        imgs, blind = orch._display_image_items(uid, [
            {"path": "here.png"},                        # exists -> image
            {"path": "deleted.png"},                     # never existed -> skipped entirely
            {"path": "../../etc/passwd"},                # escape -> skipped, never read
            {"path": "also-gone.mp3", "name": "also-gone.mp3"},   # missing audio -> skipped
            {"path": "song.mp3", "name": "song.mp3"}])   # existing audio -> named as unseen
        self.assertEqual([i["path"] for i in imgs], ["here.png"])
        self.assertEqual(blind, ["song.mp3 (audio)"],
                         "only files that actually exist may be reported as on-screen")

    def test_display_replay_handles_dot_prefixed_paths(self):
        """`./file.png` must not be silently dropped.

        resolve_in_jail() returns a Path, and bool(Path(".")) is False — an earlier version
        used it as a truth value and every './'-prefixed path vanished from the model's view
        while absolute-looking and nested paths worked, which is exactly the kind of bug that
        survives a happy-path test.
        """
        from hearth import orchestrator as orch, workspaces as ws
        uid = "dm-dot"
        self._conv(uid, "dm-dot-conv")
        ws.write_bytes(uid, "a.png", self.PNG)
        imgs, blind = orch._display_image_items(uid, [{"path": "./a.png"}])
        self.assertEqual([i["path"] for i in imgs], ["./a.png"])
        self.assertEqual(blind, [])

    def test_call_is_capped_and_says_so(self):
        from hearth import workspaces as ws
        from hearth.tools import ToolContext
        from hearth.tools.display_media import MAX_FILES_PER_CALL
        uid, cid = "dm-cap", "dm-cap-conv"
        self._conv(uid, cid)
        paths = []
        for i in range(MAX_FILES_PER_CALL + 3):
            rel = f"p{i}.png"
            ws.write_bytes(uid, rel, self.PNG)
            paths.append(rel)
        res = asyncio.run(self._tool().run({"paths": paths},
                                           ToolContext(uid, {}, conversation_id=cid)))
        self.assertTrue(res.ok)
        self.assertEqual(len(res.meta["media"]), MAX_FILES_PER_CALL)
        self.assertIn("second call", res.content)

    def test_ui_meta_carries_media_for_live_rendering(self):
        from hearth import workspaces as ws, orchestrator as orch
        from hearth.tools import ToolContext
        uid, cid = "dm-uimeta", "dm-uimeta-conv"
        self._conv(uid, cid)
        ws.write_bytes(uid, "shot.png", self.PNG)
        res = asyncio.run(self._tool().run({"paths": ["shot.png"], "caption": "hi"},
                                           ToolContext(uid, {}, conversation_id=cid)))
        ui = orch._ui_meta("display_media", res)
        self.assertEqual([m["path"] for m in ui["media"]], ["shot.png"])
        self.assertEqual(res.meta["caption"], "hi")

    def test_model_only_image_tools_do_not_draw_media_bubbles(self):
        """pdf_read/view_video render pixels FOR THE MODEL — they must not also pop a bubble.

        Both set ToolResult.images, so a UI that read that field would draw a window per
        rendered page behind every tool chip. Only display_media's kind-tagged entries are
        presentation for the user.
        """
        from hearth import orchestrator as orch
        from hearth.tools.base import ToolResult

        class PdfRes:
            content = "Rendered 2 pages."
            meta = {"pages": [1, 2]}
            images = [{"path": "media/pdf/a_p1.png", "mime": "image/png"},
                      {"path": "media/pdf/a_p2.png", "mime": "image/png"}]

        ui = orch._ui_meta("pdf_read", PdfRes())
        self.assertNotIn("media", ui)
        row = orch._tool_row_meta("pdf_read", PdfRes(), "cp1")
        self.assertNotIn("display", row)
        # The model still gets its pages.
        self.assertEqual(len(row["images"]), 2)

    def test_stub_never_leaks_base64_from_media_messages(self):
        # The stub flattens content lists; a media tool's data URLs must become
        # '[image]' rather than being counted or keyword-matched as text.
        from hearth.llm.stub import _flatten_content
        parts = [{"type": "text", "text": "look"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}]
        flat = _flatten_content(parts)
        self.assertIn("[image]", flat)
        self.assertNotIn("base64", flat)

    def test_frame_and_page_limits_are_bounded(self):
        # Unbounded sampling would blow the context window in one call.
        from hearth.tools import TOOLS_BY_NAME
        self.assertLessEqual(TOOLS_BY_NAME["view_video"].DEFAULT_FRAMES, 8)
        self.assertLessEqual(TOOLS_BY_NAME["view_video"].MAX_FRAMES, 16)
        self.assertLessEqual(TOOLS_BY_NAME["pdf_read"].MAX_PAGES, 8)

    def test_view_video_audio_window_derivation(self):
        """DEC-064: the audio span is the sampled span — no separate audio range exists."""
        from hearth.tools import TOOLS_BY_NAME
        T = TOOLS_BY_NAME["view_video"]
        frames = [(23.75, b""), (31.25, b""), (46.25, b"")]
        # start_time/end_time drive it when given...
        self.assertEqual(T._audio_window({"start_time": 20, "end_time": 50}, frames, 600.0),
                         (20.0, 50.0))
        # ...and without them it is first-to-last sampled frame, so looking at three moments
        # never captions the whole film between them.
        self.assertEqual(T._audio_window({}, frames, 600.0), (23.75, 46.25))
        # One timestamp alone would be a zero-length span; give it a second of substance.
        self.assertEqual(T._audio_window({}, [(7.0, b"")], 600.0), (7.0, 8.0))
        # end_time omitted falls back to the runtime; junk gives no window rather than guessing.
        self.assertEqual(T._audio_window({"start_time": 10}, [], 600.0), (10.0, 600.0))
        self.assertIsNone(T._audio_window({"start_time": "x"}, frames, 600.0))
        self.assertIsNone(T._audio_window({}, [], 0.0))

    def test_view_video_audio_guidance_is_in_the_schema(self):
        """The 2-minute rule must be stated where the model reads it (DEC-064)."""
        from hearth.tools import TOOLS_BY_NAME
        T = TOOLS_BY_NAME["view_video"]
        desc = T.description + " " + str(T.parameters()["properties"]["with_audio"])
        for needle in ("with_audio", "2 minutes", "gist"):
            self.assertIn(needle, desc, f"schema/description should mention {needle!r}")
        # The enforced ceiling matches the one advertised.
        self.assertEqual(int(T.AUDIO_WINDOW_LIMIT_S), 120)

    def test_view_video_refuses_audio_over_the_limit_but_keeps_frames(self):
        """A long span must not silently buy N captioner calls; frames are still delivered."""
        from hearth.tools import TOOLS_BY_NAME, media_tools as mt
        T = TOOLS_BY_NAME["view_video"]

        async def no_call(**kw):  # pragma: no cover - must not run
            raise AssertionError("captioner must not be called for an over-limit span")

        from hearth import captioner as cap
        orig_preflight, orig_describe = cap.preflight, cap.describe_file
        cap.preflight = lambda s: ""
        cap.describe_file = no_call
        self.addCleanup(setattr, cap, "preflight", orig_preflight)
        self.addCleanup(setattr, cap, "describe_file", orig_describe)

        seen = {}

        def fake_extract(*a, **k):
            # Synchronous, like the real extract_frames_even — an async fake would hand the
            # tool a coroutine where it expects a list of frames.
            return [(10.0, b"\x89PNG"), (200.0, b"\x89PNG")]

        orig_even = mt.mr.extract_frames_even
        mt.mr.extract_frames_even = fake_extract
        self.addCleanup(setattr, mt.mr, "extract_frames_even", orig_even)

        class Ctx:
            user_id = "u-limit"
            conversation_id = "c"

            def settings(self):
                return {"captioner.enabled": True}

            def setting(self, k, default=None):
                return default

            def report_progress(self, **ev):
                pass

            def cancel_event(self):
                return None

        import hearth.workspaces as ws_mod
        orig_store = mt._store_images
        mt._store_images = lambda ctx, blobs, subdir: [{"path": f"media/{subdir}/f{i}.png",
                                                        "mime": "image/png"}
                                                       for i, _ in enumerate(blobs)]
        self.addCleanup(setattr, mt, "_store_images", orig_store)
        orig_info = mt.mr.video_info
        mt.mr.video_info = lambda p: {"duration_s": 600.0, "fps": 25.0, "frames": 15000,
                                      "width": 640, "height": 360, "codec": "h264",
                                      "size_bytes": 1000, "audio_streams": 1}
        self.addCleanup(setattr, mt.mr, "video_info", orig_info)
        orig_jail = ws_mod.resolve_in_jail
        ws_mod.resolve_in_jail = lambda uid, rel, allow_missing=True: Path("/nonexistent.mp4")
        self.addCleanup(setattr, ws_mod, "resolve_in_jail", orig_jail)

        res = asyncio.run(T.run({"path": "clip.mp4", "with_audio": True}, Ctx()))
        self.assertTrue(res.ok, "frames must still be delivered when audio is refused")
        self.assertIn("over the 120s limit", res.content)
        self.assertIn("describe_audio", res.content, "should point at the affordable alternative")
        self.assertFalse(res.meta["audio_described"])

    def test_render_backends_report_honestly(self):
        from hearth.tools import media_render as mr
        caps = mr.capabilities()
        for key in ("pymupdf", "pdftoppm", "gs", "mutool", "ffmpeg", "ffprobe", "opencv"):
            self.assertIn(key, caps)
        line = mr.capabilities_line()
        self.assertTrue(line.startswith("render backends available: "))

    def test_pdf_render_roundtrip_when_backend_available(self):
        from hearth.tools import media_render as mr
        if not (mr._pymupdf() or mr.have("pdftoppm") or mr.have("gs") or mr.have("mutool")):
            self.skipTest("no PDF rendering backend on this machine")
        # A minimal valid one-page PDF, written by hand so no PDF library is needed.
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
               b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 240 320]>>endobj\n"
               b"trailer<</Root 1 0 R>>\n%%EOF\n")
        pages = mr.render_pdf_bytes(pdf, [0], dpi=60)
        self.assertIn(0, pages)
        self.assertEqual(pages[0][:4], b"\x89PNG", "rendered page must be a PNG")

    def test_pdf_info_error_is_actionable(self):
        from hearth.tools import media_render as mr
        with self.assertRaises(mr.MediaError):
            mr.pdf_info_bytes(b"not a pdf at all")
        with self.assertRaises(mr.MediaError):
            mr.pdf_info_bytes(b"")

    def test_even_sampling_timestamps_are_ordered_and_clamped(self):
        from hearth.tools import media_render as mr
        # Pure maths used for spacing: fmt_time must round the total, not the remainder.
        self.assertEqual(mr.fmt_time(0), "0:00")
        self.assertEqual(mr.fmt_time(65), "1:05")
        self.assertEqual(mr.fmt_time(3599.6), "60:00")
        self.assertEqual(mr.fmt_time(None), "?")
        self.assertEqual(mr._parse_rate("25/1"), 25.0)
        self.assertEqual(mr._parse_rate("0/0"), 0.0)
        self.assertEqual(mr._parse_rate("N/A"), 0.0)

    def test_context_usage_tool_reports_absolute_and_percent(self):
        import asyncio
        from hearth.tools import TOOLS_BY_NAME, ToolContext
        uid, cid = "media-ctx", "media-ctx-conv"
        self._conv(uid, cid)
        ctx = ToolContext(uid, {"model.n_ctx": 10_000}, {}, conversation_id=cid)
        res = asyncio.run(TOOLS_BY_NAME["context_usage"].run({}, ctx))
        self.assertTrue(res.ok)
        self.assertIn("context used", res.content)
        self.assertIn("remaining", res.content)
        self.assertEqual(res.meta["n_ctx"], 10_000)
        self.assertIsInstance(res.meta["pct"], float)
        self.assertLessEqual(res.meta["pct"], 1.0)

    def test_media_paths_are_jailed(self):
        # Absolute/traversal paths must be refused before any renderer runs.
        import asyncio
        from hearth.tools import TOOLS_BY_NAME, ToolContext
        ctx = ToolContext("media-jailed", {}, {}, conversation_id="")
        for name in ("pdf_info", "pdf_read", "video_info", "view_video"):
            res = asyncio.run(TOOLS_BY_NAME[name].run({"path": "/etc/passwd"}, ctx))
            self.assertFalse(res.ok, f"{name} accepted an absolute path")
            self.assertIn("refused", res.content)


class OutputGuardTests(unittest.TestCase):
    """The per-tool-result character budget (Admin → Agent)."""

    def _make_conv(self, conv_id: str, user_id: str = "u") -> None:
        """Create the user + conversation a turn needs (FK on Message.conversation_id)."""
        from hearth.db import init_db, session_scope
        from hearth.models import Conversation, User
        init_db()
        with session_scope() as s:
            if s.get(User, user_id) is None:
                s.add(User(id=user_id, username=f"user-{user_id}", pw_hash="x"))
            if s.get(Conversation, conv_id) is None:
                s.add(Conversation(id=conv_id, user_id=user_id, title="test"))

    def test_guard_clips_and_announces_the_loss(self):
        from hearth import output_guard as og
        text = "x" * 5000
        clipped, was = og.guard_text(text, 1000, "fs_read")
        self.assertTrue(was)
        self.assertIn(og.GUARD_MARKER, clipped)
        self.assertIn("1,000", clipped)
        self.assertIn("5,000", clipped)
        self.assertIn("fs_read", clipped)
        # The head must be untouched (a truncated file read still starts at the top),
        # and the marker is what tells the model the rest is missing.
        self.assertEqual(clipped[:1000], text[:1000])
        self.assertTrue(clipped[1000:].lstrip().startswith(og.GUARD_MARKER))

    def test_guard_is_idempotent_and_leaves_short_text_alone(self):
        from hearth import output_guard as og
        short, was = og.guard_text("hello", 1000)
        self.assertFalse(was)
        self.assertEqual(short, "hello")
        # Guarding twice must not stack markers or shrink the budget again: a guarded
        # result passes back through this seam on every re-send of the history.
        once, _ = og.guard_text("y" * 5000, 1000)
        twice, was2 = og.guard_text(once, 1000)
        self.assertFalse(was2)
        self.assertEqual(twice, once)
        self.assertEqual(twice.count(og.GUARD_MARKER), 1)

    def test_multimodal_content_is_never_sliced(self):
        from hearth import output_guard as og
        parts = [{"type": "text", "text": "hi"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 99999}}]
        out, was = og.guard_message_content(parts, {"enabled": True, "max_chars": 200})
        # A part list is not a string: slicing it would corrupt the base64 payload and
        # hand the model a broken image. Returned untouched, identity preserved.
        self.assertFalse(was)
        self.assertIs(out, parts)

    def test_policy_coercion_never_produces_a_zero_cap(self):
        from hearth import output_guard as og
        for bad in ({"max_chars": 0}, {"max_chars": 5}, {"max_chars": "abc"}, {}):
            p = og.normalize_policy(bad)
            # A zero/tiny cap would silently blank every tool result: the model would see
            # nothing and the failure would look like a broken tool, not a bad setting.
            self.assertGreaterEqual(p["max_chars"], og.MIN_MAX_CHARS, f"policy {bad} too small")
            self.assertFalse(og.normalize_policy({"enabled": "false"})["enabled"])
            self.assertTrue(og.normalize_policy({"enabled": "true"})["enabled"])
            p = og.normalize_policy({"exempt_tools": "fs_read, exec ,"})
            self.assertEqual(p["exempt_tools"], ["exec", "fs_read"])
            self.assertFalse(og.applies({"enabled": True, "exempt_tools": "fs_read"}, "fs_read"))
            self.assertTrue(og.applies({"enabled": True, "exempt_tools": "fs_read"}, "exec"))

    def test_tool_result_guard_returns_new_object_only_on_clip(self):
        import asyncio  # noqa: F401  (kept for parity with sibling tests)
        from hearth import output_guard as og
        from hearth.tools.base import ToolResult
        small = ToolResult(True, "short", meta={"download": "tok"})
        same, was = og.guard_tool_result(small, {"max_chars": 1000}, "fs_read")
        # Identity on the unchanged path: downstream code compares results by object.
        self.assertFalse(was)
        self.assertIs(same, small)
        big = ToolResult(True, "z" * 5000, meta={"download": "tok"})
        guarded, was = og.guard_tool_result(big, {"max_chars": 1000}, "fs_read")
        self.assertTrue(was)
        self.assertIsNot(guarded, big)
        self.assertTrue(guarded.ok)
        # The download token must survive the clip or the artifact link disappears.
        self.assertEqual(guarded.meta, {"download": "tok"})

    def test_orchestrator_applies_the_guard_to_a_tool_result(self):
        import asyncio
        from hearth import orchestrator as orch
        from hearth import output_guard as og
        from hearth.tools import ToolContext
        from hearth.tools.base import ToolResult, Tool

        class BigTool(Tool):
            name = "fs_read"
            description = "test double"
            enable_key = "tools.fs_read"

            def parameters(self) -> dict:
                return {"type": "object", "properties": {}, "required": []}

            async def run(self, args, ctx):
                return ToolResult(True, "Q" * 60000)

        conv = "guard-conv"
        self._make_conv(conv)
        # The seam lives in orchestrator._run_tool, so the guard is proven through the real
        # call path rather than by calling output_guard directly.
        _o = orch
        orig = dict(_o.TOOLS_BY_NAME)
        _o.TOOLS_BY_NAME["fs_read"] = BigTool()
        try:
            ctx = ToolContext("u", {"agent.tool_output_guard": True,
                                    "agent.tool_output_max_chars": 2000})
            req = _o._mk_request(BigTool(), {}, "c1")
            res = asyncio.run(_o._run_tool(req, ctx))
            self.assertLess(len(res.content), 4000)
            self.assertIn(og.GUARD_MARKER, res.content)
            # Off means off: full text, no marker.
            ctx_off = ToolContext("u", {"agent.tool_output_guard": False})
            res_off = asyncio.run(_o._run_tool(req, ctx_off))
            self.assertNotIn(og.GUARD_MARKER, res_off.content)
            self.assertEqual(len(res_off.content), 60000)
        finally:
            _o.TOOLS_BY_NAME.clear()
            _o.TOOLS_BY_NAME.update(orig)


class ErrorHintTests(unittest.TestCase):
    """Model-facing next-action advice on tool failures."""

    def test_hint_keeps_message_first_and_is_idempotent(self):
        from hearth import errors as err
        out = err.hint('no_match', 'old_text not found')
        self.assertTrue(out.startswith('old_text not found'),
                        'the original message must stay first (tests and prompt prose rely on it)')
        self.assertIn(err.HINT_PREFIX, out)
        self.assertEqual(out.count(err.HINT_PREFIX), 1)
        # Re-hinting an already-hinted message must not stack advice.
        self.assertEqual(err.hint('no_match', out), out)

    def test_every_hint_names_an_action(self):
        from hearth import errors as err
        for kind, text in err.ERROR_HINTS.items():
            self.assertTrue(text.strip(), f"empty hint for {kind}")
            # A hint that only restates the problem is dead weight in the prompt: it has to
            # contain a verb the model can act on. Checked against a list rather than
            # grammar, because the useful wording is short and specific.
            self.assertTrue(
                any(v in text for v in ("fs_list", "fs_read", "Re-read", "Include", "Read",
                                        "Use", "Try", "Resend", "Do not", "do not",
                                        "answer", "tell")),
                f"hint for {kind} gives no next action: {text}")

    def test_unknown_kind_returns_message_unchanged(self):
        from hearth import errors as err
        self.assertEqual(err.hint('no_such_kind', 'boom'), 'boom')

    def test_fs_edit_and_read_failures_carry_hints(self):
        import asyncio
        from hearth.tools.fs_tools import FsEditTool, FsReadTool
        from hearth.tools import ToolContext
        from hearth import errors as err
        ws_mod = __import__('hearth.workspaces', fromlist=['x'])
        ws_mod.write_text('hintuser', 'h.txt', 'alpha\nbeta\nalpha\n')
        ctx = ToolContext('hintuser', {})
        # Ambiguous match: the advice is to include surrounding lines, not to retry blindly.
        multi = asyncio.run(FsEditTool().run(
            {"path": "h.txt", "old_text": "alpha", "new_text": "x"}, ctx))
        self.assertFalse(multi.ok)
        self.assertIn(err.HINT_PREFIX, multi.content)
        self.assertIn('surrounding lines', multi.content)
        # No match at all: re-read before guessing.
        nomatch = asyncio.run(FsEditTool().run(
            {"path": "h.txt", "old_text": "zzz", "new_text": "x"}, ctx))
        self.assertFalse(nomatch.ok)
        self.assertIn('Re-read', nomatch.content)
        # Jail escape: point at a workspace-relative path instead.
        esc = asyncio.run(FsReadTool().run({"path": "../secret"}, ctx))
        self.assertFalse(esc.ok)
        self.assertIn('relative to your workspace', esc.content)


class FeatureI18nTests(unittest.TestCase):
    """Keys added with the reasoning / steering / prefill work must be translated."""

    def test_feature_i18n_keys_have_german(self):
        from hearth import i18n
        want = ['chat.decode', 'chat.prefill', 'chat.left', 'chat.reasoning',
                'chat.steer_queued', 'chat.steer_withdraw', 'chat.steer_hint',
                'chat.listening', 'chat.caption_failed', 'chat.caption_streaming',
                'chat.caption_failed_short']
        de = i18n.dict_for('de')
        en = i18n.dict_for('en')
        # A key missing from EN renders as an empty label; missing DE shows English to a
        # German user. Both are silent, so assert them explicitly.
        missing_en = [k for k in want if k not in en]
        missing_de = [k for k in want if k not in de]
        self.assertEqual(missing_en, [], f"EN keys missing: {missing_en}")
        self.assertEqual(missing_de, [], f"DE translations missing: {missing_de}")

    def test_steering_queue_semantics(self):
        from hearth import steering as st
        conv = 'steer-unit-conv'
        st.clear(conv)
        # Whitespace is not a message: queue() reports -1 so the route can 400.
        self.assertEqual(st.queue(conv, '   '), -1)
        self.assertEqual(st.pending_count(conv), 0)
        self.assertEqual(st.queue(conv, 'first thought'), 1)
        self.assertEqual(st.queue(conv, 'second thought'), 2)
        self.assertEqual(st.preview(conv), ['first thought', 'second thought'])
        self.assertTrue(st.withdraw(conv, 0))
        self.assertFalse(st.withdraw(conv, 9))       # stale index: already delivered
        self.assertEqual(st.preview(conv), ['second thought'])
        items = st.drain(conv)
        self.assertEqual([i['text'] for i in items], ['second thought'])
        self.assertEqual(st.drain(conv), [])          # drain is destructive
        # Queues are per-conversation: one chat must never read another's steering.
        other = 'steer-other-conv'
        st.queue(other, 'belong elsewhere')
        self.assertEqual(st.drain(conv), [])
        self.assertEqual(len(st.drain(other)), 1)

    def test_reasoning_retention_policies(self):
        from hearth.routes_chat import _apply_reasoning_retention
        rows = [{'role': 'assistant', 'content': 'answer one', 'meta': '{"reasoning":"think A"}'},
                {'role': 'user', 'content': 'next question', 'meta': ''},
                {'role': 'assistant', 'content': 'answer two', 'meta': '{"reasoning":"think B"}'}]
        out = _apply_reasoning_retention([dict(r) for r in rows], 'keep_all')
        self.assertEqual(out[0].get('reasoning'), 'think A')
        self.assertEqual(out[2].get('reasoning'), 'think B')
        # keep_last keeps only the newest — the one the user is still reading.
        out = _apply_reasoning_retention([dict(r) for r in rows], 'keep_last')
        self.assertNotIn('reasoning', out[0])
        self.assertEqual(out[2].get('reasoning'), 'think B')
        for pol in ('hide', 'drop'):
            out = _apply_reasoning_retention([dict(r) for r in rows], pol)
            self.assertTrue(all('reasoning' not in r for r in out), pol)
        # An unknown policy must not destroy content nobody asked to lose.
        out = _apply_reasoning_retention([dict(r) for r in rows], 'nonsense')
        self.assertEqual(out[2]['content'], 'answer two')

    def test_think_splitter_handles_tags_across_chunks(self):
        from hearth.llm.llama_server import _ThinkSplitter
        sp = _ThinkSplitter()
        # Tags arrive split across SSE chunks; a per-chunk regex would miss them or leak
        # a bare '<thi' into the answer.
        got = (sp.feed('lead<th') + sp.feed('ink>deep thought</thi')
               + sp.feed('nk>the answer') + sp.flush())
        kinds = [k for k, _ in got]
        self.assertIn('reasoning', kinds)
        text = ''.join(t for k, t in got if k == 'token')
        self.assertNotIn('<think>', text)
        self.assertTrue(text.endswith('the answer'))

    def test_think_splitter_unclosed_block_stays_reasoning(self):
        from hearth.llm.llama_server import _ThinkSplitter
        sp = _ThinkSplitter()
        got = sp.feed('<think>started but never closed') + sp.flush()
        # An unclosed block IS reasoning: it must not be dropped or shown as answer text.
        self.assertIn('reasoning', [k for k, _ in got])
        self.assertNotIn('token', [k for k, _ in got])

    def test_vision_reject_detection_is_specific(self):
        from hearth.orchestrator import _looks_like_vision_reject
        self.assertTrue(_looks_like_vision_reject(
            'llama-server 400: this model does not support image input'))
        self.assertTrue(_looks_like_vision_reject('HTTP 500 — mmproj failed to load'))
        # Must NOT match unrelated errors, or a genuine context overflow gets papered over
        # by silently stripping the user's images.
        self.assertFalse(_looks_like_vision_reject('400 Bad Request: n_ctx exceeded'))
        self.assertFalse(_looks_like_vision_reject(''))

    def test_output_guard_exempt_list_is_honoured_end_to_end(self):
        import asyncio
        from hearth import orchestrator as orch
        from hearth.tools import ToolContext
        from hearth.tools.base import ToolResult, Tool

        class R(Tool):
            name = "fs_read"
            description = "d"
            enable_key = "tools.fs_read"

            def parameters(self) -> dict:
                return {"type": "object", "properties": {}, "required": []}

            async def run(self, args, ctx):
                return ToolResult(True, "W" * 30000)

        req = orch._mk_request(R(), {}, "c1")
        orig_reg = dict(orch.TOOLS_BY_NAME)
        orch.TOOLS_BY_NAME["fs_read"] = R()
        try:
            # Exempt: the full result reaches the model even though the cap is active.
            ctx = ToolContext("u", {"agent.tool_output_guard": True,
                                    "agent.tool_output_max_chars": 1000,
                                    "agent.tool_output_exempt_tools": "fs_read"})
            res = asyncio.run(orch._run_tool(req, ctx))
            self.assertEqual(len(res.content), 30000)
            # Not exempt: same tool, same cap, clipped.
            ctx_on = ToolContext("u", {"agent.tool_output_guard": True,
                                       "agent.tool_output_max_chars": 1000})
            res_on = asyncio.run(orch._run_tool(req, ctx_on))
            self.assertLess(len(res_on.content), 2000)
        finally:
            orch.TOOLS_BY_NAME.clear()
            orch.TOOLS_BY_NAME.update(orig_reg)

    def test_progress_event_eta_is_honest(self):
        from hearth import orchestrator as orch
        ev = orch._progress_event({'kind': 'prefill', 'n_tokens': 900,
                                   'progress': 0.005, 'rate_tps': 700})
        # Below 1 % llama.cpp's fraction is rounded to two decimals, so dividing a handful
        # of tokens by a hundredth of a prompt invents a huge ETA out of noise.
        self.assertNotIn('eta', ev.data)


class StreamLoopReentryTests(unittest.TestCase):
    """The idle-progress poller must not re-enter or time out the provider read.

    Two bugs shared one root: `_stream_checked()` treated "only the log poll fired" as a
    first-class outcome and retired/re-armed the provider `__anext__()` around it. An async
    generator allows only ONE read in flight, and cancelling a read task does not unwind its
    frame synchronously — so the next iteration re-entered a still-RUNNING generator and the
    SSE route died with "anext(): asynchronous generator is already running". The same
    fall-through also cut off prefill early, while a genuinely wedged stream timed out never
    at all, because the idle deadline was recomputed per iteration and each poll restarted
    the countdown.
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
                except (asyncio.CancelledError, StopAsyncIteration, orch.TurnAborted):
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


class CaptionerTests(unittest.TestCase):
    """The audio captioner: config resolution, argv building, chunking, refusal paths.

    No real model is ever launched — these check that Hearth builds the right command and
    refuses clearly when something is missing, which is where an operator actually needs
    help. Network/ffmpeg work is exercised only through fakes or absent-binary paths.
    """

    def _settings(self, **over):
        from hearth import runtime_settings as rs
        base = {s.key: s.default for s in rs.ALL_SPECS}
        base.update({"captioner.enabled": True, "captioner.auto_route": True})
        base.update(over)
        return base

    def test_defaults_are_off_and_cpu_first(self):
        from hearth import runtime_settings as rs
        specs = {s.key: s for s in rs.CAPTIONER_SPECS}
        # Off until an admin supplies paths: a second model must never start by surprise.
        self.assertIs(specs["captioner.enabled"].default, False)
        # MoE-friendly default: CPU-only offload, its own port, modest context.
        self.assertEqual(specs["captioner.n_gpu_layers"].default, "0")
        self.assertEqual(specs["captioner.port"].default, 8091)
        self.assertLess(specs["captioner.n_ctx"].default, 32768)

    def test_ngl_normalisation_never_passes_the_word_auto(self):
        # llama.cpp parses the literal "auto" as -2 and silently degrades to CPU tensor
        # overrides; 'all' must become -1. Same rule as manager.build_argv.
        from hearth import captioner as cap
        self.assertEqual(cap._ngl_value({"captioner.n_gpu_layers": "auto"}), "")
        self.assertEqual(cap._ngl_value({"captioner.n_gpu_layers": "all"}), "-1")
        self.assertEqual(cap._ngl_value({"captioner.n_gpu_layers": "0"}), "0")
        self.assertEqual(cap._ngl_value({"captioner.n_gpu_layers": "18"}), "18")
        self.assertEqual(cap._ngl_value({"captioner.n_gpu_layers": "nonsense"}), "0")

    def test_build_argv_cpu_default_and_flags(self):
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            s = self._settings(**{"captioner.model_path": model,
                                  "captioner.server_bin": sys.executable})
            argv = cap.build_argv(s)
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--model", argv)
        self.assertIn("--n-gpu-layers", argv)
        self.assertEqual(argv[argv.index("--n-gpu-layers") + 1], "0")
        # Loopback only, on its own port, one slot (captioning is serialised anyway).
        self.assertIn("--host", argv)
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--parallel") + 1], "1")
        self.assertNotIn("auto", argv)

    def test_build_argv_refuses_missing_paths_with_actionable_text(self):
        from hearth import captioner as cap
        with self.assertRaises(cap.CaptionError) as cm:
            cap.build_argv(self._settings(**{"captioner.model_path": "",
                                             "captioner.server_bin": sys.executable}))
        self.assertIn("model", str(cm.exception).lower())
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.gguf")
            with self.assertRaises(cap.CaptionError) as cm2:
                cap.build_argv(self._settings(**{"captioner.model_path": missing,
                                                 "captioner.server_bin": sys.executable}))
        self.assertIn("not found", str(cm2.exception))
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            with self.assertRaises(cap.CaptionError) as cm3:
                cap.build_argv(self._settings(**{"captioner.model_path": model,
                                                 "captioner.server_bin": "/no/such/llama-server"}))
        self.assertIn("binary", str(cm3.exception).lower())

    def test_bad_extra_args_are_refused_not_guessed(self):
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            s = self._settings(**{"captioner.model_path": model,
                                  "captioner.server_bin": sys.executable,
                                  "captioner.extra_args": "--unbalanced-quote \""})
            with self.assertRaises(cap.CaptionError):
                cap.build_argv(s)

    def test_is_audio_by_extension_and_mime(self):
        from hearth import captioner as cap
        for name in ("a.wav", "b.MP3", "c.flac", "d.opus", "e.m4a", "f.webm"):
            self.assertTrue(cap.is_audio(name), name)
        self.assertTrue(cap.is_audio("voice-note", mime="audio/ogg"))
        for name in ("notes.md", "photo.png", "archive.zip", ""):
            self.assertFalse(cap.is_audio(name), name)

    def test_route_candidate_requires_enabled_and_auto(self):
        from hearth import captioner as cap
        self.assertTrue(cap.route_candidate(self._settings(), "x.mp3"))
        off = self._settings(**{"captioner.enabled": False})
        self.assertFalse(cap.route_candidate(off, "x.mp3"))
        manual = self._settings(**{"captioner.auto_route": False})
        self.assertFalse(cap.route_candidate(manual, "x.mp3"))
        self.assertFalse(cap.route_candidate(self._settings(), "notes.txt"))

    def test_preflight_names_the_missing_thing(self):
        from hearth import captioner as cap
        s = self._settings(**{"captioner.enabled": False})
        self.assertIn("disabled", cap.preflight(s))
        s = self._settings(**{"captioner.model_path": ""})
        self.assertIn("model path", cap.preflight(s))
        # CLI backend needs both a cli binary and a projector — with a REAL model file so
        # the (correctly) higher-priority model check does not mask these.
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            s = self._settings(**{"captioner.backend": "cli",
                                  "captioner.model_path": model,
                                  "captioner.cli_bin": "/definitely/not/here"})
            self.assertIn("mtmd", cap.preflight(s).lower())
            s = self._settings(**{"captioner.backend": "cli",
                                  "captioner.model_path": model,
                                  "captioner.cli_bin": sys.executable,
                                  "captioner.mmproj_path": ""})
            self.assertIn("mmproj", cap.preflight(s))

    def test_prepare_chunks_without_ffmpeg_passes_file_through(self):
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            if cap.have("ffmpeg"):
                self.skipTest("ffmpeg is installed here; passthrough path not reachable")
            src = os.path.join(d, "input.wav")
            with open(src, "wb") as fh:
                fh.write(b"RIFF....WAVEfmt ")
            logs = []
            chunks = cap.prepare_chunks(src, d, resample=True, chunk_seconds=30,
                                        log=logs.append)
            self.assertEqual(chunks, [(src, 0.0)])
            self.assertTrue(any("ffmpeg" in m for m in logs))

    def test_chunk_offsets_are_labelled_per_chunk(self):
        from hearth import captioner as cap
        # The clamp floor is 5 s, so the test audio must be comfortably longer than that or
        # ffmpeg produces a single segment and there is nothing to assert about offsets.
        with tempfile.TemporaryDirectory() as d:
            if not cap.have("ffmpeg"):
                self.skipTest("ffmpeg needed to produce real chunks")
            src = self._real_wav(d, seconds=13.0)
            chunks = cap.prepare_chunks(src, d, resample=True, chunk_seconds=5)
            self.assertGreater(len(chunks), 2, f"expected ~3 chunks, got {chunks}")
            offsets = [c[1] for c in chunks]
            self.assertEqual(offsets, [0.0, 5.0, 10.0])

    def test_window_cut_extracts_only_that_span(self):
        """DEC-064: a window must yield ONLY that part of the file, not the whole thing.

        Verified acoustically rather than by reading argv: a 12 s clip whose tone changes
        pitch at t=6 proves whether ffmpeg really cut, because describing the wrong half
        would still 'work' as far as the code is concerned and silently feed the model audio
        from a moment it never asked about.
        """
        from hearth import captioner as cap
        import struct
        with tempfile.TemporaryDirectory() as d:
            if not cap.have("ffmpeg"):
                self.skipTest("ffmpeg needed to cut a time window")
            src = os.path.join(d, "two_tones.mp4")
            subprocess.run(["ffmpeg", "-y", "-v", "error",
                            "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
                            "-f", "lavfi", "-i", "sine=frequency=1760:duration=6",
                            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                            "-map", "[a]", "-c:a", "aac", src], check=True)

            # Read the chunk bytes BEFORE their temporary directory is torn down, or the
            # assertion measures a file that no longer exists. Returns [(offset_s, pcm_bytes)].
            def measure(**kw):
                with tempfile.TemporaryDirectory() as t:
                    ch = cap.prepare_chunks(src, t, resample=True, chunk_seconds=30, **kw)
                    out = []
                    for path, offset in ch:
                        with open(path, "rb") as fh:
                            out.append((offset, fh.read()))
                    return out

            def approx_hz(data):
                """Zero-crossing rate of 16 kHz mono PCM16 — a pitch proxy needing no library.

                220 Hz and 1760 Hz are far enough apart that any reasonable threshold tells
                them apart; the point is WHICH half of the clip survived the cut, not an
                exact frequency.
                """
                raw = data[44:]                     # skip the WAV header ffmpeg wrote
                n = len(raw) // 2
                if n < 200:
                    return 0.0
                prev, cross = 0, 0
                for i in range(0, (n - 1) * 2, 2):
                    v = struct.unpack_from("<h", raw, i)[0]
                    if (v >= 0) != (prev >= 0):
                        cross += 1
                    prev = v
                return (cross / 2.0) / (n / 16000.0)

            late = measure(window=(7.0, 12.0))
            self.assertEqual(len(late), 1)
            # Offset stays ABSOLUTE so caption labels quote real timestamps of the clip.
            self.assertAlmostEqual(late[0][0], 7.0, delta=0.6)
            self.assertGreater(approx_hz(late[0][1]), 900, "window should hold only the high tone")

            early = measure(window=(0.0, 3.0))
            self.assertLess(approx_hz(early[0][1]), 900, "window should hold only the low tone")

    def test_window_needs_ffmpeg_rather_than_describing_whole_file(self):
        """Without ffmpeg a window cannot be honoured — refuse instead of captioning everything."""
        from hearth import captioner as cap
        orig = cap.shutil.which
        cap.shutil.which = lambda name: None
        self.addCleanup(setattr, cap.shutil, "which", orig)
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.wav")
            with open(src, "wb") as fh:
                fh.write(b"RIFF....WAVEfmt ")
            with self.assertRaises(cap.CaptionError):
                cap.prepare_chunks(src, d, resample=True, chunk_seconds=30, window=(5.0, 9.0))

    def test_window_chunk_offsets_stay_absolute(self):
        """Chunk labels inside a window count from the WINDOW start, not from zero."""
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            if not cap.have("ffmpeg"):
                self.skipTest("ffmpeg needed to produce real chunks")
            src = self._real_wav(d, seconds=30.0)
            chunks = cap.prepare_chunks(src, d, resample=True, chunk_seconds=5,
                                        window=(10.0, 25.0))
            offsets = [c[1] for c in chunks]
            self.assertEqual(offsets[0], 10.0)
            self.assertTrue(all(o >= 10.0 for o in offsets), offsets)

    def test_chunk_clamp_floor_and_off(self):
        # Sub-5-second chunks shred audio into pieces too short to describe; 0 means "don't
        # split". Both are admin-facing values, so the clamping is deliberate and tested.
        from hearth import captioner as cap
        self.assertEqual(cap._clamp_chunk(1), 5)
        self.assertEqual(cap._clamp_chunk(0), 0)
        self.assertEqual(cap._clamp_chunk(None), 0)
        self.assertEqual(cap._clamp_chunk("junk"), 0)
        self.assertEqual(cap._clamp_chunk(30), 30)
        self.assertEqual(cap._clamp_chunk(100000), cap._MAX_CHUNK_SECONDS)

    def test_fmt_time(self):
        from hearth import captioner as cap
        self.assertEqual(cap.fmt_time(0), "0:00")
        self.assertEqual(cap.fmt_time(90), "1:30")
        self.assertEqual(cap.fmt_time(None), "?")

    def test_caption_sync_refuses_when_disabled(self):
        from hearth import captioner as cap
        c = cap.Captioner()
        with self.assertRaises(cap.CaptionError) as cm:
            c.caption_sync("/tmp/whatever.wav", self._settings(**{"captioner.enabled": False}))
        self.assertIn("disabled", str(cm.exception))

    def test_cli_backend_reports_missing_binary_clearly(self):
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            wav = self._real_wav(d)
            c = cap.Captioner()
            s = self._settings(**{"captioner.backend": "cli",
                                  "captioner.model_path": model,
                                  "captioner.cli_bin": "/definitely/not/llama-mtmd-cli",
                                  "captioner.chunk_seconds": 0})
            with self.assertRaises(cap.CaptionError) as cm:
                c.caption_sync(wav, s)
        self.assertIn("mtmd", str(cm.exception).lower())

    def test_cli_backend_needs_mmproj(self):
        from hearth import captioner as cap
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "omni.gguf")
            open(model, "wb").write(b"x")
            wav = self._real_wav(d)
            c = cap.Captioner()
            # `env` exists as a stand-in CLI, so the projector check is what must fire.
            s = self._settings(**{"captioner.backend": "cli",
                                  "captioner.model_path": model,
                                  "captioner.cli_bin": "/usr/bin/env",
                                  "captioner.mmproj_path": "",
                                  "captioner.chunk_seconds": 0})
            with self.assertRaises(cap.CaptionError) as cm:
                c.caption_sync(wav, s)
        self.assertIn("mmproj", str(cm.exception))

    def _real_wav(self, d: str, seconds: float = 3.0, rate: int = 16000) -> str:
        """A genuine PCM wav (sine tone) — ffmpeg and real models both accept it, whereas a
        file of fake bytes is refused before any interesting code path runs."""
        import math
        import struct as _struct
        import wave as _wave
        path = os.path.join(d, "tone.wav")
        with _wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            n = int(rate * seconds)
            for i in range(n):
                wf.writeframes(_struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))))
        return path

    def test_status_never_raises_and_reports_both_models_fields(self):
        from hearth import captioner as cap
        import asyncio
        c = cap.Captioner()
        st = asyncio.run(c.status(self._settings(**{"captioner.model_path": "",
                                                    "captioner.server_bin": ""})))
        self.assertEqual(st["kind"], "captioner")
        self.assertIn("reachable", st)
        self.assertIn("capabilities", st)
        self.assertFalse(st["configured"])

    def test_caption_cache_is_scoped_per_user_and_conversation(self):
        # One family member's recording must never be quoted into another's context.
        from hearth import orchestrator as orch
        orch._remember_caption("alice", "c1", "uploads/a.mp3", "two speakers")
        self.assertEqual(orch.get_caption("alice", "c1", "uploads/a.mp3"), "two speakers")
        self.assertEqual(orch.get_caption("bob", "c1", "uploads/a.mp3"), "")
        self.assertEqual(orch.get_caption("alice", "c2", "uploads/a.mp3"), "")

    def test_prepare_captions_disabled_still_informs_the_model(self):
        import asyncio
        from hearth import orchestrator as orch
        att = [{"name": "voice.mp3", "mime": "audio/mpeg", "rel_path": "uploads/voice.mp3"}]
        out = asyncio.run(orch.prepare_captions("u1", "conv-off", att,
                                                self._settings(**{"captioner.enabled": False})))
        self.assertIn("uploads/voice.mp3", out)
        self.assertIn("NOT analysed", out["uploads/voice.mp3"])
        # And the reason is not cached, so switching it on takes effect immediately.
        self.assertEqual(orch.get_caption("u1", "conv-off", "uploads/voice.mp3"), "")

    def test_prepare_captions_ignores_non_audio(self):
        import asyncio
        from hearth import orchestrator as orch
        att = [{"name": "notes.md", "mime": "text/markdown", "rel_path": "notes.md"}]
        out = asyncio.run(orch.prepare_captions("u1", "conv-na", att, self._settings()))
        self.assertEqual(out, {})

    def test_attachment_content_inserts_caption_text_for_audio(self):
        from hearth import orchestrator as orch
        att = [{"name": "voice.mp3", "mime": "audio/mpeg", "rel_path": "uploads/voice.mp3"}]
        caps = {"uploads/voice.mp3": "CAPTIONED"}
        # With only an audio attachment and no image, content stays a plain string — the
        # caption is folded into it (multimodal lists are for pixels the model can see).
        content = orch._attachment_content("u1", att, "what is this?", caps)
        self.assertIsInstance(content, str)
        self.assertIn("CAPTIONED", content)
        self.assertIn("what is this?", content)
        # An image alongside still produces multimodal parts, plus the audio caption.
        uid = "capattuser"
        from hearth import workspaces as ws
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        ws.write_bytes(uid, "pic.png", b"\x89PNG\r\n\x1a\nfake")
        att2 = att + [{"name": "pic.png", "mime": "image/png", "rel_path": "pic.png"}]
        content2 = orch._attachment_content(uid, att2, "look", caps)
        self.assertIsInstance(content2, list)
        joined = "\n".join(str(p.get("text") or "") for p in content2
                           if p.get("type") == "text")
        self.assertIn("CAPTIONED", joined)
        self.assertTrue([p for p in content2 if p.get("type") == "image_url"])

    def test_attachment_content_without_captions_is_unchanged(self):
        from hearth import orchestrator as orch
        att = [{"name": "notes.md", "mime": "text/markdown", "rel_path": "notes.md"}]
        self.assertEqual(orch._attachment_content("u1", att, "hello"), "hello")

    def test_describe_audio_tool_refuses_without_config(self):
        import asyncio
        from hearth.tools.audio_tools import DescribeAudioTool
        from hearth.tools import ToolContext
        uid = "captooluser"
        from hearth import workspaces as ws
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        ws.write_text(uid, "uploads/clip.mp3", "pretend audio")
        ctx = ToolContext(uid, self._settings(**{"captioner.enabled": False}), {})
        res = asyncio.run(DescribeAudioTool().run({"path": "uploads/clip.mp3"}, ctx))
        self.assertFalse(res.ok)
        self.assertIn("switched off", res.content)

    def test_describe_audio_tool_rejects_escape_and_missing(self):
        import asyncio
        from hearth.tools.audio_tools import DescribeAudioTool
        from hearth.tools import ToolContext
        uid = "captooluser2"
        from hearth import workspaces as ws
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        t = DescribeAudioTool()
        res = asyncio.run(t.run({"path": "../../etc/passwd"}, ToolContext(uid, {}, {})))
        self.assertFalse(res.ok)
        self.assertIn("refused", res.content)
        res2 = asyncio.run(t.run({"path": "nope.mp3"}, ToolContext(uid, {}, {})))
        self.assertFalse(res2.ok)
        self.assertIn("not found", res2.content)

    def test_audio_info_tool_without_ffprobe_says_so(self):
        import asyncio
        from hearth.tools import audio_tools as at
        from hearth.tools import ToolContext
        uid = "capinfouser"
        from hearth import workspaces as ws
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        ws.write_text(uid, "uploads/x.mp3", "not really audio")
        # Force the no-ffprobe answer regardless of this machine.
        orig = at.cap.audio_info_for_workspace
        at.cap.audio_info_for_workspace = lambda u, r: {"note": "ffprobe not installed"}
        try:
            res = asyncio.run(at.AudioInfoTool().run({"path": "uploads/x.mp3"},
                                                     ToolContext(uid, {}, {})))
        finally:
            at.cap.audio_info_for_workspace = orig
        self.assertFalse(res.ok)
        self.assertIn("ffmpeg", res.content)






class CaptionerHttpTests(unittest.TestCase):
    """Admin endpoints for the second model, through the real app (needs web deps)."""

    def _login_admin(self, c):
        from hearth.main import ensure_bootstrap_admin
        pw = os.environ.get("BOOT_PW") or ensure_bootstrap_admin()
        r = c.post("/auth/login", data={"username": "admin", "password": pw})
        self.assertEqual(r.status_code, 200, f"admin login must work: {r.status_code}")
        return {"X-CSRF-Token": c.cookies.get("hearth_csrf", "")}

    def test_models_listing_shows_both_models(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            self._login_admin(c)
            r = c.get("/api/admin/models")
            self.assertEqual(r.status_code, 200, r.text)
            models = r.json()["models"]
            ids = [m["id"] for m in models]
            # The whole point of the endpoint: a second loaded model is never invisible.
            # Asserted as a SUBSET on purpose, and each entry must carry its own state.
            # The old assertion compared a literal list of panel rows, so adding the
            # image generator — a genuine improvement — "broke" the suite while telling
            # nobody anything about behaviour (AUDIT B1).
            self.assertIn("chat", ids)
            self.assertIn("captioner", ids)
            for m in models:
                self.assertTrue(m.get("id") and m.get("role"),
                                f"every model row must identify itself: {m!r}")

    def test_captioner_admin_routes_exist_and_require_csrf(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            self._login_admin(c)
            # Mutations need CSRF; a GET must not.
            self.assertEqual(c.post("/api/admin/captioner/stop").status_code, 403)
            r = c.post("/api/admin/captioner/stop", headers=self._login_admin(c))
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("stopped"))
            # Command preview works with no paths configured and explains itself.
            cmd = c.get("/api/admin/captioner/command")
            self.assertEqual(cmd.status_code, 200)
            self.assertFalse(cmd.json()["ok"])
            self.assertIn("captioner", cmd.json()["note"].lower())

    def test_captioner_test_endpoint_reports_a_reason_not_a_crash(self):
        # With no model configured the test probe must answer with an actionable note, never
        # a 500 — an admin clicks this precisely when something is wrong.
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            self._login_admin(c)
            r = c.get("/api/admin/captioner/test")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertFalse(body["ok"])
            self.assertTrue(body["note"])

    def test_captioner_settings_round_trip(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            H = self._login_admin(c)
            r = c.post("/api/admin/settings",
                       headers={**H, "Content-Type": "application/json"},
                       data=json.dumps({"captioner.enabled": True, "captioner.port": 8123,
                                        "captioner.chunk_seconds": 45}))
            self.assertEqual(r.status_code, 200, r.text)
            vals = c.get("/api/admin/settings").json()["values"]
            self.assertIs(vals["captioner.enabled"], True)
            self.assertEqual(vals["captioner.port"], 8123)
            self.assertEqual(vals["captioner.chunk_seconds"], 45)
            # Restore the off default so later tests in this process see a clean slate.
            c.post("/api/admin/settings", headers={**H, "Content-Type": "application/json"},
                   data=json.dumps({"captioner.enabled": False, "captioner.port": 8091,
                                    "captioner.chunk_seconds": 30}))

    def test_audio_tools_appear_in_the_tools_setting_list(self):
        from fastapi.testclient import TestClient
        from hearth.main import create_app
        with TestClient(create_app()) as c:
            self._login_admin(c)
            specs = c.get("/api/admin/settings").json()["specs"]
            keys = {s["key"] for s in specs}
            for k in ("tools.describe_audio", "tools.audio_info", "captioner.enabled",
                      "captioner.n_gpu_layers"):
                self.assertIn(k, keys)
class CaptionStreamAndPersistenceTests(unittest.TestCase):
    """DEC-061: describe_audio must stream like the upload path, and captions must survive.

    The two complaints these guard against: a tool-driven caption arrived with no live
    output at all (the captioner streamed internally and the text sat in a buffer), and a
    description vanished on refresh — plus later turns reached the model as a bare
    `[attached: x.mp3]` because build_messages only ever saw the current turn's dict.
    """

    def _settings(self, **over):
        from hearth import runtime_settings as rs
        s = {k: v.default for k, v in rs.SPEC_BY_KEY.items()}
        s.update({"captioner.enabled": True, "captioner.auto_route": True})
        s.update(over)
        return s

    def test_delta_throttle_batches_and_flushes(self):
        from hearth.tools.base import DeltaThrottle
        t = DeltaThrottle(min_chars=10, min_s=999)   # time-based flush disabled here
        self.assertEqual(t.add("abc"), "")            # below threshold: buffered
        out = t.add("defghij")                        # now >= 10 chars
        self.assertEqual(out, "abcdefghij")
        self.assertEqual(t.add("xyz"), "")            # buffered again
        self.assertEqual(t.flush(), "xyz")            # tail is never lost
        self.assertEqual(t.flush(), "")               # and flushing twice is harmless

    def test_delta_throttle_emits_on_time_even_when_short(self):
        import time as _t
        from hearth.tools.base import DeltaThrottle
        t = DeltaThrottle(min_chars=10_000, min_s=0.0)   # force the time path
        self.assertEqual(t.add("hi"), "hi")

    def test_tool_context_progress_sink_never_raises(self):
        from hearth.tools import ToolContext
        ctx = ToolContext("u", {}, {})
        # No sink attached: a tool reporting progress must be a silent no-op, not a crash.
        ctx.report_progress(type="notice", kind="caption_chunk", text="x")
        seen = []
        ctx.attach_progress(seen.append)
        ctx.report_progress(type="notice", kind="caption_chunk", text="hello")
        self.assertEqual(seen, [{"type": "notice", "kind": "caption_chunk", "text": "hello"}])

        # A sink that explodes must not take the tool down with it.
        def bad(ev): raise RuntimeError("queue gone")
        ctx.attach_progress(bad)
        ctx.report_progress(type="notice", kind="caption_chunk", text="still fine")
        ctx.attach_progress(None)
        ctx.report_progress(type="notice", kind="caption_chunk", text="ignored")

    def test_ui_preview_is_generous_but_bounded(self):
        from hearth.tools.base import ui_preview, UI_PREVIEW_MAX_CHARS
        self.assertEqual(ui_preview("short"), "short")
        clipped = ui_preview("x" * 50_000)
        # Bounded — but bounded WITH A MARKER. The exact-length assertion was the bug: it
        # locked in a silent cut, so a user could not tell a truncated chip from a short
        # tool result (AUDIT #13). The slice is still exactly UI_PREVIEW_MAX_CHARS long;
        # only the appended note extends it.
        self.assertTrue(clipped.startswith("x" * UI_PREVIEW_MAX_CHARS))
        self.assertIn("truncated for display", clipped)
        self.assertLess(len(clipped), UI_PREVIEW_MAX_CHARS + 200)
        self.assertEqual(ui_preview(None), "")

    def test_describe_audio_streams_progress_to_the_ui(self):
        # The regression: the tool called describe_file WITHOUT on_delta, so nothing reached
        # the client during a multi-minute listen. Now it must emit start/chunks/done.
        import asyncio
        from hearth.tools.audio_tools import DescribeAudioTool
        from hearth.tools import ToolContext
        from hearth import captioner as cap, workspaces as ws
        uid = "capstreamuser"
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        ws.write_text(uid, "uploads/clip.mp3", "pretend audio")

        seen = []

        async def fake_describe(user_id, rel, extra_prompt="", settings=None,
                                cancel=None, on_delta=None):
            self.assertIsNotNone(on_delta, "the tool must pass on_delta or nothing streams")
            for piece in ["a long ", "caption ", "about voices "]:
                on_delta(piece)
            return {"caption": "two people talk", "chunks": 1, "duration_s": 4.0,
                    "backend": "server", "notes": []}

        orig = cap.describe_file
        orig_pre = cap.preflight
        # preflight() checks for a real ffmpeg/captioner install, which CI does not have;
        # this test is about the tool's STREAMING contract, so pretend it is configured.
        cap.preflight = lambda settings: ""
        cap.describe_file = fake_describe
        try:
            ctx = ToolContext(uid, self._settings(), {})
            ctx.attach_progress(seen.append)
            res = asyncio.run(DescribeAudioTool().run({"path": "uploads/clip.mp3"}, ctx))
        finally:
            cap.describe_file = orig
            cap.preflight = orig_pre
        self.assertTrue(res.ok)
        kinds = [e.get("kind") for e in seen]
        self.assertIn("caption_start", kinds)
        self.assertIn("caption_done", kinds)
        streamed = "".join(e["text"] for e in seen if e.get("kind") == "caption_chunk")
        # The tail must be flushed, so the streamed text is complete, not truncated.
        self.assertEqual(streamed, "a long caption about voices ")

    def test_describe_audio_reports_failure_completion(self):
        import asyncio
        from hearth.tools.audio_tools import DescribeAudioTool
        from hearth.tools import ToolContext
        from hearth import captioner as cap, workspaces as ws
        uid = "capstreamfail"
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)
        ws.write_text(uid, "uploads/bad.mp3", "pretend audio")

        async def boom(user_id, rel, extra_prompt="", settings=None, cancel=None,
                       on_delta=None):
            raise cap.CaptionError("captioner model file not found")

        seen = []
        orig = cap.describe_file
        orig_pre = cap.preflight
        cap.preflight = lambda settings: ""   # see note in the streaming test above
        cap.describe_file = boom
        try:
            ctx = ToolContext(uid, self._settings(), {})
            ctx.attach_progress(seen.append)
            res = asyncio.run(DescribeAudioTool().run({"path": "uploads/bad.mp3"}, ctx))
        finally:
            cap.describe_file = orig
            cap.preflight = orig_pre
        self.assertFalse(res.ok)
        done = [e for e in seen if e.get("kind") == "caption_done"]
        self.assertTrue(done, "a failed caption must still close its progress block")
        self.assertFalse(done[0]["ok"])

    def test_caption_meta_round_trip(self):
        import json
        from hearth import orchestrator as orch
        meta = json.dumps({"attachments": [{"rel_path": "uploads/a.mp3", "name": "a.mp3"}],
                           "captions": {"uploads/a.mp3": {"text": "a voice", "name": "a.mp3",
                                                          "ok": True}}})
        self.assertEqual(orch.captions_from_meta(meta), {"uploads/a.mp3": "a voice"})
        self.assertEqual(orch.caption_state(meta),
                         {"uploads/a.mp3": {"name": "a.mp3", "ok": True}})
        # Junk / missing meta must read as "no captions", never raise.
        self.assertEqual(orch.captions_from_meta(""), {})
        self.assertEqual(orch.captions_from_meta("not json"), {})
        self.assertEqual(orch.caption_state(None), {})
        # A plain-string value is tolerated rather than dropped.
        self.assertEqual(orch.captions_from_meta(json.dumps({"captions": {"x": "t"}})),
                         {"x": "t"})

    def test_build_messages_uses_stored_captions_for_older_attachments(self):
        """The core persistence claim: a caption from an EARLIER message stays in context.

        Before DEC-061 the model saw `[attached: x.mp3]` with no description on every turn
        after the one that produced it, because build_messages only received the current
        turn's dict and never consulted the row's stored captions.
        """
        import json
        from hearth import orchestrator as orch
        uid = "capprogressuser"
        from hearth import workspaces as ws
        ws.root_for(uid).mkdir(parents=True, exist_ok=True)

        meta = json.dumps({"attachments": [{"name": "a.mp3", "mime": "audio/mpeg",
                                           "rel_path": "uploads/a.mp3"}],
                           "captions": {"uploads/a.mp3": {"text": "CAPTURED EARLIER",
                                                          "name": "a.mp3", "ok": True}}})
        # No turn-level captions at all — exactly the later-turn case.
        content = orch._attachment_content(uid, json.loads(meta)["attachments"],
                                           "[attached: a.mp3]",
                                           orch.captions_from_meta(meta))
        self.assertIn("CAPTURED EARLIER", content)






class AuditRegressionTests(unittest.TestCase):
    """Regressions for the silent-decision audit (AUDIT_SILENT_DECISIONS.md).

    Each test names the finding it locks down, so a future change that reintroduces a
    silent cap, a swallowed failure, or a hard-coded policy has a failing test to argue
    with rather than a paragraph of prose.
    """

    def _ctx(self, uid, settings=None, conv="c"):
        from hearth.tools import ToolContext
        return ToolContext(uid, settings or {}, conversation_id=conv)

    def test_fs_list_announces_its_cap(self):
        """AUDIT #3 — fs_list is the fallback for a capped prompt listing; it used to
        stop at 500 silently, so both layers lied at once on a big folder."""
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsListTool, FsWriteTool
        from hearth import workspaces as ws
        uid = "listcap"
        for i in range(6):
            ws.write_text(uid, f"d{i}.txt", "x")
        capped = asyncio.run(FsListTool().run({"path": "."}, self._ctx(uid, {"agent.fs_list_entries": 3})))
        self.assertTrue(capped.ok)
        self.assertIn("truncated at 3 entries", capped.content)
        self.assertIn("more exist here", capped.content)
        self.assertTrue(capped.meta.get("truncated"))
        self.assertEqual(len([l for l in capped.content.splitlines() if "truncated" not in l]), 3)
        plain = asyncio.run(FsListTool().run({"path": "."}, self._ctx(uid, {})))
        self.assertNotIn("truncated", plain.content)
        self.assertFalse(plain.meta.get("truncated"))

    def test_zip_pack_reports_omitted_paths(self):
        """AUDIT #18 — 250 named paths produced a 200-file zip and a reply saying
        'packed 200 files', which reads as complete."""
        import asyncio
        from hearth.db import init_db
        init_db()
        from hearth.tools.fs_tools import FsWriteTool, ZipPackTool
        uid = "zipcap"
        for i in range(5):
            asyncio.run(FsWriteTool().run({"path": f"f{i}.txt", "content": str(i)}, self._ctx(uid)))
        res = asyncio.run(ZipPackTool().run(
            {"paths": [f"f{i}.txt" for i in range(5)], "zip_name": "cap.zip"},
            self._ctx(uid, {"tools.zip_pack.max_files": 2})))
        self.assertTrue(res.ok, res.content)
        self.assertIn("packed 2 files", res.content)
        self.assertIn("3 path(s) were not included", res.content)
        self.assertEqual(res.meta.get("skipped"), 3)

    def test_upload_reports_when_it_replaced_different_bytes(self):
        """AUDIT #2 — attaching the same filename twice destroyed the first file with no
        signal at all. The replacement still happens (DEC-040 §2), but it is now reported."""
        from hearth.db import init_db
        init_db()
        from fastapi.testclient import TestClient
        from hearth.main import create_app, ensure_bootstrap_admin
        pw = os.environ.get("BOOT_PW") or ensure_bootstrap_admin()
        with TestClient(create_app()) as c:
            r = c.post("/auth/login", data={"username": "admin", "password": pw})
            self.assertEqual(r.status_code, 200)
            csrf = {"X-CSRF-Token": c.cookies.get("hearth_csrf", "")}
            # First attach: nothing existed before.
            r1 = c.post("/api/upload", headers=csrf,
                        files={"files": ("dup.txt", b"first contents", "text/plain")})
            self.assertEqual(r1.status_code, 200, r1.text)
            a1 = r1.json()["artifacts"][0]
            self.assertFalse(a1["replaced"])
            self.assertFalse(a1["replaced_different_content"])
            # Same name, DIFFERENT bytes: replaced, and the response says so.
            r2 = c.post("/api/upload", headers=csrf,
                        files={"files": ("dup.txt", b"completely different", "text/plain")})
            a2 = r2.json()["artifacts"][0]
            self.assertTrue(a2["replaced"])
            self.assertTrue(a2["replaced_different_content"])
            self.assertNotEqual(a1["id"], a2["id"])
            # Identical bytes again: replaced is true, but no false alarm about content.
            r3 = c.post("/api/upload", headers=csrf,
                        files={"files": ("dup.txt", b"completely different", "text/plain")})
            a3 = r3.json()["artifacts"][0]
            self.assertTrue(a3["replaced"])
            self.assertFalse(a3["replaced_different_content"])
            # The limit travels with the response so chat.js never needs its own copy.
            self.assertEqual(r1.json()["max_bytes"], 25 * 1024 * 1024)

    def test_upload_limit_is_an_admin_setting(self):
        """AUDIT #8 — 25 MiB was a source constant with 'Configurable later' above it,
        duplicated in chat.js."""
        from hearth.runtime_settings import SPEC_BY_KEY, set_many, get
        from hearth.routes_chat import upload_limit_bytes
        spec = SPEC_BY_KEY.get("uploads.max_bytes")
        self.assertIsNotNone(spec, "uploads.max_bytes must be an admin-editable setting")
        self.assertEqual(spec.default, 25 * 1024 * 1024)
        self.assertEqual(upload_limit_bytes(), 25 * 1024 * 1024)
        set_many({"uploads.max_bytes": str(3 * 1024 * 1024)})
        try:
            self.assertEqual(get("uploads.max_bytes"), 3 * 1024 * 1024)
            self.assertEqual(upload_limit_bytes(), 3 * 1024 * 1024)
        finally:
            set_many({"uploads.max_bytes": str(25 * 1024 * 1024)})

    def test_provider_registry_and_stub_fallback_note(self):
        """AUDIT #7 — provider choice was an if-chain, and a dead llama-server meant
        silently talking to a fake model."""
        import asyncio
        from hearth.manager import ModelManager, PROVIDERS, backend_choices
        from hearth.llm.stub import StubProvider
        self.assertIn("stub", PROVIDERS)
        self.assertIn("llama-server", PROVIDERS)
        self.assertEqual(backend_choices(), sorted(PROVIDERS))
        m = ModelManager()
        # A configured llama-server that is not answering must be announced, not hidden.
        # (An unknown backend name cannot reach get_provider() at all: SettingSpec._coerce
        # maps a value outside `choices` back to the default before it gets here — so this
        # is also why model.backend gains no new option without its registry entry.)
        from hearth import runtime_settings as rs
        from hearth import manager as mgr_mod
        orig_ensure = m.ensure_server

        async def dead(*a, **k):
            return ""
        m.ensure_server = dead  # type: ignore[assignment]
        rs.set_many({"model.backend": "llama-server"})
        try:
            p = asyncio.run(m.get_provider())
            self.assertIsInstance(p, StubProvider)
            self.assertIn("llama-server", m.last_fallback_note)
            self.assertIn("STUB", m.last_fallback_note.upper())
        finally:
            rs.set_many({"model.backend": "stub"})
        # Deliberately the stub -> no fallback note (nothing was substituted).
        p2 = asyncio.run(m.get_provider())
        self.assertIsInstance(p2, StubProvider)
        self.assertEqual(m.last_fallback_note, "")

    def test_provider_capabilities_replace_name_sniffing(self):
        """AUDIT Part B — `getattr(provider,'name')=='llama-server'` was a capability
        test written as string equality."""
        from hearth.llm.stub import StubProvider
        from hearth.llm.llama_server import LlamaServerProvider
        stub = StubProvider().capabilities()
        self.assertTrue(stub["is_stub"])
        self.assertFalse(stub["exposes_log_progress"])
        llama = LlamaServerProvider("http://127.0.0.1:1").capabilities()
        self.assertTrue(llama["exposes_log_progress"])
        self.assertFalse(llama["is_stub"])

    def test_caption_cache_evicts_least_recently_used(self):
        """AUDIT #12 — eviction followed insertion order, so with >64 chats a hot
        conversation lost its caption and the model got `[attached: x.mp3]` alone while
        the UI still showed a description."""
        from hearth import orchestrator as orch
        saved = dict(orch._captions)
        try:
            orch._captions.clear()
            for i in range(orch._CAPTION_CACHE_MAX_CONVOS):
                orch._remember_caption("u", f"conv{i}", "a.mp3", f"cap{i}")
            # Touch the OLDEST entry so it becomes the most recently used.
            first = orch.get_caption("u", "conv0", "a.mp3")
            self.assertEqual(first, "cap0")
            orch._remember_caption("u", "overflow", "a.mp3", "new")
            self.assertTrue(orch.get_caption("u", "conv0", "a.mp3"),
                            "a recently-used chat must not be the eviction victim")
            self.assertEqual(orch.get_caption("u", "conv1", "a.mp3"), "",
                            "the least recently used chat is the one that goes")
        finally:
            orch._captions.clear()
            orch._captions.update(saved)

    def test_tool_labels_are_derived_from_the_classes(self):
        """AUDIT #17 — labels lived in a second hand-maintained registry; add a tool and
        forget the entry and Admin rendered nothing for it."""
        from hearth.tools import TOOLS_BY_NAME, TOOL_LABELS_BY_NAME
        self.assertEqual(set(TOOL_LABELS_BY_NAME), set(TOOLS_BY_NAME))
        # Every enable/approval spec must resolve to a real label (no KeyError at import,
        # no blank row in the admin UI).
        from hearth.runtime_settings import ALL_SPECS
        for s in ALL_SPECS:
            if s.key.startswith("tools.") and s.type == "bool" and s.key.count(".") == 1:
                name = s.key.split(".", 1)[1]
                if name in TOOLS_BY_NAME:
                    # The label must carry the tool's own human-readable name, and its
                    # enable spec must be derived rather than hand-keyed.
                    self.assertTrue(s.label.endswith(": enable"), s.label)
                    self.assertNotEqual(s.label, f"{name}: enable",
                                        f"{name} has no label — Admin would show a bare key")

    def test_stale_approval_is_not_executed(self):
        """AUDIT #4 — an approved card could sit for a day and then run against a
        workspace that had moved on."""
        from hearth import orchestrator as orch
        old = {"created_at": int(__import__("time").time()) - 7200}
        fresh = {"created_at": int(__import__("time").time())}
        limit = {"agent.pending_max_age_s": 3600}
        self.assertTrue(orch._pending_is_stale(old, limit))
        self.assertFalse(orch._pending_is_stale(fresh, limit))
        # Setting it to 0 disables the check entirely (operator's explicit choice).
        self.assertFalse(orch._pending_is_stale(old, {"agent.pending_max_age_s": 0}))
        # A row with no timestamp is never treated as stale — approvals are not deleted
        # on account of a missing field.
        self.assertFalse(orch._pending_is_stale({}, limit))

    def test_pdf_read_announces_its_page_clamp(self):
        """AUDIT #6 — asking for pages 1-40 returned 1-6 with no complaint."""
        from pathlib import Path
        from hearth.tools.media_tools import PdfReadTool
        src = (Path(__file__).resolve().parent.parent / "hearth/tools/media_tools.py").read_text()
        self.assertIn("you asked for pages up to", src)
        self.assertLessEqual(PdfReadTool.MAX_PAGES, 8)

    def test_no_dead_duplicate_of_progress_event(self):
        """AUDIT B2 — 43 unreachable lines duplicating _progress_event sat after
        _merge_progress's return, where a future ETA fix would silently do nothing."""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "hearth/orchestrator.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_progress":
                returns = [i for i, st in enumerate(node.body) if isinstance(st, ast.Return)]
                self.assertTrue(returns)
                last_return = returns[-1]
                trailing = [type(st).__name__ for st in node.body[last_return + 1:]]
                self.assertEqual(trailing, [],
                                 f"unreachable statements after the final return: {trailing}")

    def test_single_worker_assumption_is_warned_about(self):
        """AUDIT #11 — README told operators to run `uvicorn --workers`, which silently
        breaks Stop, steering and captions (all per-process dicts)."""
        import io, contextlib
        from hearth import main as main_mod
        buf = io.StringIO()
        old = os.environ.get("WEB_CONCURRENCY")
        try:
            os.environ["WEB_CONCURRENCY"] = "4"
            with contextlib.redirect_stdout(buf):
                main_mod._warn_if_multi_worker()
            self.assertIn("single-process", buf.getvalue())
            self.assertIn("Stop", buf.getvalue())
        finally:
            if old is None:
                os.environ.pop("WEB_CONCURRENCY", None)
            else:
                os.environ["WEB_CONCURRENCY"] = old
        buf2 = io.StringIO()
        try:
            os.environ.pop("WEB_CONCURRENCY", None)
            with contextlib.redirect_stdout(buf2):
                main_mod._warn_if_multi_worker()
            self.assertEqual(buf2.getvalue(), "", "no warning for the supported single worker")
        finally:
            if old is not None:
                os.environ["WEB_CONCURRENCY"] = old

    def test_hardcoded_policies_now_have_settings(self):
        """AUDIT Part B, 'Settings / config' — eight policy values lived outside the
        settings system that exists to govern them. These are the ones moved in."""
        from hearth.runtime_settings import SPEC_BY_KEY
        for key in ("uploads.max_bytes", "agent.workspace_listing_entries",
                    "agent.fs_list_entries", "tools.zip_pack.max_files",
                    "agent.pending_max_age_s"):
            self.assertIn(key, SPEC_BY_KEY, f"{key} should be admin-editable")
class DiffusionQwenImageTests(unittest.TestCase):
    """Image generation supports ONE architecture — Qwen Image / Qwen Image 2.1 — and the
    contract is the three files plus their flags. Verified against a real `sd-cli` build
    (master-1106-532d2c1) with qwen_image_2.1_Q8_0.gguf + Qwen3VL-8B-Instruct-Q4_K_M.gguf +
    diffusion_pytorch_model.safetensors: those three are required, `--model` is rejected for
    them, and a missing one fails as hundreds of '… not in model metadata' lines. These tests
    exist so a future 'generalise it' edit cannot silently re-break the only setup that works.
    """

    def setUp(self):
        from hearth import diffusion as sd
        self.sd = sd
        self.tmp = tempfile.mkdtemp(prefix="sd-models-")
        self.dit = os.path.join(self.tmp, "qwen_image_2.1_Q8_0.gguf")
        self.llm = os.path.join(self.tmp, "Qwen3VL-8B-Instruct-Q4_K_M.gguf")
        self.vae = os.path.join(self.tmp, "diffusion_pytorch_model.safetensors")
        for p in (self.dit, self.llm, self.vae):
            with open(p, "wb") as fh:
                fh.write(b"x")
        self.bin = os.path.join(self.tmp, "sd-cli")
        with open(self.bin, "wb") as fh:
            fh.write(b"#!/bin/sh\nexit 0\n")
        os.chmod(self.bin, 0o755)

    def s(self, **over):
        base = {"diffusion.enabled": True, "diffusion.cli_bin": self.bin,
                "diffusion.model_dir": self.tmp}
        base.update(over)
        return base

    # -- discovery ------------------------------------------------------------
    def test_model_folder_alone_resolves_all_three_files(self):
        p = self.sd.resolve_paths(self.s())
        self.assertEqual(p["models"], {"dit": self.dit, "llm": self.llm, "vae": self.vae})
        self.assertEqual(set(p["sources"].values()), {"folder"})
        self.assertEqual(self.sd.preflight(self.s()), "")

    def test_discovery_never_gives_the_llm_file_to_the_diffusion_role(self):
        """`qwen` appears in BOTH file names; the encoder must not claim the DiT .gguf."""
        p = self.sd.resolve_paths(self.s())
        self.assertEqual(p["models"]["dit"], self.dit)
        self.assertEqual(p["models"]["llm"], self.llm)
        self.assertNotEqual(p["models"]["dit"], p["models"]["llm"])

    def test_explicit_path_wins_over_the_folder(self):
        other = os.path.join(self.tmp, "chosen-vae.safetensors")
        with open(other, "wb") as fh:
            fh.write(b"x")
        p = self.sd.resolve_paths(self.s(**{"diffusion.vae_path": other}))
        self.assertEqual(p["models"]["vae"], other)
        self.assertEqual(p["sources"]["vae"], "setting")

    def test_legacy_model_path_key_still_feeds_the_diffusion_role(self):
        """Older installs stored the main model under diffusion.model_path — don't orphan it."""
        s = {"diffusion.enabled": True, "diffusion.cli_bin": self.bin,
             "diffusion.model_path": self.dit, "diffusion.vae": self.vae,
             "diffusion.llm_path": self.llm}
        p = self.sd.resolve_paths(s)
        self.assertEqual(p["models"]["dit"], self.dit)
        self.assertEqual(p["sources"]["dit"], "legacy")
        self.assertEqual(self.sd.preflight(s), "")

    # -- preflight ------------------------------------------------------------
    def test_missing_file_names_the_setting_not_a_generic_failure(self):
        os.remove(self.vae)
        msg = self.sd.preflight(self.s())
        self.assertIn("VAE (.safetensors)", msg)
        self.assertNotIn("Diffusion model", msg, "only the missing file should be listed")
        self.assertIn("Admin → Images", msg, "the refusal must say where to fix it")

    def test_no_files_at_all_lists_every_role(self):
        s = {"diffusion.enabled": True, "diffusion.cli_bin": self.bin}
        msg = self.sd.preflight(s)
        for label in ("Diffusion model", "Text-encoder LLM", "VAE"):
            self.assertIn(label, msg)

    def test_disabled_switch_is_reported_before_any_file(self):
        msg = self.sd.preflight({"diffusion.enabled": False})
        self.assertIn("disabled", msg)

    # -- argv -----------------------------------------------------------------
    def test_argv_uses_the_three_dedicated_flags_and_never_model(self):
        argv = self.sd.build_argv(self.s(), "a lighthouse at dusk", "/tmp/out", steps=0, batch=2)
        for flag in ("--diffusion-model", "--llm", "--vae"):
            self.assertIn(flag, argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("-m", argv)
        self.assertEqual(argv[argv.index("--diffusion-model") + 1], self.dit)
        self.assertEqual(argv[argv.index("--llm") + 1], self.llm)
        self.assertEqual(argv[argv.index("--vae") + 1], self.vae)

    def test_argv_pins_the_flags_that_make_qwen_image_work(self):
        argv = self.sd.build_argv(self.s(), "x", "/tmp/out")
        i = argv.index("--sampling-method")
        self.assertEqual(argv[i + 1], "euler")
        self.assertIn("--diffusion-fa", argv)
        j = argv.index("--preview")
        self.assertEqual(argv[j + 1], "none",
                         "previews would write ./preview.png into Hearth's cwd and cost VAE decodes")
        self.assertTrue(any(a.endswith("hearth_%03d.png") for a in argv))

    def test_build_argv_refuses_before_launching_when_a_file_is_missing(self):
        os.remove(self.llm)
        with self.assertRaises(self.sd.DiffusionError) as cm:
            self.sd.build_argv(self.s(), "x", "/tmp/out")
        self.assertIn("Text-encoder LLM", str(cm.exception))

    def test_seed_zero_is_passed_and_random_is_omitted(self):
        """`-s 0` is a real seed; `-s -1` would be swallowed as the next flag's value."""
        argv = self.sd.build_argv(self.s(), "x", "/tmp/out", seed=0)
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "0")
        self.assertEqual(self.sd.Diffusion._seed_from_argv(argv), 0)
        rand = self.sd.build_argv(self.s(), "x", "/tmp/out", seed=-1)
        self.assertNotIn("-s", rand)
        self.assertIsNone(self.sd.Diffusion._seed_from_argv(rand))

    def test_numbers_are_clamped_not_trusted(self):
        argv = self.sd.build_argv(self.s(), "x", "/tmp/out", steps=10_000, cfg=999.0,
                                  width=99_000, height=8, batch=50)
        self.assertEqual(argv[argv.index("--steps") + 1], str(self.sd.MAX_STEPS))
        self.assertEqual(argv[argv.index("--cfg-scale") + 1], str(int(self.sd.MAX_CFG)))
        self.assertEqual(argv[argv.index("-W") + 1], str(self.sd.MAX_EDGE))
        self.assertEqual(argv[argv.index("-H") + 1], str(self.sd.MIN_EDGE))
        self.assertEqual(argv[argv.index("-b") + 1], str(self.sd.MAX_BATCH_PER_IMAGE))

    # -- failure translation ---------------------------------------------------
    def test_sd_cli_log_signatures_are_translated(self):
        cases = {
            "[E] get sd version from file failed: 'x.gguf' --- diffusion_engine.cpp:979": "diffusion",
            "[E] VAE tensor 'first_stage_model.encoder.head.0.gamma' not in model metadata": "VAE",
            "[E] Conditioner model tensor 'text_encoders.llm.model.norm.weight' nope": "Text-encoder",
        }
        for tail, want in cases.items():
            hint = self.sd.explain_failure(tail)
            self.assertTrue(hint, f"no hint for {tail[:40]}")
            self.assertIn(want.lower(), hint.lower())
        self.assertEqual(self.sd.explain_failure("something unrecognised"), "")

    # -- status / settings surface --------------------------------------------
    def test_status_reports_each_file_separately(self):
        st = self.sd.get_diffusion().status(self.s())
        self.assertTrue(st["configured"])
        self.assertEqual(st["present"], {"dit": True, "llm": True, "vae": True})
        os.remove(self.dit)
        st2 = self.sd.get_diffusion().status(self.s())
        self.assertFalse(st2["configured"])
        self.assertFalse(st2["present"]["dit"])
        self.assertTrue(st2["present"]["llm"], "one missing file must not hide the other two")

    def test_settings_offered_match_the_supported_setup(self):
        from hearth.runtime_settings import SPEC_BY_KEY
        for key in ("diffusion.model_dir", "diffusion.dit_path", "diffusion.llm_path",
                    "diffusion.vae_path"):
            self.assertIn(key, SPEC_BY_KEY, f"{key} should be admin-editable")
        for gone in ("diffusion.clip_l", "diffusion.clip_g", "diffusion.t5xxl",
                     "diffusion.lora_dir", "diffusion.preview"):
            self.assertNotIn(gone, SPEC_BY_KEY,
                             f"{gone} belongs to architectures Hearth no longer claims")

    def test_tool_label_says_qwen_image(self):
        from hearth.tools.image_tools import GenerateImageTool
        self.assertIn("Qwen Image", GenerateImageTool.label)
class DiffusionCancellationTests(unittest.TestCase):
    """Stop must reach a RUNNING sd-cli. `stop_active()` used to read

        if p is None or p.poll() is None: return False

    and a running child has `poll() is None` — so Stop sent no signal at all, returned in
    0.00 s, and the generation ran to completion holding the GPU. Measured before the fix:
    stop_active() -> False, child alive 42 s later. These tests use a fake CLI that ignores
    nothing but signals, so they fail loudly if the early-return ever comes back.
    """

    def setUp(self):
        from hearth import diffusion as sd
        self.sd = sd
        # One folder per role, so a filename can only match one slot. resolve_paths() does
        # prefer an explicit path over `model_dir`, but keeping the candidates disjoint means
        # argv is checked against exactly the file this test created.
        self.bins = {}
        for role, name in (("dit", "qwen_image.gguf"), ("llm", "qwen3vl.gguf"),
                           ("vae", "diffusion_pytorch_model.safetensors")):
            d = tempfile.mkdtemp(prefix=f"sd-{role}-")
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(b"x")
            self.bins[role] = d
        # A wrapper that spawns a grandchild and waits: Hearth must take out the group.
        self.dir = tempfile.mkdtemp(prefix="sd-kill-")
        self.wrapper = os.path.join(self.dir, "sd-cli")
        with open(self.wrapper, "w") as fh:
            fh.write("#!/bin/sh\n( sleep 120 & wait )\n")
        os.chmod(self.wrapper, 0o755)
        self.settings = {"diffusion.enabled": True, "diffusion.cli_bin": self.wrapper,
                         "diffusion.dit_path": os.path.join(self.bins["dit"], "qwen_image.gguf"),
                         "diffusion.llm_path": os.path.join(self.bins["llm"], "qwen3vl.gguf"),
                         "diffusion.vae_path": os.path.join(
                             self.bins["vae"], "diffusion_pytorch_model.safetensors"),
                         "diffusion.timeout_s": 300}

    def tearDown(self):
        import signal as _sig
        for pid in self._group_members():
            try:
                os.killpg(os.getpgid(pid), _sig.SIGKILL)
            except OSError:
                pass

    def _group_members(self):
        """Live processes whose PGID leader is our wrapper (i.e. Hearth created them)."""
        r = subprocess.run(["ps", "-eo", "pid,pgid,args", "--no-headers"],
                           capture_output=True, text=True)
        out = []
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or "sleep 120" not in parts[2]:
                continue
            try:
                out.append(int(parts[0]))
            except ValueError:
                pass
        return out

    def _start(self):
        d = self.sd.get_diffusion()
        box = {}
        th = threading.Thread(
            target=lambda: box.setdefault(
                "err", self._run(d, box) or "finished"), daemon=True)
        th.start()
        return d, th, box

    def _run(self, d, box):
        try:
            d.generate_sync(self.settings, "x")
            return None
        except self.sd.DiffusionError as e:
            box["err"] = str(e)
            return str(e)

    def test_stop_kills_a_running_generation_and_its_group(self):
        d, th, box = self._start()
        deadline = time.time() + 10
        while time.time() < deadline and not self._group_members():
            time.sleep(0.2)
        self.assertTrue(self._group_members(), "the fake sd-cli never started")
        t0 = time.time()
        stopped = d.stop_active()
        # Killing takes real time (SIGTERM, grace, SIGKILL) — a 0.0 s return means it did
        # nothing, which is exactly the bug.
        self.assertGreater(time.time() - t0, 0.05,
                           "stop_active returned instantly: it signalled nothing")
        self.assertTrue(stopped, "stop_active claims the child survived")
        th.join(30)
        self.assertFalse(th.is_alive(), "generate_sync never unwound after Stop")
        time.sleep(0.5)
        self.assertEqual(self._group_members(), [],
                         "processes survived Stop — they would hold the GPU")

    def test_stop_during_model_load_is_honoured(self):
        """A Stop that lands while sd-cli is still being spawned must not be lost.

        The window is real: `stop_requested` is cleared at the start of generate_sync() and
        the child only becomes reachable a moment later, so `_set_active()` has to honour a
        Stop that arrived in between — otherwise pressing Stop during model load silently
        lets a full generation run afterwards.
        """
        d = self.sd.get_diffusion()
        box = {}
        th = threading.Thread(target=lambda: self._run(d, box), daemon=True)
        th.start()

        # Wait for the exact moment the child exists but has produced nothing yet, then Stop
        # from this thread — racing _set_active() the same way a user's Stop does.
        deadline = time.time() + 10
        while time.time() < deadline and not self._group_members():
            time.sleep(0.02)
        d.stop_active()
        th.join(40)
        self.assertFalse(th.is_alive(), "generate_sync never unwound after a Stop")
        time.sleep(0.5)
        self.assertEqual(self._group_members(), [],
                         f"processes survived a Stop; result was: {box.get('err')}")
        self.assertTrue(box.get("err"), "a stopped run must report an error, not success")

    def test_a_stopped_run_does_not_blame_the_model_files(self):
        """Signal exits used to carry 'the model files must all come from the same package'."""
        d, th, box = self._start()
        deadline = time.time() + 10
        while time.time() < deadline and not self._group_members():
            time.sleep(0.2)
        d.stop_active()
        th.join(30)
        msg = box.get("err", "")
        self.assertTrue(msg, "no error reported for a stopped generation")
        self.assertNotIn("same Qwen Image package", msg)
        self.assertIn("stopped by Hearth", msg)


class DiffusionEditProfileTests(unittest.TestCase):
    """Qwen Image Edit is a SECOND profile in the same runner (DEC-078): its own settings
    prefix (`image_edit.*`), its own file slots, an optional vision mmproj, `-r` for the
    reference image (NOT img2img `-i`), CFG 2.5 and `--flow-shift 3`. Generation must stay
    byte-identical. These lock the contract so neither profile can silently drift into the
    other's flags or settings.
    """

    def setUp(self):
        from hearth import diffusion as sd
        self.sd = sd
        # Separate folders for gen and edit so a filename can only match one profile, and
        # discovery is checked against exactly the file this test created.
        self.gen_dir = tempfile.mkdtemp(prefix="sd-gen-")
        with open(os.path.join(self.gen_dir, "qwen_image_2.1_Q8_0.gguf"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(self.gen_dir, "Qwen3VL-8B-Instruct-Q4_K_M.gguf"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(self.gen_dir, "diffusion_pytorch_model.safetensors"), "wb") as fh:
            fh.write(b"x")

        self.edit_dir = tempfile.mkdtemp(prefix="sd-edit-")
        self.edit_dit = os.path.join(self.edit_dir, "Qwen_Image_Edit-Q8_0.gguf")
        self.edit_llm = os.path.join(self.edit_dir, "Qwen2.5-VL-7B-Instruct-Q8_0.gguf")
        self.edit_vae = os.path.join(self.edit_dir, "qwen_image_vae.safetensors")
        for p in (self.edit_dit, self.edit_llm, self.edit_vae):
            with open(p, "wb") as fh:
                fh.write(b"x")

        self.bin = os.path.join(tempfile.mkdtemp(prefix="sd-bin-"), "sd-cli")
        with open(self.bin, "wb") as fh:
            fh.write(b"#!/bin/sh\nexit 0\n")
        os.chmod(self.bin, 0o755)

    def gen_settings(self, **over):
        base = {"diffusion.enabled": True, "diffusion.cli_bin": self.bin,
                "diffusion.model_dir": self.gen_dir}
        base.update(over)
        return base

    def edit_settings(self, **over):
        base = {"image_edit.enabled": True, "image_edit.cli_bin": self.bin,
                "image_edit.model_dir": self.edit_dir}
        base.update(over)
        return base

    # -- independent settings --------------------------------------------------
    def test_edit_profile_reads_its_own_prefix_not_generation(self):
        p = self.sd.resolve_paths(self.edit_settings(), self.sd.PROFILE_EDIT)
        self.assertEqual(p["models"]["dit"], self.edit_dit)
        self.assertEqual(p["models"]["llm"], self.edit_llm)
        self.assertEqual(p["models"]["vae"], self.edit_vae)
        # Generation, given the same dict, must NOT pick up the edit files.
        pg = self.sd.resolve_paths(self.edit_settings(), self.sd.PROFILE_GEN)
        self.assertEqual(pg["models"]["dit"], "")

    def test_edit_folder_discovery_finds_the_edit_dit_by_name(self):
        p = self.sd.resolve_paths(self.edit_settings(), self.sd.PROFILE_EDIT)
        self.assertEqual(p["sources"]["dit"], "folder")
        self.assertEqual(p["models"]["dit"], self.edit_dit)

    # -- optional vision mmproj ------------------------------------------------
    def test_optional_vision_mmproj_is_not_required(self):
        # No llm_vision set → still preflight-clean and configured (original/2511 fold it in).
        self.assertEqual(self.sd.preflight(self.edit_settings(), self.sd.PROFILE_EDIT), "")
        st = self.sd.get_diffusion().status(self.edit_settings(), self.sd.PROFILE_EDIT)
        self.assertTrue(st["configured"])
        self.assertIn("llm_vision", st["present"])   # reported, but not required

    def test_vision_mmproj_emitted_only_when_set(self):
        argv = self.sd.build_argv(self.edit_settings(), "make it night", "/tmp/out",
                                  ref_image="/tmp/in.png", profile=self.sd.PROFILE_EDIT)
        self.assertNotIn("--llm_vision", argv)
        mm = os.path.join(self.edit_dir, "Qwen2.5-VL-7B-Instruct.mmproj-Q8_0.gguf")
        with open(mm, "wb") as fh:
            fh.write(b"x")
        argv2 = self.sd.build_argv(self.edit_settings(**{"image_edit.llm_vision_path": mm}),
                                   "make it night", "/tmp/out", ref_image="/tmp/in.png",
                                   profile=self.sd.PROFILE_EDIT)
        self.assertIn("--llm_vision", argv2)
        self.assertEqual(argv2[argv2.index("--llm_vision") + 1], mm)

    # -- argv contract ---------------------------------------------------------
    def test_edit_argv_uses_reference_flag_and_edit_sampling(self):
        argv = self.sd.build_argv(self.edit_settings(), "remove the car", "/tmp/out",
                                  ref_image="/tmp/in.png", profile=self.sd.PROFILE_EDIT)
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-r") + 1], "/tmp/in.png")
        self.assertNotIn("-i", argv, "editing is reference conditioning, not img2img")
        i = argv.index("--cfg-scale")
        self.assertEqual(argv[i + 1], "2.5", "edit CFG default is 2.5, not generation's 4.5")
        j = argv.index("--flow-shift")
        self.assertEqual(argv[j + 1], "3")

    def test_generation_argv_unchanged_no_flowshift_reference(self):
        argv = self.sd.build_argv(self.gen_settings(), "a fox", "/tmp/out", steps=0)
        self.assertNotIn("--flow-shift", argv)
        self.assertNotIn("-r", argv)
        i = argv.index("--cfg-scale")
        self.assertEqual(argv[i + 1], "4.5")

    def test_zero_cond_t_off_by_default_on_when_set(self):
        # Original Qwen-Image-Edit: no --model-args by default.
        argv = self.sd.build_argv(self.edit_settings(), "x", "/tmp/out", ref_image="/tmp/i.png",
                                  profile=self.sd.PROFILE_EDIT)
        self.assertNotIn("--model-args", argv)
        # 2511 weights: the admin toggle adds it verbatim.
        argv2 = self.sd.build_argv(self.edit_settings(**{"image_edit.zero_cond_t": True}),
                                   "x", "/tmp/out", ref_image="/tmp/i.png",
                                   profile=self.sd.PROFILE_EDIT)
        self.assertIn("--model-args", argv2)
        self.assertEqual(argv2[argv2.index("--model-args") + 1], "qwen_image_zero_cond_t=true")

    def test_missing_edit_file_names_the_setting(self):
        os.remove(self.edit_vae)
        msg = self.sd.preflight(self.edit_settings(), self.sd.PROFILE_EDIT)
        self.assertIn("VAE (.safetensors)", msg)
        self.assertIn("Image Edit", msg)   # says which profile, not a generic message

    def test_edit_disabled_is_reported_before_files(self):
        msg = self.sd.preflight({"image_edit.enabled": False}, self.sd.PROFILE_EDIT)
        self.assertIn("disabled", msg)

    # -- shared binary fallback ------------------------------------------------
    def test_edit_reuses_generation_cli_when_unset(self):
        s = {"image_edit.enabled": True, "diffusion.cli_bin": self.bin,
             "image_edit.model_dir": self.edit_dir}
        p = self.sd.resolve_paths(s, self.sd.PROFILE_EDIT)
        self.assertEqual(p["bin"], self.bin)

    # -- settings surface ------------------------------------------------------
    def test_edit_settings_are_admin_editable(self):
        from hearth.runtime_settings import SPEC_BY_KEY
        for key in ("image_edit.enabled", "image_edit.model_dir", "image_edit.dit_path",
                    "image_edit.llm_path", "image_edit.vae_path", "image_edit.llm_vision_path",
                    "tools.edit_image", "tools.edit_image.approval"):
            self.assertIn(key, SPEC_BY_KEY, f"{key} should be admin-editable")

    def test_edit_tool_registered_and_gated(self):
        from hearth.tools import TOOLS_BY_NAME
        t = TOOLS_BY_NAME["edit_image"]
        self.assertTrue(t.gated)
        self.assertEqual(t.enable_key, "tools.edit_image")
        self.assertIn("Qwen Image Edit", t.label)

    def test_edit_tool_requires_image_and_prompt(self):
        import asyncio
        from hearth.tools import ToolContext, TOOLS_BY_NAME
        t = TOOLS_BY_NAME["edit_image"]
        ctx = ToolContext("u1", {})
        r = asyncio.run(t.run({"prompt": "x"}, ctx))
        self.assertFalse(r.ok)
        self.assertIn("image", r.content.lower())
        r2 = asyncio.run(t.run({"image": "a.png"}, ctx))
        self.assertFalse(r2.ok)
        self.assertIn("prompt", r2.content.lower())


if __name__ == "__main__":
    print(f"[selftest] data dir = {_tmp}")
    unittest.main(verbosity=2)

