"""
Persistent Chrome fetch for bot-protected storefronts.

Uses a dedicated profile (~/.deepowl-chrome), real Chrome (channel="chrome"),
and captures JSON XHR/fetch responses during navigation (e.g. Leroy Merlin specs).
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from playwright.async_api import Response, async_playwright
except ImportError:
    async_playwright = None  # type: ignore
    Response = Any  # type: ignore

DEFAULT_CHROME_PROFILE = os.path.expanduser("~/.deepowl-chrome")
SETTLE_MS = 6000
PRE_NAV_SLEEP = (2.5, 6.0)
POST_FETCH_SLEEP = (8.0, 18.0)
DOMAIN_MIN_INTERVAL = 12.0

# Reduce Playwright/automation signals (does not fix IP reputation blocks).
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""

CHROME_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]
CHROME_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


@dataclass
class ChromeFetchResult:
    html: str = ""
    status: int = 0
    final_url: str = ""
    error: str = ""
    json_responses: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0


class ChromeScraper:
    """Single persistent Chrome context; one navigation at a time (shared profile)."""

    def __init__(self, user_data_dir: str = DEFAULT_CHROME_PROFILE,
                 headless: bool = False, timeout_ms: int = 60_000,
                 settle_ms: int = SETTLE_MS,
                 pre_nav_sleep: Tuple[float, float] = PRE_NAV_SLEEP,
                 post_fetch_sleep: Tuple[float, float] = POST_FETCH_SLEEP,
                 domain_min_interval: float = DOMAIN_MIN_INTERVAL):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.pre_nav_sleep = pre_nav_sleep
        self.post_fetch_sleep = post_fetch_sleep
        self.domain_min_interval = domain_min_interval
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self._domain_last: Dict[str, float] = {}
        self._init_script_added = False

    async def start(self) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed")
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            channel="chrome",
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="it-IT",
            timezone_id="Europe/Rome",
            extra_http_headers={"Accept-Language": "it-IT,it;q=0.9"},
            ignore_https_errors=True,
            args=CHROME_LAUNCH_ARGS,
            ignore_default_args=CHROME_IGNORE_DEFAULT_ARGS,
        )
        if not self._init_script_added:
            await self._ctx.add_init_script(STEALTH_INIT_SCRIPT)
            self._init_script_added = True

    async def close(self) -> None:
        if self._ctx:
            try:
                await self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def fetch(self, url: str) -> ChromeFetchResult:
        import time

        assert self._ctx
        t0 = time.monotonic()
        async with self._lock:
            host = (urlparse(url).netloc or "").lower().removeprefix("www.")
            last = self._domain_last.get(host, 0.0)
            wait = self.domain_min_interval - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            await asyncio.sleep(random.uniform(*self.pre_nav_sleep))

            pending_json: List[Response] = []

            def on_response(r: Response) -> None:
                ct = (r.headers.get("content-type") or "").lower()
                if "json" in ct:
                    pending_json.append(r)

            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
            page.on("response", on_response)
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms,
                )
                await page.wait_for_timeout(self.settle_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                html = await page.content()
                status = resp.status if resp else 0
                final = page.url
                captured: List[Dict[str, Any]] = []
                for r in pending_json:
                    entry: Dict[str, Any] = {"url": r.url, "status": r.status}
                    # Body reads have no Playwright timeout; a streaming response never resolves.
                    try:
                        entry["body"] = await asyncio.wait_for(r.json(), 10)
                    except Exception:
                        try:
                            text = await asyncio.wait_for(r.text(), 10)
                            entry["body"] = text[:500_000] if text else ""
                        except Exception as e:
                            entry["error"] = f"{type(e).__name__}: {e}"
                    captured.append(entry)
                self._domain_last[host] = time.monotonic()
                await asyncio.sleep(random.uniform(*self.post_fetch_sleep))
                return ChromeFetchResult(
                    html=html,
                    status=status,
                    final_url=final,
                    json_responses=captured,
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                )
            except Exception as e:
                return ChromeFetchResult(
                    error=f"{type(e).__name__}: {e}",
                    final_url=url,
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                )
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass

    async def fetch_raw(self, url: str, wait_s: float = 20.0) -> bytes:
        """Navigate a real tab (so WAF challenges run), capture the raw XML response body."""
        assert self._ctx
        async with self._lock:
            page = await self._ctx.new_page()
            got: asyncio.Future = asyncio.get_running_loop().create_future()
            target = urlparse(url).path

            async def on_resp(resp):
                if got.done() or resp.status != 200 or urlparse(resp.url).path != target:
                    return
                try:
                    body = await resp.body()
                except Exception:
                    return
                head = body.lstrip()[:300]
                if head.startswith(b"<?xml") or b"<urlset" in head or b"<sitemapindex" in head \
                        or body[:2] == b"\x1f\x8b":
                    if not got.done():
                        got.set_result(body)

            page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
            try:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                except Exception:
                    pass  # challenge reloads / .gz download start can throw; listener still runs
                try:
                    return await asyncio.wait_for(got, wait_s)
                except asyncio.TimeoutError:
                    return b""
            finally:
                await page.close()
