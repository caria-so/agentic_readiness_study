"""
Product-page detection, shared by discovery (stage 1) and product sampling (stage 2).

A page is a product page when its markup describes one main product
(og:type=product, or a Product/ProductGroup node whose url/@id is this page, or
the only top-level url-less one) AND its path is not a category URL of the
detected platform.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urljoin, urlparse

from .md_extract import extract_items                         # microdata + RDFa

# ─────────────────────────────────────────────────────────────────────
# Platform fingerprints
# ─────────────────────────────────────────────────────────────────────

PLATFORM_HTML_SIGNS = [
    ("shopify",    [r"cdn\.shopify\.com", r"Shopify\.theme", r"/cdn/shop/"]),
    ("magento",    [r"Magento_", r"/static/version\d+/", r"mage/requirejs"]),
    ("prestashop", [r"prestashop", r"/modules/ps_", r"var prestashop"]),
    ("woocommerce",[r"woocommerce", r"wp-content/plugins/woocommerce"]),
    ("sfcc",       [r"/on/demandware\.store", r"demandware\.static"]),
    ("bigcommerce",[r"cdn\d+\.bigcommerce\.com"]),
    ("shopware",   [r"/bundles/storefront", r"shopware"]),
    ("salesforce", [r"dwcustom", r"/dw/shop/"]),
    ("vtex",       [r"vtexassets\.com", r"vtex\.com"]),
    ("custom",     []),
]

# PrestaShop root category (/5-slug or /it/5-slug); no dot, so /1624-x.html is not matched.
_PS_ROOT_CATEGORY_RE = re.compile(r"^/(?:[a-z]{2}/)?\d+-[^/.]+/?$")

# URL shapes that indicate a product detail page, per platform.
# "category" paths veto a page as a product page even when its markup passes;
# "exclude" only keeps URLs out of the sampling pool (e.g. Shopify duplicates).
PLATFORM_URL_RULES: Dict[str, Dict[str, List[str]]] = {
    "shopify":    {"include": [r"/products/[^/?#]+$"], "exclude": [r"/collections/.*/products/"]},
    "magento":    {"include": [r"\.html$"], "exclude": [r"/cat(alog)?/", r"/category/"]},
    "prestashop": {"include": [r"/\d+[-/][^/]+\.html$"],
                   "exclude": [_PS_ROOT_CATEGORY_RE.pattern],
                   "category": [_PS_ROOT_CATEGORY_RE.pattern]},
    "woocommerce":{"include": [r"/prodotto/[^/?#]+/?$", r"/product/[^/?#]+/?$",
                               r"/shop/[^/?#]+/?$"], "exclude": []},
    "sfcc":       {"include": [r"/.+-\d+\.html$", r"/p/"], "exclude": []},
    "shopware":   {"include": [r"/detail/", r"/[^/]+/[A-Z0-9]{6,}$"], "exclude": []},
}

# Generic fallbacks used when the platform is unknown or its rules match nothing.
GENERIC_INCLUDE = [
    r"/prodotto/", r"/prodotti/[^/?#]+$", r"/product/", r"/products/", r"/p/",
    r"/artikel/", r"/produkt/", r"/detail/", r"/dp/[A-Z0-9]{10}",
    r"-p-\d+", r"/item/", r"\.html$",
]
# Anything matching these is never a product detail page.
GENERIC_EXCLUDE = [
    r"/cart", r"/checkout", r"/account", r"/login", r"/register", r"/wishlist",
    r"/blog", r"/news", r"/faq", r"/contatti", r"/contact", r"/about", r"/chi-siamo",
    r"/informativa", r"/privacy", r"/cookie", r"/termini", r"/terms", r"/resi",
    r"/spedizion", r"/pagament", r"/brand[s]?/?$", r"/marche/?$",
    r"/categor", r"/collections/[^/]+$", r"/c/", r"/search", r"/cerca",
    r"/sitemap", r"\.(jpg|jpeg|png|gif|webp|svg|pdf|css|js|xml|zip)$",
    r"/tag/", r"/author/", r"/page/\d+", r"[?&](page|p)=\d+",
    r"/feed", r"/rss", r"/media/", r"/static/", r"/assets/",
]


def detect_platform(html: str, urls: List[str]) -> str:
    joined = " ".join(urls[:300])
    for name, pats in PLATFORM_HTML_SIGNS:
        if not pats:
            continue
        for p in pats:
            if re.search(p, html, re.I) or re.search(p, joined, re.I):
                return name
    if re.search(r"/products/[^/]+$", joined):
        return "shopify"
    if re.search(r"/\d+-[^/]+\.html$", joined):
        return "prestashop"
    return "custom"


def is_platform_category_url(url: str, platform: str) -> bool:
    path = (urlparse(url).path or "/").lower()
    return any(re.search(p, path)
               for p in PLATFORM_URL_RULES.get(platform, {}).get("category", []))


# ─────────────────────────────────────────────────────────────────────
# Markup
# ─────────────────────────────────────────────────────────────────────

SCRIPT_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
MICRODATA_PRODUCT_RE = re.compile(
    r'itemtype\s*=\s*["\']https?://schema\.org/Product["\']', re.I)
OG_TYPE_PRODUCT_RE = re.compile(
    r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']product["\']', re.I)
CANONICAL_RE = re.compile(
    r'<link\b(?=[^>]*\brel=["\']?canonical\b)[^>]*\bhref=["\']([^"\']+)["\']', re.I)


def parse_jsonld_blocks(html: str) -> List[Any]:
    out = []
    for m in SCRIPT_LDJSON_RE.finditer(html):
        raw = m.group(1).strip()
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw).strip()
        raw = re.sub(r"^\s*//\s*<!\[CDATA\[|//\s*\]\]>\s*$", "", raw).strip()
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # Trailing-comma and single-quote breakage is common; one cheap retry.
            try:
                out.append(json.loads(re.sub(r",\s*([}\]])", r"\1", raw)))
            except json.JSONDecodeError:
                out.append(None)
    return out


def iter_nodes(data: Any) -> Iterable[dict]:
    """Walk a JSON-LD structure yielding every dict, flattening @graph."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from iter_nodes(v)
    elif isinstance(data, list):
        for v in data:
            yield from iter_nodes(v)


def type_names(node: dict) -> List[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


def _is_product_node(node: dict) -> bool:
    names = [n.split("/")[-1].split("#")[-1] for n in type_names(node)]
    return any(n in ("Product", "ProductModel", "IndividualProduct",
                     "ProductGroup", "Vehicle") for n in names)


def find_product_nodes_nested(blocks: List[Any]) -> List[Tuple[dict, bool]]:
    """(node, nested) for every Product-like node; nested = inside another Product/ProductGroup."""
    out: List[Tuple[dict, bool]] = []

    def walk(o: Any, inside: bool) -> None:
        if isinstance(o, dict):
            is_p = _is_product_node(o)
            if is_p:
                out.append((o, inside))
            for v in o.values():
                walk(v, inside or is_p)
        elif isinstance(o, list):
            for v in o:
                walk(v, inside)

    for b in blocks:
        walk(b, False)
    return out


def find_product_nodes(blocks: List[Any]) -> List[dict]:
    return [n for n, _ in find_product_nodes_nested(blocks)]


def _norm(u: str) -> str:
    p = urlparse(u)
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def _node_refs(n: dict) -> List[str]:
    refs = []
    for key in ("url", "@id"):
        v = n.get(key)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, dict):
                x = x.get("@id") or x.get("url")
            if isinstance(x, str) and x.split("#")[0].strip():
                refs.append(x.split("#")[0].strip())
    return refs


def has_main_product(nodes: List[Tuple[dict, bool]], page_url: str, canonical: str = "") -> bool:
    """True if a Product node describes this page.

    A node whose url/@id resolves to the page or its canonical always counts. A node
    with no url counts only if it is the sole top-level Product/ProductGroup: listing
    themes emit one url-less node per tile, so several of them mean a grid.
    """
    targets = {_norm(t) for t in (page_url, canonical) if t}
    base = page_url or canonical
    for n, _ in nodes:
        if any(_norm(urljoin(base, r)) in targets for r in _node_refs(n)):
            return True
    top = [n for n, nested in nodes if not nested]
    return len(top) == 1 and not _node_refs(top[0])


# ─────────────────────────────────────────────────────────────────────
# Decision
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PageSignals:
    jsonld_blocks: List[Any] = field(default_factory=list)
    jsonld_nodes: List[Tuple[dict, bool]] = field(default_factory=list)
    microdata_nodes: List[Tuple[dict, bool]] = field(default_factory=list)
    has_og_product: bool = False
    has_microdata_product: bool = False
    main_product_markup: bool = False  # og:type=product or a Product node describing this page
    category_url_veto: bool = False    # path matched the detected platform's category rule

    @property
    def is_product_page(self) -> bool:
        return self.main_product_markup and not self.category_url_veto


def page_signals(html: str, page_url: str = "") -> PageSignals:
    s = PageSignals()
    s.jsonld_blocks = parse_jsonld_blocks(html)
    s.jsonld_nodes = find_product_nodes_nested(s.jsonld_blocks)
    s.has_og_product = bool(OG_TYPE_PRODUCT_RE.search(html))

    # Microdata / RDFa: parse, don't just detect. Only pay for the parse when
    # the page actually uses one of the two syntaxes.
    if MICRODATA_PRODUCT_RE.search(html) or re.search(r'typeof\s*=\s*["\'][^"\']*Product', html, re.I):
        s.microdata_nodes = find_product_nodes_nested(extract_items(html))
    s.has_microdata_product = bool(s.microdata_nodes) or bool(MICRODATA_PRODUCT_RE.search(html))

    m = CANONICAL_RE.search(html)
    canonical = urljoin(page_url, m.group(1).strip()) if m else ""
    s.main_product_markup = s.has_og_product or has_main_product(
        s.jsonld_nodes + s.microdata_nodes, page_url, canonical)
    platform = detect_platform(html, [page_url] if page_url else [])
    s.category_url_veto = is_platform_category_url(page_url or canonical, platform)
    return s


def has_product_markup(html: str, page_url: str = "") -> bool:
    """Markup layer only: does a Product node describe this page?"""
    return page_signals(html, page_url).main_product_markup


def is_product_page_url(html: str, page_url: str = "") -> bool:
    """Combined decision: main-product markup and not a platform category URL."""
    return page_signals(html, page_url).is_product_page
