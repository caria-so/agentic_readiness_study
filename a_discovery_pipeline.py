#!/usr/bin/env python3
"""
Site Discovery & Enrichment Pipeline
====================================

For each URL, performs a set of independent "discovery" probes and saves
everything to disk:

  1. fetch_url            -> main page HTML (+ redirect target, status, timing)
  2. discover_sitemap     -> /sitemap.xml (+ robots.txt Sitemap: directives,
                             sitemap-index recursion, .xml.gz support)
  3. discover_jsonld      -> every <script type="application/ld+json"> block,
                             parsed & flattened
  4. discover_llms_txt    -> /llms.txt and /llms-full.txt
  5. extract_links        -> all <a href> (and optionally <link>) targets,
                             deduped, internal vs external
  6. run_lighthouse       -> Lighthouse JSON report

Design notes
------------
* The HTTP probes (2-5) use plain `httpx` with redirects + a browser-like
  UA. Lighthouse uses its own Chrome. The main page is fetched with Playwright
  so JS-rendered links/JSON-LD are captured, but the auxiliary probes do NOT
  spin up a browser (cheaper, and sitemap/llms.txt are static files).
* Every probe is best-effort: a failure is recorded per-probe, the site as a
  whole is not marked failed unless the main fetch fails.
* Concurrency is a plain asyncio.Semaphore. Lighthouse is gated by its own
  semaphore because each instance is a full Chrome.
* Output is one folder per site + one row appended to results.csv, with
  resume support via a processed-URLs set.

Usage
-----
  python a_discovery_pipeline.py --urls sites.csv
  python a_discovery_pipeline.py --urls sites.csv --browser firefox --concurrency 5
  python a_discovery_pipeline.py --urls sites.csv --no-playwright      # httpx only
  python a_discovery_pipeline.py --urls sites.csv --no-lighthouse
  python a_discovery_pipeline.py --urls sites.csv --persistent-chrome --concurrency 1
  python a_discovery_pipeline.py --urls sites.csv --only leroymerlin.it,tecnomat.it
  python a_discovery_pipeline.py --urls sites.csv --resume
  python a_discovery_pipeline.py --urls sites.csv --lighthouse-concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import io
import json
import logging
import math
import os
import random
import re
import ssl
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import httpx
import pandas as pd

from utils.batch import lang_info
from utils.product_detection import is_product_page_url
from utils.scraping import ChromeScraper, DEFAULT_CHROME_PROFILE

try:
    from playwright.async_api import async_playwright, Page, Browser
except ImportError:  # allow --no-playwright usage without the dep installed
    async_playwright = None
    Page = Browser = Any  # type: ignore

log = logging.getLogger("discovery")


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

CRAWLER_USER_AGENT = (
    "DeepOwlSiteDiscovery/1.0 (+https://deepowl.com/crawler) "
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Identifying UA for robots.txt compliance checks only.
DEFAULT_USER_AGENT = CRAWLER_USER_AGENT
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
}
SITEMAP_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/xml,text/xml,*/*;q=0.8",
}

# Paths probed for each "file" discovery, tried in order.
SITEMAP_CANDIDATES = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap.xml.gz", "/sitemap-index.xml.gz", "/wp-sitemap.xml",
    "/sitemap/sitemap.xml", "/sitemap/index.xml",
]
LLMS_TXT_CANDIDATES = ["/llms.txt", "/llms-full.txt", "/.well-known/llms.txt"]

MAX_SITEMAP_DOCS = 150         # safety cap on sitemap document fetches (index + urlsets)
MAX_SITEMAP_URLS_STORED = 20000
MAX_HTML_BYTES = 8 * 1024 * 1024
PRODUCT_ESTIMATE_SAMPLE = 100
PRODUCT_ESTIMATE_MIN = 20
PRODUCT_CI_HALFWIDTH = 0.10
SITEMAP_DOMAIN_INTERVAL = 0.3


@dataclass
class ProbeResult:
    """Outcome of a single discovery probe."""
    ok: bool = False
    url: str = ""
    status: int = 0
    error: str = ""
    path: str = ""          # saved file, if any
    count: int = 0          # items found (links, jsonld blocks, sitemap urls)
    total_available: int = 0  # items seen before truncation
    truncated: bool = False
    product_url_count: int = 0
    product_total_available: int = 0


@dataclass
class SiteResult:
    company: str
    url: str
    site_dir: str = ""
    final_url: str = ""
    status: str = "unknown"          # fetched | blocked | challenge | failed
    timestamp: str = ""
    http_status: int = 0
    fetch_ms: float = 0
    fetch_engine: str = ""
    probes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    probe_summary: Dict[str, bool] = field(default_factory=dict)
    error_details: str = ""


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sanitize(name: str, fallback: str = "site") -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"[^\w\s.\-]", "", name, flags=re.UNICODE)
    name = re.sub(r"[-\s]+", "_", name).strip("._")
    return (name or fallback)[:100]


def domain_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return ""
    return raw


def same_site(a: str, b: str) -> bool:
    return domain_of(a) == domain_of(b)


_PRODUCT_DOC_RE = re.compile(r"product|prodott", re.I)
_NOT_PRODUCT_DOC_RE = re.compile(
    r"taxonom|categor|product[_-]?(?:cat|tag|brand|attribute)|[_/-]tags?[_.-]", re.I)


def is_definitive_product_sitemap(doc_url: str) -> bool:
    """Doc URL path says product/prodott → every URL in it is a product page."""
    path = urlparse(doc_url or "").path
    if not path or _NOT_PRODUCT_DOC_RE.search(path):
        return False
    return bool(_PRODUCT_DOC_RE.search(path))


def _maybe_gunzip(b: bytes) -> bytes:
    if b[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(b)
        except Exception:
            pass
    return b


def _is_sitemap_xml(content: bytes) -> bool:
    if not content:
        return False
    head = content.lstrip()[:200].lower()
    return head.startswith(b"<?xml") or b"<urlset" in head or b"<sitemapindex" in head


def _looks_like_bot_wall(content: bytes) -> bool:
    if not content or _is_sitemap_xml(content):
        return False
    text = content[:4000].lower()
    return any(m in text for m in (
        b"captcha-delivery.com", b"please enable js", b"geo.captcha",
        b"cf-chl", b"challenges.cloudflare.com", b"just a moment...",
    ))


def _sitemap_fetch_priority(url: str) -> Tuple[int, str]:
    """Lower sorts first — product/catalog sitemaps before marketing/am stubs."""
    low = (url or "").lower()
    if is_definitive_product_sitemap(url) or is_product_sitemap_doc(url):
        return (0, low)
    if "/pub/media/sitemap" in low:
        return (1, low)
    if "sitemap-index" in low or "sitemap_index" in low:
        return (2, low)
    if re.search(r"/am/[^/]*sitemap", low):
        return (90, low)
    if re.search(r"category|brands-navigation|sub-universe", low):
        return (80, low)
    return (50, low)


def _dedupe_preserve_order(urls: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _filter_sitemap_seeds(seeds: List[str]) -> List[str]:
    """Drop known stub sitemaps when a canonical product index is also declared."""
    lows = [s.lower() for s in seeds]
    if any("/pub/media/sitemap" in s for s in lows):
        seeds = [
            s for s in seeds
            if not re.search(r"/am/[^/]*sitemap", s, re.I)
        ]
    return seeds


# xx-YY / xx_YY with an uppercase region: it-IT, ar-AE, de-DE … (case-sensitive on purpose)
_REGION_LOCALE_RE = re.compile(r"(?<![A-Za-z])([a-z]{2})[-_]([A-Z]{2})(?![A-Za-z])")
# bare language codes as PrestaShop/Magento use them: 1_de_0, sitemap_en_…, obi_it_it
_BARE_LANG_RE = re.compile(
    r"(?<![a-z])(it|en|de|fr|es|nl|pt|pl|cs|hu|ro|sv|da|fi|no|el|tr|ru|ja|zh|ko|"
    r"ar|he|hr|sk|sl|sr|et|lt|lv|bg|uk|ms)(?![a-z])", re.I)


LANG, REGION = "it", "IT"          # the batch's language; set by --lang (default: Italian for Italy)


def set_language(lang3: str) -> None:
    """Point the Accept-Language headers and the sitemap locale filter at another language."""
    global LANG, REGION
    LANG, REGION, accept = lang_info(lang3)
    DEFAULT_HEADERS["Accept-Language"] = accept     # in place: SITEMAP_HEADERS and clients hold copies
    SITEMAP_HEADERS["Accept-Language"] = accept


def _foreign_locale_doc(url: str) -> bool:
    """Keep only sitemap docs in the batch language for its country (default: Italian for Italy)."""
    path = urlparse(url).path
    regional = _REGION_LOCALE_RE.findall(path)
    if regional:
        return (LANG, REGION) not in regional
    bare = {m.lower() for m in _BARE_LANG_RE.findall(path)}
    return bool(bare) and LANG not in bare


def wilson_ci(hits: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def is_product_sitemap_doc(sitemap_url: str) -> bool:
    return is_definitive_product_sitemap(sitemap_url)


def decode_body(resp: httpx.Response) -> str:
    """Decode possibly-gzipped / odd-charset bodies safely."""
    raw = resp.content or b""
    # httpx auto-decompresses, but a *.gz path may still arrive raw.
    if resp.headers.get("content-type", "").lower().startswith(
        ("application/x-gzip", "application/gzip")
    ) or resp.url.path.endswith(".gz"):
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    enc = resp.encoding or ""
    if enc and enc.lower() not in ("iso-8859-1", "ascii"):
        try:
            return raw.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        m = re.search(rb'charset=["\']?([\w\-]+)', raw[:4096], re.I)
        if m:
            try:
                return raw.decode(m.group(1).decode("ascii"), errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass
    return raw.decode("cp1252", errors="replace")


# ─────────────────────────────────────────────────────────────────────
# Discovery engine
# ─────────────────────────────────────────────────────────────────────

class DiscoveryPipeline:

    def __init__(self, base_dir: Path, concurrency: int = 4,
                 run_lighthouse: bool = True, lighthouse_concurrency: int = 1,
                 use_playwright: bool = True, browser_type: str = "chromium",
                 headless: bool = True, timeout: float = 60.0,
                 lighthouse_timeout: float = 90.0,
                 collect_html_links: bool = True,
                 collect_css_links: bool = False,
                 verify_ssl: bool = True,
                 persistent_chrome: bool = False,
                 chrome_profile_dir: str = DEFAULT_CHROME_PROFILE):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.concurrency = concurrency
        self.lighthouse_enabled = run_lighthouse
        self.lighthouse_concurrency = lighthouse_concurrency
        self.use_playwright = use_playwright and async_playwright is not None
        self.browser_type = browser_type
        self.headless = headless
        self.timeout = timeout
        self.lighthouse_timeout = lighthouse_timeout
        self.collect_html_links = collect_html_links
        self.collect_css_links = collect_css_links
        self.verify_ssl = verify_ssl
        self.persistent_chrome = persistent_chrome and async_playwright is not None
        self.chrome_profile_dir = chrome_profile_dir
        self._domain_interval = 1.0
        self._sitemap_interval = SITEMAP_DOMAIN_INTERVAL
        self._domain_last: Dict[str, float] = {}
        self._robots: Dict[str, RobotFileParser] = {}
        self._sm_failures: Dict[str, str] = {}

        self._io_sem = asyncio.Semaphore(concurrency)
        self._lh_sem = asyncio.Semaphore(lighthouse_concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        self._pw = None
        self._browser: Optional[Browser] = None
        self._chrome: Optional[ChromeScraper] = None
        if self.persistent_chrome and concurrency > 1:
            log.warning(
                "persistent Chrome uses one profile; concurrency=%d will queue fetches",
                concurrency,
            )

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self):
        limits = httpx.Limits(max_connections=self.concurrency * 4,
                              max_keepalive_connections=self.concurrency * 2)
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS, follow_redirects=True,
            timeout=httpx.Timeout(self.timeout), limits=limits,
            verify=self.verify_ssl,
        )
        if self.persistent_chrome:
            self._chrome = ChromeScraper(
                user_data_dir=self.chrome_profile_dir,
                headless=self.headless,
                timeout_ms=int(self.timeout * 1000),
            )
            await self._chrome.start()
            log.info(
                "Chrome persistent profile ready (dir=%s, headless=%s)",
                self.chrome_profile_dir, self.headless,
            )
        elif self.use_playwright:
            self._pw = await async_playwright().start()
            launcher = {
                "chromium": self._pw.chromium,
                "firefox": self._pw.firefox,
                "webkit": self._pw.webkit,
            }.get(self.browser_type, self._pw.chromium)
            self._browser = await launcher.launch(headless=self.headless)
            log.info("Playwright %s ready (headless=%s)", self.browser_type, self.headless)

    async def close(self):
        if self._chrome:
            try:
                await self._chrome.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        if self._client:
            await self._client.aclose()

    # ── per-domain politeness ────────────────────────────────────────

    async def _throttle(self, url: str, interval: Optional[float] = None):
        d = domain_of(url)
        gap = interval if interval is not None else self._domain_interval
        last = self._domain_last.get(d, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._domain_last[d] = time.monotonic()

    # ── HTTP helpers ─────────────────────────────────────────────────

    def _can_fetch(self, url: str) -> bool:
        rp = self._robots.get(domain_of(url))
        if rp is None:
            return True
        return rp.can_fetch(CRAWLER_USER_AGENT, url)

    async def _ensure_robots(self, base_url: str, out_dir: Optional[Path] = None) -> None:
        d = domain_of(base_url)
        if d in self._robots:
            return
        robots_url = urljoin(base_url, "/robots.txt")
        r, _ = await self._get(robots_url, skip_robots=True)
        rp = RobotFileParser()
        rp.set_url(robots_url)
        if r is not None and r.status_code == 200:
            body = decode_body(r)
            rp.parse(body.splitlines())
            if out_dir is not None:
                (out_dir / "robots.txt").write_text(body, encoding="utf-8")
        else:
            rp.parse([])
        self._robots[d] = rp

    async def _get(self, url: str, *, text_ok: bool = True,
                   skip_robots: bool = False) -> Tuple[Optional[httpx.Response], str]:
        assert self._client
        if not skip_robots and not self._can_fetch(url):
            return None, "disallowed by robots.txt"
        await self._throttle(url)
        try:
            r = await self._client.get(url)
            return r, ""
        except httpx.HTTPError as e:
            return None, f"{type(e).__name__}: {e}"
        except ssl.SSLError as e:
            return None, f"SSLError: {e}"

    # ── probe 1: main page (Playwright, falls back to httpx) ─────────

    async def fetch_main_page(self, url: str) -> Tuple[str, int, float, str, str, str, List[Dict[str, Any]]]:
        """Return (html, status, ms, error, final_url, fetch_engine, json_responses)."""
        t0 = time.monotonic()
        empty_json: List[Dict[str, Any]] = []
        if self._chrome:
            res = None
            for attempt in range(2):
                try:
                    res = await self._chrome.fetch(url)
                except Exception as e:
                    log.warning("Chrome fetch failed for %s (attempt %d): %s", url, attempt + 1, e)
                    res = None
                if res and res.html and (not res.error or _looks_like_bot_wall(
                        res.html[:4000].encode("utf-8", "replace"))):
                    break
                if attempt == 0:
                    log.info("retrying main fetch for %s", url)
            if res is not None:
                if res.error and not res.html:
                    return "", 0, res.elapsed_ms, res.error, url, "chrome_persistent", empty_json
                return (
                    res.html, res.status, res.elapsed_ms,
                    res.error, res.final_url or url, "chrome_persistent", res.json_responses,
                )

        if self.use_playwright and not self.persistent_chrome:
            try:
                assert self._browser
                ctx = await self._browser.new_context(
                    user_agent=CRAWLER_USER_AGENT,
                    extra_http_headers={
                        **DEFAULT_HEADERS,
                        "User-Agent": CRAWLER_USER_AGENT,
                    },
                    ignore_https_errors=not self.verify_ssl,
                    viewport={"width": 1440, "height": 900},
                )
                page = await ctx.new_page()
                # Auto-dismiss JS dialogs, else goto/screenshot can hang.
                page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded",
                                           timeout=int(self.timeout * 1000))
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    html = await page.content()
                    status = resp.status if resp else 0
                    final = page.url
                    return html, status, (time.monotonic() - t0) * 1000, "", final, "playwright", empty_json
                finally:
                    await ctx.close()
            except Exception as e:
                log.warning("Playwright fetch failed for %s (%s) — falling back to httpx: %s",
                            url, type(e).__name__, e)

        # httpx fallback
        r, err = await self._get(url)
        if r is None:
            return "", 0, (time.monotonic() - t0) * 1000, err, url, "httpx", empty_json
        return decode_body(r), r.status_code, (time.monotonic() - t0) * 1000, "", str(r.url), "httpx", empty_json

    # ── probe 2: sitemap ─────────────────────────────────────────────

    async def discover_sitemap(self, base_url: str, out_dir: Path) -> ProbeResult:
        """Find sitemap(s) via robots.txt + common paths; parse and save URL list."""
        res = ProbeResult()
        self._sm_failures = {}
        found: List[Dict[str, Any]] = []
        tried: Set[str] = set()
        totals = {"n": 0, "catalog_products": 0}
        seeds: List[str] = []

        await self._ensure_robots(base_url, out_dir)
        robots_path = out_dir / "robots.txt"
        if robots_path.exists():
            for line in robots_path.read_text(encoding="utf-8").splitlines():
                line = line.strip().lstrip("\ufeff")
                if line.lower().startswith("sitemap:"):
                    sm = line.split(":", 1)[1].strip()
                    if sm:
                        seeds.append(sm)

        seeds = _filter_sitemap_seeds(_dedupe_preserve_order(seeds))
        seeds = [s for s in seeds if not _foreign_locale_doc(s)]
        seeds = sorted(seeds, key=_sitemap_fetch_priority)
        await self._walk_all_async(seeds, found, tried, totals)
        if not found:
            fb = sorted((urljoin(base_url, c) for c in SITEMAP_CANDIDATES),
                        key=_sitemap_fetch_priority)
            await self._walk_all_async(fb, found, tried, totals)

        product_meta = await asyncio.to_thread(
            self._estimate_product_counts, base_url, found, totals)

        failed = {u: self._sm_failures[u] for u in sorted(tried) if u in self._sm_failures}
        if not found and totals["n"] == 0:
            res.error = "no sitemap found"
            (out_dir / "sitemap.json").write_text(json.dumps({
                "source": base_url, "generated": datetime.now().isoformat(),
                "url_count": 0, "sitemap_docs_tried": sorted(tried),
                "sitemap_docs_failed": failed,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            return res

        by_source = Counter(e.get("_source", "") for e in found)
        res.ok = True
        res.count = len(found)
        res.total_available = totals["n"]
        res.truncated = totals["n"] > len(found)
        res.url = by_source.most_common(1)[0][0] if by_source else ""
        res.product_url_count = product_meta["product_url_count"] or 0
        res.product_total_available = product_meta["product_total_available"] or 0
        payload = {
            "source": base_url,
            "generated": datetime.now().isoformat(),
            "url_count": len(found),
            "total_available": totals["n"],
            "product_url_count": res.product_url_count,
            "product_total_available": res.product_total_available,
            "product_count_method": product_meta["method"],
            **{k: v for k, v in product_meta.items()
               if k not in ("product_url_count", "product_total_available", "method")},
            "truncated": res.truncated,
            "sitemaps_fetched": {u: n for u, n in by_source.most_common()},
            "sitemap_docs_tried": sorted(tried),
            "sitemap_docs_failed": failed,
            "doc_cap_hit": len(tried) >= MAX_SITEMAP_DOCS,
            "urls": found,
        }
        path = out_dir / "sitemap.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        res.path = str(path)
        return res

    def _estimate_product_counts(self, base_url: str, found: List[Dict[str, Any]],
                                 totals: Dict[str, int]) -> Dict[str, Any]:
        catalog = totals.get("catalog_products", 0)
        pool_total = max(0, totals["n"] - catalog)
        catalog_stored = sum(
            1 for e in found if is_definitive_product_sitemap(e.get("_source", "")))
        pool = [e["loc"] for e in found
                if e.get("loc") and not is_definitive_product_sitemap(e.get("_source", ""))]
        out: Dict[str, Any] = {
            "method": "product_sitemap_docs" if catalog else "none",
            "catalog_products": catalog,
            "pool_total": pool_total,
            "product_total_available": catalog,
            "product_url_count": catalog_stored,
        }
        if not pool or pool_total == 0:
            return out

        rnd = random.Random(domain_of(base_url))
        sample = rnd.sample(pool, min(PRODUCT_ESTIMATE_SAMPLE, len(pool)))
        hits = fetched = 0
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0,
                          follow_redirects=True, verify=self.verify_ssl) as c:
            for u in sample:
                d = domain_of(u)
                gap = self._domain_interval - (time.monotonic() - self._domain_last.get(d, 0.0))
                if gap > 0:
                    time.sleep(gap)
                self._domain_last[d] = time.monotonic()
                try:
                    r = c.get(u)
                except Exception:
                    continue
                if r.status_code != 200 or not r.text:
                    continue
                fetched += 1
                hits += is_product_page_url(r.text, str(r.url))
                if fetched >= PRODUCT_ESTIMATE_MIN:
                    lo, hi = wilson_ci(hits, fetched)
                    if (hi - lo) / 2 <= PRODUCT_CI_HALFWIDTH:
                        break

        if not fetched:
            out["method"] += "+sample_failed"
            if not catalog:
                out["product_total_available"] = None
                out["product_url_count"] = None
            return out
        rate = hits / fetched
        lo, hi = wilson_ci(hits, fetched)
        out.update({
            "method": "product_sitemap_docs+sampled" if catalog else "sampled",
            "product_page_rate": round(rate, 4),
            "product_sample_size": len(sample),
            "product_sample_fetched": fetched,
            "product_sample_hits": hits,
            "product_estimate_ci_low": catalog + math.floor(lo * pool_total),
            "product_estimate_ci_high": catalog + math.ceil(hi * pool_total),
            "product_total_available": catalog + int(round(rate * pool_total)),
            "product_url_count": catalog_stored + int(round(rate * len(pool))),
        })
        return out

    async def _fetch_sitemap_bytes(self, url: str) -> Optional[bytes]:
        if not self._can_fetch(url):
            self._sm_failures[url] = "disallowed by robots.txt"
            return None
        await self._throttle(url, self._sitemap_interval)
        content: Optional[bytes] = None
        try:
            r = await self._client.get(url, headers=SITEMAP_HEADERS)
            body = _maybe_gunzip(r.content or b"")
            if r.status_code == 200 or _looks_like_bot_wall(body):
                content = body
            else:
                self._sm_failures[url] = f"HTTP {r.status_code}"
        except Exception as e:
            self._sm_failures[url] = f"{type(e).__name__}: {e}"
        if content and _is_sitemap_xml(content):
            self._sm_failures.pop(url, None)
            return content
        if content and _looks_like_bot_wall(content):
            if not self._chrome:
                self._sm_failures[url] = "bot wall (no Chrome)"
                return None
            log.info("sitemap bot-wall on %s — retrying with persistent Chrome", url)
            try:
                raw = _maybe_gunzip(await self._chrome.fetch_raw(url) or b"")
                if _is_sitemap_xml(raw):
                    self._sm_failures.pop(url, None)
                    return raw
                self._sm_failures[url] = "bot wall (Chrome retry also failed)"
            except Exception as e:
                self._sm_failures[url] = f"bot wall; Chrome: {type(e).__name__}: {e}"
        elif content:
            self._sm_failures[url] = f"non-XML body ({len(content)} bytes)"
        return None

    async def _walk_all_async(self, seeds: List[str],
                              found: List[Dict[str, Any]], tried: Set[str],
                              totals: Dict[str, int]):
        for s in seeds:
            if len(tried) >= MAX_SITEMAP_DOCS:
                break
            if s not in tried:
                await self._parse_sitemap_tree_async(s, found, tried, totals, depth=0)

    async def _parse_sitemap_tree_async(self, url: str, found: List[Dict[str, Any]],
                                        tried: Set[str], totals: Dict[str, int], depth: int):
        """Recursively parse a sitemap or sitemap-index. Counts past the store cap."""
        if depth > 5 or len(tried) >= MAX_SITEMAP_DOCS:
            return
        if url in tried:
            return
        tried.add(url)

        content = await self._fetch_sitemap_bytes(url)
        if not content:
            return
        try:
            tree = ET.fromstring(content)
        except ET.ParseError:
            try:
                tree = ET.fromstring(gzip.decompress(content))
            except Exception:
                self._sm_failures[url] = "XML parse error"
                return

        tag = tree.tag.lower()
        ns = ""
        if tag.startswith("{"):
            ns = tag.split("}")[0] + "}"

        if "sitemapindex" in tag:
            child_locs: List[str] = []
            for sm in tree.findall(f"{ns}sitemap"):
                loc = sm.findtext(f"{ns}loc")
                if loc:
                    child_locs.append(loc.strip())
            for loc in sorted(child_locs, key=_sitemap_fetch_priority):
                if _foreign_locale_doc(loc):
                    continue
                await self._parse_sitemap_tree_async(loc, found, tried, totals, depth + 1)
            return

        for u in tree.findall(f"{ns}url"):
            loc = u.findtext(f"{ns}loc")
            if not loc:
                continue
            loc_s = loc.strip()
            totals["n"] += 1
            if is_definitive_product_sitemap(url):
                totals["catalog_products"] += 1
            if len(found) >= MAX_SITEMAP_URLS_STORED:
                continue
            found.append({
                "loc": loc_s,
                "lastmod": (u.findtext(f"{ns}lastmod") or "").strip(),
                "changefreq": (u.findtext(f"{ns}changefreq") or "").strip(),
                "priority": (u.findtext(f"{ns}priority") or "").strip(),
                "_source": url,
            })

    # ── probe 3: JSON-LD in the main HTML ────────────────────────────

    async def discover_jsonld(self, html: str, page_url: str, out_dir: Path) -> ProbeResult:
        res = ProbeResult()
        blocks = extract_jsonld_blocks(html)
        if not blocks:
            res.error = "no application/ld+json found"
            return res

        types: List[str] = []
        for b in blocks:
            types.extend(jsonld_types(b.get("data")))

        payload = {
            "source": page_url,
            "generated": datetime.now().isoformat(),
            "block_count": len(blocks),
            "types": sorted(set(types)),
            "blocks": blocks,
        }
        path = out_dir / "jsonld.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        res.ok = True
        res.count = len(blocks)
        res.path = str(path)
        return res

    # ── probe 4: llms.txt ────────────────────────────────────────────

    async def discover_llms_txt(self, base_url: str, out_dir: Path) -> ProbeResult:
        res = ProbeResult()
        hits = []
        for cand in LLMS_TXT_CANDIDATES:
            target = urljoin(base_url, cand)
            r, err = await self._get(target)
            if r is None or r.status_code != 200 or not r.content:
                continue
            body = decode_body(r)
            # Guard against soft-404 HTML pages served with 200.
            if body.lstrip()[:1] == "<" and "<html" in body[:600].lower():
                continue
            hits.append({"path": cand, "url": str(r.url), "bytes": len(body.encode()), "text": body})

        if not hits:
            res.error = "no llms.txt found"
            return res

        payload = {"source": base_url, "generated": datetime.now().isoformat(), "files": hits}
        path = out_dir / "llms_txt.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        # also write the raw files for convenience
        for h in hits:
            name = sanitize(h["path"].strip("/").replace("/", "_"), "llms") + ".txt"
            (out_dir / name).write_text(h["text"], encoding="utf-8")

        res.ok = True
        res.count = len(hits)
        res.path = str(path)
        res.url = hits[0]["url"]
        return res

    # ── probe 5: links ───────────────────────────────────────────────

    async def extract_links(self, html: str, page_url: str,
                            out_dir: Path) -> ProbeResult:
        res = ProbeResult()
        links, seen = [], set()

        for href, text, rel, tag in iter_anchors(html):
            href = (href or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
                continue
            absolute, frag = urldefrag(urljoin(page_url, href))
            key = (absolute, frag)
            if key in seen:
                continue
            seen.add(key)
            links.append({
                "url": absolute,
                "fragment": frag,
                "text": text,
                "rel": rel,
                "internal": same_site(absolute, page_url),
                "tag": tag,
            })

        if self.collect_css_links:
            for href in re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', html, re.I):
                absolute, _ = urldefrag(urljoin(page_url, href))
                if absolute not in {l["url"] for l in links}:
                    links.append({"url": absolute, "text": "", "rel": "link",
                                  "internal": same_site(absolute, page_url), "tag": "link"})

        if not links:
            res.error = "no links found"
            return res

        internal = [l for l in links if l["internal"]]
        external = [l for l in links if not l["internal"]]
        payload = {
            "source": page_url,
            "generated": datetime.now().isoformat(),
            "total": len(links),
            "internal_count": len(internal),
            "external_count": len(external),
            "links": links,
        }
        path = out_dir / "links.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        # A convenience flat URL list (internal only) for downstream crawling.
        (out_dir / "links_internal.txt").write_text(
            "\n".join(sorted({l["url"] for l in internal})), encoding="utf-8"
        )
        (out_dir / "links_external.txt").write_text(
            "\n".join(sorted({l["url"] for l in external})), encoding="utf-8"
        )

        res.ok = True
        res.count = len(links)
        res.path = str(path)
        return res

    # ── probe 6: lighthouse ──────────────────────────────────────────

    async def run_lighthouse(self, url: str, out_dir: Path) -> ProbeResult:
        res = ProbeResult(url=url)
        async with self._lh_sem:
            out = out_dir / "lighthouse.json"
            cmd = [
                "lighthouse", url,
                "--output=json",
                f"--output-path={out}",
                "--quiet",
                "--only-categories=performance,seo,accessibility,best-practices",
                "--skip-audits=screenshot-thumbnails,final-screenshot,full-page-screenshot",
                "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage",
                f"--max-wait-for-load={int(self.timeout * 1000)}",
                f"--timeout={int(self.lighthouse_timeout * 1000)}",
            ]
            log.info("Lighthouse → %s", url)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.lighthouse_timeout + 15
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                res.error = "lighthouse timeout"
                return res

            if proc.returncode == 0 and out.exists():
                res.path = str(out)
                try:
                    rt = json.loads(out.read_text(encoding="utf-8")).get("runtimeError") or {}
                except Exception:
                    rt = {}
                if rt.get("code"):
                    res.error = f"lighthouse runtimeError: {rt['code']}"
                    return res
                res.ok = True
                return res
            res.error = (stderr or b"").decode(errors="replace")[:500] or \
                        f"lighthouse exit {proc.returncode}"
            return res

    # ── orchestration ────────────────────────────────────────────────

    async def process_site(self, company: str, url: str) -> SiteResult:
        site = SiteResult(company=company, url=url, timestamp=datetime.now().isoformat())
        site_key = sanitize(company or domain_of(url), "site")
        site_dir = self.base_dir / "sites" / f"{site_key}_{uuid.uuid4().hex[:8]}"
        site_dir.mkdir(parents=True, exist_ok=True)
        site.site_dir = str(site_dir)

        async with self._io_sem:
            log.info("── %s (%s)", company or url, url)

            await self._ensure_robots(url, site_dir)
            if not self._can_fetch(url):
                site.status = "failed"
                site.error_details = "disallowed by robots.txt"
                site.probes["fetch"] = asdict(ProbeResult(error="disallowed by robots.txt"))
                (site_dir / "probe_results.json").write_text(
                    json.dumps(asdict(site), indent=2), encoding="utf-8")
                return site

            html, status, ms, err, final_url, fetch_engine, json_xhr = await self.fetch_main_page(url)
            site.http_status, site.fetch_ms = status, round(ms, 1)
            site.fetch_engine = fetch_engine
            if json_xhr:
                (site_dir / "fetch_json_responses.json").write_text(
                    json.dumps(json_xhr, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )

            challenged = bool(html) and _looks_like_bot_wall(
                html[:4000].encode("utf-8", "replace"))
            if err or not html or status >= 400 or challenged:
                if challenged:
                    site.status = "challenge"
                    site.error_details = "bot challenge — visit gold products by hand"
                else:
                    site.status = "blocked" if status in (401, 403, 429) else "failed"
                    site.error_details = err or (f"HTTP {status}" if status else "empty response")
                log.warning("main fetch failed: %s", site.error_details)
                if html:
                    (site_dir / "index.html").write_text(html, encoding="utf-8")
                site.probes["fetch"] = asdict(ProbeResult(
                    ok=False, url=url, status=status, error=site.error_details,
                    path=str(site_dir / "index.html") if html else "",
                    count=len(html) if html else 0))
                # Sitemaps are often public XML even when the storefront blocks our UA.
                sm = await self.discover_sitemap(url, site_dir)
                site.probes["sitemap"] = asdict(sm)
                site.probe_summary = {k: bool(v.get("ok")) for k, v in site.probes.items()}
                (site_dir / "probe_results.json").write_text(
                    json.dumps(asdict(site), indent=2), encoding="utf-8")
                return site

            (site_dir / "index.html").write_text(html, encoding="utf-8")
            site.final_url = final_url or url
            if re.match(r"^/en(?:[/-]|$)", urlparse(site.final_url).path):
                site.error_details = "; ".join(
                    filter(None, [site.error_details, "served English catalogue"]))
            if final_url and domain_of(final_url) != domain_of(url):
                site.error_details = "; ".join(filter(None, [
                    site.error_details, f"redirected off-domain to {domain_of(final_url)}"]))
            site.probes["fetch"] = asdict(ProbeResult(
                ok=True, url=url, status=status, path=str(site_dir / "index.html"),
                count=len(html)))

            # Run the independent probes concurrently — they don't depend on
            # each other, only on the fetched HTML / base URL.
            tasks = {
                "sitemap": self.discover_sitemap(url, site_dir),
                "jsonld": self.discover_jsonld(html, url, site_dir),
                "llms_txt": self.discover_llms_txt(url, site_dir),
                "links": (self.extract_links(html, url, site_dir)
                          if self.collect_html_links else _noop_probe("links disabled")),
            }
            names = list(tasks)
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, r in zip(names, results):
                if isinstance(r, Exception):
                    site.probes[name] = asdict(ProbeResult(error=f"{type(r).__name__}: {r}"))
                else:
                    site.probes[name] = asdict(r)

        # Lighthouse outside the IO semaphore: it's a separate Chrome and
        # shouldn't hold an HTTP slot.
        if self.lighthouse_enabled and site.status not in ("blocked", "challenge"):
            try:
                lh = await self.run_lighthouse(url, site_dir)
            except Exception as e:
                lh = ProbeResult(url=url, error=f"{type(e).__name__}: {e}")
            site.probes["lighthouse"] = asdict(lh)

        if site.status not in ("blocked", "challenge", "failed"):
            site.status = "fetched" if site.probes.get("fetch", {}).get("ok") else "failed"
        site.probe_summary = {k: bool(v.get("ok")) for k, v in site.probes.items()}
        (site_dir / "probe_results.json").write_text(
            json.dumps(asdict(site), indent=2, ensure_ascii=False), encoding="utf-8")
        return site


async def _noop_probe(msg: str) -> ProbeResult:
    return ProbeResult(error=msg)


# ─────────────────────────────────────────────────────────────────────
# HTML parsing helpers (no external HTML lib — regex + stdlib)
# ─────────────────────────────────────────────────────────────────────

SCRIPT_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
ANCHOR_RE = re.compile(r"<a\s+([^>]*?)>(.*?)</a>", re.I | re.S)
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']')
TAG_STRIP_RE = re.compile(r"<[^>]+>")


def extract_jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    blocks = []
    for i, m in enumerate(SCRIPT_LDJSON_RE.finditer(html)):
        raw = m.group(1).strip()
        # Strip HTML comments / CDATA wrappers some sites add.
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw).strip()
        raw = re.sub(r"^\s*//\s*<!\[CDATA\[|//\s*\]\]>\s*$", "", raw).strip()
        try:
            data = json.loads(raw)
            parse_error = ""
        except json.JSONDecodeError as e:
            data, parse_error = None, str(e)
        blocks.append({"index": i, "chars": len(raw), "parse_error": parse_error, "data": data})
    return blocks


def jsonld_types(data: Any) -> List[str]:
    """Recursively collect @type values from a parsed JSON-LD structure."""
    out: List[str] = []

    def walk(node: Any):
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, list):
                out.extend(str(x) for x in t)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def iter_anchors(html: str):
    """Yield (href, text, rel, tag) for anchors. Regex-based, resilient to bad HTML."""
    for m in ANCHOR_RE.finditer(html):
        attrs = dict((k.lower(), v) for k, v in ATTR_RE.findall(m.group(1)))
        inner = m.group(2)
        text = TAG_STRIP_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()[:200]
        yield attrs.get("href", ""), text, attrs.get("rel", ""), "a"


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    base_dir: Path
    results_file: Path
    processed_file: Path
    processed: Set[str]

    @classmethod
    def load(cls, base_dir: Path, resume: bool) -> "Session":
        base_dir.mkdir(parents=True, exist_ok=True)
        results_file = base_dir / "results.csv"
        processed_file = base_dir / "processed_urls.json"
        processed: Set[str] = set()
        if resume and processed_file.exists():
            processed = set(json.loads(processed_file.read_text()))
        if not results_file.exists():
            cols = [f.name for f in SiteResult.__dataclass_fields__.values()]
            with open(results_file, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(cols)
        return cls(base_dir, results_file, processed_file, processed)

    def append(self, site: SiteResult):
        row = asdict(site)
        row["probes"] = json.dumps(row["probes"], ensure_ascii=False)
        row["probe_summary"] = json.dumps(row["probe_summary"], ensure_ascii=False)
        with open(self.results_file, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=row.keys()).writerow(row)

    def mark(self, url: str):
        self.processed.add(url)
        self.processed_file.write_text(
            json.dumps(sorted(self.processed), indent=2), encoding="utf-8")


async def run(csv_path: str, out_dir: str, concurrency: int, resume: bool,
              run_lighthouse: bool, lighthouse_concurrency: int,
              use_playwright: bool, browser_type: str, headless: bool,
              limit: Optional[int], only_domains: Optional[Set[str]],
              verify_ssl: bool, collect_css_links: bool,
              persistent_chrome: bool, chrome_profile_dir: str):

    df = pd.read_csv(csv_path)
    col_map = {c.lower(): c for c in df.columns}
    url_col = next(
        (col_map[c] for c in ("company_link", "url", "site", "website") if c in col_map),
        df.columns[0],
    )
    name_col = next(
        (col_map[c] for c in ("company", "name", "company_name", "retailer") if c in col_map),
        None,
    )
    log.info("Loaded %d rows from %s (url col: %s)", len(df), csv_path, url_col)

    session = Session.load(Path(out_dir), resume)

    only = only_domains or set()
    jobs: List[Tuple[str, str]] = []
    for _, row in df.iterrows():
        url = normalize_url(str(row.get(url_col, "")))
        if not url:
            continue
        if only and domain_of(url) not in only:
            continue
        if resume and url in session.processed:
            continue
        company = str(row.get(name_col, "")).strip() if name_col else domain_of(url)
        jobs.append((company or domain_of(url), url))
        if limit and len(jobs) >= limit:
            break

    if not jobs:
        log.info("Nothing to do.")
        return

    log.info("Processing %d sites (concurrency=%d, lighthouse=%s)",
             len(jobs), concurrency, run_lighthouse)

    pipeline = DiscoveryPipeline(
        base_dir=Path(out_dir), concurrency=concurrency,
        run_lighthouse=run_lighthouse, lighthouse_concurrency=lighthouse_concurrency,
        use_playwright=use_playwright, browser_type=browser_type,
        headless=headless, verify_ssl=verify_ssl,
        collect_css_links=collect_css_links,
        persistent_chrome=persistent_chrome,
        chrome_profile_dir=chrome_profile_dir,
    )
    await pipeline.start()

    done = 0
    t0 = time.monotonic()
    try:
        tasks = [asyncio.create_task(pipeline.process_site(c, u)) for c, u in jobs]
        for fut in asyncio.as_completed(tasks):
            try:
                site = await fut
            except Exception as e:
                log.error("site task crashed: %s: %s", type(e).__name__, e)
                continue
            session.append(site)
            session.mark(site.url)
            done += 1
            el = time.monotonic() - t0
            rate = done / el if el else 0
            eta = (len(jobs) - done) / rate if rate else 0
            log.info("[%d/%d] %s → %s (%.0fs elapsed, ETA %.0fs)",
                     done, len(jobs), site.company, site.status, el, eta)
    finally:
        await pipeline.close()

    log.info("Done. Results: %s | Site dirs: %s/sites", session.results_file, out_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Site discovery & enrichment pipeline")
    p.add_argument("--urls", required=True, help="CSV with a URL column")
    p.add_argument("--out", default="discovery_output", help="Output directory (a batch folder)")
    p.add_argument("--lang", default="ita", help="language of the leads, e.g. ita, deu (default ita)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel sites (default 4)")
    p.add_argument("--resume", action="store_true", help="Skip already-processed URLs")
    p.add_argument("--limit", type=int, default=None, help="Max sites to process")
    p.add_argument("--only", default="", help="Comma-separated domains to restrict to")
    p.add_argument("--no-lighthouse", action="store_true", help="Skip Lighthouse")
    p.add_argument("--lighthouse-concurrency", type=int, default=1,
                   help="Parallel Lighthouse runs (each is a Chrome; keep low)")
    p.add_argument("--no-playwright", action="store_true",
                   help="Use plain HTTP instead of a real browser for the main fetch")
    p.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--persistent-chrome", action="store_true",
        help="Fetch main pages with real Chrome + ~/.deepowl-chrome profile (anti-bot)",
    )
    p.add_argument(
        "--chrome-profile", default=DEFAULT_CHROME_PROFILE,
        help="User data dir for --persistent-chrome (default: ~/.deepowl-chrome)",
    )
    p.add_argument("--no-css-links", action="store_true",
                   help="Don't collect <link> hrefs, only <a>")
    p.add_argument("--insecure", action="store_true",
                   help="Disable TLS certificate verification (httpx + sitemap fetches)")
    p.add_argument("--verbose", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Only set the macOS-specific cache path on macOS. On Linux/CI the env
    # var (if needed) should come from the environment.
    if sys.platform == "darwin":
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.expanduser("~/Library/Caches/ms-playwright"),
        )
    set_language(args.lang)
    headless = not args.headed
    if args.persistent_chrome:
        headless = False  # profile + channel=chrome; use visible window for WAF clearance

    only_domains = {
        s.strip().lower().removeprefix("www.")
        for s in (args.only or "").split(",")
        if s.strip()
    }
    asyncio.run(run(
        csv_path=args.urls,
        out_dir=args.out,
        concurrency=args.concurrency,
        resume=args.resume,
        run_lighthouse=not args.no_lighthouse,
        lighthouse_concurrency=args.lighthouse_concurrency,
        use_playwright=not args.no_playwright,
        browser_type=args.browser,
        headless=headless,
        limit=args.limit,
        only_domains=only_domains or None,
        verify_ssl=not args.insecure,
        collect_css_links=not args.no_css_links,
        persistent_chrome=args.persistent_chrome,
        chrome_profile_dir=os.path.expanduser(args.chrome_profile),
    ))


if __name__ == "__main__":
    main()
