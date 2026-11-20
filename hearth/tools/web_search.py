"""Web search tool — backend chosen by the admin's web-access mode.

Mirrors the llm_coder harness design: the model always calls the same tool and
never sees *how* the search happened. The mode (Admin → Tools → Web) selects:

  search_and_fetch_safe — DuckDuckGo HTML endpoint via httpx (no JS). Default.
  tor                   — same, routed through the local Tor SOCKS5 proxy.
  open_browser          — Selenium + the operator's real Chrome (full JS,
                          logged-in cookies). Google engine supported here too.

Results are returned to the model as a compact list AND carried in
ToolResult.meta["results"] so the chat UI can render them as clickable source
links (see hearth/static/chat.js + orchestrator web-meta persistence).
Network/parse errors never crash: they return an actionable tool error string.
"""
from __future__ import annotations
import asyncio
import html
import re

import httpx

from .base import Tool, ToolResult
from . import web_policy as wp

_UA = "Mozilla/5.0 (compatible; HearthBot/0.1; +private-family-assistant)"

_NO_RESULTS_HINT = ("No results for query '{query}'. Check in order: (1) the query may be "
                    "too long or phrased as a sentence — retry with 2-6 core keywords "
                    "(adding MORE words narrows results further and will not help); "
                    "(2) the engine rate-limited or changed its markup — try once more, "
                    "or use web_fetch directly on a well-known URL for this topic.")

# DDG answers suspicious IPs with an anomaly/CAPTCHA page instead of results. The
# HTTP backends can't solve image puzzles (the open_browser backend clicks the
# tiles — see browser_connector._solve_ddg_anomaly), so say what happened rather
# than "no results", which would send the model off shortening a fine query.
_CHALLENGE_HINT = ("DuckDuckGo returned a bot-challenge (CAPTCHA) page instead of results, "
                   "so this search could not complete over plain HTTP. Wait before retrying, "
                   "or an admin can switch Web access mode to 'tor' or 'open_browser' "
                   "(Admin → Tools → Web), which avoid this.")

# Markers that the HTML endpoint refused us rather than simply having nothing.
_CHALLENGE_MARKERS = ("anomaly-modal", "challenge-form", "img-form")


def looks_challenged(page: str) -> bool:
    low = (page or "").lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


async def ddg_search(query: str, max_results: int = 6, proxies: dict | None = None) -> list[dict]:
    """DuckDuckGo HTML endpoint. `proxies` routes via Tor when set.

    Raises DDGChallengeError when the endpoint answers with a bot-challenge page,
    so callers can report that distinctly from a genuinely empty result set.
    """
    params = {"q": query}
    headers = {"User-Agent": wp.TOR_USER_AGENT if proxies else _UA}
    async with httpx.AsyncClient(headers=headers, timeout=12, follow_redirects=True,
                                 proxy=proxies.get("https") if proxies else None) as c:
        r = await c.get("https://html.duckduckgo.com/html/", params=params)
        r.raise_for_status()
    if looks_challenged(r.text):
        raise DDGChallengeError()
    return _parse_ddg(r.text, max_results)


class DDGChallengeError(RuntimeError):
    """The HTML endpoint returned a CAPTCHA/anomaly page instead of results."""


_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snip>.*?)</a>', re.S)


def _parse_ddg(page: str, max_results: int) -> list[dict]:
    out = []
    for m in _RESULT_RE.finditer(page):
        href = html.unescape(m.group("href"))
        # DDG wraps URLs in /l/?uddg=<encoded>
        mm = re.search(r"uddg=([^&]+)", href)
        if mm:
            from urllib.parse import unquote
            href = unquote(mm.group(1))
        title = _strip_tags(m.group("title"))
        snip = _strip_tags(m.group("snip"))
        if href.startswith("http"):
            out.append({"title": title, "url": href, "snippet": snip})
        if len(out) >= max_results:
            break
    return out


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()[:400]


class WebSearchTool(Tool):
    name = "web_search"
    label = "Web search"
    description = (
        "Search the web and return titles, URLs, and snippets. Keep the query SHORT: "
        "2-6 keywords, not a natural sentence — long descriptive queries return little "
        "or nothing. Iterate by shortening or changing a term, not by adding more terms. "
        "The backend is chosen automatically by the admin's web-access mode: lightweight "
        "DuckDuckGo HTML ('search_and_fetch_safe'), DuckDuckGo via a local Tor SOCKS5 "
        "proxy ('tor'), or the operator's real Chrome with cookies ('open_browser', which "
        "also supports engine='google')."
    )
    setting_key = "tools.web_search"
    enable_key = "tools.web_search"

    def parameters(self):
        return {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": ("A short keyword string of 2-6 words, NOT a natural sentence. "
                                      "Strip stopwords; keep only the distinctive terms. When a "
                                      "query returns nothing, SHORTEN it or swap one term.")},
            "engine": {"type": "string", "enum": ["duckduckgo", "google"],
                       "description": "'duckduckgo' (default) or 'google'. Google only works in "
                                      "'open_browser' web mode; other modes use DuckDuckGo."},
            "max_results": {"type": "integer", "description": "Maximum number of results (default 6)"}},
            "required": ["query"]}

    async def run(self, args, ctx):
        pol = wp.resolve(ctx.settings())
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(False, "a search query is required")
        try:
            max_results = max(1, min(20, int(args.get("max_results", 6))))
        except (TypeError, ValueError):
            max_results = 6

        refuse = wp.check_search(pol, pol.engine)
        if refuse:
            return ToolResult(False, refuse)

        engine = str(args.get("engine") or pol.engine)
        if engine not in ("duckduckgo", "google"):
            engine = "duckduckgo"

        try:
            if pol.mode == "open_browser":
                results, backend = await self._via_browser(query, engine, max_results, pol)
                if results is None:
                    return ToolResult(False, backend)   # dependency/launch error string
            elif pol.mode == "tor":
                if not wp.tor_available():
                    return ToolResult(False, wp.tor_deps_error())
                results = await ddg_search(query, max_results,
                                           proxies={"https": pol.tor_proxy, "http": pol.tor_proxy})
                backend = "duckduckgo-html (tor)"
                engine = "duckduckgo"
            else:
                results = await ddg_search(query, max_results)
                backend = "duckduckgo-html"
                engine = "duckduckgo"
        except DDGChallengeError:
            return ToolResult(False, _CHALLENGE_HINT)
        except Exception as e:  # noqa: BLE001 — graceful failure is the contract
            return ToolResult(False, f"web search failed: {str(e)[:200]}")

        # Domain blocklist applies to what we hand back, whatever produced it.
        results = [r for r in results
                   if not wp._match(wp.domain_of(r["url"]), pol.block)]
        if not results:
            return ToolResult(True, _NO_RESULTS_HINT.format(query=query[:80]),
                              meta={"engine": engine, "backend": backend, "query": query})

        lines = [f"{i}. {r['title']}\n   {r['url']}"
                 + (f"\n   {r['snippet']}" if r.get("snippet") else "")
                 for i, r in enumerate(results, 1)]
        return ToolResult(True, "\n".join(lines),
                          meta={"results": results, "engine": engine, "backend": backend,
                                "query": query})

    async def _via_browser(self, query: str, engine: str, max_results: int, pol):
        """open_browser mode: Selenium in a worker thread (it blocks)."""
        from . import browser_connector as bc
        if not bc.selenium_available():
            return None, bc.DEPS_ERROR
        try:
            results = await asyncio.to_thread(bc.browser_search, query, engine,
                                              max_results, pol.headless)
        except ImportError:
            return None, bc.DEPS_ERROR
        except Exception as e:  # noqa: BLE001
            return None, f"browser search failed: {str(e)[:200]}"
        return results, "real-browser"
