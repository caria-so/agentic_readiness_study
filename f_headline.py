#!/usr/bin/env python3
"""
Headline results table, generated from the coverage files (nothing is computed by hand).

For each attribute, the share of a retailer's pages is taken first, then averaged over
retailers (every retailer weighs the same, whatever its page count):

  stated            found in any layer, L1-L4        tables/spec_coverage_l4.csv  stated_any_rate
  found by rules    L1-L3, no LLM                    tables/spec_coverage_l4.csv  l13_rate
  machine-readable  L1 (schema.org) only             tables/spec_coverage.csv     l1_rate

"Any dimension" takes, per retailer, the highest of width, height, depth, length and overall
dimensions: a lower bound. series and brand identify a product more than they describe it and
are listed apart from the headline. Retailers with fewer than MIN_PAGES scored pages are left out.

  python f_headline.py --industry bathroom --lang ita          # latest batch
  python f_headline.py --batch results/bathroom/ita/20261003_merged --min-pages 10

Writes <batch>/tables/headline_results.md and .csv (machine output, rewritten on every run).
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict

from utils.batch import add_batch_args, resolve_batch

DIMS = ["width", "height", "depth", "length", "dimensions"]
HEADLINE = [("Material", ["material"]), ("Colour", ["colour"]), ("Any dimension", DIMS)]
OTHER = [("Finish", ["finish"]), ("Weight", ["weight"]), ("Installation", ["installation"]),
         ("Opening", ["opening"]), ("Warranty", ["warranty"]), ("Certification", ["certification"]),
         ("Origin", ["origin"]), ("Shape / style", ["shape_style"]), ("Series", ["series"]),
         ("Brand", ["brand"])]
MAX_DIFF = 0.01          # tolerated gap between the two files' rules-based share


def load(path: str) -> dict:
    """{(site, attribute): row}, plus the pages count per site."""
    out, pages = {}, {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[(r["site"], r["attribute"])] = r
            pages[r["site"]] = int(r.get("pages") or r.get("pages_scored") or 0)
    return out, pages


def site_value(rows: dict, site: str, attrs: list[str], col: str) -> float:
    """Per-retailer share; for several attributes the highest one (a lower bound)."""
    return max(float(rows[(site, a)][col]) for a in attrs if (site, a) in rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_batch_args(ap)
    ap.add_argument("--min-pages", type=int, default=10)
    args = ap.parse_args()
    batch = resolve_batch(args).ensure()

    l4, l4_pages = load(batch.tables / "spec_coverage_l4.csv")
    l1, l1_pages = load(batch.tables / "spec_coverage.csv")
    both = sorted(set(l4_pages) & set(l1_pages))
    kept = [s for s in both if min(l4_pages[s], l1_pages[s]) >= args.min_pages]
    left_out = [f"{s} ({min(l4_pages[s], l1_pages[s])} pages)" for s in both if s not in kept]
    if not kept:
        sys.exit("no retailer with enough pages in both files")

    # the two files must agree on what the rules found, or the table mixes two different runs
    worst = (0.0, "")
    for s in kept:
        for a in {a for _, a in l4 if _ == s} & {a for _, a in l1 if _ == s}:
            d = abs(float(l4[(s, a)]["l13_rate"]) - float(l1[(s, a)]["stated_rate"]))
            if d > worst[0]:
                worst = (d, f"{s} {a}")
    warn = worst[0] > MAX_DIFF

    rows = []
    for group, items in (("headline", HEADLINE), ("other", OTHER)):
        for name, attrs in items:
            rows.append({
                "group": group, "attribute": name, "retailers": len(kept),
                "stated": statistics.mean(site_value(l4, s, attrs, "stated_any_rate") for s in kept),
                "found_by_rules": statistics.mean(site_value(l4, s, attrs, "l13_rate") for s in kept),
                "machine_readable": statistics.mean(site_value(l1, s, attrs, "l1_rate") for s in kept),
            })

    pct = lambda x: f"{x * 100:.1f}%"
    lines = [f"Averaged over {len(kept)} retailers (each weighs the same).",
             f"Left out: {', '.join(left_out) or 'none'}.", ""]
    for group, title in (("headline", ""), ("other", "Other attributes (not in the headline):")):
        if title:
            lines += ["", title, ""]
        lines += ["| Attribute | Stated (L1–L4) | Found by rules (L1–L3) | Machine-readable (L1) |",
                  "| --- | --- | --- | --- |"]
        lines += [f"| {r['attribute']} | {pct(r['stated'])} | {pct(r['found_by_rules'])} | "
                  f"{pct(r['machine_readable'])} |" for r in rows if r["group"] == group]
    lines += ["", "\"Any dimension\" is the highest of width, height, depth, length and dimensions per "
              "retailer (a lower bound)."]
    if warn:
        lines += ["", f"WARNING: the two files disagree on the rules-based share by up to {worst[0]:.3f} "
                      f"({worst[1]}); rerun spec_extract / d_llm_prose so both come from the same run."]
    text = "\n".join(lines)
    print(text)
    open(batch.tables / "headline_results.md", "w", encoding="utf-8").write(text + "\n")
    with open(batch.tables / "headline_results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows({**r, **{k: round(r[k], 4) for k in ("stated", "found_by_rules", "machine_readable")}}
                    for r in rows)
    print(f"\nWrote headline_results.md, headline_results.csv in {batch.tables}")


if __name__ == "__main__":
    main()
