#!/usr/bin/env python3
"""
Stage 2: product-page sampling and structured-data audit.
=========================================================

Reads the output of discovery_pipeline.py and, for each site:

  1. detects the e-commerce platform from HTML + sitemap fingerprints
  2. builds a candidate product-URL pool from sitemap.json (falling back to
     links.json only when no sitemap was found)
  3. draws a reproducible random sample of N product pages
  4. fetches them, honouring robots.txt
  5. extracts product structured data and measures WHICH FIELDS ARE POPULATED

The point is step 5. Presence of @type=Product is near-universal and tells you
nothing. What matters is whether the fields a buyer constrains on -- dimensions,
material, finish, mounting, compatibility -- exist in machine-readable form.

Usage
-----
  python b_product_sample.py --out discovery_output --n 25
  python b_product_sample.py --out discovery_output --n 25 --seed 42
  python b_product_sample.py --out discovery_output --only inbagno.it,arcshop.it
  python b_product_sample.py --out discovery_output --ignore-robots   # records, never silent
  python b_product_sample.py --out discovery_output --persistent-chrome --concurrency 1

Outputs
-------
  <out>/product_audit.csv                  one row per site
  <out>/sites/<site>/products/*.html       raw pages
  <out>/sites/<site>/product_audit.json    per-product detail
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import statistics as stats
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx

from utils.batch import lang_info
from utils.page_files import page_file_name
from utils.product_detection import (
    GENERIC_EXCLUDE, GENERIC_INCLUDE, PLATFORM_URL_RULES,
    detect_platform, iter_nodes, page_signals, type_names,
)
from utils.robots_sweep import parse_robots, select_groups, is_allowed   # RFC 9309 matcher
from utils.scraping import (
    ChromeScraper, DEFAULT_CHROME_PROFILE,
    DOMAIN_MIN_INTERVAL, POST_FETCH_SLEEP, PRE_NAV_SLEEP, SETTLE_MS,
)

log = logging.getLogger("product_sample")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────
# What we measure on a product page
# ─────────────────────────────────────────────────────────────────────

# Fields a buyer constrains on. Presence of these -- not presence of Product --
# is the actual gate-4 measurement.
CONSTRAINT_FIELDS = [
    "name", "sku", "gtin", "mpn", "brand", "price", "currency", "availability",
    "description", "image", "color", "material", "size", "width", "height",
    "depth", "weight", "additionalProperty", "isRelatedTo", "isSimilarTo",
    "isAccessoryOrSparePartFor", "isConsumableFor", "hasMeasurement",
]
# The relational predicates. schema.org has them; nobody populates them.
# That absence is the finding, so it gets counted separately.
RELATIONAL_FIELDS = [
    "isRelatedTo", "isSimilarTo", "isAccessoryOrSparePartFor", "isConsumableFor",
]

# Values themes emit when a field is empty. Counting these as populated made
# gtin/mpn read 1.0 on every site in the first run.
PLACEHOLDERS = {"", "0", "-", "--", "n/a", "na", "n.d.", "nd", "none", "null",
                "undefined", "tbd", "false", "[]", "{}"}

# Bot walls that answer 200 with a challenge page. Without this, a blocked
# site looks like a site with no structured data — a false zero.
CHALLENGE_SIGNS = [
    ("cloudflare", r"(cf-browser-verification|challenge-platform|Just a moment\.\.\.|cf_chl_)"),
    ("akamai",     r"(Access Denied</h1>.*Reference&#32;&#35;|_abck|ak_bmsc)"),
    ("datadome",   r"(captcha-delivery\.com|datadome)"),
    ("perimeterx", r"(px-captcha|_pxAppId|perimeterx)"),
    ("incapsula",  r"(_Incapsula_Resource|incap_ses_|Incapsula incident)"),
    ("generic",    r"(<title>\s*(Access denied|Attention Required|Robot Check|Captcha)\b)"),
    ("leroy",      r"(Automated\s*\(bot\)\s*activity|JavaScript disabled or not working|"
                   r"Rapid taps or clicks|developer or inspection tools)"),
]


def detect_challenge(html: str, status: int) -> str:
    head = html[:20000]
    for name, pat in CHALLENGE_SIGNS:
        if re.search(pat, head, re.I | re.S):
            # akamai/datadome cookies appear on normal pages too; only trust them
            # when the page is also tiny or non-200
            if name in ("akamai", "datadome", "perimeterx") and status == 200 and len(html) > 60000:
                continue
            return name
    return ""


@dataclass
class ProductAudit:
    url: str
    http_status: int = 0
    bytes: int = 0
    error: str = ""
    robots_allowed: bool = True
    challenge: str = ""              # name of bot-wall detected in a 200 response
    is_product_page: bool = False    # main-product markup AND not a platform category URL
    main_product_markup: bool = False  # og:type=product or a Product node describing this page
    category_url_veto: bool = False    # path matched the detected platform's category rule
    structured_source: str = "none"  # jsonld | microdata | none — where the audited node came from
    jsonld_blocks: int = 0
    jsonld_types: List[str] = field(default_factory=list)
    has_product_jsonld: bool = False
    has_product_microdata: bool = False
    has_og_product: bool = False
    fields_present: List[str] = field(default_factory=list)
    additional_property_count: int = 0
    additional_property_names: List[str] = field(default_factory=list)
    relational_fields_present: List[str] = field(default_factory=list)
    offer_count: int = 0


# ─────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────

def audit_product_node(node: dict) -> Tuple[Set[str], int, List[str], List[str], int]:
    """Return (fields_present, addProp count, addProp names, relational, offers)."""
    present: Set[str] = set()

    def nonempty(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip().lower() not in PLACEHOLDERS
        if isinstance(v, (list, dict)):
            return len(v) > 0
        return True

    for key in ("name", "sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14",
                "mpn", "brand", "description", "image", "color", "material",
                "size", "width", "height", "depth", "weight", "hasMeasurement"):
        if nonempty(node.get(key)):
            present.add("gtin" if key.startswith("gtin") else key)

    # Offers: price / currency / availability live one level down.
    offers = node.get("offers")
    offer_list = offers if isinstance(offers, list) else ([offers] if offers else [])
    for o in offer_list:
        if not isinstance(o, dict):
            continue
        if nonempty(o.get("price")) or nonempty(o.get("lowPrice")) or \
           nonempty((o.get("priceSpecification") or {}).get("price")
                    if isinstance(o.get("priceSpecification"), dict) else None):
            present.add("price")
        if nonempty(o.get("priceCurrency")):
            present.add("currency")
        if nonempty(o.get("availability")):
            present.add("availability")

    # additionalProperty is where real attributes hide when a site does it well.
    ap = node.get("additionalProperty")
    ap_list = ap if isinstance(ap, list) else ([ap] if ap else [])
    ap_names = []
    for p in ap_list:
        if isinstance(p, dict):
            nm = p.get("name") or p.get("propertyID") or ""
            if isinstance(nm, str) and nm.strip():
                ap_names.append(nm.strip()[:60])
    if ap_list:
        present.add("additionalProperty")

    relational = [f for f in RELATIONAL_FIELDS if nonempty(node.get(f))]
    present.update(relational)

    return present, len(ap_list), ap_names, relational, len(offer_list)


def audit_html(url: str, html: str, status: int, allowed: bool) -> ProductAudit:
    a = ProductAudit(url=url, http_status=status, bytes=len(html), robots_allowed=allowed)
    s = page_signals(html, url)
    a.jsonld_blocks = len(s.jsonld_blocks)
    types: Counter = Counter()
    for b in s.jsonld_blocks:
        for node in iter_nodes(b):
            for t in type_names(node):
                types[t.split("/")[-1].split("#")[-1]] += 1
    a.jsonld_types = [t for t, _ in types.most_common(12)]

    jl_products = [n for n, _ in s.jsonld_nodes]
    md_products = [n for n, _ in s.microdata_nodes]
    a.has_product_jsonld = bool(jl_products)
    a.has_og_product = s.has_og_product
    a.has_product_microdata = s.has_microdata_product
    a.main_product_markup = s.main_product_markup
    a.category_url_veto = s.category_url_veto
    a.is_product_page = s.is_product_page

    candidates = [("jsonld", p) for p in jl_products] + [("microdata", p) for p in md_products]
    if candidates:
        # Several nodes (both syntaxes, or related-product carousels): audit the
        # richest -- that's the main product. Ties go to JSON-LD.
        best, best_n = None, -1
        for src, p in candidates:
            f, n_ap, names, rel, n_off = audit_product_node(p)
            score = len(f) + n_ap
            if score > best_n:
                best, best_n = (src, f, n_ap, names, rel, n_off), score
        a.structured_source, f, n_ap, names, rel, n_off = best
        a.fields_present = sorted(f)
        a.additional_property_count = n_ap
        a.additional_property_names = names[:40]
        a.relational_fields_present = rel
        a.offer_count = n_off
    return a


# ─────────────────────────────────────────────────────────────────────
# Candidate selection
# ─────────────────────────────────────────────────────────────────────

LANGS = {"it", "en", "de", "fr", "es", "pt", "nl", "ee", "et", "pl", "cs", "sk", "hu", "ro", "sv", "da",
         "no", "fi", "el", "bg", "hr", "sl", "lt", "lv", "ru", "tr", "ja", "zh", "ko", "uk", "at", "ch",
         "be", "us", "gb", "am"}
LOCALE_RE = re.compile(
    r"^/([a-z]{2})(?:[-_][a-z]{2})?(?:/([a-z]{2})(?:[-_][a-z]{2})?)?(?=/)", re.I)


DEFAULT_LANG = "it"          # a site with no locale prefix is read as this language; set by --lang


def set_language(lang3: str) -> None:
    global DEFAULT_LANG
    DEFAULT_LANG, _region, accept = lang_info(lang3)
    HEADERS["Accept-Language"] = accept


def in_locale(url: str, lang: str = "it") -> bool:
    m = LOCALE_RE.match(urlparse(url).path or "/")
    if not m or m.group(1).lower() not in LANGS:
        return True                      # no locale prefix: site default
    return lang in {g.lower() for g in m.groups() if g}


def looks_like_product(url: str, platform: str) -> bool:
    path = urlparse(url).path or "/"
    low = url.lower()
    path_low = path.lower()
    for pat in GENERIC_EXCLUDE:
        if re.search(pat, low):
            return False
    rules = PLATFORM_URL_RULES.get(platform)
    if rules:
        for pat in rules.get("exclude", []):
            if re.search(pat, path_low):
                return False
        for pat in rules.get("include", []):
            if re.search(pat, path_low):
                return True
    for pat in GENERIC_INCLUDE:
        if re.search(pat, low):
            return True
    # Long multi-hyphen slug at depth >= 2 is usually a product.
    segs = [s for s in path.split("/") if s]
    return len(segs) >= 2 and segs[-1].count("-") >= 3 and len(segs[-1]) > 20


def looks_like_root_slug(url: str) -> bool:
    """A single long multi-hyphen path segment at the site root, e.g. /brand-model-tap-123."""
    low = url.lower()
    if any(re.search(pat, low) for pat in GENERIC_EXCLUDE) or urlparse(url).query:
        return False
    segs = [s for s in (urlparse(url).path or "/").split("/") if s]
    return len(segs) == 1 and segs[0].count("-") >= 3 and len(segs[0]) > 20


def load_site(site_dir: Path) -> Optional[dict]:
    pr = site_dir / "probe_results.json"
    if not pr.exists():
        return None
    try:
        meta = json.loads(pr.read_text(encoding="utf-8"))
    except Exception:
        return None
    html = ""
    idx = site_dir / "index.html"
    if idx.exists():
        try:
            html = idx.read_text(encoding="utf-8", errors="replace")
        except Exception:
            html = ""
    sitemap_urls: List[str] = []
    sitemap_total = 0
    sm = site_dir / "sitemap.json"
    if sm.exists():
        try:
            data = json.loads(sm.read_text(encoding="utf-8"))
            sitemap_urls = [u.get("loc", "") for u in data.get("urls", []) if u.get("loc")]
            # The stored list is capped at 20,000; the true count is total_available
            # (written by the fixed pipeline). Old runs lack it -> falls back to the list.
            sitemap_total = int(data.get("total_available") or data.get("url_count") or 0)
        except Exception:
            pass
    sitemap_total = max(sitemap_total, len(sitemap_urls))
    link_urls: List[str] = []
    lk = site_dir / "links.json"
    if lk.exists():
        try:
            data = json.loads(lk.read_text(encoding="utf-8"))
            link_urls = [l.get("url", "") for l in data.get("links", [])
                         if l.get("internal") and l.get("url")]
        except Exception:
            pass
    return dict(meta=meta, html=html, sitemap_urls=sitemap_urls,
                sitemap_total=sitemap_total, link_urls=link_urls, dir=site_dir)


def build_robots(site_dir: Path, base_url: str):
    """Groups that apply to a generic client (User-agent: *), RFC 9309 semantics.

    urllib.robotparser ignores '*' and '$' wildcards and gets longest-match
    wrong, so it silently allowed URLs that rules like 'Disallow: /*?' forbid.
    """
    f = site_dir / "robots.txt"
    if not f.exists():
        return None
    try:
        rb = parse_robots(f.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    groups, _ = select_groups(rb, "*")
    return groups


def robots_ok(groups, url: str) -> bool:
    if groups is None:
        return True
    p = urlparse(url)
    return is_allowed(groups, (p.path or "/") + (f"?{p.query}" if p.query else ""))[0]


# ─────────────────────────────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────────────────────────────

class Sampler:
    def __init__(self, n: int, seed: int, concurrency: int, timeout: float,
                 ignore_robots: bool, delay: float,
                 max_consec_fail: int = 5, no_markup_after: int = 15,
                 persistent_chrome: bool = False,
                 chrome_profile_dir: str = DEFAULT_CHROME_PROFILE):
        self.n = n
        self.seed = seed
        self.timeout = timeout
        self.ignore_robots = ignore_robots
        self.delay = delay
        self.max_consec_fail = max_consec_fail
        self.no_markup_after = no_markup_after
        self.persistent_chrome = persistent_chrome
        self.chrome_profile_dir = chrome_profile_dir
        self.sem = asyncio.Semaphore(concurrency)
        self.client: Optional[httpx.AsyncClient] = None
        self._chrome: Optional[ChromeScraper] = None
        if self.persistent_chrome and concurrency > 1:
            log.warning(
                "persistent Chrome uses one profile; concurrency=%d will queue fetches",
                concurrency,
            )

    async def __aenter__(self):
        if self.persistent_chrome:
            chrome_timeout_ms = int(max(self.timeout, 120) * 1000)
            self._chrome = ChromeScraper(
                user_data_dir=self.chrome_profile_dir,
                headless=False,
                timeout_ms=chrome_timeout_ms,
                settle_ms=SETTLE_MS,
                pre_nav_sleep=PRE_NAV_SLEEP,
                post_fetch_sleep=POST_FETCH_SLEEP,
                domain_min_interval=DOMAIN_MIN_INTERVAL,
            )
            await self._chrome.start()
            log.info(
                "Chrome persistent profile ready (%s); ~%.0fs min between same-domain loads",
                self.chrome_profile_dir, DOMAIN_MIN_INTERVAL,
            )
            log.info(
                "If blocks cite your IP, use a clean network once in the visible "
                "browser — code cannot fix datacenter/VPN reputation alone.",
            )
        self.client = httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True,
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        return self

    async def __aexit__(self, *exc):
        if self._chrome:
            await self._chrome.close()
        if self.client:
            await self.client.aclose()

    async def fetch(self, url: str) -> Tuple[str, int, str, List[Dict[str, Any]]]:
        async with self.sem:
            if self._chrome:
                cap = self._chrome.timeout_ms / 1000 + 180   # goto + settle + sleeps + body reads
                try:
                    res = await asyncio.wait_for(self._chrome.fetch(url), cap)
                except asyncio.TimeoutError:
                    return "", 0, f"chrome fetch exceeded {cap:.0f}s", []
                if res.error and not res.html:
                    return "", 0, res.error, []
                return res.html, res.status, res.error, res.json_responses
            assert self.client
            await asyncio.sleep(self.delay)
            try:
                r = await self.client.get(url)
                return r.text, r.status_code, "", []
            except httpx.HTTPError as e:
                return "", 0, f"{type(e).__name__}: {e}", []

    async def audit_site(self, site: dict) -> dict:
        meta, sd = site["meta"], site["dir"]
        base = meta.get("url") or ""
        company = meta.get("company") or urlparse(base).netloc

        pool_source = "sitemap"
        pool = site["sitemap_urls"]
        pool_total = site.get("sitemap_total") or len(pool)
        if not pool:
            pool_source = "homepage_links"
            pool = site["link_urls"]
            pool_total = len(pool)

        platform = detect_platform(site["html"], pool or site["link_urls"])
        lang = DEFAULT_LANG
        _base_m = LOCALE_RE.match(urlparse(base).path or "/")
        if _base_m and _base_m.group(1).lower() in LANGS:
            lang = _base_m.group(1).lower()
        all_uniq = list(dict.fromkeys(pool))
        uniq = [u for u in all_uniq if in_locale(u, lang)]
        locale_dropped = len(all_uniq) - len(uniq)
        # Priority = URLs the platform's own rules say are products; fallback =
        # anything the generic heuristics accept. Recorded, never silent.
        rules = PLATFORM_URL_RULES.get(platform, {}).get("include", [])
        priority = [u for u in uniq if rules and looks_like_product(u, platform)
                    and any(re.search(p, u.lower()) for p in rules)]
        fallback = [u for u in uniq if u not in set(priority) and looks_like_product(u, platform)]
        # Last resort, only when the two tiers above can't fill the sample: sites
        # whose product URLs are a single long slug at the root.
        slug_pool: List[str] = []
        if len(priority) + len(fallback) < self.n:
            taken = set(priority) | set(fallback)
            slug_pool = [u for u in uniq if u not in taken and looks_like_root_slug(u)]

        robots_groups = None if self.ignore_robots else build_robots(sd, base)

        rnd = random.Random(f"{self.seed}:{company}")
        rnd.shuffle(priority)
        rnd.shuffle(fallback)
        rnd.shuffle(slug_pool)
        queue = priority + fallback + slug_pool

        pdir = sd / "products"
        pdir.mkdir(exist_ok=True)

        audits: List[ProductAudit] = []
        product_pages = 0
        ok_pages = 0
        consec_fail = 0
        stop_reason = "sample_complete"
        attempts = 0
        max_attempts = self.n * 5

        for u in queue:
            if product_pages >= self.n:
                break
            if attempts >= max_attempts:
                stop_reason = "attempt_cap"
                break
            if consec_fail >= self.max_consec_fail:
                stop_reason = f"{self.max_consec_fail}_consecutive_failures"
                break
            if ok_pages >= self.no_markup_after and product_pages == 0:
                stop_reason = f"no_product_markup_in_{ok_pages}_pages"
                break

            if not robots_ok(robots_groups, u):
                audits.append(ProductAudit(url=u, robots_allowed=False,
                                           error="disallowed by robots.txt"))
                continue               # not a request, doesn't count as an attempt

            attempts += 1
            html, status, err, json_xhr = await self.fetch(u)
            challenge = detect_challenge(html, status) if html else ""
            if err or not html or status >= 400 or challenge:
                consec_fail += 1
                audits.append(ProductAudit(
                    url=u, http_status=status, bytes=len(html or ""),
                    challenge=challenge,
                    error=err or (f"challenge:{challenge}" if challenge else
                                  f"HTTP {status}" if status >= 400 else "empty")))
                continue

            consec_fail = 0
            ok_pages += 1
            a = audit_html(u, html, status, True)
            audits.append(a)
            if a.is_product_page:
                product_pages += 1
                name = page_file_name(u)
                try:
                    (pdir / f"{name}.html").write_text(html, encoding="utf-8")
                    if json_xhr:
                        (pdir / f"{name}_xhr.json").write_text(
                            json.dumps(json_xhr, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                except OSError:
                    pass
        else:
            if product_pages < self.n:
                stop_reason = "pool_exhausted"

        # ── access classification: blocking is a measured variable ──────
        fails = [a for a in audits if a.error and a.robots_allowed]
        fail_codes = Counter(a.http_status for a in fails if a.http_status)
        challenges = Counter(a.challenge for a in fails if a.challenge)
        robots_blocked = sum(1 for a in audits if not a.robots_allowed)
        if product_pages >= self.n * 0.8:
            access = "open"
        elif product_pages > 0:
            access = "degraded"
        elif robots_blocked and not attempts:
            access = "robots_disallowed"
        elif fails and all((a.http_status in (401, 403, 429)) or a.challenge for a in fails):
            access = "blocked"
        elif stop_reason.startswith("no_product_markup"):
            access = "open_no_markup"
        elif fails:
            access = "error"
        else:
            access = "no_candidates"
        block_signal = "; ".join(
            [f"HTTP {c}×{n}" for c, n in fail_codes.most_common(3)] +
            [f"{c}×{n}" for c, n in challenges.most_common(2)])

        info = {"company": company, "base_url": base, "platform": platform,
                "pool_source": pool_source, "pool_total": pool_total,
                "pool_stored": len(pool), "locale_dropped": locale_dropped,
                "priority_pool": len(priority),
                "fallback_pool": len(fallback), "slug_pool": len(slug_pool),
                "attempts": attempts,
                "stop_reason": stop_reason, "access": access,
                "block_signal": block_signal,
                "n_target": self.n,
                "generated": datetime.now().isoformat()}
        (sd / "product_audit.json").write_text(
            json.dumps({**info, "products": [asdict(a) for a in audits]},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        return site_row(info, audits)


def site_row(info: dict, audits: List[ProductAudit]) -> dict:
    """One product_audit.csv row from a site's run info + per-page audits."""
    prods = [a for a in audits if a.is_product_page and not a.error]
    n = len(prods)
    ok_pages = sum(1 for a in audits if not a.error)
    robots_blocked = sum(1 for a in audits if not a.robots_allowed)

    def share(field_name: str) -> float:
        return round(sum(1 for a in prods if field_name in a.fields_present) / n, 3) if n else 0.0

    def share_any(names) -> float:
        return round(sum(1 for a in prods if set(names) & set(a.fields_present)) / n, 3) if n else 0.0

    ap_names: Counter = Counter()
    for a in prods:
        ap_names.update(a.additional_property_names)
    src = Counter(a.structured_source for a in prods)

    return {
        "company": info["company"],
        "base_url": info["base_url"],
        "platform": info["platform"],
        "access": info["access"],
        "block_signal": info["block_signal"],
        "stop_reason": info["stop_reason"],
        "pool_source": info["pool_source"],
        "pool_total": info["pool_total"],
        "pool_stored": info["pool_stored"],
        "locale_dropped": info["locale_dropped"],
        "priority_pool": info["priority_pool"],
        "fallback_pool": info["fallback_pool"],
        "slug_pool": info.get("slug_pool", 0),
        "priority_fallback_used": n > 0 and info["priority_pool"] < info.get("n_target", 25),
        "fetch_attempts": info["attempts"],
        "ok_pages": ok_pages,
        "product_pages": n,
        "product_page_rate": round(n / ok_pages, 3) if ok_pages else 0.0,
        "robots_disallowed": robots_blocked,
        "src_jsonld": src.get("jsonld", 0),
        "src_microdata": src.get("microdata", 0),
        "src_none": src.get("none", 0),
        "has_name": share("name"),
        "has_price": share("price"),
        "has_availability": share("availability"),
        "has_gtin_or_mpn": share_any(("gtin", "mpn")),
        "has_brand": share("brand"),
        "has_material": share("material"),
        "has_color": share("color"),
        "has_size_or_dims": share_any(("size", "width", "height", "depth", "hasMeasurement")),
        "has_additional_property": share("additionalProperty"),
        "median_additional_properties": (
            stats.median([a.additional_property_count for a in prods]) if prods else 0),
        "has_any_relational": round(
            sum(1 for a in prods if a.relational_fields_present) / n, 3) if n else 0.0,
        "top_additional_property_names": "; ".join(
            f"{k}({v})" for k, v in ap_names.most_common(12)),
    }


def load_saved_row(site_dir: Path, since: str) -> Optional[dict]:
    """Row from a product_audit.json written at or after `since` (ISO time), else None."""
    f = site_dir / "product_audit.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (data.get("generated") or "") < since:
        return None
    audits = [ProductAudit(**p) for p in data.pop("products", [])]
    return site_row(data, audits)

# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────

# Kept out of automated sampling but written to product_audit.csv with the reason.
EXCLUDED_SITES: Dict[str, Tuple[str, str, str]] = {
    "idrocentro.com": ("Idrocentro", "https://idrocentro.com/", "pdf_catalog_only"),
    "manomano.it": ("ManoMano Italia", "https://www.manomano.it/",
                    "cloudflare_challenge_manual_visit_pending"),
}


def write_audit_csv(out: Path, rows: List[dict], excluded_rows: List[dict]) -> Path:
    dest = out / "product_audit.csv"
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), restval="")
        w.writeheader()
        w.writerows(rows + excluded_rows)
    return dest


async def main_async(args) -> int:
    out = Path(args.out)
    sites_root = out / "sites"
    if not sites_root.is_dir():
        log.error("no such directory: %s", sites_root)
        return 2

    only = {s.strip().lower() for s in (args.only or "").split(",") if s.strip()}
    sites = []
    for d in sorted(sites_root.iterdir()):
        if not d.is_dir():
            continue
        s = load_site(d)
        if not s:
            continue
        company = (s["meta"].get("company") or "").lower()
        host = (urlparse(s["meta"].get("url") or "").netloc or "").lower()
        if only and not (company in only or host in only
                         or host.removeprefix("www.") in only):
            continue
        if host.removeprefix("www.") in EXCLUDED_SITES:
            continue
        sites.append(s)

    excluded_rows = [
        {"company": name, "base_url": url, "access": "excluded", "stop_reason": reason}
        for dom, (name, url, reason) in EXCLUDED_SITES.items()
        if not only or dom in only or name.lower() in only
    ]
    for r in excluded_rows:
        log.info("excluded: %s (%s)", r["company"], r["stop_reason"])

    if not sites:
        log.error("no sites matched")
        return 2
    log.info("auditing %d sites, n=%d products each", len(sites), args.n)

    rows: List[dict] = []
    async with Sampler(
        args.n, args.seed, args.concurrency, args.timeout,
        args.ignore_robots, args.delay,
        max_consec_fail=args.max_consec_fail,
        no_markup_after=args.no_markup_after,
        persistent_chrome=args.persistent_chrome,
        chrome_profile_dir=os.path.expanduser(args.chrome_profile),
    ) as sampler:
        for i, s in enumerate(sites, 1):
            t0 = time.monotonic()
            row = load_saved_row(s["dir"], args.resume_since) if args.resume_since else None
            if row:
                rows.append(row)
                log.info("[%d/%d] %-24s resumed from product_audit.json", i, len(sites),
                         row["company"][:24])
                continue
            try:
                row = await sampler.audit_site(s)
            except Exception as e:
                log.error("site failed: %s: %s", type(e).__name__, e)
                continue
            rows.append(row)
            write_audit_csv(out, rows, excluded_rows)
            log.info("[%d/%d] %-24s %-11s %-17s tries=%-3d prod=%-3d ld/md=%d/%d "
                     "dims=%.2f mat=%.2f rel=%.2f  %s (%.0fs)",
                     i, len(sites), row["company"][:24], row["platform"], row["access"],
                     row["fetch_attempts"], row["product_pages"],
                     row["src_jsonld"], row["src_microdata"],
                     row["has_size_or_dims"], row["has_material"], row["has_any_relational"],
                     row["block_signal"] or row["stop_reason"], time.monotonic() - t0)

    if rows:
        dest = write_audit_csv(out, rows, excluded_rows)
        log.info("wrote %s (%d rows, %d excluded)", dest, len(rows) + len(excluded_rows),
                 len(excluded_rows))

        log.info("── access: %s", dict(Counter(r["access"] for r in rows + excluded_rows)))
        okrows = [r for r in rows if r["product_pages"] >= 20]
        if okrows:
            log.info("── across %d sites with >=20 product pages ──", len(okrows))
            for k in ("has_price", "has_gtin_or_mpn", "has_brand", "has_material",
                      "has_color", "has_size_or_dims", "has_additional_property",
                      "has_any_relational"):
                vals = [r[k] for r in okrows]
                log.info("   %-26s median %.2f  sites>0: %d/%d", k,
                         stats.median(vals), sum(1 for v in vals if v > 0), len(vals))
            log.info("   platforms: %s", dict(Counter(r["platform"] for r in okrows)))
            log.info("   pages audited: %d  (json-ld %d, microdata %d, no markup %d)",
                     sum(r["product_pages"] for r in okrows),
                     sum(r["src_jsonld"] for r in okrows),
                     sum(r["src_microdata"] for r in okrows),
                     sum(r["src_none"] for r in okrows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sample product pages and audit structured data")
    p.add_argument("--out", default="discovery_output", help="discovery_pipeline output dir (a batch folder)")
    p.add_argument("--lang", default="ita", help="language of the leads, e.g. ita, deu (default ita)")
    p.add_argument("--n", type=int, default=25, help="product pages per site (default 25)")
    p.add_argument("--seed", type=int, default=1, help="sampling seed (keep fixed!)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--delay", type=float, default=0.7, help="per-request delay, seconds")
    p.add_argument("--only", default="", help="comma-separated domains to restrict to")
    p.add_argument("--ignore-robots", action="store_true",
                   help="fetch even where robots.txt disallows (recorded in output)")
    p.add_argument("--max-consec-fail", type=int, default=5,
                   help="stop a site after this many consecutive failed fetches")
    p.add_argument("--no-markup-after", type=int, default=15,
                   help="stop a site after this many OK pages with no product markup")
    p.add_argument(
        "--persistent-chrome", action="store_true",
        help="Fetch product pages with real Chrome + profile (see scraping.py)",
    )
    p.add_argument(
        "--chrome-profile", default=DEFAULT_CHROME_PROFILE,
        help="User data dir for --persistent-chrome",
    )
    p.add_argument("--resume-since", default="",
                   help="ISO time; reuse sites whose product_audit.json was written after it")
    p.add_argument("--verbose", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    set_language(args.lang)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.persistent_chrome:
        if args.concurrency > 1:
            log.warning("--persistent-chrome: forcing concurrency=1 (avoid rapid taps)")
            args.concurrency = 1
    if sys.platform == "darwin":
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.expanduser("~/Library/Caches/ms-playwright"),
        )
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()