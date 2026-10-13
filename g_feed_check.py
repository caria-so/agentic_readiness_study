#!/usr/bin/env python3
"""
Feed check: does a retailer's public product endpoint expose structured attributes that the
product page does not (in schema.org) or does not state at all?

Only platforms with a standard public product endpoint are tried:
  shopify       <product page url>.json                      (the storefront product JSON)
  woocommerce   /wp-json/wc/store/v1/products?slug=<slug>    (the public Store API)

Polite by construction: robots.txt is read first and the endpoint is skipped if it disallows it;
a few sampled pages per retailer; at least DELAY seconds between requests to one site; one
descriptive User-Agent. Nothing is logged in or bypassed: a wall, login or non-JSON answer is
recorded as "not exposed".

For each sampled page it compares the attributes the feed carries with what the page states
(spec JSONs from c_spec_extract.py) and writes <batch>/tables/feed_check_results.json.

  python g_feed_check.py --industry bathroom --lang ita     # all shopify / woocommerce retailers, 5 pages each
  python g_feed_check.py --batch <folder> --sites arcshop --per-site 3
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

from utils.batch import add_batch_args, resolve_batch
from utils.spec_rules import canonical

UA = "Mozilla/5.0 (compatible; datasheet-coverage-study; offline research, light sampling)"
DELAY = 1.5
SUPPORTED = {"shopify", "woocommerce"}


def get(client: httpx.Client, url: str) -> httpx.Response | None:
    time.sleep(DELAY)
    try:
        return client.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25,
                          follow_redirects=True)
    except Exception as e:                           # network trouble is a result, not a crash
        print(f"    request failed: {type(e).__name__}", flush=True)
        return None


def json_or_none(r: httpx.Response | None):
    if r is None or r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
        return None
    try:
        return r.json()
    except Exception:
        return None


def feed_attrs_shopify(data: dict) -> dict:
    """Canonical attributes carried by a Shopify product JSON, as {attr: sample value}."""
    p = data.get("product") or {}
    out = {}
    v = (p.get("variants") or [{}])[0]
    if v.get("sku"):
        out["sku"] = v["sku"]
    if v.get("barcode"):
        out["gtin"] = v["barcode"]
    if (v.get("grams") or 0) > 0 or (v.get("weight") or 0) > 0:
        out["weight"] = f"{v.get('weight')} {v.get('weight_unit', '')}".strip()
    if p.get("vendor"):
        out["brand"] = p["vendor"]
    for o in p.get("options") or []:
        c = canonical(o.get("name", ""))
        if c and o.get("values") and o["values"] != ["Default Title"]:
            out[c] = ", ".join(o["values"])[:80]
    if p.get("product_type"):
        out["product_type"] = p["product_type"]
    return out


def feed_attrs_woo(data) -> dict:
    p = data[0] if isinstance(data, list) and data else {}
    out = {}
    if p.get("sku"):
        out["sku"] = p["sku"]
    if p.get("weight"):
        out["weight"] = p["weight"]
    d = p.get("dimensions") or {}
    if any(d.get(k) for k in ("length", "width", "height")):
        out["dimensions"] = "x".join(str(d.get(k)) for k in ("length", "width", "height"))
    for b in p.get("brands") or []:
        out["brand"] = b.get("name", "")
    for a in p.get("attributes") or []:
        c = canonical(a.get("name", ""))
        terms = ", ".join(t.get("name", "") for t in a.get("terms") or [])
        if c and terms:
            out[c] = terms[:80]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", default="", help="comma-separated site-dir prefixes (default: all supported)")
    ap.add_argument("--per-site", type=int, default=5)
    add_batch_args(ap)
    args = ap.parse_args()
    batch = resolve_batch(args).ensure()
    ROOT, out_file = batch.sites, batch.tables / "feed_check_results.json"
    only = [s.strip().lower() for s in args.sites.split(",") if s.strip()]

    results = json.loads(out_file.read_text(encoding="utf-8")) \
        if only and out_file.exists() else {}      # a partial rerun updates, not replaces
    with httpx.Client() as client:
        for sd in sorted(p for p in ROOT.iterdir() if (p / "product_audit.json").exists()):
            audit = json.loads((sd / "product_audit.json").read_text(encoding="utf-8"))
            platform = audit.get("platform")
            if platform not in SUPPORTED or (only and not any(sd.name.lower().startswith(o) for o in only)):
                continue
            base = audit.get("base_url", "")
            print(f"{sd.name} ({platform})", flush=True)
            rp = robotparser.RobotFileParser()
            rr = get(client, urljoin(base, "/robots.txt"))
            rp.parse(rr.text.splitlines() if rr is not None and rr.status_code == 200 else [])
            pages = []
            for f in sorted((sd / "specs").glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                if not d.get("excluded_reason"):
                    pages.append(d)
            pages = random.Random(7).sample(pages, min(args.per_site, len(pages)))
            site_res = {"platform": platform, "pages": []}
            for d in pages:
                url = d["url"].split("?")[0]
                path = urlparse(url).path
                if platform == "shopify":
                    path = re.sub(r"^/[a-z]{2}[_-][A-Za-z]{2}(?=/)", "", path)     # /it_IT/products/x -> /products/x
                    ep = urljoin(base, path.rstrip("/") + ".json")
                else:
                    slug = re.sub(r"\.html?$", "", path.rstrip("/").split("/")[-1])   # permalink suffix is not part of the slug
                    ep = urljoin(base, f"/wp-json/wc/store/v1/products?slug={slug}")
                if not rp.can_fetch(UA, ep):
                    site_res["pages"].append({"url": url, "endpoint": ep, "status": "robots_disallow"})
                    print("    robots.txt disallows the endpoint; skipped", flush=True)
                    continue
                r = get(client, ep)
                data = json_or_none(r)
                if data is None:
                    site_res["pages"].append({"url": url, "endpoint": ep, "status": "not_exposed",
                                              "http": r.status_code if r is not None else None})
                    print(f"    {ep[:70]} -> not exposed ({r.status_code if r is not None else 'no answer'})", flush=True)
                    continue
                if platform == "woocommerce" and not data:          # the API answered, but no product has this slug
                    site_res["pages"].append({"url": url, "endpoint": ep, "status": "not_found"})
                    print(f"    {ep[-60:]} -> answered, product not found by slug", flush=True)
                    continue
                feed = feed_attrs_shopify(data) if platform == "shopify" else feed_attrs_woo(data)
                page_attrs = d.get("attributes") or {}
                l1 = {a for a, v in page_attrs.items() if any(s["layer"] == "L1_schema" for s in v["sources"])}
                site_res["pages"].append({
                    "url": url, "endpoint": ep, "status": "ok", "feed": feed,
                    "page_any_layer": sorted(page_attrs), "page_l1": sorted(l1),
                    "feed_not_in_page_l1": sorted(a for a in feed if a not in l1),
                    "feed_not_on_page_at_all": sorted(a for a in feed if a not in page_attrs),
                })
                print(f"    ok: feed carries {sorted(feed)}", flush=True)
            results[sd.name] = site_res
    out_file.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_file}")


if __name__ == "__main__":
    main()
