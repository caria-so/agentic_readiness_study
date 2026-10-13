#!/usr/bin/env python3
"""
Acceptance tests for product-page detection (tweaks.md, Task 3).

Fetches each page once with the pipeline's headers and caches it under
tests/fixtures/ (HTML + final URL), so reruns work offline.

Run from the project root:
  python -m tests.test_product_detection            # use fixtures, fetch missing ones
  python -m tests.test_product_detection --refetch  # ignore fixtures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

import httpx

from a_discovery_pipeline import DEFAULT_HEADERS
from b_product_sample import looks_like_product
from utils.md_extract import extract_items
from utils.product_detection import (
    CANONICAL_RE, find_product_nodes_nested, has_product_markup, is_product_page_url,
    parse_jsonld_blocks,
)

FIXTURES = Path(__file__).parent / "fixtures"

# (id, label, url, expected is_product_page_url, expected looks_like_product or None)
CASES = [
    (1, "boleco_category", "https://www.boleco.it/5-tutto-per-le-docce", False, False),
    (2, "boleco_pdp_1624",
     "https://www.boleco.it/mobili-bagno/1624-mobile-bagno-sospeso-zoe-60-cm.html", True, True),
    (3, "boleco_subcat_1_tile", "https://www.boleco.it/it/859-appendini-bagno-a-parete", False, False),
    (4, "boleco_subcat_2_tiles", "https://www.boleco.it/it/842-mobili-scandinavi", False, False),
    (5, "boleco_subcat_8_tiles", "https://www.boleco.it/it/799-specchi-filo-lucido", False, False),
    (6, "ikea_pdp",
     "https://www.ikea.com/it/it/p/zebrasaev-lampada-a-sospensione-bianco-plastica-10522481/",
     True, None),
    (7, "shopify_shopbagno_pdp",
     "https://www.shopbagno.it/products/box-doccia-tre-lati-anta-battente-cristallo-8mm-aries",
     True, None),
    # Guards: real product pages on other platforms must not be rejected by the
    # "single top-level url-less node" rule.
    (8, "prestashop_bagnodistile_pdp",
     "https://www.bagnodistile.com/pavimenti-per-esterno/9-adamello-33x33-grigio-693059703642.html",
     True, None),
    (9, "prestashop_ceramikeshop_pdp", "https://ceramikeshop.it/ultracare-keranet-lt-1", True, None),
    (10, "prestashop_mondialshop_pdp",
     "https://www.mondialshop.it/it/specchio-bagno-retroilluminato/"
     "1669-specchio-bagno-tondo-con-illuminazione-a-led-moonlight.html", True, None),
    (11, "woocommerce_jobagno_pdp",
     "https://jo-bagno.it/catalano/copriwater-polis-ceramica-catalano.html", True, None),
    (12, "magento_kvstore_pdp",
     "https://www.kvstore.it/rubinetto-due-maniglie-bidet-retro-vendita-online/", True, None),
]

# Markup layer alone cannot tell a one-tile category from a product page; only the
# URL veto separates them. (label, expected has_product_markup)
KNOWN_MARKUP_LIMITATIONS = [("boleco_subcat_1_tile", True)]


def load(label: str, url: str, refetch: bool) -> tuple[str, str]:
    html_f, meta_f = FIXTURES / f"{label}.html", FIXTURES / f"{label}.json"
    if not refetch and html_f.exists() and meta_f.exists():
        return html_f.read_text(encoding="utf-8"), json.loads(meta_f.read_text())["final_url"]
    r = httpx.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=45)
    r.raise_for_status()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    html_f.write_text(r.text, encoding="utf-8")
    meta_f.write_text(json.dumps({"url": url, "final_url": str(r.url), "status": r.status_code}))
    return r.text, str(r.url)


def explain(html: str) -> str:
    m = CANONICAL_RE.search(html)
    lines = [f"    canonical: {m.group(1) if m else '(none)'}"]
    nodes = [("jsonld", n, d) for n, d in find_product_nodes_nested(parse_jsonld_blocks(html))] + \
            [("microdata", n, d) for n, d in find_product_nodes_nested(extract_items(html))]
    for src, n, nested in nodes[:25]:
        lines.append(f"    {src:9} {'nested ' if nested else 'top    '} type={n.get('@type')} "
                     f"url={n.get('url')} @id={n.get('@id')}")
    if len(nodes) > 25:
        lines.append(f"    ... {len(nodes) - 25} more")
    return "\n".join(lines)


def run_case(cid: int, label: str, url: str, want_markup: bool,
             want_url: Optional[bool], refetch: bool) -> bool:
    try:
        html, final = load(label, url, refetch)
    except Exception as e:
        print(f"[{cid}] FAIL {label}: fetch error {type(e).__name__}: {e}")
        return False
    got = is_product_page_url(html, final)
    ok = got == want_markup
    msg = f"is_product_page_url={got} (want {want_markup}), markup={has_product_markup(html, final)}"
    if want_url is not None:
        got_url = looks_like_product(final, "prestashop")
        ok = ok and got_url == want_url
        msg += f", looks_like_product={got_url} (want {want_url})"
    print(f"[{cid}] {'PASS' if ok else 'FAIL'} {label}: {msg}")
    if not ok:
        print(f"    final_url: {final}")
        print(explain(html))
    return ok


def test_all():
    assert main([]) == 0


def main(argv) -> int:
    refetch = "--refetch" in argv
    results = [run_case(*c, refetch=refetch) for c in CASES]
    urls = {c[1]: c[2] for c in CASES}
    for label, want in KNOWN_MARKUP_LIMITATIONS:
        html, final = load(label, urls[label], False)
        got = has_product_markup(html, final)
        results.append(got == want)
        print(f"[known markup-level limitation] {'PASS' if got == want else 'FAIL'} {label}: "
              f"has_product_markup={got} (want {want}; only the URL veto rejects it)")
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
