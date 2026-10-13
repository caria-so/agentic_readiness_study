#!/usr/bin/env python3
"""
Stage 3: spec extraction from saved product pages (offline).
============================================================

For every page under <batch>/sites/*/products/ finds each attribute the page
states and the layer it was found in:

  L1_schema  schema.org JSON-LD / microdata (Product, Offer, additionalProperty)
  L2_block   spec blocks: <dl> dt/dd, two-cell <tr>, label/value <div> pairs
  L3_lines   "Label: value" lines in the main product area

Values are checked before counting: weight must be > 0, dimensions must contain a
number, a schema.org weight/dimension repeated on >= 40% of a site's pages is a
platform default, and a brand equal to the retailer's own name is "brand_is_retailer_name".

Usage
-----
  python c_spec_extract.py --industry bathroom --lang ita     # latest batch, all sites
  python c_spec_extract.py --batch results/bathroom/ita/20261003_merged
  python c_spec_extract.py --only Boleco,Tecnomat       # site-dir prefixes
  python c_spec_extract.py --report                     # also print the report tables

Outputs
-------
  <batch>/sites/<site>/specs/<page>.json     per page
  <batch>/tables/spec_coverage.csv           per site x attribute
  <batch>/tables/spec_summary.csv            per site
  <batch>/tables/spec_checks.csv             GTIN, identical ids, platform defaults, GPSR
  <batch>/tables/spec_dropped.jsonl          every pair the junk filter dropped, with its rule
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from utils.batch import add_batch_args, resolve_batch
from utils.html_main import (
    full_text, link_texts, main_area, sections_under, make_soup, page_title, section_lines, strip_chrome, strip_reviews,
)
from utils.page_files import iter_saved_pages
from utils.spec_pairs import block_pairs, line_pairs
from utils.spec_rules import (
    BRAND_IS_RETAILER_NAME, CANONICAL, COMPLIANCE, MAX_LABEL_REPEATS, SCHEMA_DEFAULT_SHARE, SPEC_ATTRS,
    GPSR_TERMS_RE, MANUFACTURER_HEADING_RE, Retailer, canonical, display_label, eu_contact_state, exclusion_reason, find_eu_contact,
    has_valid_gtin, is_manufacturer_block, typo_of,
    invalid_value, is_internal_code, is_own_brand, junk_reason, norm_id, norm_label, packaged,
    pair_key, repeated_defaults, template_pairs,
)
from utils.spec_schema import SchemaFacts, schema_facts

LAYERS = ("L1_schema", "L2_block", "L3_lines")
Pair = Tuple[str, str, str]                   # (label, value, section heading)


@dataclass
class RawPage:
    file: str
    url: str
    title: str = ""
    method: str = ""
    text_chars: int = 0
    links: set = field(default_factory=set)
    l1: SchemaFacts = field(default_factory=SchemaFacts)
    l2: List[Pair] = field(default_factory=list)
    l3: List[Pair] = field(default_factory=list)
    eu_contact: str = ""                  # value found by the whole-page text search
    gpsr: dict = field(default_factory=dict)   # raw_html / text hits of the GPSR terms, contact state


def read_page(html: str, url: str = "", file: str = "") -> RawPage:
    """Everything a page states, before any filtering."""
    soup = make_soup(html)
    raw = RawPage(file=file, url=url, title=page_title(soup), l1=schema_facts(html, url))
    text = full_text(soup)
    raw.eu_contact = find_eu_contact(text)
    blocks = [(h, b) for h, b in sections_under(soup, MANUFACTURER_HEADING_RE) if is_manufacturer_block(b)]
    raw.gpsr = {"raw_html": bool(GPSR_TERMS_RE.search(html)), "text": bool(GPSR_TERMS_RE.search(text)),
                "state": eu_contact_state(text), "manufacturer_block": bool(blocks),
                "manufacturer_text": blocks[0][1][:240] if blocks else ""}
    strip_chrome(soup)
    strip_reviews(soup)
    roots, raw.method = main_area(soup)
    raw.links = link_texts(roots)
    raw.l2 = block_pairs(roots)
    lines = section_lines(roots)
    raw.text_chars = len(" ".join(ln for ln, _ in lines))
    l2_keys = {pair_key(k, v) for k, v, _ in raw.l2}
    l2_labels = {norm_label(k) for k, _, _ in raw.l2}
    # A spec-block label containing a colon ("Prodotto imballato: peso") is not an L3 line.
    raw.l3 = [(k, v, h) for k, v, h in line_pairs(lines)
              if pair_key(k, v) not in l2_keys and norm_label(f"{k}: {v}") not in l2_labels]
    return raw


@dataclass
class SiteContext:
    """Site-level facts the page-level rules need."""
    template: set = field(default_factory=set)       # (label, value) on >= 80% of pages
    defaults: set = field(default_factory=set)       # (attr, value) schema.org platform defaults
    retailer: Optional[Retailer] = None


def l1_attrs(raw: RawPage, retailer: Optional[Retailer] = None) -> List[Tuple[str, str]]:
    """(attribute, value) of a page's schema.org pairs, for the platform-default counts.
    A brand equal to the retailer's name is excluded: it is re-tagged, not a default."""
    return [(a, v) for canon, k, v in raw.l1.pairs
            if (a := canon or canonical(k) or "") and not (a == "brand" and is_own_brand(v, retailer))]


def ids_identical(raw: RawPage) -> List[str]:
    """Which of GTIN, MPN, SKU share a value on this page (schema.org, checked before any filter):
    'gtin=mpn=sku' when all three do, else the identical pairs."""
    ids = {a: {norm_id(v) for c, _, v in raw.l1.pairs if c == a} - {""} for a in ("gtin", "mpn", "sku")}
    pairs = [f"{a}={b}" for a, b in (("gtin", "mpn"), ("gtin", "sku"), ("mpn", "sku")) if ids[a] & ids[b]]
    return (["gtin=mpn=sku"] if ids["gtin"] & ids["mpn"] & ids["sku"] else []) + pairs


def filter_pairs(raw: RawPage, ctx: SiteContext) -> Tuple[Dict[str, list], List[dict]]:
    """Kept (attr or None, label, value) per layer, and every dropped pair with its rule.

    L2/L3 get the junk filter; L1 only drops internal codes in additionalProperty and
    platform defaults. Value checks run on every layer; an own-brand value is re-tagged.
    """
    kept: Dict[str, list] = {"L1_schema": [], "L2_block": [], "L3_lines": []}
    dropped: List[dict] = []
    layered = [("L1_schema", canon or canonical(k), k, v) for canon, k, v in raw.l1.pairs]
    layered += [(layer, packaged(canonical(k), h), k, v)
                for layer, pairs in (("L2_block", raw.l2), ("L3_lines", raw.l3)) for k, v, h in pairs]
    repeats = Counter(norm_label(k) for layer, _, k, _ in layered if layer != "L1_schema")
    seen: set = set()
    for layer, attr, k, v in layered:
        if layer != "L1_schema" and repeats[norm_label(k)] > MAX_LABEL_REPEATS:
            if norm_label(k) in seen:          # a list: keep the first (main product), drop the rest
                dropped.append({"layer": layer, "attr": attr or "", "label": display_label(k),
                                "value": v[:200], "rule": "repeated_label_list"})
                continue
            seen.add(norm_label(k))
        if layer == "L1_schema":
            rule = ("internal_code" if not attr and is_internal_code(k, v) else
                    "repeated_default" if attr and (attr, v.strip().lower()) in ctx.defaults else None)
        else:
            rule = junk_reason(k, v, raw.title, raw.links) or \
                ("template" if pair_key(k, v) in ctx.template else None)
        if not rule and attr == "gtin" and not has_valid_gtin(v):
            rule = "invalid_gtin"
        if not rule and attr and invalid_value(attr, v):
            rule = "invalid_value"
        if rule:
            dropped.append({"layer": layer, "attr": attr or "", "label": display_label(k),
                            "value": v[:200], "rule": rule})
            continue
        if attr == "brand" and is_own_brand(v, ctx.retailer):
            attr = BRAND_IS_RETAILER_NAME
        kept[layer].append((attr, k, v))
    if raw.eu_contact and not any(a == COMPLIANCE for ly in kept.values() for a, _, _ in ly):
        kept["L3_lines"].append((COMPLIANCE, "Persona responsabile", raw.eu_contact))
    return kept, dropped


def merge_attributes(kept: Dict[str, list]) -> Tuple[dict, dict]:
    """One value per canonical attribute, preferring L1 over L2 over L3; every source kept."""
    attrs: dict = {}
    other: dict = {}
    layered = [(layer, canon, label, value) for layer in LAYERS for canon, label, value in kept[layer]]
    for layer, canon, label, value in layered:
        if canon:
            a = attrs.setdefault(canon, {"value": value, "layer": layer, "sources": []})
            a["sources"].append({"layer": layer, "label": display_label(label), "value": value[:200]})
        else:
            other.setdefault(display_label(label), value[:200])
    return attrs, other


def score_page(raw: RawPage, ctx: Optional[SiteContext] = None) -> Tuple[dict, List[dict]]:
    kept, dropped = filter_pairs(raw, ctx or SiteContext())
    attrs, other = merge_attributes(kept)
    reason = exclusion_reason(raw.title, raw.url or raw.file, raw.l1.has_price, raw.l1.has_sku,
                              len(kept["L2_block"]) + len(kept["L3_lines"]))
    page = {
        "url": raw.url, "file": raw.file, "title": raw.title, "excluded_reason": reason,
        "main_area_method": raw.method, "main_area_chars": raw.text_chars,
        "attributes": attrs, "other_labels": other, "ids_identical": ids_identical(raw),
        "gpsr": raw.gpsr,
        "typo_labels": {display_label(k): typo_of(k)[1] for ly in kept.values()
                        for _, k, _ in ly if typo_of(k)},
        "counts": {"L1": len(kept["L1_schema"]), "L2": len(kept["L2_block"]),
                   "L3": len(kept["L3_lines"]), "dropped": len(dropped)},
    }
    return page, dropped


def extract_page(html: str, url: str = "", retailer: Optional[Retailer] = None) -> Tuple[dict, List[dict]]:
    """Single page; the template and platform-default rules need the whole site."""
    return score_page(read_page(html, url), SiteContext(retailer=retailer))


def site_retailer(site_dir: Path) -> Optional[Retailer]:
    pj = site_dir / "product_audit.json"
    if not pj.exists():
        return None
    info = json.loads(pj.read_text(encoding="utf-8"))
    return Retailer.from_site(info.get("company", ""), info.get("base_url", ""))


def run_site(site_dir: Path) -> Tuple[List[dict], List[dict]]:
    raws = [read_page(f.read_text(encoding="utf-8", errors="replace"), url, f.name)
            for f, url in iter_saved_pages(site_dir)]
    retailer = site_retailer(site_dir)
    values = [l1_attrs(r, retailer) for r in raws]
    all_attrs = {a for vs in values for a, _ in vs}
    ctx = SiteContext(template=template_pairs([[(k, v) for k, v, _ in r.l2 + r.l3] for r in raws]),
                      defaults=repeated_defaults(values)
                      | repeated_defaults(values, SCHEMA_DEFAULT_SHARE, all_attrs),
                      retailer=retailer)
    out_dir = site_dir / "specs"
    out_dir.mkdir(exist_ok=True)
    pages, dropped_all = [], []
    for raw in raws:
        page, dropped = score_page(raw, ctx)
        pages.append(page)
        dropped_all += [{"site": site_dir.name, "file": raw.file, **d} for d in dropped]
        (out_dir / (raw.file[:-len(".html")] + ".json")).write_text(
            json.dumps(page, indent=1, ensure_ascii=False), encoding="utf-8")
    return pages, dropped_all


# ─────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────

def _has_layer(page: dict, attr: str, layer: str) -> bool:
    a = page["attributes"].get(attr)
    return bool(a) and any(s["layer"] == layer for s in a["sources"])


def coverage_rows(site: str, pages: List[dict]) -> List[dict]:
    scored = [p for p in pages if not p["excluded_reason"]]
    n = len(scored)
    rows = []
    for attr in CANONICAL:
        rate = lambda pred: round(sum(1 for p in scored if pred(p)) / n, 3) if n else 0.0
        rows.append({
            "site": site, "pages_scored": n, "attribute": attr,
            "stated_rate": rate(lambda p: attr in p["attributes"]),
            "l1_rate": rate(lambda p: _has_layer(p, attr, "L1_schema")),
            "l2_rate": rate(lambda p: _has_layer(p, attr, "L2_block")),
            "l3_rate": rate(lambda p: _has_layer(p, attr, "L3_lines")),
        })
    return rows


def _layer_attrs(page: dict, layer: str) -> int:
    """Distinct canonical spec attributes found in a layer (not raw pairs)."""
    return sum(a in SPEC_ATTRS and any(s["layer"] == layer for s in v["sources"])
               for a, v in page["attributes"].items())


def _median(xs: Iterable[float]) -> float:
    xs = list(xs)
    return statistics.median(xs) if xs else 0


def summary_row(site: str, pages: List[dict]) -> dict:
    scored = [p for p in pages if not p["excluded_reason"]]
    other = Counter(k for p in scored for k in p["other_labels"])
    return {
        "site": site, "pages_scored": len(scored), "excluded": len(pages) - len(scored),
        "median_attributes": _median(sum(a in SPEC_ATTRS for a in p["attributes"]) for p in scored),
        "median_L1": _median(_layer_attrs(p, "L1_schema") for p in scored),
        "median_L2": _median(_layer_attrs(p, "L2_block") for p in scored),
        "median_L3": _median(_layer_attrs(p, "L3_lines") for p in scored),
        "top_other_labels": "; ".join(f"{k} ({c})" for k, c in other.most_common(5)),
        "eu_contact_rate": round(sum(COMPLIANCE in p["attributes"] for p in scored) / len(scored), 3)
                           if scored else 0.0,
    }


def checks_row(site: str, pages: List[dict], dropped: List[dict]) -> dict:
    """Per-site counts for the validity, identical-identifier, platform-default and GPSR checks."""
    n = len(pages)
    mine = [d for d in dropped if d["site"] == site]
    ids = Counter(k for p in pages for k in p["ids_identical"])
    defaults = Counter(d["attr"] for d in mine if d["rule"] == "repeated_default")
    scored = [p for p in pages if not p["excluded_reason"]]
    return {
        "site": site, "pages": n,
        "gtin_pages": sum("gtin" in p["attributes"] for p in scored),
        "gtin_failed_check_digit": sum(d["rule"] == "invalid_gtin" for d in mine),
        "gtin=mpn=sku": ids["gtin=mpn=sku"], "gtin=mpn": ids["gtin=mpn"],
        "gtin=sku": ids["gtin=sku"], "mpn=sku": ids["mpn=sku"],
        "defaults_removed": sum(defaults.values()),
        "defaults_by_attr": "; ".join(f"{a} {c}" for a, c in defaults.most_common()),
        "gpsr_terms_raw_html": sum(p["gpsr"]["raw_html"] for p in pages),
        "gpsr_terms_visible_text": sum(p["gpsr"]["text"] for p in pages),
        "eu_contact_stated": sum(p["gpsr"]["state"] == "contact" for p in pages),
        "eu_contact_unavailable": sum(p["gpsr"]["state"] == "unavailable" for p in pages),
        "manufacturer_block": sum(p["gpsr"]["manufacturer_block"] for p in pages),
    }


def write_csv(path: Path, rows: List[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ─────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────

HEADLINE = {"material": ["material"], "dimensions": ["width", "height", "depth", "dimensions"],
            "colour": ["colour"], "finish": ["finish"], "weight": ["weight"],
            "gtin": ["gtin"], "mpn": ["mpn"], "sku": ["sku"], "brand": ["brand"], BRAND_IS_RETAILER_NAME: [BRAND_IS_RETAILER_NAME],
            "opening": ["opening"]}


def print_table(rows: List[dict], cols: List[str]) -> None:
    w = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w[c]) for c in cols))


def print_report(results: Dict[str, List[dict]], summaries: List[dict], dropped: List[dict],
                 checks: List[dict]) -> None:
    print("\n## Per site")
    print_table(summaries, ["site", "pages_scored", "excluded", "median_attributes",
                            "median_L1", "median_L2", "median_L3"])
    ranked = sorted((s for s in summaries if s["pages_scored"]), key=lambda s: s["median_attributes"])
    print("\nStrongest:", ", ".join(f"{s['site']} ({s['median_attributes']})" for s in ranked[-3:][::-1]))
    print("Weakest:  ", ", ".join(f"{s['site']} ({s['median_attributes']})" for s in ranked[:3]))

    print("\n## Stated vs structured (mean across sites with scored pages)")
    for name, attrs in HEADLINE.items():
        stated, l1 = [], []
        for pages in results.values():
            scored = [p for p in pages if not p["excluded_reason"]]
            if not scored:
                continue
            stated.append(sum(any(a in p["attributes"] for a in attrs) for p in scored) / len(scored))
            l1.append(sum(any(_has_layer(p, a, "L1_schema") for a in attrs) for p in scored) / len(scored))
        print(f"  {name:11} stated {statistics.mean(stated):.2f}   structured (L1) {statistics.mean(l1):.2f}")

    print("\n## GTIN validity (8/12/13/14 digits + valid check digit)")
    gt = Counter(d["site"] for d in dropped if d["rule"] == "invalid_gtin")
    for site, pages in results.items():
        n = sum(1 for p in pages if not p["excluded_reason"])
        ok = sum("gtin" in p["attributes"] for p in pages if not p["excluded_reason"])
        if ok or gt[site]:
            print(f"  {site:30} valid gtin on {ok}/{n} pages; {gt[site]} invalid values dropped")

    print("\n## Pages where schema.org identifiers carry the same value (before any filter; pages with both stated)")
    kinds = ("gtin=mpn=sku", "gtin=mpn", "gtin=sku", "mpn=sku")
    print(f"  {'site':30} {'pages':>5}  " + "  ".join(f"{k:>12}" for k in kinds))
    for site, pages in results.items():
        c = Counter(k for p in pages for k in p["ids_identical"])
        if c:
            print(f"  {site:30} {len(pages):>5}  " + "  ".join(f"{c[k]:>12}" for k in kinds))

    print(f"\n## Schema.org platform defaults removed (identical on >= {SCHEMA_DEFAULT_SHARE:.0%} of a site's "
          f"pages; weight/dimensions at 40%)")
    by_attr: Counter = Counter()
    sites_by_attr: dict = {}
    for d in dropped:
        if d["rule"] == "repeated_default":
            by_attr[d["attr"]] += 1
            sites_by_attr.setdefault(d["attr"], Counter())[d["site"].split("_")[0]] += 1
    for attr, n in by_attr.most_common():
        print(f"  {attr:14} {n:4} values  " + ", ".join(f"{s} {c}" for s, c in sites_by_attr[attr].most_common(6)))
    if not by_attr:
        print("  none")

    print("\n## Checks per site: GTIN check digit, identical identifiers, platform defaults")
    cols = ["site", "pages", "gtin_pages", "gtin_failed_check_digit", "gtin=mpn=sku", "gtin=mpn", "gtin=sku",
            "mpn=sku", "defaults_removed", "defaults_by_attr"]
    print_table(checks, cols)

    print("\n## GPSR text search (hits/pages) and EU contact (reported apart from specs)")
    print(f"  {'site':30} {'raw html':>9} {'visible':>8} {'contact':>8} {'unavail.':>8} {'mfr block':>9}")
    for c in checks:
        n = c["pages"]
        print(f"  {c['site']:30} {c['gpsr_terms_raw_html']:>5}/{n:<3} {c['gpsr_terms_visible_text']:>4}/{n:<3} "
              f"{c['eu_contact_stated']:>4}/{n:<3} {c['eu_contact_unavailable']:>4}/{n:<3} {c['manufacturer_block']:>5}/{n:<3}")

    print("\n## Misspelled labels matched by one-letter edit distance")
    typos = Counter((site, lab, syn) for site, pages in results.items() for p in pages
                    for lab, syn in p.get("typo_labels", {}).items())
    for (site, lab, syn), c in typos.most_common():
        print(f"  {site:30} {lab!r} -> {syn}  ({c} pages)")
    if not typos:
        print("  none")

    other = Counter(k.lower() for pages in results.values() for p in pages
                    if not p["excluded_reason"] for k in p["other_labels"])
    print("\n## Top 15 other: labels")
    for k, c in other.most_common(15):
        print(f"  {c:4}  {k}")

    print("\n## Top 10 dropped labels")
    drops = Counter((d["label"].lower(), d["rule"]) for d in dropped)
    for (k, rule), c in drops.most_common(10):
        print(f"  {c:4}  {k}  [{rule}]")

    print("\n## Main-area text per site (pages with > 500 chars)")
    for site, pages in results.items():
        methods = Counter(p["main_area_method"] for p in pages)
        print(f"  {site:30} {sum(p['main_area_chars'] > 500 for p in pages):3}/{len(pages):<3} "
              f"{dict(methods)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_batch_args(ap)
    ap.add_argument("--only", default="", help="comma-separated site-dir prefixes")
    ap.add_argument("--report", action="store_true", help="print the report tables")
    args = ap.parse_args()

    batch = resolve_batch(args).ensure()
    out = batch.root
    only = [s.strip().lower() for s in args.only.split(",") if s.strip()]
    sites = [d for d in sorted((out / "sites").iterdir())
             if d.is_dir() and (d / "products").is_dir() and any((d / "products").glob("*.html"))
             and (not only or any(d.name.lower().startswith(o) for o in only))]

    results: Dict[str, List[dict]] = {}
    coverage, summaries, dropped, checks = [], [], [], []
    for d in sites:
        pages, drops = run_site(d)
        results[d.name] = pages
        coverage += coverage_rows(d.name, pages)
        summaries.append(summary_row(d.name, pages))
        dropped += drops
        checks.append(checks_row(d.name, pages, drops))
        s = summaries[-1]
        print(f"{d.name:30} pages={len(pages):3} scored={s['pages_scored']:3} "
              f"median_attrs={s['median_attributes']}", flush=True)

    if not sites:
        raise SystemExit(f"no sites with saved product pages under {out / 'sites'}")
    write_csv(batch.tables / "spec_coverage.csv", coverage)
    write_csv(batch.tables / "spec_summary.csv", summaries)
    write_csv(batch.tables / "spec_checks.csv", checks)
    with open(batch.tables / "spec_dropped.jsonl", "w", encoding="utf-8") as f:
        for d in dropped:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"\nWrote spec_coverage.csv, spec_summary.csv, spec_checks.csv, spec_dropped.jsonl in {batch.tables}")
    if args.report:
        print_report(results, summaries, dropped, checks)


if __name__ == "__main__":
    main()
