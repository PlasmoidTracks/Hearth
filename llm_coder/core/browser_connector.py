"""
Browser connector — bridges the app to the user's real browser using Selenium.

Strategy:
1. Find and launch Chrome (or attach to an already-running instance) with the
   user's profile so cookies / logins are available.
2. Use Selenium's driver to navigate to the target URL.
3. Extract the rendered HTML via driver.page_source (no JS injection needed).
4. Use BeautifulSoup to pull out visible text and links.
5. Return plaintext + links to the caller.

This replaces the old local-HTTP-server + manual "Page Capture" fallback with
a clean Selenium-based flow inspired by search_and_scrape.py.

Dependencies:
    pip install selenium  (webdriver-manager is optional; Selenium 4+ auto-manages ChromeDriver)

For Chrome auto-detection we try a list of platform-specific paths, then fall
back to "chrome" / "chromium" on the system PATH.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Optional, List, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Chrome / Chromium detection
# ---------------------------------------------------------------------------

def _find_chrome_executable() -> str:
    """Try to locate a Chrome / Chromium executable on the system."""
    candidates = [
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        path = path.strip()
        if os.path.isfile(path):
            return path
    # Fall back to PATH lookup
    return "chrome"


def _get_chrome_user_data_dir() -> Optional[str]:
    """
    Return the path to the user's *default* Chrome profile (where cookies
    live).  Returns None if the profile directory cannot be found.
    """
    system = platform.system()

    if system == "Linux":
        home = os.path.expanduser("~")
        paths = [
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, ".config", "chromium"),
            os.path.join(home, ".config", "google-chrome-stable"),
        ]
    elif system == "Darwin":  # macOS
        home = os.path.expanduser("~")
        paths = [
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, "Library", "Application Support", "Chromium"),
        ]
    elif system == "Windows":
        appdata = os.getenv("LOCALAPPDATA", "")
        paths = [
            os.path.join(appdata, "Google", "Chrome", "User Data"),
        ]
    else:
        return None

    for path in paths:
        if os.path.isdir(path):
            return path
    return None


def _copy_profile_to_temp(user_data_dir: str) -> str:
    """
    Copy the Chrome user profile to a temp directory so Selenium gets
    its own instance (no profile lock conflict with a running Chrome).
    Returns the temp path.
    """
    tmp_dir = tempfile.mkdtemp(prefix="chrome_profile_")
    default_src = os.path.join(user_data_dir, "Default")
    if os.path.isdir(default_src):
        default_dst = os.path.join(tmp_dir, "Default")
        shutil.copytree(default_src, default_dst, dirs_exist_ok=True)
    local_state = os.path.join(user_data_dir, "Local State")
    if os.path.isfile(local_state):
        shutil.copy2(local_state, os.path.join(tmp_dir, "Local State"))
    return tmp_dir


# ---------------------------------------------------------------------------
# Selenium driver creation
# ---------------------------------------------------------------------------

def create_driver(headless: bool = False) -> "selenium.webdriver.Chrome":
    """
    Create a Selenium WebDriver instance that connects to the user's
    *already running* Chrome via remote debugging (port 9222), or launches
    a new Chrome with the user's profile if no debugging port is found.

    Returns a selenium.webdriver.Chrome instance.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        raise ImportError(
            "Selenium is not installed. Install it with: pip install selenium "
            "and ensure a Chrome/Chromium browser is available."
        )

    chrome_options = Options()

    if headless:
        chrome_options.add_argument("--headless=new")

    # General options
    # --no-sandbox is only needed on Linux; on Windows/macOS it produces a warning.
    if platform.system() == "Linux":
        chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-gpu-compositing")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--disable-hang-monitor")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-prompt-on-repost")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--force-color-profile=srgb")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    debugging_port = 9222

    # First, try: does Chrome already have remote debugging on this port?
    try:
        resp = urllib.request.urlopen(
            f"http://localhost:{debugging_port}/json/version", timeout=2
        )
        version = resp.read().decode()
        # Chrome is already running on this port — connect to it directly
        # (don't try to launch a new Chrome with --remote-debugging-port=9222,
        #  as the port is already in use and webdriver.Chrome will hang)
        chrome_options.add_experimental_option(
            "debuggerAddress", f"localhost:{debugging_port}"
        )
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(30)
        driver._chrome_options = chrome_options  # type: ignore
        return driver
    except Exception:
        pass

    # Fallback: launch Chrome with the user's profile (copied to temp dir).
    user_data_dir = _get_chrome_user_data_dir()
    chrome_process = None
    temp_profile = None

    if user_data_dir:
        temp_profile = _copy_profile_to_temp(user_data_dir)
        chrome_options.add_argument(f"--user-data-dir={temp_profile}")
        chrome_options._temp_profile = temp_profile  # type: ignore
    else:
        pass  # Run without existing cookies

    chrome_options.add_argument(f"--remote-debugging-port={debugging_port}")

    chrome_path = _find_chrome_executable()

    # Start Chrome as a subprocess so it binds to the debugging port first
    cmd = [chrome_path]
    for arg in chrome_options.arguments:
        cmd.append(arg)

    chrome_process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    # Wait for Chrome to be ready on the debugging port
    ready = False
    for _ in range(30):
        try:
            resp = urllib.request.urlopen(
                f"http://localhost:{debugging_port}/json/version", timeout=2
            )
            if resp.status == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        chrome_process.kill()
        if temp_profile and os.path.isdir(temp_profile):
            shutil.rmtree(temp_profile, ignore_errors=True)
        raise RuntimeError(
            f"Chrome did not become ready on port {debugging_port} within 15s"
        )

    # Connect Selenium to the already-running Chrome
    chrome_options.add_experimental_option(
        "debuggerAddress", f"localhost:{debugging_port}"
    )
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)
    driver._chrome_process = chrome_process  # type: ignore
    driver._chrome_options = chrome_options  # type: ignore
    return driver


# ---------------------------------------------------------------------------
# Search logic (adapted from search_and_scrape.py)
# ---------------------------------------------------------------------------

_SEARCH_URLS = {
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
}


def browser_search(
    query: str,
    engine: str = "duckduckgo",
    max_results: int = 6,
    headless: bool = False,
) -> List[Tuple[str, str]]:
    """
    Perform a web search using Selenium (real browser) and return structured
    result entries as a list of (title, url) tuples.

    Parameters:
        query: Search query string
        engine: "duckduckgo" (default) or "google"
        max_results: Maximum number of results to return
        headless: Whether to run the browser in headless mode

    Returns:
        List of (title, url) tuples, empty list on failure.
    """
    driver = None
    temp_profile = None
    try:
        driver = create_driver(headless=headless)
        chrome_opts = getattr(driver, "_chrome_options", None)
        if chrome_opts:
            temp_profile = getattr(chrome_opts, "_temp_profile", None)

        search_url = _SEARCH_URLS.get(engine, _SEARCH_URLS["duckduckgo"]).format(query=query)
        driver.get(search_url)

        # Wait for page load
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.common.by import By
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        time.sleep(2)  # Extra time for client-side rendering

        # Handle DDG anomaly CAPTCHA — click all puzzle tiles
        try:
            boxes = driver.find_elements(By.CSS_SELECTOR, '[data-testid^="anomaly-modal-tile-"]')
            for box in boxes:
                try:
                    box.click()
                    time.sleep(0.15)
                except Exception:
                    pass
            form = driver.find_element(By.ID, "challenge-form")
            form.submit()
            time.sleep(5)
        except Exception:
            pass  # No CAPTCHA or handling failed — continue anyway

        results = []
        seen_urls = set()

        if engine == "google":
            results = _extract_google_results(driver, seen_urls, max_results)
        elif engine == "duckduckgo":
            results = _extract_ddg_results(driver, seen_urls, max_results)
        else:
            results = _extract_ddg_results(driver, seen_urls, max_results)

        return results[:max_results]

    except Exception:
        return []
    finally:
        if driver:
            chrome_process = getattr(driver, "_chrome_process", None)
            if chrome_process:
                try:
                    chrome_process.terminate()
                    chrome_process.wait(timeout=3)
                except Exception:
                    try:
                        chrome_process.kill()
                    except Exception:
                        pass
            driver.quit()
        if temp_profile and os.path.isdir(temp_profile):
            try:
                shutil.rmtree(temp_profile, ignore_errors=True)
            except Exception:
                pass


def _extract_google_results(driver, seen_urls: set, max_results: int) -> List[Tuple[str, str]]:
    """Extract organic search results from a Google search results page."""
    results = []
    try:
        from selenium.webdriver.common.by import By
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            WebDriverWait(driver, 10).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "div.g, h3")) > 0
            )
        except Exception:
            pass

        links = driver.find_elements(By.CSS_SELECTOR,
            "div.g a[href^='http'], "
            "h3 a[href^='http'], "
            "div[data-hveid] a[href^='http']"
        )
        for a_tag in links:
            href = a_tag.get_attribute("href")
            title = a_tag.text.strip()
            if not href or not href.startswith("http"):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            domain = urlparse(href).netloc.lower()
            if any(domain.endswith(d) for d in ("google.com", "youtube.com", "blogger.com", "blogspot.com")):
                continue

            # Check parent div has class 'g' for organic results
            parent = a_tag.find_element(By.XPATH, "./ancestor::div[@class='g']") if a_tag.find_elements(By.XPATH, "./ancestor::div[@class='g']") else None
            if not parent:
                continue

            title = title or href.split('/')[2]
            results.append((title, href))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


def _extract_ddg_results(driver, seen_urls: set, max_results: int) -> List[Tuple[str, str]]:
    """Extract search results from a DuckDuckGo search results page."""
    results = []
    try:
        from selenium.webdriver.common.by import By
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            WebDriverWait(driver, 10).until(
                lambda d: len(d.find_elements(By.TAG_NAME, "a")) > 10
            )
        except Exception:
            pass

        all_links = driver.find_elements(By.TAG_NAME, "a")
        excluded_domains = {"duckduckgo.com", "www.ddg.no", "ddg.no"}

        from urllib.parse import urlparse as _urlparse, unquote as _unquote
        for a_tag in all_links:
            href = a_tag.get_attribute("href")
            title = a_tag.text.strip()
            if not href or not href.startswith("http"):
                continue
            parsed = _urlparse(href)
            if parsed.hostname in excluded_domains:
                continue
            if href in seen_urls:
                continue

            # Handle DDG redirect links: duckduckgo.com/c/?uddg=URL
            if "duckduckgo.com/c/" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    href = _unquote(m.group(1))
                else:
                    continue
                if href in seen_urls:
                    continue

            seen_urls.add(href)

            if not title:
                try:
                    title = _urlparse(href).netloc
                except Exception:
                    title = href

            results.append((title, href))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

def _extract_text_and_links(html: str, max_chars: int = 8000,
                             max_links: int = 30) -> Tuple[str, List[str]]:
    """
    Strip scripts, styles, and clutter; extract visible text and links.

    Parameters:
        html: Raw HTML string
        max_chars: Maximum number of text characters to return
        max_links: Maximum number of links to return

    Returns:
        (text_content, links_list) where text_content is plaintext and
        links_list is a list of "text -> url" strings.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script and style elements
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    # Grab text from <main> / <article> / <body> for best relevance
    main = soup.find("main") or soup.find("article") or soup.find("body")
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Collapse excessive blank lines
    lines = [line for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(lines)

    # Extract links
    links = []
    for m in re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE
    ):
        href, link_text = m.groups()
        if not href.startswith("http"):
            continue
        text_clean = re.sub(r"<[^>]+>", "", link_text).strip()
        if text_clean:
            links.append(f"{text_clean} -> {href}")

    cleaned = cleaned[:max_chars]
    return cleaned, links[:max_links]


def fetch_page_via_browser(
    url: str,
    timeout: int = 30,
    headless: bool = False,
    max_fetch_policy: Optional[dict] = None,
) -> str:
    """
    Open a URL in the user's real browser (via Selenium) and extract the
    rendered content.

    Flow:
    1. Create/connect to a Selenium WebDriver with the user's Chrome profile
    2. Navigate to the target URL
    3. Wait for the page to load
    4. Extract text and links via BeautifulSoup from driver.page_source
    5. Return formatted result

    Parameters:
        url: URL to fetch
        timeout: Seconds to wait for page load
        headless: Whether to run in headless mode
        max_fetch_policy: {"max_text_chars": 8000, "max_links": 30}

    Returns:
        Extracted text (up to max_chars) + links, or an error message.
    """
    mfp = max_fetch_policy or {}
    max_chars = mfp.get("max_text_chars", 8000)
    max_links = mfp.get("max_links", 30)

    driver = None
    temp_profile = None
    try:
        driver = create_driver(headless=headless)
        chrome_opts = getattr(driver, "_chrome_options", None)
        if chrome_opts:
            temp_profile = getattr(chrome_opts, "_temp_profile", None)

        driver.get(url)

        # Wait for page load
        max_wait = min(timeout, 30)
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            WebDriverWait(driver, max_wait).until(
                lambda d: d.execute_script("return document.readyState === 'complete'")
            )
        except Exception:
            pass  # Continue anyway with partial content
        time.sleep(2)  # Extra time for lazy-loaded content

        # Handle DDG anomaly CAPTCHA
        try:
            from selenium.webdriver.common.by import By
            boxes = driver.find_elements(By.CSS_SELECTOR, '[data-testid^="anomaly-modal-tile-"]')
            for box in boxes:
                try:
                    box.click()
                    time.sleep(0.15)
                except Exception:
                    pass
            form = driver.find_element(By.ID, "challenge-form")
            form.submit()
            time.sleep(5)
        except Exception:
            pass  # No CAPTCHA or handling failed

        html = driver.page_source

    except Exception as e:
        return f"Error fetching URL via browser: {e}"
    finally:
        if driver:
            chrome_process = getattr(driver, "_chrome_process", None)
            if chrome_process:
                try:
                    chrome_process.terminate()
                    chrome_process.wait(timeout=3)
                except Exception:
                    try:
                        chrome_process.kill()
                    except Exception:
                        pass
            driver.quit()
        if temp_profile and os.path.isdir(temp_profile):
            try:
                shutil.rmtree(temp_profile, ignore_errors=True)
            except Exception:
                pass

    text_content, links_list = _extract_text_and_links(html, max_chars, max_links)

    link_block = "\n".join(links_list) if links_list else "(no external links found)"
    return (
        f"--- Extracted text (via user's real browser) ---\n{text_content}\n\n"
        f"--- Links (first {max_links}) ---\n{link_block}"
    )


def browser_fetch(
    url: str,
    timeout: int = 30,
    headless: bool = False,
    max_fetch_policy: Optional[dict] = None,
) -> str:
    """
    Fetch a URL and return extracted plain text + links, using the user's
    real browser via Selenium.

    This is a convenience wrapper around fetch_page_via_browser that provides
    the same interface as web_tools.fetch_text_url but uses the user's logged-in
    browser state.

    Returns extracted text + links.
    """
    return fetch_page_via_browser(url, timeout=timeout, headless=headless,
                                  max_fetch_policy=max_fetch_policy)


def kill_active_chrome() -> bool:
    """Find and kill any Chrome/Chromium process using port 9222.

    Used before a new Selenium web operation to ensure the debugging
    port is free and no zombie Chrome process is lingering.

    Returns True if a Chrome process was killed, False otherwise.
    """
    killed = False
    try:
        if platform.system() == "Windows":
            # Windows: use netstat + taskkill
            try:
                result = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    if ":9222" in line and "LISTENING" in line:
                        pid = line.strip().split()[-1]
                        if pid.isdigit():
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/PID", pid],
                                    capture_output=True, timeout=5
                                )
                                killed = True
                            except Exception:
                                pass
            except Exception:
                pass
        else:
            # Unix-like: lsof or ss to find PID on port 9222
            for cmd in (
                ["lsof", "-i", ":9222", "-t"],
                ["ss", "-tlnp"],
            ):
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=5
                    )
                    if cmd[0] == "lsof":
                        for pid in result.stdout.strip().splitlines():
                            pid = pid.strip()
                            if pid.isdigit():
                                try:
                                    subprocess.run(
                                        ["kill", "-9", pid],
                                        capture_output=True, timeout=5
                                    )
                                    killed = True
                                except Exception:
                                    pass
                    elif "9222" in result.stdout:
                        # ss output: extract PID from "users:((\"chrome\",pid=1234,fd=...))"
                        import re as _re
                        pids = _re.findall(r"pid=(\d+)", result.stdout)
                        for pid in pids:
                            try:
                                subprocess.run(
                                    ["kill", "-9", pid],
                                    capture_output=True, timeout=5
                                )
                                killed = True
                            except Exception:
                                pass
                    break
                except Exception:
                    continue
    except Exception:
        pass
    return killed


def open_url_in_browser(url: str) -> bool:
    """Open a URL in the user's default system browser (non-Selenium)."""
    import sys
    if sys.platform == "win32":
        try:
            os.startfile(url)
            return True
        except OSError:
            pass
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=True)
            return True
        except Exception:
            return False
    elif sys.platform == "darwin":
        try:
            subprocess.Popen(["open", url])
            return True
        except Exception:
            return False
    else:
        try:
            subprocess.Popen(["xdg-open", url])
            return True
        except Exception:
            return False
