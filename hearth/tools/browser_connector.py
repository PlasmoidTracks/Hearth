"""Browser connector — drives the operator's real Chrome/Chromium via Selenium.

Ported from the llm_coder harness's browser_connector.py and adapted for Hearth
(async-friendly, graceful when Selenium is absent, audit hooks). Strategy:

1. If Chrome is already running with --remote-debugging-port=9222, attach to it.
2. Otherwise launch Chrome with a *copy* of the user profile in a temp dir
   (so cookies/logins are available without fighting a profile lock), wait for
   the debugging port, and connect Selenium to it.
3. Read the rendered page via driver.page_source — full JS, logged-in state.
4. Extract visible text + links with stdlib regex/HTML stripping (no bs4 dep).

Everything here is synchronous/blocking by design (Selenium is); callers in the
web tools run it via asyncio.to_thread. Nothing in this module raises to the
model: failures come back as empty results / error strings the tool layer turns
into a helpful ToolResult.

Dependencies: pip install selenium  (Selenium 4+ manages ChromeDriver itself).
"""
from __future__ import annotations
import html as _html
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Optional, Tuple
from urllib.parse import urlparse

DEBUGGING_PORT = 9222

SEARCH_URLS = {
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
}


def selenium_available() -> bool:
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        return False


DEPS_ERROR = ("Error: Selenium is not installed or Chrome/Chromium was not found, so "
              "'open_browser' web mode cannot run. Install with: pip install selenium "
              "(and have Google Chrome or Chromium on this machine). An admin can switch "
              "back to 'search_and_fetch_safe' under Admin → Tools → Web.")


# ---------------------------------------------------------------------------
# Chrome / profile detection
# ---------------------------------------------------------------------------

def _find_chrome_executable() -> str:
    """Locate a Chrome/Chromium executable, falling back to PATH lookup."""
    candidates = [
        # Linux
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser", "/usr/bin/chromium",
        "/snap/bin/chromium",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    from shutil import which
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = which(name)
        if p:
            return p
    return "chrome"


def _get_chrome_user_data_dir() -> Optional[str]:
    """Path to the user's Chrome profile root (where cookies live), or None."""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Linux":
        paths = [os.path.join(home, ".config", d) for d in
                 ("google-chrome", "google-chrome-stable", "chromium")]
    elif system == "Darwin":
        paths = [os.path.join(home, "Library", "Application Support", app)
                 for app in (("Google", "Chrome"), ("Chromium",))]
    elif system == "Windows":
        ad = os.getenv("LOCALAPPDATA", "")
        paths = [os.path.join(ad, "Google", "Chrome", "User Data")] if ad else []
    else:
        paths = []
    for path in paths:
        if os.path.isdir(path):
            return path
    return None


def _copy_profile_to_temp(user_data_dir: str) -> str:
    """Copy the Default profile + Local State into a temp dir (no lock conflict)."""
    tmp_dir = tempfile.mkdtemp(prefix="hearth_chrome_profile_")
    default_src = os.path.join(user_data_dir, "Default")
    if os.path.isdir(default_src):
        shutil.copytree(default_src, os.path.join(tmp_dir, "Default"), dirs_exist_ok=True)
    local_state = os.path.join(user_data_dir, "Local State")
    if os.path.isfile(local_state):
        shutil.copy2(local_state, os.path.join(tmp_dir, "Local State"))
    return tmp_dir


# ---------------------------------------------------------------------------
# Selenium driver creation (attach-first, then launch-with-copied-profile)
# ---------------------------------------------------------------------------

def _port_alive(port: int = DEBUGGING_PORT) -> bool:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return resp.status == 200
    except Exception:  # noqa: BLE001 — any failure means "not our Chrome"
        return False


def create_driver(headless: bool = True):
    """Create a Selenium WebDriver attached to (or launching) real Chrome.

    Mirrors llm_coder's connector: attach to an existing debugging port first;
    otherwise launch Chrome with the user's profile copied to a temp dir and
    connect through the debugging port. The driver carries private attributes
    `_chrome_process` / `_temp_profile` so callers can clean up after themselves.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        raise ImportError(DEPS_ERROR)

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    if platform.system() == "Linux":
        opts.add_argument("--no-sandbox")   # only meaningful on Linux
    for flag in ("--disable-dev-shm-usage", "--disable-gpu", "--disable-background-networking",
                 "--disable-background-timer-throttling", "--disable-hang-monitor",
                 "--disable-popup-blocking", "--disable-prompt-on-repost", "--disable-sync",
                 "--metrics-recording-only", "--no-first-run", "--no-default-browser-check",
                 "--window-size=1920,1080"):
        opts.add_argument(flag)

    # 1) An already-running Chrome with remote debugging wins — attaching is
    #    faster and keeps the user's live session exactly as it is.
    if _port_alive():
        opts.add_experimental_option("debuggerAddress", f"localhost:{DEBUGGING_PORT}")
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        return driver

    # 2) Launch our own Chrome with a copy of the profile (cookies without locks).
    user_data_dir = _get_chrome_user_data_dir()
    temp_profile = None
    if user_data_dir:
        try:
            temp_profile = _copy_profile_to_temp(user_data_dir)
            opts.add_argument(f"--user-data-dir={temp_profile}")
        except Exception:  # noqa: BLE001 — cookies are a nice-to-have, not required
            temp_profile = None
    opts.add_argument(f"--remote-debugging-port={DEBUGGING_PORT}")

    chrome_path = _find_chrome_executable()
    cmd = [chrome_path] + list(opts.arguments)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ready = False
    for _ in range(30):
        if _port_alive():
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        _terminate(proc)
        _rmtree(temp_profile)
        raise RuntimeError(f"Chrome did not become ready on debugging port {DEBUGGING_PORT} within 15s")

    opts.add_experimental_option("debuggerAddress", f"localhost:{DEBUGGING_PORT}")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    try:
        driver._chrome_process = proc          # type: ignore[attr-defined]
        driver._temp_profile = temp_profile    # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — attributes are best-effort bookkeeping
        pass
    return driver


def _terminate(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass


def _rmtree(path) -> None:
    if path and os.path.isdir(path):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def close_driver(driver) -> None:
    """Quit the driver and reap any Chrome process / temp profile we created."""
    if driver is None:
        return
    proc = getattr(driver, "_chrome_process", None)
    try:
        driver.quit()
    except Exception:  # noqa: BLE001
        if proc:
            _terminate(proc)
    if proc:
        _terminate(proc)
    _rmtree(getattr(driver, "_temp_profile", None))


def kill_active_chrome() -> bool:
    """Kill any Chrome holding the debugging port (zombie from a prior call).

    Ported from llm_coder: before a new browser operation, a lingering Chrome on
    9222 makes Selenium hang; this frees the port. Best-effort, never raises.
    """
    killed = False
    try:
        if platform.system() == "Windows":
            res = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if f":{DEBUGGING_PORT}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                       capture_output=True, timeout=5)
                        killed = True
        else:
            try:
                res = subprocess.run(["lsof", "-i", f":{DEBUGGING_PORT}", "-t"],
                                     capture_output=True, text=True, timeout=5)
                for pid in res.stdout.strip().splitlines():
                    if pid.strip().isdigit():
                        os.kill(int(pid.strip()), 9)
                        killed = True
            except (FileNotFoundError, OSError):
                pass   # no lsof — nothing safe to do; leave the port alone
    except Exception:  # noqa: BLE001 — cleanup is opportunistic
        pass
    return killed


# ---------------------------------------------------------------------------
# Page settle helpers
# ---------------------------------------------------------------------------

def _wait_settled(driver, extra_s: float = 2.0) -> None:
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(driver, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:  # noqa: BLE001 — partial content still beats nothing
        pass
    time.sleep(extra_s)   # client-side rendering / lazy content


def _solve_ddg_anomaly(driver) -> None:
    """Best-effort DuckDuckGo CAPTCHA handling (click puzzle tiles, submit form).

    Straight from llm_coder's connector: DDG shows an anomaly modal to suspicious
    IPs; the tile puzzle is trivially machine-clickable. Silently no-ops when
    there is no challenge or clicking fails — the caller proceeds regardless.
    """
    try:
        from selenium.webdriver.common.by import By
        boxes = driver.find_elements(By.CSS_SELECTOR, '[data-testid^="anomaly-modal-tile-"]')
        for box in boxes:
            try:
                box.click()
                time.sleep(0.15)
            except Exception:  # noqa: BLE001 — stale/detached tile
                pass
        if not boxes:
            return
        form = driver.find_element(By.ID, "challenge-form")
        form.submit()
        time.sleep(5)
    except Exception:  # noqa: BLE001 — no CAPTCHA or handling failed; continue anyway
        pass


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def browser_search(query: str, engine: str = "duckduckgo", max_results: int = 6,
                   headless: bool = True) -> list[dict]:
    """Search via the real browser. Returns [{title,url,snippet}] (empty on failure)."""
    url = SEARCH_URLS.get(engine, SEARCH_URLS["duckduckgo"]).format(query=query)
    driver = None
    try:
        driver = create_driver(headless=headless)
        driver.get(url)
        _wait_settled(driver)
        _solve_ddg_anomaly(driver)
        page = driver.page_source
    except ImportError:
        raise
    except Exception:  # noqa: BLE001 — model sees a friendly message, not a traceback
        return []
    finally:
        close_driver(driver)
    if engine == "google":
        return _extract_google(page)[:max_results]
    return _extract_ddg_page(page)[:max_results]


_EXCLUDE_SEARCH_HOSTS = {"duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com",
                         "google.com", "www.google.com"}


def _unwrap_uddg(href: str) -> str:
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return ""


def _extract_ddg_page(page_html: str) -> list[dict]:
    """Parse the rendered duckduckgo.com SERP (works for both lite and JS layouts)."""
    out: list[dict] = []
    seen: set[str] = set()
    # Result anchors: DDG wraps external links with uddg= redirects; organic links
    # may also be direct http(s) hrefs inside result blocks.
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_html,
                         re.S | re.I):
        href, inner = m.group(1), m.group(2)
        title = _strip_tags(inner).strip()
        if "uddg=" in href:
            real = _unwrap_uddg(href)
        elif href.startswith("http") and urlparse(href).netloc.lower() not in _EXCLUDE_SEARCH_HOSTS:
            real = href
        else:
            continue
        if not real.startswith("http") or real in seen:
            continue
        host = urlparse(real).netloc.lower()
        if host in _EXCLUDE_SEARCH_HOSTS or not host:
            continue
        seen.add(real)
        out.append({"title": title or host, "url": real, "snippet": ""})
        if len(out) >= 20:
            break
    return _attach_snippets(page_html, out)


def _attach_snippets(page_html: str, results: list[dict]) -> list[dict]:
    """Best-effort: pull each result's snippet text from near its anchor."""
    for r in results:
        try:
            # find the anchor for this URL, then a following snippet-ish block
            esc = re.escape(r["url"][:60])
            mm = re.search(esc + r'".{0,3000}?(?:result__snippet|--WebSnippet)[^>]*>(.*?)</',
                           page_html, re.S)
            if mm:
                r["snippet"] = _strip_tags(mm.group(1)).strip()[:300]
        except Exception:  # noqa: BLE001 — snippets are decoration; never fail the search
            pass
    return results


def _extract_google(page_html: str) -> list[dict]:
    """Parse a rendered Google SERP for organic results (div.g anchors with h3)."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
            r'<a[^>]*href="(https?://(?!www\.google\.|accounts\.google\.)[^"]+)"[^>]*>'
            r'.{0,400}?<h3[^>]*>(.*?)</h3>', page_html, re.S | re.I):
        href, title = m.group(1), _strip_tags(m.group(2)).strip()
        if href in seen:
            continue
        seen.add(href)
        out.append({"title": title or urlparse(href).netloc, "url": href, "snippet": ""})
        if len(out) >= 20:
            break
    return out


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

def fetch_page_via_browser(url: str, timeout: int = 30, headless: bool = True) -> dict:
    """Open `url` in the real browser; return {html} or {error} (never raises)."""
    driver = None
    try:
        driver = create_driver(headless=headless)
        driver.get(url)
        _wait_settled(driver, extra_s=min(3.0, max(1.0, timeout / 10)))
        return {"html": driver.page_source, "final_url": driver.current_url or url}
    except ImportError:
        return {"error": DEPS_ERROR}
    except Exception as e:  # noqa: BLE001
        return {"error": f"browser fetch failed: {str(e)[:200]}"}
    finally:
        close_driver(driver)


# ---------------------------------------------------------------------------
# Text/link extraction (shared with the HTTP backends — stdlib only, no bs4)
# ---------------------------------------------------------------------------

def extract_text_and_links(page_html: str, max_chars: int = 8000,
                           max_links: int = 30) -> Tuple[str, list[dict]]:
    """Strip scripts/styles/clutter; return (visible text, [{text,url}])."""
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", " ",
               page_html or "")
    # Prefer the main content region when the page marks one up.
    main = re.search(r"(?is)<(?:main|article)[^>]*>(.*?)</(?:main|article)>", s)
    body_src = main.group(1) if main else s

    links: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body_src, re.S | re.I):
        href, lt = m.group(1), _strip_tags(m.group(2)).strip()
        if not href.startswith("http") or not lt or href in seen:
            continue
        seen.add(href)
        links.append({"text": lt[:200], "url": href})

    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", body_src)
    text = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    lines = [ln.strip() for ln in text.splitlines()]
    text = re.sub(r"[ \t]+", " ", "\n".join(lines))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    return text[:max_chars], links[:max_links]


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
