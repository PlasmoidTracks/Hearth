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
with open("/tmp/models/sd.gguf", "wb") as fh:
    fh.write(b"fake")

WRAPPER = os.path.join(ROOT, "scratch", "fake_tools", "wrapper-cli")
os.chmod(WRAPPER, 0o755)
BASE = {"diffusion.enabled": True,
        "diffusion.model_path": "/tmp/models/sd.gguf",
        "diffusion.cli_bin": WRAPPER}


def grandchildren():
    """Any `sleep 60` still alive — the grandchild the wrapper spawns."""
    r = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True, text=True)
    return [p for p in r.stdout.split() if p]


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
    th.join(20)
    ok = settle()
    return ("PASS" if ok else "FAIL (orphan)") + f" | unwound in {time.time() - t0:.1f}s, {out.get('r', '')}"


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
    print("TIMEOUT :", case_timeout())
    print("CANCEL  :", case_cancel())
    print("BUSY    :", case_busy())
