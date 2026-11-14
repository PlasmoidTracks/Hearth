"""Sandbox backends for gated command execution (DEC-004).

Auto-detects: bubblewrap (bwrap) -> docker -> stub(refuse). Commands run with
cwd = user's jail, no network, dropped caps, read-only system + writable jail,
hard timeout and output caps. If NO real sandbox is available we DO NOT run the
command — we return a refusal (fail-closed).

This is defense-in-depth, not a hard multi-tenant boundary: exec is also gated by
explicit per-call user approval and admin enablement. Run on a dedicated/VM host
for stronger isolation.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import time
from dataclasses import dataclass


@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    backend: str
    duration_s: float = 0.0
    note: str = ""


def detect_backend(preference: str = "auto") -> str:
    if preference != "auto":
        return preference if _have(preference) else "stub"
    for b in ("bwrap", "docker"):
        if _have(b):
            return b
    return "stub"


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _bwrap_argv(command: str, jail: str) -> list[str]:
    # Unprivileged user+pid+net+ipc+uts namespaces; read-only host bind; jail rw.
    #
    # Ordering matters on merged-usr distros (Debian/Ubuntu/Fedora): /bin is a
    # symlink to /usr/bin, and bwrap creates the *destination* of each binding
    # before processing later options. Binding "/lib"->"/lib" first would make
    # bwrap mkdir "/lib", which resolves through the host's /bin symlink and
    # materializes a real empty /bin directory inside the sandbox — after that
    # "--symlink /usr/bin /bin" can't replace it, and exec of /bin/sh fails with
    # "execvp /bin/sh: No such file or directory". So create /bin (and /lib*)
    # before any bind touches them. ro-bind-try keeps non-merged-usr hosts
    # working (missing dirs are skipped instead of aborting).
    argv = [
        "bwrap",
        "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "/usr/bin", "/bin",
        "--symlink", "/usr/lib", "/lib",       # merged-usr; skipped if /lib is real below
        "--symlink", "/usr/lib64", "/lib64",   # ditto (x86_64 SUSE/RHEL-style layouts)
    ]
    for libdir in ("/lib", "/lib64", "/lib32", "/usr/libx32"):
        if os.path.isdir(libdir) and not os.path.islink(libdir):
            # Non-merged-usr host: a real top-level library dir must be bound.
            argv += ["--ro-bind-try", libdir, libdir]
    argv += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", jail, jail,          # writable workspace
        "--chdir", jail,
        "--setenv", "HOME", jail,
        "--setenv", "PATH", "/usr/bin:/bin",
        "/bin/sh", "-c", command,
    ]
    return argv


def _docker_argv(command: str, jail: str, container_image: str = "alpine:latest") -> list[str]:
    return [
        "docker", "run", "--rm", "-i",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "512m", "--cpus", "1",
        "--read-only",
        "-v", f"{jail}:{jail}",
        "-w", jail,
        container_image,
        "/bin/sh", "-c", command,
    ]


async def run_command(command: str, jail: str, *, backend_pref: str = "auto",
                      timeout: float = 30.0, max_output: int = 100_000) -> ExecResult:
    t0 = time.time()
    jail = os.path.realpath(jail)
    backend = detect_backend(backend_pref)

    if backend == "stub":
        return ExecResult(False, -1, "", "", "stub",
                          note="No sandbox available (bwrap/docker missing). Refusing to run "
                               "commands unsandboxed. Install bubblewrap or Docker.")

    argv = _bwrap_argv(command, jail) if backend == "bwrap" else _docker_argv(command, jail)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=jail)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return ExecResult(False, 124, "", f"command timed out after {timeout}s", backend,
                              duration_s=time.time() - t0)
        stdout = out.decode("utf-8", "replace")[:max_output]
        stderr = err.decode("utf-8", "replace")[:max_output]
        return ExecResult(proc.returncode == 0, proc.returncode or 0, stdout, stderr, backend,
                          duration_s=time.time() - t0)
    except Exception as e:  # noqa: BLE001
        return ExecResult(False, -1, "", f"sandbox launch error: {str(e)[:200]}", backend,
                          duration_s=time.time() - t0)
