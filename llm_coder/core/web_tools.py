"""
Unified web search / fetch tools. The agent calls **web_search(query)** to find
pages and **fetch_text_url(url)** to read them.  The *how* (requests,
Playwright headless, or Selenium real-browser) is determined by the current
web-access policy set in Settings — the agent never sees the backend choice.

Web access modes:
- off: no web tools available
- whitelist_only: fetch-only, restricted to allowlisted domains, no search
- search_and_fetch_safe: DuckDuckGo HTML search + requests-based fetch (no JS)
- tor: same DuckDuckGo HTML search + requests-based fetch, but every request is
  routed through the local Tor SOCKS5 proxy (see _tor_session) for anonymity
- open_web: Playwright headless Chromium for search + fetch (full JS)
- open_browser: Selenium + user's Chrome profile for search + fetch (full JS,
  logged-in cookies, best for SPAs / login-gated pages)
"""
import re
import requests
from urllib.parse import urlparse, unquote, parse_qs
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urljoin

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/130.0"
)

# Tor Browser's own UA (Firefox ESR on the Tor Browser build).  Tor traffic
# must look like every *other* Tor Browser user — reusing the Chrome/macOS
# USER_AGENT above would fingerprint this app's traffic distinctly against
# the bulk of real Tor Browser traffic on the same exit/circuits.
TOR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"
)

# Default local Tor SOCKS5 proxy (the standard SocksPort of a local tor
# daemon — Tor Browser's own browser is 9150, but the standalone daemon and
# this app's requests-based path use 9050). Overridden at runtime from
# Settings > Web Access via set_tor_proxy() (see core/tools.py
# ToolExecutor.set_tor_proxy, wired from the tor_socks_host/tor_socks_port
# keys of the persisted web_policy dict).
DEFAULT_TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"


def set_tor_proxy(host: str, port: int) -> None:
    """Point the Tor backends at a non-default local SOCKS5 proxy (e.g.
    127.0.0.1:9150 for Tor Browser's built-in browser proxy instead of the
    standalone daemon's 9050)."""
    global DEFAULT_TOR_SOCKS_PROXY
    DEFAULT_TOR_SOCKS_PROXY = f"socks5h://{host}:{int(port)}"
# if __name__ == "__main__":
#     import sys
#     query = sys.argv[1] if len(sys.argv) > 1 else "pancake recipe"
#     policy = sys.argv[2] if len(sys.argv) > 2 else "search_and_fetch_safe"
#     print("=== web_search HTTP backend ===")
#     r1 = web_search(query, policy=policy)
#     print(r1[:500] if len(r1) > 500 else r1)
#     print("\n=== fetch_text_url HTTP backend ===")
#     if "http" in r1:
#         url = [l for l in r1.split("\n") if l.startswith("http")][0] if any("http" in l for l in r1.split("\n")) else ""
#         if url:
#             print(fetch_text_url(url, policy=policy)[:500])
#         else:
#             print("(no URLs found in search results)")
DEFAULT_MAX_FETCH_BYTES = 500_000

SEARCH_URLS = {
    "google":      "https://www.google.com/search?q={query}",
    "duckduckgo":  "https://duckduckgo.com/?q={query}",
}


class WebPolicyError(Exception):
    pass


class WebBrowserKillNeeded(Exception):
    """Raised when a web operation detects an active browser that should be killed first."""
    def __init__(self, browser_type: str):
        self.browser_type = browser_type


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def check_policy(url: Optional[str], policy: str,
                 allowlist: List[str], blocklist: List[str]) -> None:
    """Raise WebPolicyError when the url/policy combo is not allowed.

    ``url`` may be *None* (used for search where there is no URL).
    """
    domain = _domain(url) if url else ""
    if policy == "off":
        raise WebPolicyError(
            "Web access is disabled by current policy. "
            "Enable it in Settings > Web Access."
        )
    if domain and any(domain.endswith(b) for b in blocklist):
        raise WebPolicyError(
            f"Domain '{domain}' is explicitly blocked by policy."
        )
    if policy == "whitelist_only":
        if not domain or not any(domain.endswith(a) for a in allowlist):
            raise WebPolicyError(
                f"Domain '{domain}' is not in the whitelist. "
                "Use 'open_browser' or 'search_and_fetch_safe' in Settings > Web "
                "Access, or add the domain to the allowlist."
            )
    # search_and_fetch_safe / open_web / open_browser / tor: allowed unless blocklisted


# ---------------------------------------------------------------------------
# PUBLIC TOOL 1 — web_search
# ---------------------------------------------------------------------------

def web_search(query: str, engine: str = "duckduckgo", max_results: int = 6,
               policy: str = "off", allowlist: Optional[List[str]] = None,
               blocklist: Optional[List[str]] = None) -> str:
    """Search the web for a query and return structured results.

    The backend used is determined by the current web-access policy:
    - *search_and_fetch_safe* → DuckDuckGo HTML via plain HTTP
    - *open_web* → Playwright headless Chromium
    - *open_browser* → Selenium + user's real Chrome (logged-in cookies)

    Supported engines: ``duckduckgo`` (default) and ``google``.

    Returns a formatted string with each result as::

        1. TITLE
           URL
           snippet  (HTTP backend only)

    On failure, the message suggests what to do next.
    """
    allowlist = allowlist or []
    blocklist = blocklist or []

    check_policy(None, policy, allowlist, blocklist)  # skip domain checks

    # Pre-flight: check for active browser that needs killing
    if policy == "open_web":
        _check_and_raise_if_browser_active("playwright")
    elif policy == "open_browser":
        _check_and_raise_if_browser_active("selenium")

    if policy == "whitelist_only":
        return (
            "Error: search is not available in whitelist_only mode — only direct "
            "fetches to whitelisted domains are allowed. "
            "Use fetch_text_url for whitelisted URLs, or switch to "
            "'search_and_fetch_safe' or higher in Settings > Web Access."
        )

    if policy == "search_and_fetch_safe":
        return _web_search_http(query, engine, max_results, blocklist)
    if policy == "open_web":
        return _web_search_playwright(query, engine, max_results, blocklist)
    if policy == "open_browser":
        return _web_search_selenium(query, engine, max_results, blocklist)
    if policy == "tor":
        return _web_search_tor(query, engine, max_results, blocklist)

    return f"Error: unknown web policy '{policy}'."


# ---------------------------------------------------------------------------
# PUBLIC TOOL 2 — fetch_text_url
# ---------------------------------------------------------------------------

def fetch_text_url(url: str, timeout: int = 30,
                   policy: str = "off", allowlist: Optional[List[str]] = None,
                   blocklist: Optional[List[str]] = None,
                   max_fetch_policy: Optional[dict] = None) -> str:
    """Fetch a URL and return extracted plain text and links.

    The backend is determined by policy:
    - *search_and_fetch_safe* → plain HTTP, no JS rendering
    - *open_web* → Playwright headless Chromium (full JS rendering)
    - *open_browser* → Selenium + user's Chrome (full JS, logged-in cookies)

    Scripts, styles, and visual clutter are stripped.

    ``max_fetch_policy`` controls truncation behaviour:
        {"max_text_chars": 8000, "max_raw_bytes": 500000,
         "max_links": 30, "truncation_mode": "show_popup"}

    Subject to the active web-access policy (whitelist / blocklist enforcement).
    """
    allowlist = allowlist or []
    blocklist = blocklist or []
    mfp = max_fetch_policy or {
        "max_text_chars": 8000,
        "max_raw_bytes": DEFAULT_MAX_FETCH_BYTES,
        "max_links": 30,
        "truncation_mode": "show_popup",
    }

    check_policy(url, policy, allowlist, blocklist)

    # Pre-flight: check for active browser that needs killing
    if policy == "open_web":
        _check_and_raise_if_browser_active("playwright")
    elif policy == "open_browser":
        _check_and_raise_if_browser_active("selenium")

    if policy == "search_and_fetch_safe":
        return _fetch_text_http(url, mfp)
    if policy == "open_web":
        return _fetch_text_playwright(url, timeout, mfp)
    if policy == "open_browser":
        return _fetch_text_selenium(url, timeout, mfp)
    if policy == "tor":
        return _fetch_text_tor(url, timeout, mfp)

    return f"Error: unknown web policy '{policy}'."


# ---------------------------------------------------------------------------
# Internal: HTTP backend (search)
# ---------------------------------------------------------------------------

def _web_search_http(query: str, engine: str, max_results: int,
                     blocklist: List[str]) -> str:
    """DuckDuckGo HTML scraping via requests."""
    if engine != "duckduckgo":
        engine = "duckduckgo"  # html endpoint is DDG-only

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

        def _get_text(resp):
            if resp.text:
                return resp.text
            if resp.raw:
                raw = resp.raw.read()
                if resp.headers.get("Content-Encoding", "").startswith("gzip"):
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
            return ""

        resp = session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query}, timeout=10
        )
        resp.raise_for_status()
        page_text = _get_text(resp)

        # Auto-submit DuckDuckGo image challenge (CAPTCHA)
        if "anomaly-modal" in page_text:
            soup = BeautifulSoup(page_text, "html.parser")
            img_form = soup.find("form", id="img-form")
            challenge_form = soup.find("form", id="challenge-form")
            if img_form and challenge_form:
                img_action = img_form.get("action", "")
                if img_action:
                    session.post(urljoin("https://duckduckgo.com", img_action), timeout=10)
                payload = {}
                for inp in challenge_form.find_all("input"):
                    name = inp.get("name")
                    if name:
                        payload[name] = "1"
                if img_form:
                    for inp in img_form.find_all("input"):
                        name = inp.get("name")
                        if name and name not in payload:
                            payload[name] = inp.get("value", "")
                challenge_action = challenge_form.get("action", "")
                if challenge_action:
                    resp = session.post(
                        urljoin("https://duckduckgo.com", challenge_action),
                        data=payload, timeout=10
                    )
                    resp.raise_for_status()
                    page_text = _get_text(resp)

        soup = BeautifulSoup(page_text, "html.parser")
        results = []
        for row in soup.find_all("div", class_="result__wrapper"):
            link_el = row.find("a", class_="result__a")
            snippet_el = row.find("a", class_="result__snippet")
            if not link_el:
                continue
            raw_url = link_el.get("href", "")
            title = link_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            if raw_url.startswith("http"):
                real_url = raw_url
            elif "uddg=" in raw_url:
                m = re.search(r"uddg=([^&]+)", raw_url)
                real_url = unquote(m.group(1)) if m else raw_url
            else:
                continue

            if _domain(real_url).endswith("duckduckgo.com"):
                continue
            if any(_domain(real_url).endswith(b) for b in blocklist):
                continue

            results.append(f"{title}\n{real_url}\n{snippet}")
            if len(results) >= max_results:
                break

        if not results:
            return (
                f"No results parsed from DuckDuckGo for query '{query}'. "
                "Check these causes in order: "
                "(1) The query may be too long or phrased as a natural sentence. Search engines match "
                "keywords: retry with just 2-6 core keywords, dropping stopwords and descriptive "
                "wording. Adding MORE words to narrow it down will make this worse, not better. "
                "(2) DuckDuckGo's HTML structure changed or the request was rate-limited/blocked. "
                "If a shortened query still returns nothing: try fetch_text_url directly on a "
                "well-known URL for this topic if you know one, or inform the user that web "
                "search is currently returning no parseable results and ask how to proceed."
            )
    except Exception as e:
        return f"Error performing search: {e}"
    return "\n\n---\n\n".join(results)


# ---------------------------------------------------------------------------
# Internal: Playwright backend (search + fetch)
# ---------------------------------------------------------------------------

def _web_search_playwright(query: str, engine: str, max_results: int,
                           blocklist: List[str]) -> str:
    """Search via Playwright headless Chromium."""
    if engine not in SEARCH_URLS:
        engine = "duckduckgo"

    try:
        from . import browser_fetcher as _bf
    except ImportError:
        return (
            f"Error: Playwright is not installed. "
            "Install with: pip install playwright && playwright install chromium. "
            "Or switch to 'search_and_fetch_safe' mode in Settings > Web Access "
            "which uses lightweight HTTP search."
        )

    if not _bf._ensure_playwright():
        return (
            f"Error: Playwright could not initialise headless Chromium. "
            "Install with: pip install playwright && playwright install chromium. "
            "Or switch to 'search_and_fetch_safe' mode in Settings > Web Access."
        )

    # Re-fetch browser reference after ensure (it could have been reset by close_browser
    # between the import above and now)
    pb_browser = _bf._browser
    if pb_browser is None:
        return (
            "Error: Playwright browser instance is unavailable. "
            "This can happen if Playwright is installed but Chromium binary is missing, "
            "or the browser was closed during a previous fetch. Install Chromium with "
            "'playwright install chromium' or switch to 'search_and_fetch_safe' mode "
            "in Settings > Web Access."
        )

    try:
        page = pb_browser.new_page()
    except Exception as e:
        return (
            f"Error starting browser: {e}. Install Playwright and Chromium, or switch "
            "to 'search_and_fetch_safe' mode in Settings > Web Access."
        )

    results: List[Tuple[str, str]] = []
    seen: set = set()
    try:
        search_url = SEARCH_URLS[engine].format(query=query)
        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        if engine == "google":
            results = _extract_google_results(page, seen, max_results)
        else:
            results = _extract_ddg_results(page, seen, max_results)

        results = [r for r in results if not any(_domain(r[1]).endswith(b) for b in blocklist)]

    except Exception as e:
        return f"Error performing search: {e}"
    finally:
        try:
            page.close()
        except Exception:
            pass

    if not results:
        return (
            f"No results from browser search for query '{query}'. "
            "If the query is long or phrased as a natural sentence, retry with just 2-6 core "
            "keywords — adding more words narrows results further and will not help. "
            "Also ensure the browser is installed and try again. You can also try "
            "fetch_text_url on a well-known URL for this topic."
        )

    return _format_search_results(results)


def _fetch_text_playwright(url: str, timeout: int = 30, max_fetch_policy: Optional[dict] = None) -> str:
    """Fetch a page via Playwright headless Chromium."""
    from . import browser_fetcher as _bf

    if not _bf._browser:
        # Browser was closed; try to re-initialise
        if not _bf._ensure_playwright():
            return (
                "Error: Playwright browser is not available. "
                "Install Chromium with 'playwright install chromium' or switch to "
                "'search_and_fetch_safe' mode in Settings > Web Access."
            )
        if _bf._browser is None:
            return (
                "Error: Playwright browser instance is unavailable. "
                "Install Chromium with 'playwright install chromium' or switch to "
                "'search_and_fetch_safe' mode in Settings > Web Access."
            )

    result = _bf.fetch_page(url, timeout_ms=timeout * 1000)
    truncated_reasons = []

    if result.startswith("Error:") or result.startswith("[Playwright unavailable"):
        # Fallback to enhanced requests
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "none",
                    "sec-fetch-user": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=15,
                stream=True,
            )
            resp.raise_for_status()
            max_bytes = max_fetch_policy.get("max_raw_bytes", DEFAULT_MAX_FETCH_BYTES) if max_fetch_policy else DEFAULT_MAX_FETCH_BYTES
            content = resp.raw.read(max_bytes, decode_content=True)
            if len(content) < max_bytes:
                html = content.decode(resp.encoding or "utf-8", errors="replace")
            else:
                truncated_reasons.append(f"page content ({len(content)} bytes, limit {max_bytes})")
                html = content.decode(resp.encoding or "utf-8", errors="replace")
        except Exception as e:
            return f"{result}\nFallback requests fetch also failed: {e}. Verify the URL is correct and reachable."

        mfp = max_fetch_policy or {}
        return _extract_text_and_links_html(html, mfp.get("max_text_chars", 8000), mfp.get("max_links", 30))

    mfp = max_fetch_policy or {}
    max_chars = mfp.get("max_text_chars", 8000)
    max_links = mfp.get("max_links", 30)
    # Check if the result was truncated by browser_fetcher
    if result.startswith("[FETCH_TRUNCATED:"):
        truncated_reasons.append("extracted text")
    result = _apply_fetch_limits(result, max_chars, max_links, truncated_reasons)
    # Also check if the text section is now at the max (meaning it was truncated)
    text_marker = "--- Extracted text ---"
    if text_marker in result:
        after_marker = result.split(text_marker, 1)[1]
        link_pos = after_marker.find("--- Links")
        text_block = after_marker[:link_pos] if link_pos >= 0 else after_marker
        if len(text_block) >= max_chars:
            truncated_reasons.append("extracted text")
        # Check for truncated links note
        if "... more links truncated" in after_marker:
            truncated_reasons.append("links")
    trunc_mode = mfp.get("truncation_mode", "show_popup")
    return _maybe_prepend_truncation(result, truncated_reasons, trunc_mode)


# ---------------------------------------------------------------------------
# Internal: Selenium backend (search + fetch)
# ---------------------------------------------------------------------------

def _web_search_selenium(query: str, engine: str, max_results: int,
                         blocklist: List[str]) -> str:
    """Search via Selenium + user's real Chrome."""
    try:
        from .browser_connector import browser_search as _bs  # noqa: F811
    except ImportError:
        return (
            "Error: Selenium is not installed or Chrome/Chromium not found. "
            "Install with: pip install selenium webdriver-manager and ensure "
            "Chrome/Chromium is on your system. Or switch to "
            "'search_and_fetch_safe' mode in Settings > Web Access."
        )

    results = _bs(query, engine=engine, max_results=max_results)
    results = [r for r in results if not any(_domain(r[1]).endswith(b) for b in blocklist)]

    if not results:
        return (
            f"No results from browser search for query '{query}'. "
            "If the query is long or phrased as a natural sentence, retry with just 2-6 core "
            "keywords — adding more words narrows results further and will not help. "
            "Also ensure Chrome is installed and try again. You can also try "
            "fetch_text_url on a well-known URL for this topic."
        )

    return _format_search_results(results)


def _fetch_text_selenium(url: str, timeout: int = 30, max_fetch_policy: Optional[dict] = None) -> str:
    """Fetch a page via Selenium + user's real Chrome."""
    mfp = max_fetch_policy or {}
    max_chars = mfp.get("max_text_chars", 8000)
    max_links = mfp.get("max_links", 30)
    truncated_reasons = []
    try:
        from .browser_connector import fetch_page_via_browser
    except ImportError:
        return (
            "Error: Selenium is not installed or Chrome/Chromium not found. "
            "Install with: pip install selenium webdriver-manager and ensure "
            "Chrome/Chromium is on your system. Or switch to "
            "'search_and_fetch_safe' mode in Settings > Web Access."
        )
    try:
        result = fetch_page_via_browser(url, timeout=timeout,
                                        max_fetch_policy=mfp)
        # Also check if text was truncated to the limit
        text_marker = "--- Extracted text ---"
        if text_marker in result:
            after_marker = result.split(text_marker, 1)[1]
            link_pos = after_marker.find("--- Links")
            text_block = after_marker[:link_pos] if link_pos >= 0 else after_marker
            if len(text_block) >= max_chars:
                truncated_reasons.append("extracted text")
        trunc_mode = mfp.get("truncation_mode", "show_popup")
        return _maybe_prepend_truncation(result, truncated_reasons, trunc_mode)
    except Exception as e:
        return f"Error fetching URL via browser: {e}. Verify the URL is correct and reachable."


# ---------------------------------------------------------------------------
# Internal: plain HTTP fetch
# ---------------------------------------------------------------------------

def _fetch_text_http(url: str, max_fetch_policy: dict) -> str:
    """Fetch a page with plain requests — no JS rendering."""
    max_bytes = max_fetch_policy.get("max_raw_bytes", DEFAULT_MAX_FETCH_BYTES)
    max_chars = max_fetch_policy.get("max_text_chars", 8000)
    max_links = max_fetch_policy.get("max_links", 30)
    trunc_mode = max_fetch_policy.get("truncation_mode", "show_popup")

    truncated_reasons = []

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(max_bytes, decode_content=True)
        if len(content) < max_bytes:
            html = content.decode(resp.encoding or "utf-8", errors="replace")
        else:
            truncated_reasons.append(f"page content ({len(content)} bytes, limit {max_bytes})")
            html = content.decode(resp.encoding or "utf-8", errors="replace")
    except Exception as e:
        return f"Error fetching URL: {e}. Verify the URL is correct and reachable."

    result = _extract_text_and_links_html(html, max_chars, max_links)
    return _maybe_prepend_truncation(result, truncated_reasons, trunc_mode)


# ---------------------------------------------------------------------------
# Internal: Tor backend (search + fetch)
# ---------------------------------------------------------------------------
#
# These behave exactly like the plain-HTTP backends above (requests +
# BeautifulSoup scraping — no browser), but EVERY request is routed through a
# local Tor SOCKS5 proxy so the user's real IP/DNS never leaks to the origin
# server. Leak-avoidance choices baked in here:
#   * "socks5h" (remote DNS): the hostname is resolved by the Tor exit
#     relay, never by this machine's local resolver — so no DNS leak.
#   * TOR_USER_AGENT (Tor Browser's Firefox UA) + a generic Accept-Language,
#     so this app's traffic blends in with normal Tor Browser traffic
#     instead of standing out as Chrome/macOS.
#   * A brand-new requests.Session per call with no cookie jar persistence,
#     so no long-lived cookie identifier is carried across calls.

def _tor_session(proxies: Optional[dict] = None) -> "requests.Session":
    """Build a fresh requests.Session routed through a Tor SOCKS5 proxy.

    Uses socks5h (remote DNS) so DNS lookups happen at the exit relay, not
    locally. ``proxies`` may override the default proxy dict (e.g. to point
    at Tor Browser's 9150 port instead of the daemon's 9050).
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": TOR_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    })
    session.proxies = proxies or {"http": DEFAULT_TOR_SOCKS_PROXY,
                                   "https": DEFAULT_TOR_SOCKS_PROXY}
    return session


def _tor_proxy_or_error() -> Optional[dict]:
    """Return the proxy dict, or None plus an error string is returned by the
    caller when PySocks is missing (requests[socks] not installed)."""
    try:
        import socks  # noqa: F401  (PySocks — required for socks5h:// support)
    except ImportError:
        return None
    return {"http": DEFAULT_TOR_SOCKS_PROXY, "https": DEFAULT_TOR_SOCKS_PROXY}


def _tor_deps_error() -> str:
    return (
        "Error: the Tor tools need PySocks to talk to the local Tor SOCKS5 "
        "proxy. Install it with: pip install 'requests[socks]' (or pip install PySocks). "
        "Also make sure a Tor daemon is listening on 127.0.0.1:9050 "
        "(Tor Browser itself uses 9150)."
    )


def _web_search_tor(query: str, engine: str, max_results: int,
                    blocklist: List[str]) -> str:
    """DuckDuckGo HTML search routed through the local Tor SOCKS5 proxy."""
    proxies = _tor_proxy_or_error()
    if proxies is None:
        return _tor_deps_error()
    if engine != "duckduckgo":
        engine = "duckduckgo"  # html endpoint is DDG-only

    try:
        session = _tor_session(proxies)

        def _get_text(resp):
            if resp.text:
                return resp.text
            if resp.raw:
                raw = resp.raw.read()
                if resp.headers.get("Content-Encoding", "").startswith("gzip"):
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
            return ""

        resp = session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query}, timeout=20,
        )
        resp.raise_for_status()
        page_text = _get_text(resp)

        # Auto-submit DuckDuckGo anomaly/CAPTCHA challenge (same flow as the
        # plain-HTTP backend — Tor exits are more likely to be challenged).
        if "anomaly-modal" in page_text:
            soup = BeautifulSoup(page_text, "html.parser")
            img_form = soup.find("form", id="img-form")
            challenge_form = soup.find("form", id="challenge-form")
            if img_form and challenge_form:
                img_action = img_form.get("action", "")
                if img_action:
                    session.post(urljoin("https://duckduckgo.com", img_action), timeout=20)
                payload = {}
                for inp in challenge_form.find_all("input"):
                    name = inp.get("name")
                    if name:
                        payload[name] = "1"
                for inp in img_form.find_all("input"):
                    name = inp.get("name")
                    if name and name not in payload:
                        payload[name] = inp.get("value", "")
                challenge_action = challenge_form.get("action", "")
                if challenge_action:
                    resp = session.post(
                        urljoin("https://duckduckgo.com", challenge_action),
                        data=payload, timeout=20,
                    )
                    resp.raise_for_status()
                    page_text = _get_text(resp)

        soup = BeautifulSoup(page_text, "html.parser")
        results = []
        for row in soup.find_all("div", class_="result__wrapper"):
            link_el = row.find("a", class_="result__a")
            snippet_el = row.find("a", class_="result__snippet")
            if not link_el:
                continue
            raw_url = link_el.get("href", "")
            title = link_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            if raw_url.startswith("http"):
                real_url = raw_url
            elif "uddg=" in raw_url:
                m = re.search(r"uddg=([^&]+)", raw_url)
                real_url = unquote(m.group(1)) if m else raw_url
            else:
                continue
            if _domain(real_url).endswith("duckduckgo.com"):
                continue
            if any(_domain(real_url).endswith(b) for b in blocklist):
                continue
            results.append(f"{title}\n{real_url}\n{snippet}")
            if len(results) >= max_results:
                break

        if not results:
            return (
                f"No results parsed from DuckDuckGo (via Tor) for query '{query}'. "
                "Check these causes in order: "
                "(1) The query may be too long or phrased as a natural sentence. Search engines match "
                "keywords: retry with just 2-6 core keywords, dropping stopwords and descriptive "
                "wording. Adding MORE words to narrow it down will make this worse, not better. "
                "(2) The Tor exit relay was challenged/rate-limited, or DuckDuckGo's HTML "
                "structure changed. Retry once, and check that Tor is running on 127.0.0.1:9050."
            )
        return "\n\n---\n\n".join(results)
    except Exception as e:
        return f"Error performing Tor search: {e}"


def _fetch_text_tor(url: str, timeout: int = 30, max_fetch_policy: Optional[dict] = None) -> str:
    """Fetch a URL over the local Tor SOCKS5 proxy — no JS rendering."""
    proxies = _tor_proxy_or_error()
    if proxies is None:
        return _tor_deps_error()

    mfp = max_fetch_policy or {}
    max_bytes = mfp.get("max_raw_bytes", DEFAULT_MAX_FETCH_BYTES)
    max_chars = mfp.get("max_text_chars", 8000)
    max_links = mfp.get("max_links", 30)
    trunc_mode = mfp.get("truncation_mode", "show_popup")
    truncated_reasons = []

    try:
        session = _tor_session(proxies)
        resp = session.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(max_bytes, decode_content=True)
        if len(content) >= max_bytes:
            truncated_reasons.append(f"page content ({len(content)} bytes, limit {max_bytes})")
        html = content.decode(resp.encoding or "utf-8", errors="replace")
    except Exception as e:
        return f"Error fetching URL via Tor: {e}. Verify the URL is correct and reachable via Tor."

    result = _extract_text_and_links_html(html, max_chars, max_links)
    return _maybe_prepend_truncation(result, truncated_reasons, trunc_mode)


def _apply_fetch_limits(result: str, max_chars: int, max_links: int,
                         truncated_reasons: list) -> str:
    """Re-apply character and link limits to a pre-extracted result string.

    Detects the "--- Extracted text ---" and "--- Links ..." sections and
    re-truncates them.  Appends to ``truncated_reasons`` when limits are hit.
    Returns the possibly-truncated string.
    """
    # Re-truncate text section
    text_marker = "--- Extracted text ---"
    if text_marker in result:
        parts = result.split(text_marker, 1)
        text_block = parts[1] if len(parts) > 1 else ""
        # Text block is everything up to "--- Links ---"
        link_marker = "--- Links"
        if link_marker in text_block:
            text_only, link_part = text_block.split(link_marker, 1)
            if len(text_only) > max_chars:
                truncated_reasons.append("extracted text")
                text_only = text_only[:max_chars]
            result = parts[0] + text_marker + text_only + link_marker + link_part
        else:
            if len(text_block) > max_chars:
                truncated_reasons.append("extracted text")
                text_block = text_block[:max_chars]
            result = parts[0] + text_marker + text_block
    return result


def _extract_text_and_links_html(html: str, max_chars: int = 8000, max_links: int = 30) -> str:
    """Strip scripts/styles/clutter from HTML, extract text + links."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    links = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE):
        href, text = m.groups()
        if not href.startswith("http"):
            continue
        text_clean = re.sub(r"<[^>]+>", "", text).strip()
        if text_clean:
            links.append(f"{text_clean} -> {href}")

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    link_block = "\n".join(links[:max_links])
    return f"--- Extracted text ---\n{text}\n\n--- Links (first {max_links}) ---\n{link_block}"


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------

_TRUNCATION_MARKER = "[FETCH_TRUNCATED: "


def _maybe_prepend_truncation(content: str, truncated_reasons: list, mode: str) -> str:
    """Prepend a truncation marker if reasons exist and mode is not 'silent'."""
    if not truncated_reasons:
        return content
    reason_text = " and ".join(truncated_reasons)
    if mode == "silent":
        return f"[Truncated — {reason_text}]\n{content}"
    return f"{_TRUNCATION_MARKER}truncated — page exceeded limit ({reason_text}) — " \
           "you can increase limits in Settings > Web Access, or use fetch_text_url again " \
           f"with a higher max_fetch_policy.]\n\n{content}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _format_search_results(results: List[Tuple[str, str]]) -> str:
    """Format a list of (title, url) tuples into a numbered list."""
    lines = []
    for idx, (title, url) in enumerate(results, start=1):
        lines.append(f"{idx}. {title}")
        lines.append(f"   {url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _extract_google_results(page, seen_urls: set, max_results: int) -> List[Tuple[str, str]]:
    """Extract organic search results from a Google results page (Playwright)."""
    results = []
    try:
        try:
            page.wait_for_selector("div.g, h3", timeout=10000)
        except Exception:
            pass

        links = page.query_selector_all(
            "div.g a[href^='http'], div[data-hveid] a[href^='http'], "
            "div[jsname='UWUrTe'] a[href^='http']"
        )
        for link_el in links:
            href = link_el.get_attribute("href") or ""
            title_el = link_el.query_selector("h3")
            title = (title_el.inner_text() or "").strip() if title_el else ""

            if not href:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            domain = _domain(href)
            if domain and any(domain.endswith(d) for d in (
                "google.com", "youtube.com", "blogger.com", "blogspot.com"
            )):
                continue

            if not title:
                try:
                    title = urlparse(href).netloc
                except Exception:
                    title = href

            results.append((title, href))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


def _handle_ddg_anomaly(page) -> bool:
    """Handle DuckDuckGo anomaly CAPTCHA by clicking puzzle tiles. Returns True if passed."""
    try:
        body_html = page.inner_html("body")
        if "anomaly-modal" not in body_html:
            return True  # no CAPTCHA

        # Click all puzzle tiles to pass the CAPTCHA
        boxes = page.query_selector_all('[data-testid^="anomaly-modal-tile-"]')
        for box in boxes:
            try:
                box.click()
                page.wait_for_timeout(150)
            except Exception:
                pass

        # Submit the challenge form
        form = page.query_selector("#challenge-form")
        if form:
            form.submit()
            page.wait_for_timeout(5000)

        # Verify CAPTCHA is gone
        body_html = page.inner_html("body")
        return "anomaly-modal" not in body_html
    except Exception:
        return False


def _extract_ddg_results(page, seen_urls: set, max_results: int) -> List[Tuple[str, str]]:
    """Extract search results from a DuckDuckGo results page (Playwright)."""
    results = []
    try:
        # Try to handle CAPTCHA first
        _handle_ddg_anomaly(page)
        page.wait_for_timeout(3000)  # Let results render

        try:
            page.wait_for_selector('a[result-click], [data-result], [data-rid]', timeout=5000)
        except Exception:
            pass

        all_links = page.query_selector_all("a")
        excluded = {"duckduckgo.com", "www.ddg.no", "ddg.no"}

        for link_el in all_links:
            href = link_el.get_attribute("href") or ""
            title = (link_el.inner_text() or "").strip()

            if not href or not href.startswith("http"):
                continue
            parsed = urlparse(href)
            if parsed.hostname in excluded:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            if "duckduckgo.com/c/" in href:
                try:
                    qs = parse_qs(parsed.query)
                    uddg = qs.get("uddg", [None])[0]
                    if uddg:
                        href = uddg
                    else:
                        continue
                except Exception:
                    continue

            if not title:
                try:
                    title = urlparse(href).netloc
                except Exception:
                    title = href

            results.append((title, href))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# PUBLIC TOOL 3 — web_screen_capture
# ---------------------------------------------------------------------------

def web_screen_capture(url: str, mode: str = "full",
                       policy: str = "off", allowlist: Optional[List[str]] = None,
                       blocklist: Optional[List[str]] = None,
                       max_screenshots: int = 5) -> dict:
    """Take a screenshot (or scrolling screenshots) of a URL.

    Returns a dict with:
      - "text": Summary string describing what was captured
      - "images": List of (base64_encoded_png_string, mime_type) tuples

    The backend is determined by policy:
    - *open_web* → Playwright headless Chromium
    - *open_browser* → Selenium + user's real Chrome
    - *search_and_fetch_safe* / *whitelist_only* → limited support
    - *off* → disabled

    ``mode`` controls how the capture works:
    - "viewport": single screenshot of the visible viewport
    - "full": multiple viewport-height screenshots for long pages

    ``max_screenshots`` caps the number of chunks in "full" mode.
    """
    allowlist = allowlist or []
    blocklist = blocklist or []

    check_policy(url, policy, allowlist, blocklist)

    # Pre-flight: check for active browser that needs killing
    if policy == "open_web":
        _check_and_raise_if_browser_active("playwright")
    elif policy == "open_browser":
        _check_and_raise_if_browser_active("selenium")

    if policy == "off":
        return {"text": "Web access is disabled by current policy. Enable it in Settings > Web Access.", "images": []}
    if policy == "whitelist_only":
        # whitelist_only doesn't block fetch — screenshot is essentially a fetch
        pass
    elif policy == "search_and_fetch_safe":
        return {"text": "web_screen_capture is not available in 'search_and_fetch_safe' mode (HTTP-only, no JS rendering). "
                         "Switch to 'open_web' or 'open_browser' in Settings > Web Access to take screenshots.",
                "images": []}
    elif policy == "tor":
        return {"text": "web_screen_capture is not available in 'tor' mode (HTTP-only over Tor, no JS rendering). "
                         "Screenshots need a JS-rendering browser; use web_screen_capture in 'open_web'/'open_browser' "
                         "mode, or use fetch_text_url/tor_fetch_text_url to read the page text instead.",
                "images": []}

    if not url or not url.startswith("http"):
        return {"text": f"Error: '{url}' is not a valid URL.", "images": []}

    if policy == "open_web":
        return _capture_screenshot_playwright(url, mode, max_screenshots)
    elif policy == "open_browser":
        return _capture_screenshot_selenium(url, mode, max_screenshots)

    return {"text": f"Error: unknown web policy '{policy}'.", "images": []}


def _capture_screenshot_playwright(url: str, mode: str, max_screenshots: int) -> dict:
    """Take screenshots via Playwright headless Chromium."""
    import base64
    try:
        from . import browser_fetcher as _bf
    except ImportError:
        return {"text": "Error: Playwright is not installed. "
                         "Install with: pip install playwright && playwright install chromium.",
                "images": []}

    if not _bf._ensure_playwright():
        return {"text": "Error: Playwright could not initialise headless Chromium.", "images": []}

    # Re-fetch browser reference in case it was reset
    pb = _bf._browser
    if pb is None:
        return {"text": "Error: Playwright browser instance is unavailable.", "images": []}

    page = None
    try:
        page = pb.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)  # wait for lazy-loaded content

        images = []
        text_parts = [f"Screenshot(s) of '{url}':"]

        if mode == "viewport":
            img_bytes = page.screenshot(type="png", full_page=False)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            images.append((b64, "image/png"))
            text_parts.append("- Viewport screenshot (1 image)")
        else:
            # Full-page: split into viewport-height chunks
            viewport_height = page.viewport_size.get("height", 800)
            page_height = page.evaluate("document.documentElement.scrollHeight")

            if page_height <= viewport_height * 1.2:
                # Short page — single screenshot is fine
                img_bytes = page.screenshot(type="png", full_page=True)
                b64 = base64.b64encode(img_bytes).decode("ascii")
                images.append((b64, "image/png"))
                text_parts.append("- Full-page screenshot (page fits in one image)")
            else:
                # Scroll through and capture chunks
                scroll = 0
                count = 0
                while scroll < page_height and count < max_screenshots:
                    img_bytes = page.screenshot(type="png", full_page=False)
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    images.append((b64, "image/png"))
                    count += 1

                    if scroll + viewport_height >= page_height:
                        break
                    scroll = min(scroll + viewport_height, page_height - 1)
                    page.evaluate(f"window.scrollTo(0, {scroll})")
                    page.wait_for_timeout(500)  # brief wait after scroll

                text_parts.append(f"- Scrolling screenshot ({count} image{'s' if count > 1 else ''}, "
                                  f"viewport height={viewport_height}px, page height={int(page_height)}px)")

        page.close()
        text_parts.append("Shown above. Use view_image(path) to re-open if saved to disk.")
        return {"text": "\n".join(text_parts), "images": images}

    except Exception as e:
        if page:
            try:
                page.close()
            except Exception:
                pass
        return {"text": f"Error capturing screenshot via Playwright: {e}", "images": []}


def _capture_screenshot_selenium(url: str, mode: str, max_screenshots: int) -> dict:
    """Take screenshots via Selenium + user's real Chrome."""
    import base64
    try:
        from .browser_connector import create_driver
    except ImportError:
        return {"text": "Error: Selenium is not installed or Chrome/Chromium not found.",
                "images": []}

    def _save_to_bytes(driver) -> bytes:
        """Save screenshot to a temp file and return the bytes."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            driver.save_screenshot(temp_path)
            with open(temp_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(temp_path)

    driver = None
    try:
        driver = create_driver(headless=True)
        driver.set_page_load_timeout(30)
        driver.get(url)

        # Wait for page load — first check readyState, then verify the URL
        # actually changed (in case driver connected to an already-running
        # Chrome with existing tabs).  Then settle for heavy JS SPAs.
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            # Wait for document.readyState == 'complete'
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        # Verify the URL actually navigated to the target (handles the case
        # where Selenium connected to a Chrome instance that had existing tabs)
        import time as _time
        for _retry in range(5):
            current_url = driver.current_url
            if current_url.startswith(url.split("?")[0].split("#")[0]):
                break
            # URL didn't change — try navigating again
            driver.get(url)
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            _time.sleep(1)

        # Extra settle time for heavy JS SPAs (e.g. HuggingFace, GitHub)
        _time.sleep(3)

        images = []
        text_parts = [f"Screenshot(s) of '{url}':"]

        viewport_height = driver.execute_script("return window.innerHeight")
        page_height = driver.execute_script("return document.documentElement.scrollHeight")

        if mode == "viewport" or page_height <= viewport_height * 1.2:
            img_bytes = _save_to_bytes(driver)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            images.append((b64, "image/png"))
            if mode == "viewport":
                text_parts.append("- Viewport screenshot (1 image)")
            else:
                text_parts.append("- Full-page screenshot (page fits in one image)")
        else:
            # Full-page: scroll and capture chunks
            scroll = 0
            count = 0
            while scroll < page_height and count < max_screenshots:
                img_bytes = _save_to_bytes(driver)
                b64 = base64.b64encode(img_bytes).decode("ascii")
                images.append((b64, "image/png"))
                count += 1

                if scroll + viewport_height >= page_height:
                    break
                scroll = min(scroll + viewport_height, page_height - 1)
                driver.execute_script(f"window.scrollTo(0, {scroll});")
                _time.sleep(0.5)

            text_parts.append(f"- Scrolling screenshot ({count} image{'s' if count > 1 else ''}, "
                              f"viewport height={viewport_height}px, page height={int(page_height)}px)")

        text_parts.append("Shown above. Use view_image(path) to re-open if saved to disk.")
        return {"text": "\n".join(text_parts), "images": images}

    except Exception as e:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return {"text": f"Error capturing screenshot via Selenium: {e}", "images": []}


# ---------------------------------------------------------------------------
# PUBLIC TOOL 4 — extract_images
# ---------------------------------------------------------------------------

def extract_images(url: str, max_images: int = 5,
                   policy: str = "off", allowlist: Optional[List[str]] = None,
                   blocklist: Optional[List[str]] = None,
                   fetch_images: bool = True,
                   max_image_bytes: int = 2_000_000,
                   max_embed_images: int = 3) -> dict:
    """Extract images from a webpage URL.

    Scans the page for <img> tags and returns image URLs with optional
    base64-encoded image data for vision models.

    Returns a dict with:
      - "text": Description of found images (URLs, alt text, dimensions info)
      - "images": List of (base64_encoded_png_string, mime_type) tuples
                  (only populated when fetch_images=True and policy supports it)

    Supported image sources: <img src>, <img srcset>, <link rel="preload" as="image">,
    Open Graph images, Twitter card images.

    ``max_images`` caps the number of images to extract (default 5).
    ``max_image_bytes`` caps individual image download size (default 2 MB).
    ``max_embed_images`` caps how many images are base64-encoded for embedding
                        (default 3 — prevents context overflow with large images).

    The backend is determined by policy:
    - *search_and_fetch_safe* → plain HTTP, no JS rendering (may miss lazy-loaded images)
    - *open_web* → Playwright headless Chromium (full JS rendering)
    - *open_browser* → Selenium + user's real Chrome (full JS, logged-in cookies)
    - *tor* → plain HTTP routed through the local Tor SOCKS5 proxy (no JS)
    """
    allowlist = allowlist or []
    blocklist = []  # blocklist not used for image extraction
    fetch_policy = {"max_raw_bytes": max_image_bytes, "max_text_chars": 8000, "max_links": 30}

    # Check policy
    policy_ok = policy in ("search_and_fetch_safe", "open_web", "open_browser", "tor", "whitelist_only")
    if not policy_ok:
        return {"text": f"Error: unknown web policy '{policy}'.", "images": []}

    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return {"text": f"Error: '{url}' is not a valid URL.", "images": []}
        domain = parsed.netloc.lower()
        if any(domain.endswith(b) for b in blocklist):
            return {"text": f"Domain '{domain}' is explicitly blocked by policy.", "images": []}
        if policy == "whitelist_only" and not any(domain.endswith(a) for a in allowlist):
            return {"text": f"Domain '{domain}' is not in the whitelist.", "images": []}
    except Exception:
        return {"text": f"Error: could not parse URL '{url}'.", "images": []}

    if policy == "search_and_fetch_safe":
        return _extract_images_http(url, max_images, fetch_images, max_image_bytes, max_embed_images, fetch_policy)
    if policy == "open_web":
        return _extract_images_playwright(url, max_images, fetch_images, max_image_bytes, max_embed_images, fetch_policy)
    if policy == "open_browser":
        return _extract_images_selenium(url, max_images, fetch_images, max_image_bytes, max_embed_images, fetch_policy)
    if policy == "tor":
        return _extract_images_tor(url, max_images, fetch_images, max_image_bytes, max_embed_images, fetch_policy)


def _extract_images_tor(url: str, max_images: int, fetch_images: bool,
                        max_image_bytes: int, max_embed_images: int,
                        fetch_policy: dict) -> dict:
    """Extract images from a page fetched over the local Tor SOCKS5 proxy."""
    import base64
    import io

    proxies = _tor_proxy_or_error()
    if proxies is None:
        return {"text": _tor_deps_error(), "images": []}

    try:
        session = _tor_session(proxies)
        resp = session.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {"text": f"Error fetching URL via Tor: {e}.", "images": []}

    images_info, image_urls = _parse_image_urls_from_html(html, max_images)
    if not images_info:
        return {"text": f"No images found on '{url}'.", "images": []}

    text_parts = [f"Found {len(images_info)} image(s) on '{url}' (via Tor):"]
    for info in images_info:
        text_parts.append(f"  - Alt: '{info.get('alt', '(no alt text)')}' | URL: {info.get('src', '')}")

    images_data = []
    if fetch_images and image_urls:
        for img_url in image_urls[:max_images]:
            try:
                r = session.get(img_url, timeout=20, stream=True)
                r.raise_for_status()
                data = r.raw.read(max_image_bytes, decode_content=True)
                if not data:
                    continue
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(data))
                    if img.mode in ("P", "PA"):
                        img = img.convert("RGBA")
                    elif img.mode not in ("RGB", "RGBA", "L"):
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    png_data = buf.getvalue()
                except Exception:
                    png_data = data
                images_data.append((base64.b64encode(png_data).decode("ascii"), "image/png"))
                if len(images_data) >= max_embed_images:
                    break
            except Exception:
                continue

    return {"text": "\n".join(text_parts), "images": images_data}


def _check_and_raise_if_browser_active(browser_type: str) -> None:
    """Check if an active browser of the given type exists.

    If so, raise WebBrowserKillNeeded so the caller can prompt the user.
    If no active browser is detected, returns silently.
    """
    if browser_type == "playwright":
        from . import browser_fetcher as _bf
        if _bf._browser is not None:
            raise WebBrowserKillNeeded("playwright")
    elif browser_type == "selenium":
        # Check if port 9222 is in use by a Chrome process
        import subprocess as _subp
        import platform as _plat
        try:
            if _plat.system() == "Windows":
                result = _subp.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    if ":9222" in line and "LISTENING" in line:
                        raise WebBrowserKillNeeded("selenium")
            else:
                result = _subp.run(
                    ["lsof", "-i", ":9222", "-t"],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    raise WebBrowserKillNeeded("selenium")
        except WebBrowserKillNeeded:
            raise
        except Exception:
            pass  # Can't determine — proceed anyway


def _download_image_bytes(url: str, max_bytes: int = 2_000_000) -> tuple:
    """Download an image and return (base64_bytes, mime_type) or (None, None) on failure."""
    import base64
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15, stream=True)
        resp.raise_for_status()

        # Check content-type
        content_type = resp.headers.get("Content-Type", "")
        if "image/" not in content_type:
            # Try to detect from extension in URL
            path = urlparse(url).path.lower()
            if path.endswith((".png",)):
                content_type = "image/png"
            elif path.endswith((".jpg", ".jpeg")):
                content_type = "image/jpeg"
            elif path.endswith((".gif")):
                content_type = "image/gif"
            elif path.endswith((".webp")):
                content_type = "image/webp"
            elif path.endswith((".svg")):
                content_type = "image/svg+xml"
            else:
                # Default to jpeg
                content_type = "image/jpeg"

        # Read up to max_bytes
        data = resp.raw.read(max_bytes, decode_content=True)
        if not data:
            return None, None

        # Convert to PNG if needed (for vision models, we always use PNG)
        from PIL import Image
        import io
        try:
            img = Image.open(io.BytesIO(data))
            # Convert to RGBA or RGB for PNG compatibility
            if img.mode in ("P", "PA"):
                img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            # Save as PNG
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            png_data = buf.read()
        except Exception:
            # Fallback: use original data as base64
            png_data = data

        b64 = base64.b64encode(png_data).decode("ascii")
        return b64, "image/png"
    except Exception:
        return None, None


def _extract_images_http(url: str, max_images: int, fetch_images: bool,
                         max_image_bytes: int, max_embed_images: int,
                         fetch_policy: dict) -> dict:
    """Extract images via plain HTTP (no JS rendering)."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {"text": f"Error fetching URL: {e}.", "images": []}

    images_info, image_urls = _parse_image_urls_from_html(html, max_images)

    if not images_info:
        return {"text": f"No images found on '{url}'.", "images": []}

    text_parts = [f"Found {len(images_info)} image(s) on '{url}':"]
    for info in images_info:
        alt = info.get("alt", "(no alt text)")
        src = info.get("src", "")
        text_parts.append(f"  - Alt: '{alt}' | URL: {src}")

    if fetch_images and image_urls:
        images_data = []
        for i, img_url in enumerate(image_urls[:max_images]):
            b64, mime = _download_image_bytes(img_url, max_image_bytes)
            if b64:
                images_data.append((b64, mime))
            if len(images_data) >= max_embed_images:
                break
        return {"text": "\n".join(text_parts), "images": images_data}

    return {"text": "\n".join(text_parts), "images": []}


def _extract_images_playwright(url: str, max_images: int, fetch_images: bool,
                               max_image_bytes: int, max_embed_images: int,
                               fetch_policy: dict) -> dict:
    """Extract images via Playwright headless Chromium (full JS rendering)."""
    import base64
    try:
        from . import browser_fetcher as _bf
    except ImportError:
        return {"text": "Error: Playwright is not installed.", "images": []}

    if not _bf._ensure_playwright():
        return {"text": "Error: Playwright could not initialise headless Chromium.", "images": []}

    pb = _bf._browser
    if pb is None:
        return {"text": "Error: Playwright browser instance is unavailable.", "images": []}

    page = None
    try:
        page = pb.new_page()
        page.goto(url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(2000)  # Wait for lazy-loaded content

        # Wait for actual <img> elements to appear in the DOM (Next.js / React
        # hydration may inject elements after networkidle settles).  This
        # prevents "No images found" on SPAs whose images render client-side
        # long after the initial network traffic stops.
        try:
            page.wait_for_selector("img", timeout=10000)
        except Exception:
            pass  # No img tags at all — fall through to empty result

        # Extract image URLs via JavaScript
        image_data = page.evaluate("""() => {
            const images = [];
            // Collect <img> tags
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.dataset.src || img.dataset.lazy;
                const alt = img.alt || '';
                const width = img.naturalWidth || img.width;
                const height = img.naturalHeight || img.height;
                if (src && src.startsWith('http')) {
                    images.push({src, alt, width, height});
                }
            });
            // Collect Open Graph and Twitter images
            document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]').forEach(meta => {
                const content = meta.getAttribute('content');
                if (content && content.startsWith('http')) {
                    images.push({src: content, alt: meta.getAttribute('property') || meta.getAttribute('name'), width: 0, height: 0});
                }
            });
            // Deduplicate by src
            const seen = new Set();
            return images.filter(img => {
                if (seen.has(img.src)) return false;
                seen.add(img.src);
                return true;
            });
        }""")

        if not image_data:
            page.close()
            return {"text": f"No images found on '{url}'.", "images": []}

        # Format text description
        text_parts = [f"Found {min(len(image_data), max_images)} image(s) on '{url}':"]
        image_urls = []
        for i, img in enumerate(image_data[:max_images]):
            alt = img.get("alt", "(no alt text)")
            src = img.get("src", "")
            w = img.get("width", "?")
            h = img.get("height", "?")
            text_parts.append(f"  - Alt: '{alt}' | Size: {w}x{h} | URL: {src}")
            image_urls.append(src)

        # Download images
        images_data = []
        for img_url in image_urls[:max_images]:
            try:
                img_page = pb.new_page()
                img_page.goto(img_url, wait_until="networkidle", timeout=10000)
                img_bytes = img_page.screenshot(type="png", full_page=False)
                img_page.close()
                b64 = base64.b64encode(img_bytes).decode("ascii")
                images_data.append((b64, "image/png"))
                if len(images_data) >= max_embed_images:
                    break
            except Exception:
                continue

        return {"text": "\n".join(text_parts), "images": images_data}

    except Exception as e:
        if page:
            try:
                page.close()
            except Exception:
                pass
        return {"text": f"Error extracting images via Playwright: {e}", "images": []}


def _extract_images_selenium(url: str, max_images: int, fetch_images: bool,
                             max_image_bytes: int, max_embed_images: int,
                             fetch_policy: dict) -> dict:
    """Extract images via Selenium + user's real Chrome."""
    import base64
    try:
        from .browser_connector import create_driver
    except ImportError:
        return {"text": "Error: Selenium is not installed or Chrome/Chromium not found.", "images": []}

    def _extract_images_js(driver) -> list:
        return driver.execute_script("""() => {
            const images = [];
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.dataset.src || img.dataset.lazy;
                const alt = img.alt || '';
                const width = img.naturalWidth || img.width;
                const height = img.naturalHeight || img.height;
                if (src && src.startsWith('http')) {
                    images.push({src, alt, width, height});
                }
            });
            document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]').forEach(meta => {
                const content = meta.getAttribute('content');
                if (content && content.startsWith('http')) {
                    images.push({src: content, alt: meta.getAttribute('property') || meta.getAttribute('name'), width: 0, height: 0});
                }
            });
            const seen = new Set();
            return images.filter(img => {
                if (seen.has(img.src)) return false;
                seen.add(img.src);
                return true;
            });
        }""")

    driver = None
    temp_profile = None
    try:
        driver = create_driver(headless=True)
        driver.set_page_load_timeout(30)
        driver.get(url)

        try:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        # Wait for actual <img> elements to appear in the DOM (Next.js / React
        # hydration may inject elements after page load settles).  This
        # prevents "No images found" on SPAs whose images render client-side.
        import time as _time
        try:
            WebDriverWait(driver, 10).until(
                lambda d: len(d.find_elements("tag name", "img")) > 0
            )
        except Exception:
            pass  # No img tags at all — fall through to empty result

        _time.sleep(2)  # Additional settle time for lazy-loaded content

        image_data = _extract_images_js(driver)

        if not image_data:
            return {"text": f"No images found on '{url}'.", "images": []}

        text_parts = [f"Found {min(len(image_data), max_images)} image(s) on '{url}':"]
        image_urls = []
        for i, img in enumerate(image_data[:max_images]):
            alt = img.get("alt", "(no alt text)")
            src = img.get("src", "")
            w = img.get("width", "?")
            h = img.get("height", "?")
            text_parts.append(f"  - Alt: '{alt}' | Size: {w}x{h} | URL: {src}")
            image_urls.append(src)

        # Download images
        images_data = []
        for img_url in image_urls[:max_images]:
            try:
                temp_driver = create_driver(headless=True)
                temp_driver.set_page_load_timeout(10)
                temp_driver.get(img_url)
                _time.sleep(1)
                img_bytes = temp_driver.get_screenshot_as_png()
                temp_driver.quit()
                b64 = base64.b64encode(img_bytes).decode("ascii")
                images_data.append((b64, "image/png"))
                if len(images_data) >= max_embed_images:
                    break
            except Exception:
                continue

        return {"text": "\n".join(text_parts), "images": images_data}

    except Exception as e:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return {"text": f"Error extracting images via Selenium: {e}", "images": []}


def _parse_image_urls_from_html(html: str, max_images: int) -> tuple:
    """Parse image URLs from raw HTML using BeautifulSoup.
    
    Returns (images_info_list, image_urls_list) where images_info_list contains
    dicts with alt/src/width/height info, and image_urls_list contains the
    actual URLs to download.
    """
    from bs4 import BeautifulSoup
    images_info = []
    seen_urls = set()

    soup = BeautifulSoup(html, "html.parser")

    # Collect <img> tags
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy")
        if not src or not src.startswith("http"):
            continue
        if src in seen_urls:
            continue
        seen_urls.add(src)

        alt = img.get("alt", "") or ""
        width = img.get("width", "") or ""
        height = img.get("height", "") or ""
        images_info.append({"src": src, "alt": alt, "width": width, "height": height})

        if len(images_info) >= max_images:
            break

    # Collect Open Graph and Twitter images
    for meta in soup.find_all("meta", attrs={"property": "og:image"}):
        content = meta.get("content", "")
        if content and content.startswith("http") and content not in seen_urls:
            seen_urls.add(content)
            images_info.append({"src": content, "alt": "Open Graph image", "width": "", "height": ""})
            if len(images_info) >= max_images:
                break

    for meta in soup.find_all("meta", attrs={"name": "twitter:image"}):
        content = meta.get("content", "")
        if content and content.startswith("http") and content not in seen_urls:
            seen_urls.add(content)
            images_info.append({"src": content, "alt": "Twitter card image", "width": "", "height": ""})
            if len(images_info) >= max_images:
                break

    image_urls = [img["src"] for img in images_info]
    return images_info, image_urls


# ---------------------------------------------------------------------------
# Legacy names (kept for backwards compatibility)
# ---------------------------------------------------------------------------

# These are thin wrappers that delegate to the unified functions so existing
# call sites (tests, other modules) still work.  New code should prefer the
# direct public functions above.

def browser_search(query: str, policy: str, allowlist: Optional[List[str]] = None,
                   blocklist: Optional[List[str]] = None, engine: str = "duckduckgo",
                   max_results: int = 6) -> str:
    """Selenium-based search (open_browser / open_web policy)."""
    allowlist = allowlist or []
    blocklist = blocklist or []
    return _web_search_selenium(query, engine, max_results, blocklist)


def search_urls(query: str, policy: str, allowlist: Optional[List[str]] = None,
                blocklist: Optional[List[str]] = None, engine: str = "duckduckgo",
                max_results: int = 6) -> str:
    """Playwright-based search (open_web / open_browser policy)."""
    allowlist = allowlist or []
    blocklist = blocklist or []
    check_policy(None, policy, allowlist, blocklist)
    result = _web_search_playwright(query, engine, max_results, blocklist)
    if "No results" in result:
        # Fall back to HTTP search
        fallback = _web_search_http(query, engine, max_results, blocklist)
        return f"[Browser search returned no results — showing HTTP fallback]\n\n{fallback}"
    return result


def open_browser(url: str, policy: str, allowlist: Optional[List[str]] = None,
                 blocklist: Optional[List[str]] = None, timeout: int = 30,
                 max_fetch_policy: Optional[dict] = None) -> str:
    """Selenium-based fetch (open_browser / open_browser policy)."""
    allowlist = allowlist or []
    blocklist = blocklist or []
    return _fetch_text_selenium(url, timeout, max_fetch_policy)


def browser_fetch(url: str, policy: str, allowlist: Optional[List[str]] = None,
                  blocklist: Optional[List[str]] = None, timeout: int = 30,
                  max_fetch_policy: Optional[dict] = None) -> str:
    """Selenium-based fetch (open_browser / open_web policy)."""
    allowlist = allowlist or []
    blocklist = blocklist or []
    return _fetch_text_selenium(url, timeout, max_fetch_policy)