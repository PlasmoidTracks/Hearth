"""Real-browser smoke for the admin 📁 Browse picker (DEC-080).

Starts Hearth on 127.0.0.1:8137 against a throwaway data dir, drives Chromium through
the actual UI, and asserts the two things the operator asked for:

  1. a Browse button next to path fields that fills the textbox by choosing, and
  2. it opens on the folder of whatever the field already holds ("where I left off").

Run: python scratch/browse_picker_smoke.py [path/to/chromium]
"""
import os
import sys
import pathlib
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="hearth-browse-smoke-")
os.environ["HEARTH_DATA_DIR"] = _tmp
os.environ["HEARTH_HOST"] = "127.0.0.1"
os.environ["HEARTH_PORT"] = "8137"
os.environ["HEARTH_BOOTSTRAP_ADMIN_PASSWORD"] = "testpass123"

# A model tree that looks like the real thing, so picking is meaningful.
MODELS = pathlib.Path(tempfile.mkdtemp(prefix="browse-models-"))
(MODELS / "Qwen2.5-7B-Q4_K_M.gguf").write_bytes(b"a" * 1024)
(MODELS / "mmproj-Qwen2.5-VL-f16.gguf").write_bytes(b"b" * 2048)
(MODELS / "other").mkdir()
(MODELS / "other" / "llama-server").write_bytes(b"c" * 4096)

from playwright.sync_api import sync_playwright   # noqa: E402

PORT = 8137
BASE = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + ("" if cond else f"   -> {extra}"))
    if not cond:
        FAILS.append(label)


def sign_in(page):
    """The login button has no type=submit and is handled by login.js, so click it by id."""
    page.goto(BASE + "/login")
    page.wait_for_selector("#username", timeout=10000)
    page.fill("#username", "admin")
    page.fill("#password", "testpass123")
    page.click("#loginForm button")
    page.wait_for_url(BASE + "/chat", timeout=15000)


def main():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "hearth.main:app",
                             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                import urllib.request
                urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("server never came up:", proc.stdout.read().decode()[:2000])
            return 2

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            print("\n[1] sign in")
            sign_in(page)
            check("logged in and reached /chat", "/chat" in page.url, page.url)

            print("\n[2] Admin → Model: Browse buttons render on path fields only")
            page.goto(BASE + "/admin")
            page.wait_for_selector("#panel", timeout=8000)
            page.click('button[data-tab="model"]')
            page.wait_for_selector('[data-key="model.model_path"]', timeout=8000)
            browse = page.eval_on_selector_all("[data-browse]", "els => els.map(e=>e.dataset.browse)")
            # The Model tab holds exactly these three path fields today; assert the set rather
            # than a count so adding one elsewhere cannot mask a regression here.
            check("Model tab path fields all present",
                  browse == ["model.server_bin", "model.model_path", "model.mmproj_path"], f"got {browse}")
            check("model.model_path has one", "model.model_path" in browse, str(browse))
            check("model.device did NOT", "model.device" not in browse, str(browse))
            labels = page.eval_on_selector_all("[data-browse]", "els => els.map(e=>e.textContent.trim())")
            check("buttons read 'Browse'", all("Browse" in t for t in labels), str(labels))

            print("\n[3] click Browse on an EMPTY field → starts at filesystem root")
            page.click('[data-browse="model.model_path"]')
            page.wait_for_selector("#pathPickShell", timeout=8000)
            page.wait_for_function("() => document.querySelectorAll('#pathPickList [data-nav]').length > 3",
                                   timeout=15000)
            names = page.eval_on_selector_all("#pathPickList [data-nav]", "els=>els.map(e=>e.textContent.trim())")
            check("listing rendered from root", any("tmp/" in n for n in names), str(names[:8]))

            print("\n[4] navigate into the model folder and pick a file")
            page.fill("#pathPickAddr", str(MODELS))
            page.press("#pathPickAddr", "Enter")
            page.wait_for_function("() => [...document.querySelectorAll('#pathPickList [data-pick]')]"
                                   ".some(e=>e.textContent.includes('mmproj'))", timeout=15000)
            dirs_first = page.eval_on_selector_all(
                "#pathPickList > button", "els=>els.slice(0,3).map(e=>({d:!!e.querySelector('[class*=sky-400]')}))")
            check("folders sort above files", dirs_first[0]["d"], str(dirs_first))
            page.click("#pathPickList [data-pick] >> text=mmproj-Qwen2.5-VL-f16.gguf")
            page.wait_for_selector("#pathPickShell", state="detached", timeout=8000)
            val = page.input_value('[data-key="model.model_path"]')
            check("picked path landed in the textbox",
                  val == str(MODELS / "mmproj-Qwen2.5-VL-f16.gguf"), val)
            check("dialog closed after picking", page.query_selector("#pathPickShell") is None)

            print("\n[5] THE ASK: with a value present, Browse opens ITS folder")
            page.click('[data-browse="model.model_path"]')
            page.wait_for_selector("#pathPickShell", timeout=8000)
            page.wait_for_function("() => [...document.querySelectorAll('#pathPickList [data-pick]')]"
                                   ".some(e=>e.textContent.includes('Qwen2.5-7B-Q4_K_M'))", timeout=15000)
            addr = page.input_value("#pathPickAddr")
            note = page.inner_text("#pathPickNote")
            check("address bar shows the current file's folder", addr == str(MODELS), addr)
            check("says it is showing that folder", "containing" in note, note)
            picks = page.eval_on_selector_all("#pathPickList [data-pick]", "els=>els.map(e=>e.textContent.trim())")
            check("sibling .gguf visible for one-click pick", any("Qwen2.5-7B-Q4_K_M" in p for p in picks), str(picks))

            print("\n[6] filter narrows without a round trip; Esc closes")
            page.fill("#pathPickFilter", "qwen2.5-7b")
            page.wait_for_function("() => document.querySelectorAll('#pathPickList [data-pick]').length === 1",
                                   timeout=8000)
            one = page.eval_on_selector_all("#pathPickList [data-pick]", "els=>els.map(e=>e.textContent.trim())")
            check("filter leaves only the match", len(one) == 1 and "Qwen2.5-7B-Q4_K_M" in one[0], str(one))
            page.keyboard.press("Escape")
            page.wait_for_selector("#pathPickShell", state="detached", timeout=8000)
            check("Escape dismisses", page.query_selector("#pathPickShell") is None)

            print("\n[7] picking does not save by itself")
            saved = page.evaluate("""() => fetch('/api/admin/settings',{credentials:'same-origin'})
                                        .then(r=>r.json()).then(d=>d.values['model.model_path'])""")
            check("server still holds the old (empty) value", saved == "", repr(saved))

            print("\n[8] ↑ / ⌂ root / ⌂ ~ all navigate")
            page.click('[data-browse="model.model_path"]')
            page.wait_for_selector("#pathPickShell", timeout=8000)
            page.wait_for_function("() => [...document.querySelectorAll('#pathPickList [data-nav]')].length>0",
                                   timeout=15000)
            page.click("#pathPickUp")
            page.wait_for_function("""() => {
                const a = document.querySelector('#pathPickAddr');
                return a && a.value !== arguments0;
            }""".replace("arguments0", repr(str(MODELS))), timeout=15000)
            up = page.input_value("#pathPickAddr")
            check("address moved to the parent", up == str(MODELS.parent), up)
            home_target = page.evaluate("""() => fetch('/api/admin/browse-path?path=~',
                                          {credentials:'same-origin'}).then(r=>r.json()).then(d=>d.path)""")
            page.click("#pathPickHome")
            page.wait_for_timeout(900)
            addr_home = page.input_value("#pathPickAddr")
            check("⌂ ~ jumps to the server's home", addr_home == home_target, f"{addr_home} != {home_target}")
            page.click("#pathPickRoots")
            page.wait_for_timeout(700)
            check("⌂ root returns to the filesystem root (address cleared)",
                  page.input_value("#pathPickAddr") == "", page.input_value("#pathPickAddr"))

            print("\n[9] permission-denied path explains itself, stays open")
            page.fill("#pathPickAddr", "/root/secret")
            page.press("#pathPickAddr", "Enter")
            page.wait_for_function("() => !document.querySelector('#pathPickErr').classList.contains('hidden')",
                                   timeout=15000)
            err = page.inner_text("#pathPickErr")
            check("error line shown", len(err) > 5, err)
            check("dialog still usable (listing present)",
                  page.query_selector("#pathPickList") is not None)

            print("\n[10] Audio + Images tabs also carry Browse")
            for tab, key in [("audio", "captioner.model_path"), ("images", "diffusion.model_dir")]:
                # Reload the admin page per tab: a fresh load runs the boot render, and
                # switching tabs on a live page is covered separately in [13].
                page.goto(BASE + "/admin")
                page.wait_for_selector(".tab", timeout=15000)
                page.click(f'button[data-tab="{tab}"]')
                page.wait_for_selector(f'[data-key="{key}"]', timeout=20000)
                has = page.query_selector(f'[data-browse="{key}"]') is not None
                check(f"{tab}: Browse next to {key}", has, "not rendered")

            print("\n[11] Project tab folder box has Browse")
            page.goto(BASE + "/admin")
            page.wait_for_selector(".tab", timeout=15000)
            page.click('button[data-tab="project"]')
            page.wait_for_selector("#projTarget", timeout=15000)
            check("Project: Browse next to the folder box",
                  page.query_selector("#projBrowse") is not None)
            page.click("#projBrowse")
            page.wait_for_selector("#pathPickShell", timeout=8000)
            page.fill("#pathPickAddr", str(MODELS / "other"))
            page.press("#pathPickAddr", "Enter")
            page.wait_for_function("() => [...document.querySelectorAll('#pathPickList [data-pick]')]"
                                   ".some(e=>e.textContent.includes('llama-server'))", timeout=15000)
            page.click("#pathPickClose")

            print("\n[12] mobile viewport: button reachable, dialog fits")
            mob = browser.new_page(viewport={"width": 390, "height": 780})
            sign_in(mob)
            mob.goto(BASE + "/admin")
            mob.wait_for_selector('[data-key="model.model_path"]', timeout=15000)
            btn = mob.query_selector('[data-browse="model.model_path"]')
            box = btn.bounding_box()
            check("Browse button visible on a phone", box is not None and box["width"] > 20, str(box))
            btn.click()
            mob.wait_for_selector("#pathPickShell", timeout=8000)
            shell = mob.query_selector("#pathPickShell div >> nth=0").bounding_box()
            check("dialog fits the viewport width", shell["width"] <= 390, str(shell))
            mob.screenshot(path="/tmp/browse_mobile.png")

            js_errs = [e for e in errors if "favicon" not in e.lower()]
            check("no JS console/page errors", not js_errs, str(js_errs[:3]))

            print("\n[13] tab SWITCH on a live page wires Browse too (the awaited-render fix)")
            # Regression guard: the panel used to be rendered fire-and-forget, so buttons
            # drawn by a later render had no click handler attached.
            pg2 = browser.new_page(viewport={"width": 1280, "height": 900})
            sign_in(pg2)
            pg2.goto(BASE + "/admin")
            pg2.wait_for_selector(".tab", timeout=15000)
            for tab, key in [("audio", "captioner.model_path"), ("images", "diffusion.model_dir"),
                             ("model", "model.model_path")]:
                pg2.click(f'button[data-tab="{tab}"]')
                pg2.wait_for_selector(f'[data-key="{key}"]', timeout=20000)
                pg2.wait_for_timeout(300)
                pg2.click(f'[data-browse="{key}"]')
                try:
                    pg2.wait_for_selector("#pathPickList [data-nav]", timeout=10000)
                    ok = True
                    addr0 = pg2.input_value("#pathPickAddr")
                except Exception:
                    ok = False
                    addr0 = ""
                check(f"{tab}: Browse opens a usable listing after a tab switch", ok, "no listing")
                pg2.keyboard.press("Escape")
                pg2.wait_for_timeout(200)
            browser.close()
        return 1 if FAILS else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    rc = main()
    print("\n" + ("ALL CHECKS PASSED" if rc == 0 else f"{len(FAILS)} FAILED: {FAILS}"))
    sys.exit(rc)
