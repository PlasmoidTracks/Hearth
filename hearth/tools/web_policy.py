"""Web-access policy shared by the web tools (mode + allow/blocklist).

Modes mirror the llm_coder harness's web-access ladder, trimmed to what Hearth
can actually back:

  off                  — web tools refuse; nothing leaves the machine.
  search_and_fetch_safe— default. DuckDuckGo HTML endpoint over plain HTTP plus
                          plain-HTTP fetch of one URL (no JS rendering).
  tor                  — same HTTP behaviour, but every request is routed through
                          a local Tor SOCKS5 proxy so the origin never sees the
                          server's IP/DNS (needs PySocks: pip install "httpx[socks]").
  open_browser         — Selenium drives the operator's *real* Chrome/Chromium
                          (logged-in cookies, full JS). Best for SPAs and
                          login-gated pages. Needs `pip install selenium` and a
                          Chrome binary on the machine running Hearth.

Enforcement lives here (not in the UI): each tool calls resolve() to get its
backend + limits and check_url() before any outbound request. A blocklisted
domain is refused in every mode except "off" (which refuses everything anyway);
an allowlist, when non-empty, restricts fetch targets in every mode.

SSRF guard: by default the tools refuse loopback/link-local/private addresses
(including the 169.254.169.254 cloud-metadata IP), so a prompt-injected page or
a chatty model cannot make Hearth probe its own LAN. The check is best-effort —
a public hostname resolving to a private IP is only caught by the upstream
network refusing it — which is why 'open_browser' (where the operator's browser
is the trust boundary) skips it rather than pretending to be airtight.

Security note for open_browser: this mode hands the model a browser that holds
the *operator's* logged-in sessions. It is admin-only by default (approval key
defaults to "ask") and should stay off on multi-tenant installs — see
docs/SECURITY.md.
"""
from __future__ import annotations
import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse

MODES = ("off", "search_and_fetch_safe", "tor", "open_browser")
DEFAULT_MODE = "search_and_fetch_safe"

# Tor Browser's own UA (Firefox ESR on the Tor Browser build). Tor traffic must
# look like every other Tor Browser user's — a Chrome UA on a Tor exit would
# fingerprint this app distinctly against the bulk of traffic on those circuits.
TOR_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"


def _domains(raw) -> list[str]:
    """Parse a comma/newline-separated domain list into lowercase bare domains."""
    out = []
    for part in str(raw or "").replace("\n", ",").split(","):
        d = part.strip().lower().lstrip("*.")
        if d:
            out.append(d)
    return out


def _match(domain: str, entries: list[str]) -> bool:
    return any(domain == e or domain.endswith("." + e) for e in entries)


@dataclass
class WebPolicy:
    """Resolved per-call web policy (from admin runtime settings)."""
    mode: str = DEFAULT_MODE
    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)
    headless: bool = True                 # open_browser: run Chrome without a window
    tor_proxy: str = "socks5h://127.0.0.1:9050"
    max_text_chars: int = 8_000           # fetch text handed to the model
    max_links: int = 30                   # trailing link list for fetched pages
    engine: str = "duckduckgo"            # web_search default engine
    allow_private: bool = False           # SSRF guard: reach loopback/LAN addresses?

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def refuse(self, what: str = "web access") -> str:
        if self.mode == "off":
            return (f"{what} is disabled by the current web-access policy "
                    "(Admin → Tools → Web → Web access mode). Ask an admin to enable a mode.")
        return ""


def resolve(settings: dict) -> WebPolicy:
    """Build a WebPolicy from a runtime-settings dict (tolerant of partial dicts)."""
    mode = str(settings.get("web.mode") or DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE
    try:
        host = str(settings.get("web.tor_socks_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(settings.get("web.tor_socks_port") or 9050)
    except (TypeError, ValueError):
        host, port = "127.0.0.1", 9050
    # socks5h = remote DNS: the SOCKS peer resolves the hostname, so the local
    # resolver never leaks the lookup (the whole point of routing via Tor).
    return WebPolicy(
        mode=mode,
        allow=_domains(settings.get("web.allow_domains")),
        block=_domains(settings.get("web.block_domains")),
        headless=bool(settings.get("web.headless", True)),
        tor_proxy=f"socks5h://{host}:{port}",
        max_text_chars=max(1000, int(settings.get("web.max_text_chars") or 8000)),
        max_links=max(0, int(settings.get("web.max_links") or 30)),
        engine=("google" if str(settings.get("tools.web_search.engine")) == "google"
                else "duckduckgo"),
        allow_private=bool(settings.get("web.http_allow_private", False)),
    )


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001 — an unparseable URL simply matches nothing
        return ""


def host_of(url: str) -> str:
    """Bare hostname from a URL's netloc (port stripped, IPv6 brackets removed)."""
    netloc = domain_of(url)
    try:
        # rsplit-on-":" breaks bracketed IPv6 ([::1] → "[::" ...), so handle that first
        if netloc.startswith("["):
            return netloc.split("]")[0] + "]"
        return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
    except Exception:  # noqa: BLE001
        return netloc


def _private_target(host: str) -> bool:
    """Does `host` name a loopback/link-local/private address (or localhost)?

    Literal IPs are classified directly. Hostnames are only matched against the
    obvious local names — we deliberately do NOT resolve DNS here (a lookup per
    fetch would be slow and itself leak; see the module docstring on limits).
    """
    h = host.strip("[]").rstrip(".").lower()
    if h in ("localhost", "localhost.localdomain") or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_multicast or ip == ipaddress.ip_address("0.0.0.0"))


def check_url(url: str, p: WebPolicy) -> str:
    """Return a refusal message for `url` under policy `p`, or '' when allowed."""
    r = p.refuse("web fetch")
    if r:
        return r
    dom = domain_of(url)
    if not url.startswith(("http://", "https://")):
        return "only http(s) URLs are allowed"
    if not dom:
        return f"could not parse the URL's host: {url[:120]}"
    if _match(dom, p.block):
        return f"domain '{dom}' is explicitly blocked by the web policy (Admin → Tools → Web)."
    if p.allow and not _match(dom, p.allow):
        return (f"domain '{dom}' is not in the web allowlist. An admin can widen it under "
                "Admin → Tools → Web → Allow-listed domains.")
    # SSRF guard (see module docstring). In open_browser mode the operator's own
    # browser navigates, so local addresses are their choice, not a Hearth leak.
    if not p.allow_private and p.mode != "open_browser" \
            and _private_target(host_of(url) or dom):
        return (f"'{dom}' is a loopback/private address and web tools refuse those by default "
                "(SSRF guard). An admin can allow internal addresses under Admin → Tools → Web, "
                "or open the page in their own browser.")
    return ""


def check_search(p: WebPolicy, engine: str) -> str:
    """Return a refusal message for a search under policy `p`, or '' when allowed."""
    r = p.refuse("web search")
    if r:
        return r
    if p.mode == "off":
        return "web search is disabled."
    # Engine-level blocklist: refuse only if EVERY result domain of that engine's
    # own front-end is blocked — the search host itself is never allow-list-gated,
    # or search would be impossible by construction.
    return ""


def tor_deps_error() -> str:
    return ("Error: the Tor web mode needs PySocks to reach the local Tor SOCKS5 proxy. "
            "Install it with: pip install 'httpx[socks]' — and make sure a Tor daemon listens "
            "on 127.0.0.1:9050 (Tor Browser's own proxy is 9150; set that under "
            "Admin → Tools → Web).")


def tor_available() -> bool:
    try:
        import socks  # noqa: F401  (PySocks — required for socks5h:// in httpx)
        return True
    except ImportError:
        return False
