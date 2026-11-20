"""Scratch probe: does stopping/timing out an sd-cli run leave ANY process behind?

Not part of the shipped suite (scripts/selftest.py holds the real tests). This exists
because "Stop kills the generation" is a claim about OS process state that no amount of
reading the code establishes — it needs a real child with a real grandchild.

Run:  ./venv/bin/python scratch/diffusion_runtime_probe.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp(prefix="hsdt-")
os.environ["HEARTH_DATA_DIR"] = _tmp
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hearth.db import init_db  # noqa: E402
init_db()
from hearth import diffusion as sd  # noqa: E402

os.makedirs("/tmp/models", exist_ok=True)
for _p in ("/tmp/models/sd.gguf", "/tmp/models/llm.gguf", "/tmp/models/vae.safetensors"):
    with open(_p, "wb") as fh:
        fh.write(b"fake")

WRAPPER = os.path.join(ROOT, "scratch", "fake_tools", "wrapper-cli")
os.chmod(WRAPPER, 0o755)
# Qwen Image needs three files (see hearth/diffusion.py); the fake CLI ignores them, but
# preflight refuses to launch without all three, so the probe has to provide them.
BASE = {"diffusion.enabled": True,
        "diffusion.cli_bin": WRAPPER,
        "diffusion.dit_path": "/tmp/models/sd.gguf",
        "diffusion.llm_path": "/tmp/models/llm.gguf",
        "diffusion.vae_path": "/tmp/models/vae.safetensors"}


# Process groups this probe knows Hearth created (the fake wrapper runs with
# start_new_session=True, so its PGID equals its PID). Populated by the Popen spy below.
OUR_GROUPS = set()


def grandchildren():
    """Live members of a Hearth-created process group that mention `sleep 60`.

    Deliberately NOT `pgrep -f "sleep 60"`: that pattern also matches the /bin/sh -c wrapper
    of whatever shell invoked this probe, so it saw orphans that never existed and — worse —
    could keep reporting FAIL long after Hearth had killed everything. Restricting to groups
    Hearth itself created makes the claim about the thing being tested.
    """
    out = []
    r = subprocess.run(["ps", "-eo", "pid,pgid,args", "--no-headers"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or "sleep 60" not in parts[2]:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid == pgid or pgid in OUR_GROUPS:
            out.append(pid)
    return out


def _spy_popen():
    """Wrap subprocess.Popen inside hearth.diffusion to record the groups it creates."""
    real = sd.subprocess.Popen

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            try:
                OUR_GROUPS.add(os.getpgid(self.pid))
            except OSError:
                pass

    sd.subprocess.Popen = Spy


def settle():
    for _ in range(20):
        if not grandchildren():
            return True
        time.sleep(0.25)
    return False


def case_timeout():
    settle()
    settings = dict(BASE, **{"diffusion.timeout_s": 3})
    t0 = time.time()
    try:
        sd.get_diffusion().generate_sync(settings, "x")
        msg = "FAIL: no error raised"
    except sd.DiffusionError as e:
        msg = f"{time.time() - t0:.1f}s -> {str(e)[:70]}..."
    ok = settle()
    return ("PASS" if ok else "FAIL (orphan)") + " | " + msg


def case_cancel():
    """Cooperative cancel must not be relied on to interrupt a blocking subprocess.

    `cancel` is checked before launch and between steps, but sd-cli is one long blocking
    child, so the real teardown comes from hearth.cancel (Stop → stop_active()) or from the
    timeout. This case therefore asserts what is true and useful: the wait unwinds quickly
    once the child dies, and nothing of Hearth's own process groups survives it. The old
    version reported FAIL for 25 s of `th.join(20)` because nothing signalled the child at
    all — which was a symptom of stop_active() being a no-op, not a property of cancel.
    """
    settle()
    ev = threading.Event()
    out = {}

    def run():
        try:
            sd.get_diffusion().generate_sync(dict(BASE, **{"diffusion.timeout_s": 300}),
                                             "x", cancel=ev)
            out["r"] = "no error"
        except sd.DiffusionError as e:
            out["r"] = str(e)[:60]

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    t0 = time.time()
    ev.set()
    stopped = sd.get_diffusion().stop_active()   # what Stop actually does (DEC-076)
    th.join(20)
    ok = settle() and not th.is_alive() and bool(out.get("r"))
    return (("PASS" if ok else "FAIL") + f" | unwound in {time.time() - t0:.1f}s, "
            f"stop_active={stopped}, {out.get('r', 'thread still running')}")


def case_busy():
    settle()
    d = sd.get_diffusion()

    def hold():
        try:
            d.generate_sync(dict(BASE, **{"diffusion.timeout_s": 8}), "x")
        except Exception:
            pass

    threading.Thread(target=hold, daemon=True).start()
    time.sleep(1.0)
    t0 = time.time()
    try:
        d.generate_sync(dict(BASE, **{"diffusion.cli_bin": os.path.join(
            ROOT, "scratch", "fake_tools", "sd-cli"), "diffusion.busy_timeout_s": 2}), "x")
        return "FAIL: ran concurrently"
    except sd.DiffusionError as e:
        return f"queued {time.time() - t0:.1f}s -> {str(e)[:95]}"


if __name__ == "__main__":
    _spy_popen()      # so orphan checks are about Hearth's own children only
    print("TIMEOUT :", case_timeout())
    print("CANCEL  :", case_cancel())
    print("BUSY    :", case_busy())
