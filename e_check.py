#!/usr/bin/env python3
"""
Two checks on the L4 (prose) values before they go into a report.

1. <batch>/review/l4_precision_sample_<stamp>.csv   random sample of kept L4 values to mark right/wrong
                                                    by hand (never overwritten)
2. <batch>/tables/l4_repeated_evidence.csv  evidence phrases that repeat across a site's pages
                             (>=20% of pages) — likely template text, not product facts

  python e_check.py --industry bathroom --lang ita            # latest batch, 50 values
  python e_check.py --batch results/bathroom/ita/20261003_merged --n 80
"""
import argparse
import csv
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from utils.batch import add_batch_args, resolve_batch
from utils.safe_output import dated_path

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
add_batch_args(ap)
ap.add_argument("--n", type=int, default=50, help="values in the precision sample")
ap.add_argument("--seed", type=int, default=43, help="sampling seed (42 drew the first sample of the Italian bathroom batch)")
ap.add_argument("--min-pages", type=int, default=10, help="sites with fewer pages with L4 output are skipped")
args = ap.parse_args()
BATCH = resolve_batch(args).ensure()
ROOT, N, SEED = BATCH.sites, args.n, args.seed
REPEAT_SHARE, REPEAT_MIN = 0.2, 3


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", s).strip(" .;:,")


rows, pages = [], Counter()
for site in sorted(p for p in ROOT.iterdir() if (p / "specs_llm").is_dir()):
    if len(list((site / "specs_llm").glob("*.json"))) < args.min_pages:
        continue                                         # too few pages to report
    for f in sorted((site / "specs_llm").glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        pages[site.name] += 1
        for attr, v in (r.get("found") or {}).items():
            rows.append({"site": site.name, "url": r.get("url", ""), "attribute": attr,
                         "value": v.get("value", ""), "evidence": v.get("evidence", ""),
                         "location": v.get("location", "")})

if not rows:
    sys.exit(f"no specs_llm output under {ROOT}")

# 1. precision sample: a person marks this file, so never overwrite one (stamped name, stop if it exists).
# A new sample is independent of the earlier ones: rows already in any l4_precision_sample*.csv are excluded.
SAMPLE_OUT = dated_path(BATCH.review / "l4_precision_sample.csv", "%Y%m%d_%H%M%S")
used = set()
for old in BATCH.review.glob("l4_precision_sample*.csv"):
    with open(old, newline="", encoding="utf-8") as fh:
        used |= {(r["site"], r["url"], r["attribute"]) for r in csv.DictReader(fh)}
pool = [r for r in rows if (r["site"], r["url"], r["attribute"]) not in used]
if len(pool) < N:
    sys.exit(f"only {len(pool)} unused L4 values left; lower N")
sample = random.Random(SEED).sample(pool, N)
sample.sort(key=lambda r: (r["site"], r["url"], r["attribute"]))
with open(SAMPLE_OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["site", "url", "attribute", "value", "evidence",
                                      "location", "correct", "note"])
    w.writeheader()
    for r in sample:
        w.writerow({**r, "correct": "", "note": ""})

# 2. repeated evidence per (site, attribute)
groups = defaultdict(Counter)
for r in rows:
    groups[(r["site"], r["attribute"])][norm(r["evidence"])] += 1
repeated = []
for (site, attr), counter in groups.items():
    for ev, c in counter.items():
        share = c / pages[site]
        if c >= REPEAT_MIN and share >= REPEAT_SHARE:
            repeated.append({"site": site, "attribute": attr, "pages_with_it": c,
                             "share": round(share, 2), "evidence": ev[:200]})
repeated.sort(key=lambda r: -r["share"])
with open(BATCH.tables / "l4_repeated_evidence.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["site", "attribute", "pages_with_it", "share", "evidence"])
    w.writeheader()
    w.writerows(repeated)

print(f"L4 values: {len(rows)} on {sum(pages.values())} pages, {len(pages)} sites")
print(f"precision sample: {len(sample)} values → {SAMPLE_OUT} (fill in 'correct' y/n); "
      f"{len(used)} earlier-sampled values excluded, seed {SEED}")
print(f"repeated evidence (>= {REPEAT_SHARE:.0%} of a site's pages): {len(repeated)} → {BATCH.tables / 'l4_repeated_evidence.csv'}")
for r in repeated[:15]:
    print(f"  {r['share']:.0%}  {r['site']:<30} {r['attribute']:<14} {r['evidence'][:90]}")