#!/usr/bin/env python3
"""
Smoke test the saved product pages of every site before building the spec extractor.
Reads <batch>/sites/*/products/ only — fetches nothing.

  python smoke_specs.py --industry bathroom --lang ita      # latest batch
  python smoke_specs.py --batch results/bathroom/ita/20261003_merged
Writes <batch>/tables/smoke_specs.csv and prints a table + top labels per site.
"""
import csv
import json
import re
import statistics
import argparse
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

from utils.batch import add_batch_args, resolve_batch

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

ROOT = OUT = None            # the batch's sites/ folder and output file, set in main()

WALL_RE = re.compile(r"captcha-delivery|cf-chl|challenges\.cloudflare|just a moment\.\.\.|"
                     r"please enable js|px-captcha|access denied", re.I)
GIFT_RE = re.compile(r"gift.?card|buono regalo|carta regalo", re.I)
KW_RE = re.compile(r"scheda tecnica|caratteristiche|specifiche|dettagli prodotto|"
                   r"materiale|dimensioni|misure|finitura|peso|colore", re.I)
NOISE_RE = re.compile(r"(^|[-_ ])(nav|navbar|menu|megamenu|header|footer|breadcrumbs?|cookie|"
                      r"newsletter|filters?|facets?|related|upsell|crossell|cross-sell|"
                      r"carousel|reviews?|recensioni|minicart|search)([-_ ]|$)", re.I)
KV_RE = re.compile(r"^([A-ZÀ-Ý][^:\n]{1,39}?)\s*:\s*(\S.{0,120})$")
ATTR_KEYS_RE = re.compile(r'"(attributes?|specifications?|features|properties|'
                          r'technical(?:_?data)?|datasheet|caratteristiche)"\s*:', re.I)
STRIP_TAGS = ["script", "style", "noscript", "svg", "header", "nav", "footer", "aside", "form", "iframe"]
BLOCK_TAGS = ["p", "li", "div", "br", "tr", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "section"]


def clean(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def analyse(html: str) -> dict:
    out = {"wall": bool(WALL_RE.search(html[:20000])), "h1": "", "pairs": [], "kv": [],
           "text_len": 0, "kw": False}
    soup = BeautifulSoup(html, PARSER)
    h1 = soup.find("h1")
    out["h1"] = clean(h1) if h1 else ""

    for t in soup(STRIP_TAGS):
        t.decompose()
    noisy = [el for el in soup.find_all(True)
             if NOISE_RE.search(" ".join(el.get("class") or []) + " " + (el.get("id") or ""))]
    for el in noisy:
        if el.find("h1"):          # never remove the block holding the product title
            continue
        try:
            el.decompose()
        except Exception:
            pass

    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                out["pairs"].append((clean(dt), clean(dd)))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            out["pairs"].append((clean(cells[0]), clean(cells[1])))

    for el in soup.find_all(BLOCK_TAGS):
        el.insert_before("\n")
    text = soup.get_text(" ")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    for ln in lines:
        m = KV_RE.match(ln)
        if m:
            out["kv"].append((m.group(1).strip(), m.group(2).strip()))
    body = " ".join(lines)
    out["text_len"] = len(body)
    out["kw"] = bool(KW_RE.search(body))
    return out


def xhr_attr_hits(path: Path) -> Counter:
    hits: Counter = Counter()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return hits
    for r in data if isinstance(data, list) else [data]:
        if not isinstance(r, dict):
            continue
        body = r.get("body")
        s = body if isinstance(body, str) else json.dumps(body or "", ensure_ascii=False)
        if ATTR_KEYS_RE.search(s or ""):
            hits[re.sub(r"\?.*", "", str(r.get("url", "")))] += 1
    return hits


def verdict(row: dict) -> str:
    n = row["pages"]
    if n == 0:
        return "NO_PAGES"
    if row["walled"] / n >= 0.3:
        return "WALLED"
    if row["median_text"] < 500:
        return "EMPTY_SHELL"
    if row["with_h1"] / n < 0.5:
        return "CHECK_H1"
    if row["median_specs"] == 0:
        return "PROSE_ONLY" if row["kw_pages"] / n >= 0.5 else "NO_SPECS"
    return "OK"


def main():
    global ROOT, OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_batch_args(ap)
    batch = resolve_batch(ap.parse_args()).ensure()
    ROOT, OUT = batch.sites, batch.tables / "smoke_specs.csv"
    rows, samples = [], {}
    for site_dir in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        pdir = site_dir / "products"
        if not pdir.is_dir():
            continue
        pages = sorted(p for p in pdir.iterdir()
                       if p.suffix in (".html", ".htm") and not p.name.endswith("_xhr.json"))
        per_page, xhr = [], Counter()
        for p in pages:
            html = p.read_text(encoding="utf-8", errors="replace")
            a = analyse(html)
            a["name"] = p.name
            a["gift"] = bool(GIFT_RE.search(a["h1"] or p.name))
            per_page.append(a)
            xp = pdir / (p.name[:-len(".html")] + "_xhr.json") if p.name.endswith(".html.html") \
                else pdir / (p.stem + "_xhr.json")
            if xp.exists():
                xhr.update(xhr_attr_hits(xp))

        n = len(per_page)
        # pairs that repeat on >=80% of pages with the same value are template, not specs
        freq = Counter()
        for a in per_page:
            freq.update({(k.lower(), v.lower()) for k, v in a["pairs"] + a["kv"]})
        template = {kv for kv, c in freq.items() if n >= 5 and c / n >= 0.8}

        spec_counts, pair_counts, kv_counts, labels = [], [], [], Counter()
        for a in per_page:
            pr = [x for x in a["pairs"] if (x[0].lower(), x[1].lower()) not in template]
            kv = [x for x in a["kv"] if (x[0].lower(), x[1].lower()) not in template]
            pair_counts.append(len(pr))
            kv_counts.append(len(kv))
            spec_counts.append(len(pr) + len(kv))
            labels.update(k for k, _ in pr + kv)

        med = lambda xs: statistics.median(xs) if xs else 0
        row = {
            "site": site_dir.name,
            "pages": n,
            "walled": sum(a["wall"] for a in per_page),
            "with_h1": sum(bool(a["h1"]) for a in per_page),
            "gift_cards": sum(a["gift"] for a in per_page),
            "median_text": int(med([a["text_len"] for a in per_page])),
            "pages_with_pairs": sum(c > 0 for c in pair_counts),
            "median_pairs": med(pair_counts),
            "pages_with_kv": sum(c > 0 for c in kv_counts),
            "median_kv": med(kv_counts),
            "median_specs": med(spec_counts),
            "template_pairs": len(template),
            "kw_pages": sum(a["kw"] for a in per_page),
            "xhr_attr_endpoints": len(xhr),
            "xhr_top": xhr.most_common(1)[0][0] if xhr else "",
        }
        row["verdict"] = verdict(row)
        rows.append(row)
        samples[site_dir.name] = labels.most_common(8)

    if not rows:
        sys.exit(f"no site dirs with products/ under {ROOT}")

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    cols = ["site", "pages", "walled", "with_h1", "gift_cards", "median_text",
            "pages_with_pairs", "median_pairs", "pages_with_kv", "median_kv",
            "template_pairs", "kw_pages", "xhr_attr_endpoints", "verdict"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))

    print("\nTop spec labels per site (template pairs removed):")
    for site, labs in samples.items():
        print(f"  {site}: " + ", ".join(f"{k}({c})" for k, c in labs))
    xhr_sites = [r for r in rows if r["xhr_attr_endpoints"]]
    if xhr_sites:
        print("\nBackground responses with attribute-like keys:")
        for r in xhr_sites:
            print(f"  {r['site']}: {r['xhr_attr_endpoints']} endpoint(s), top {r['xhr_top']}")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()