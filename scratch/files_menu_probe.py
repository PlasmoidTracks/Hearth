"""Manual UI probe for the Files-tab three-dots menu (not part of selftest).

Drives the running Hearth server with Playwright against a throwaway data dir:
logs in, opens the Files panel, and exercises each ⋮ item (attach / copy link /
download / delete) plus the row markup itself. Prints what it observes; screenshots
to /tmp so a human can eyeball the layout too.

Run:  python3 scratch/files_menu_probe.py [base_url]
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8137"
USER, PW = "admin", "testpass123"


def shot(page, name):
    path = f"/tmp/files_menu_{name}.png"
    try:
        page.screenshot(path=path, full_page=False)
        print(f"  [shot] {path}")
    except Exception as e:
        print(f"  [shot failed] {e}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium-browser")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # --- login ---------------------------------------------------------
        page.goto(f"{BASE}/login", wait_until="load")
        page.fill('input[name="username"]', USER)
        page.fill('#password', PW)
        page.click('#loginForm button')
        page.wait_for_url("**/chat", timeout=15000)
        print("signed in ->", page.url)

        # --- a chat + an uploaded file -------------------------------------
        # Clicking "+ New chat" is what the app itself does: it creates the
        # conversation AND sets S.convId, so a later upload lands in it. A raw
        # fetch would create a conversation the page knows nothing about and the
        # file would be filed under "" (invisible under the "this chat" scope).
        page.click("#newChat")
        page.wait_for_timeout(800)
        conv = page.evaluate("() => S.convId")
        print("conversation", conv)
        page.set_input_files("#fileInput", "/tmp/probe_notes.txt")
        page.wait_for_timeout(1800)

        # --- open the Files panel -----------------------------------------
        page.click('#rail button[data-panel="files"]')
        page.wait_for_selector("#artifactList li a[href^='/api/artifact/']", timeout=8000)
        rows = page.query_selector_all("#artifactList li")
        print(f"artifact rows: {len(rows)}")

        row_html = rows[0].inner_html()
        print("row html:", row_html[:400])
        assert "⋮" in row_html, "expected a ⋮ button in the row"
        # The old standalone ⬇ must be gone (the filename link is the download).
        anchors = rows[0].query_selector_all("a")
        print(f"anchors in row: {len(anchors)} (expect 1)")
        assert len(anchors) == 1, "filename should be the only link; no separate ⬇ button"
        shot(page, "1_files_panel")

        # --- open the menu -------------------------------------------------
        rows[0].query_selector("[data-art-menu]").click()
        page.wait_for_selector("#fileMenu", timeout=4000)
        items = [b.get_attribute("data-act") for b in page.query_selector_all("#fileMenu [data-act]")]
        print("menu items:", items)
        assert items == ["download", "attach", "copylink", "delete"], f"unexpected items {items}"
        heading = page.text_content("#fileMenu > div")
        print("menu heading:", (heading or "").strip())
        shot(page, "2_menu_open")

        # --- Escape closes -------------------------------------------------
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert page.query_selector("#fileMenu") is None, "Escape must close the menu"
        print("Escape closes: ok")

        # --- Attach to message --------------------------------------------
        page.click('#rail button[data-panel="chats"]')
        rows = page.query_selector_all("#artifactList li")  # panel hidden but still in DOM
        page.click('#rail button[data-panel="files"]')
        page.wait_for_timeout(300)
        page.click("#artifactList [data-art-menu]")
        page.wait_for_selector("#fileMenu", timeout=4000)
        page.click('#fileMenu [data-act="attach"]')
        page.wait_for_timeout(500)
        chips = page.query_selector_all("#attachStrip button[data-rmatt]")
        print(f"staged attachment chips: {len(chips)}")
        assert len(chips) == 1, "attach should stage exactly one chip"
        hint = page.text_content("#turnHint")
        print("hint after attach:", (hint or "").strip())
        shot(page, "3_attached")

        # dedupe: attaching the same file again must not stack a second chip
        page.click("#artifactList [data-art-menu]")
        page.wait_for_selector("#fileMenu", timeout=4000)
        page.click('#fileMenu [data-act="attach"]')
        page.wait_for_timeout(400)
        chips = page.query_selector_all("#attachStrip button[data-rmatt]")
        print(f"chips after second attach (expect 1): {len(chips)}")
        assert len(chips) == 1, "re-attaching must dedupe"

        # --- Send with the staged attachment (proves the artifact ref works) --
        page.fill("#input", "what is in this file?")
        sent = {"ok": False}

        def on_resp(r):
            if "/api/chat/stream" in r.url:
                sent["ok"] = r.status < 500
        page.on("response", on_resp)
        page.click("#sendBtn")
        page.wait_for_timeout(4000)
        print("stream responded:", sent["ok"])

        # --- Copy link ------------------------------------------------------
        try:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        except Exception as e:
            print("  clipboard permission skipped:", e)
        page.click("#artifactList [data-art-menu]")
        page.wait_for_selector("#fileMenu", timeout=4000)
        page.click('#fileMenu [data-act="copylink"]')
        page.wait_for_timeout(500)
        print("hint after copy:", (page.text_content("#turnHint") or "").strip())

        # --- Download -------------------------------------------------------
        page.click("#artifactList [data-art-menu]")
        page.wait_for_selector("#fileMenu", timeout=4000)
        try:
            with page.expect_download(timeout=6000) as dl:
                page.click('#fileMenu [data-act="download"]')
            print("download suggested filename:", dl.value.suggested_filename)
        except Exception as e:
            print("  download not captured (may have opened a tab):", str(e)[:120])

        # --- Delete ---------------------------------------------------------
        before = len(page.query_selector_all("#artifactList li"))
        page.click("#artifactList [data-art-menu]")
        page.wait_for_selector("#fileMenu", timeout=4000)
        dialog = []

        def on_dialog(d):
            dialog.append(d.type)
            d.accept()
        page.on("dialog", on_dialog)
        page.click('#fileMenu [data-act="delete"]')
        page.wait_for_timeout(1500)
        after = len(page.query_selector_all("#artifactList li"))
        print(f"rows before delete: {before}, after: {after}; dialogs: {dialog}")
        assert dialog == ["confirm"], f"expected one confirm, got {dialog}"
        shot(page, "4_after_delete")

        # --- mobile viewport sanity ---------------------------------------
        page.set_viewport_size({"width": 390, "height": 780})
        page.wait_for_timeout(500)
        page.click("#artifactList [data-art-menu]") if page.query_selector("#artifactList [data-art-menu]") else None
        page.wait_for_timeout(400)
        if page.query_selector("#fileMenu"):
            box = page.evaluate("() => { const r = document.getElementById('fileMenu').getBoundingClientRect();"
                                "return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width}; }")
            vw = page.evaluate("() => window.innerWidth")
            print(f"mobile menu box {box} viewport {vw}")
            assert box["r"] <= vw + 1 and box["l"] >= -1, "menu must stay on-screen"
        shot(page, "5_mobile")

        print("\nJS/console errors:", errors if errors else "none")
        browser.close()


main()
