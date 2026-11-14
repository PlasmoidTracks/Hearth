"""web_fetch tool — retrieve one URL as readable text (+ its links).

Companion to web_search; the backend follows the admin's web-access mode, just
like search (see hearth/tools/web_policy.py):

  search_and_fetch_safe — plain HTTP via httpx (no JS rendering). Default.
  tor                   — same over the local Tor SOCKS5 proxy (Tor Browser UA).
  open_browser          — Selenium + operator's real Chrome: full JS + logged-in
                          cookies, best for SPAs and login-gated pages.

Scripts/styles/markup are stripped to readable text (bounded), a short link
list is appended, and the structured result goes into ToolResult.meta so the
UI can show WHICH page was read (and on which backend) under the tool chip.
Refuses non-http(s) schemes and blocklisted/non-allowlisted domains.
"""
from __future__ import annotations
import asyncio
import html
import re

import httpx

from .base import Tool, ToolResult
from . import web_policy as wp
from .browser_connector import extract_text_and_links

_UA = "Mozilla/5.0 (compatible; HearthBot/0.1; +private-family-assistant)"


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch one web page (http/https) and return its readable text plus a list of "
        "links, stripped of markup. Use after web_search to read a specific result. "
        "Output is truncated. The backend is chosen automatically by the admin's "
        "web-access mode: plain HTTP ('search_and_fetch_safe', no JS), HTTP via a local "
        "Tor SOCKS5 proxy ('tor'), or the operator's real Chrome with cookies "
        "('open_browser' — full JS, best for SPAs and login-gated pages)."
    )
    enable_key = "tools.web_fetch"

    def parameters(self):
        return {"type": "object", "properties": {
            "url": {"type": "string", "description": "absolute http(s) URL"},
            "timeout": {"type": "integer",
                        "description": "Seconds to wait for page load (default 30)"}},
            "required": ["url"]}

    async def run(self, args, ctx):
        pol = wp.resolve(ctx.settings())
        url = str(args.get("url", "")).strip()
        refuse = wp.check_url(url, pol)
        if refuse:
            return ToolResult(False, refuse)
        try:
            timeout = max(5, min(90, int(args.get("timeout", 30))))
        except (TypeError, ValueError):
            timeout = 30

        if pol.mode == "open_browser":
            res = await self._via_browser(url, timeout, pol)
        elif pol.mode == "tor":
            res = await self._via_http(url, timeout, pol, tor=True)
        else:
            res = await self._via_http(url, timeout, pol, tor=False)
        return res

    # ---- backends ----------------------------------------------------------

    async def _via_http(self, url: str, timeout: int, pol: wp.WebPolicy, tor: bool) -> ToolResult:
        proxies = None
        if tor:
            if not wp.tor_available():
                return ToolResult(False, wp.tor_deps_error())
            proxies = {"https": pol.tor_proxy, "http": pol.tor_proxy}
        headers = {"User-Agent": wp.TOR_USER_AGENT if tor else _UA}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout,
                                         follow_redirects=True,
                                         proxy=(proxies or {}).get("https")) as c:
                r = await c.get(url)
                r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"fetch failed: {str(e)[:200]}")
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return ToolResult(False, f"unsupported content-type: {ctype.split(';')[0] or '?'}")
        if "html" in ctype:
            text, links = extract_text_and_links(r.text, pol.max_text_chars, pol.max_links)
        else:
            text, links = r.text.strip()[:pol.max_text_chars], []
        return self._success(str(r.url), text, links,
                             "http (tor)" if tor else "http")

    async def _via_browser(self, url: str, timeout: int, pol: wp.WebPolicy) -> ToolResult:
        from . import browser_connector as bc
        if not bc.selenium_available():
            return ToolResult(False, bc.DEPS_ERROR)
        try:
            res = await asyncio.to_thread(bc.fetch_page_via_browser, url, timeout, pol.headless)
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"browser fetch failed: {str(e)[:200]}")
        if res.get("error"):
            return ToolResult(False, res["error"])
        text, links = extract_text_and_links(res.get("html", ""),
                                             pol.max_text_chars, pol.max_links)
        return self._success(res.get("final_url") or url, text, links, "real-browser")

    # ---- shared result shaping ----------------------------------------------

    def _success(self, final_url: str, text: str, links: list[dict], backend: str) -> ToolResult:
        if not text and not links:
            return ToolResult(True, "(page had no readable text)",
                              meta={"url": final_url, "backend": backend})
        body = text or "(no prose text — links only)"
        if links:
            link_block = "\n".join(f"- {l['text']} -> {l['url']}" for l in links)
            body += f"\n\n--- Links (first {len(links)}) ---\n{link_block}"
        return ToolResult(True, body,
                          meta={"url": final_url, "backend": backend,
                                "links": links[:20]})
