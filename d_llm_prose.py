#!/usr/bin/env python3
"""
L4 prose extraction with a local LLM (Ollama).

For every scored product page it asks the model ONLY for the canonical attributes
that layers 1-3 did not already find, reading the product title + main-area text.
A value is kept only if the model's evidence phrase appears verbatim in that text
and contains the value. No inference, no external knowledge.

Inputs  (per site):  <site>/specs/*.json      (from c_spec_extract.py)
                     <site>/products/*.html*  (saved pages)
Outputs (per site):  <site>/specs_llm/*.json
Global:              <batch>/review/llm_review_<stamp>.md  (hand-check sheet, never overwritten)
                     <batch>/tables/spec_coverage_l4.csv   (L1-3 vs L4 vs any, per site x attribute)

Usage
  ollama pull qwen2.5:14b
  python d_llm_prose.py --industry bathroom --lang ita --model qwen2.5:32b           # latest batch, resumable
  python d_llm_prose.py --batch results/bathroom/ita/20261003_merged --pilot       # 2 pages x 6 sites, then stop
  python d_llm_prose.py --batch <folder> --sites boleco,jysk --per-site 5 --model qwen2.5:7b
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from utils.batch import add_batch_args, resolve_batch
from utils.safe_output import dated_path

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

# the exact product-area logic of layers 1-3 (no silent fallback: a broken import must fail loudly)
from utils.html_main import main_area, strip_chrome, strip_reviews

PILOT_SITES = ["Mondo_Convenienza", "Arcshop", "Iperceramica", "Boleco", "Jysk", "Leroy_Merlin"]
RESULT_VERSION = 3   # bump when the prompt or validation changes; older outputs are redone
MAX_CHARS = 9000  # main-area text sent to the model (title is sent separately)
BLOCKS = ["p", "li", "div", "br", "tr", "td", "th", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "section"]

ATTRS = {
    "material":       "what the product (or its main parts) is made of, e.g. 'MDF', 'ceramica', 'vetro temperato', 'acciaio inox'",
    "finish":         "surface finish or effect, e.g. 'opaco', 'lucido', 'cromato', 'effetto legno'",
    "colour":         "colour of the product, e.g. 'bianco', 'nero opaco', 'rovere naturale'",
    "width":          "width / larghezza with unit (shorthand L or L. = larghezza)",
    "height":         "height / altezza with unit (shorthand H or A = altezza)",
    "depth":          "depth / profondità with unit (shorthand P or P. = profondità)",
    "length":         "length / lunghezza with unit, ONLY if the text says 'lunghezza' (L. means larghezza, not lunghezza)",
    "dimensions":     "overall size as one expression, e.g. '80x110 cm', 'cm 30x75'",
    "thickness":      "thickness / spessore with unit, e.g. glass '6 mm'",
    "weight":         "weight of the product itself with unit, NEVER a package/shipping weight (imballo, imballato, confezione, collo)",
    "installation":   "mounting type only, e.g. 'sospeso', 'a terra', 'a muro', 'da incasso', 'da appoggio', 'su piatto doccia'; not assembly instructions",
    "opening":        "opening/closing system, e.g. 'scorrevole', 'battente', 'push-pull', 'soft close'",
    "plumbing":       "plumbing details, e.g. overflow hole, tap hole, centre distance (interasse), drain/siphon",
    "water":          "water-related performance, e.g. flow rate (portata), water saving, cartridge, number of jets",
    "electrical":     "electrical data, e.g. watt, lumen, kelvin, IP rating, voltage",
    "shape_style":    "shape or style, e.g. 'rettangolare', 'semicircolare', 'moderno'",
    "series":         "product line / collection / model name",
    "brand":          "manufacturer brand of THIS product (a company), not the product line/model name and not the shop",
    "warranty":       "manufacturer or commercial warranty for this product, NOT the statutory 'garanzia legale di conformità'",
    "certification":  "certifications / standards / classes, e.g. 'CE', 'EN 14428', 'R10', 'FSC'",
    "origin":         "country of manufacture",
    "care":           "care / cleaning / maintenance instructions",
}


# ───────────────────────── text helpers ─────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("’", "'").replace("×", "x")
    return re.sub(r"\s+", " ", s).strip(" .;:,")


def clean(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def page_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, PARSER)
    og = soup.find("meta", attrs={"property": "og:title"})
    og_title = (og.get("content") or "").strip() if og else ""
    title = og_title or clean(soup.find("h1")) or clean(soup.find("title"))
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    strip_chrome(soup)
    strip_reviews(soup)
    area, _method = main_area(soup)              # (roots, method)
    if not area:
        for t in soup(["header", "footer", "nav", "aside"]):
            t.decompose()
        area = soup.body or soup
    nodes = list(area) if isinstance(area, (list, tuple, set)) else [area]
    parts = []
    for n in nodes:
        for el in n.find_all(BLOCKS):
            el.insert_before("\n")
        parts.append(n.get_text(" "))
    lines, prev = [], None
    for ln in "\n".join(parts).splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln and ln != prev:
            lines.append(ln)
        prev = ln
    return title, "\n".join(lines)[:MAX_CHARS]


def find_html(products: Path, url: str, file: str = "") -> Path | None:
    if file and (products / file).is_file():          # spec JSON records the saved file name
        return products / file
    slug = urlparse(url).path.strip("/").replace("/", "_")
    if not slug or not products.is_dir():
        return None
    cands = [p for p in products.iterdir()
             if p.name.startswith(slug) and not p.name.endswith("_xhr.json")]
    return min(cands, key=lambda p: len(p.name)) if cands else None


# ───────────────────────── LLM call ─────────────────────────

SYSTEM = (
    "You extract product attributes from Italian e-commerce product pages. "
    "You only report what is explicitly written in the given text. "
    "You never infer from the product category, typical products, images or general knowledge."
)


def build_prompt(title: str, text: str, wanted: list[str], known: dict) -> str:
    defs = "\n".join(f"- {a}: {ATTRS[a]}" for a in wanted)
    known_str = "; ".join(f"{a} = {v}" for a, v in known.items() if v) or "none"
    return (
        f"PRODUCT TITLE:\n{title}\n\nPAGE TEXT:\n<<<\n{text}\n>>>\n\n"
        f"Already known about this product (context only): {known_str}\n\n"
        f"Extract these attributes:\n{defs}\n\n"
        "Rules:\n"
        "1. Use only information explicitly written in the title or page text about THIS product. "
        "Ignore related products, accessories, reviews, shipping and shop information.\n"
        "2. 'evidence' is copied VERBATIM from the title or page text (max 25 words).\n"
        "3. 'value' is copied exactly from inside 'evidence' (a substring of it), keeping language and units.\n"
        "4. If an attribute is not stated, set both value and evidence to null. Do not guess or infer.\n"
        "5. Italian size shorthand: L = larghezza (width), P = profondità (depth), H or A = altezza (height). "
        "For 'HxPxL 220x17,5x20,5' map each number to its letter in order.\n"
        "6. Each piece of information belongs to ONE attribute: colours go to colour, never to finish.\n"
    )


def schema_for(wanted: list[str]) -> dict:
    item = {"type": "object",
            "properties": {"value": {"type": ["string", "null"]},
                           "evidence": {"type": ["string", "null"]}},
            "required": ["value", "evidence"]}
    return {"type": "object", "properties": {a: item for a in wanted}, "required": wanted}


def call_ollama(client: httpx.Client, host: str, model: str, prompt: str,
                schema: dict, num_ctx: int) -> tuple[dict, dict]:
    r = client.post(f"{host}/api/chat", json={
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    })
    r.raise_for_status()
    data = r.json()
    stats = {"prompt_tokens": data.get("prompt_eval_count", 0),
             "output_tokens": data.get("eval_count", 0),
             "seconds": round(data.get("total_duration", 0) / 1e9, 2)}
    return json.loads(data["message"]["content"]), stats


SIZE_ATTRS = {"weight", "width", "height", "depth", "length", "dimensions", "thickness"}
PACKAGE_RE = re.compile(r"imball|confezion|pacco|pacchi|\bcoll[oi]\b|spedizion", re.I)
LEGAL_RE = re.compile(r"garanzia legale|legale di conformit|codice del consumo", re.I)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
ORIGIN_CUE_RE = re.compile(r"made in|(prodott|fabbricat|realizzat|costruit|fatt|assemblat|progettat)\w*\s+(e\s+\w+\s+)?(in|a)\b"
                           r"|origin|produzione|manufactur|paese di|prodotto da", re.I)
GENERIC_TOKENS = {"italia", "italy", "store", "shop", "online"}


def retailer_tokens(site_name: str) -> list[str]:
    base = re.sub(r"_[0-9a-f]{8}$", "", site_name)
    return [t for t in re.split(r"[_\W]+", base.lower()) if len(t) >= 2 and t not in GENERIC_TOKENS]


def validate(attr: str, item: dict, title: str, text: str,
             retailer: list[str]) -> tuple[dict | None, str]:
    val, ev = (item or {}).get("value"), (item or {}).get("evidence")
    if not val:
        return None, "not_stated"
    if not ev:
        return None, "no_evidence"
    n_ev, n_val = norm(ev), norm(val)
    if n_ev not in norm(title) and n_ev not in norm(text):
        return None, "evidence_not_in_text"
    nums = NUM_RE.findall(n_val)
    if attr in SIZE_ATTRS and nums:
        ev_nums = {x.replace(",", ".") for x in NUM_RE.findall(n_ev)}
        if not all(x.replace(",", ".") in ev_nums for x in nums):
            return None, "value_not_in_evidence"
    elif n_val not in n_ev:
        return None, "value_not_in_evidence"
    if attr in SIZE_ATTRS and PACKAGE_RE.search(ev):
        return None, "package_value"
    if attr == "origin" and not ORIGIN_CUE_RE.search(ev):     # an address or registered office is not an origin
        return None, "origin_not_stated"
    if attr == "warranty" and LEGAL_RE.search(ev):
        return None, "statutory_warranty"
    vw = set(re.findall(r"\w+", n_val))
    if attr == "brand" and retailer and (vw <= set(retailer) or set(retailer) <= vw):   # the shop's own name
        return None, "brand_is_retailer"
    loc = "title" if n_ev in norm(title) else "body"
    return {"value": val, "evidence": ev, "location": loc}, "ok"


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="L4 prose extraction via Ollama")
    add_batch_args(ap)
    ap.add_argument("--model", default="qwen2.5:14b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--sites", default="", help="comma-separated substrings of site dir names")
    ap.add_argument("--per-site", type=int, default=0, help="pages per site (0 = all)")
    ap.add_argument("--pilot", action="store_true", help="2 pages x 6 pilot sites")
    ap.add_argument("--force", action="store_true", help="redo pages that already have output")
    args = ap.parse_args()

    if args.pilot:
        args.sites, args.per_site = ",".join(PILOT_SITES), args.per_site or 2
    batch = resolve_batch(args).ensure()
    root = batch.sites
    want_sites = [s.strip().lower() for s in args.sites.split(",") if s.strip()]

    client = httpx.Client(timeout=600)
    try:
        tags = client.get(f"{args.host}/api/tags").json().get("models", [])
    except Exception as e:
        sys.exit(f"Ollama not reachable at {args.host}: {e}  (run: ollama serve)")
    if not any(m.get("name", "").startswith(args.model) for m in tags):
        sys.exit(f"model {args.model} not pulled — run: ollama pull {args.model}")

    # collect jobs
    jobs, eligible_total = [], 0
    for site in sorted(p for p in root.iterdir() if (p / "specs").is_dir()):
        specs = sorted((site / "specs").glob("*.json"))
        scored = []
        for sp in specs:
            try:
                d = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("excluded_reason"):
                continue
            scored.append((sp, d))
        eligible_total += len(scored)
        if want_sites and not any(w in site.name.lower() for w in want_sites):
            continue
        if args.per_site:
            scored = random.Random(site.name).sample(scored, min(args.per_site, len(scored)))
        jobs.extend((site, sp, d) for sp, d in scored)

    if not jobs:
        sys.exit("no pages selected — check the batch / --sites and that c_spec_extract.py has run")
    print(f"{len(jobs)} page(s) selected, model {args.model}")

    totals = defaultdict(float)
    reasons = defaultdict(int)
    review = []
    t0 = time.monotonic()
    for i, (site, sp, d) in enumerate(jobs, 1):
        out_dir = site / "specs_llm"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / sp.name
        res = json.loads(out.read_text(encoding="utf-8")) if out.exists() and not args.force else None
        if res is not None and (res.get("version") != RESULT_VERSION or res.get("model") != args.model):
            res = None
        if res is None:
            html_path = find_html(site / "products", d.get("url", ""), d.get("file", ""))
            if html_path is None:
                print(f"  [{i}/{len(jobs)}] {site.name}: no saved HTML for {d.get('url')} — skipped")
                reasons["no_html"] += 1
                continue
            title, text = page_text(html_path.read_text(encoding="utf-8", errors="replace"))
            attrs13 = d.get("attributes") or {}
            have = set(attrs13.keys())
            # ask for ALL attributes (so nothing gets pushed into the wrong field),
            # pass the known values as context, keep only the new ones
            wanted = list(ATTRS)
            known = {a: (v or {}).get("value") for a, v in attrs13.items() if a in ATTRS}
            found, dropped, stats = {}, {}, {"prompt_tokens": 0, "output_tokens": 0, "seconds": 0}
            try:
                raw, stats = call_ollama(client, args.host, args.model,
                                         build_prompt(title, text, wanted, known),
                                         schema_for(wanted), args.num_ctx)
            except Exception as e:
                print(f"  [{i}/{len(jobs)}] {site.name}: LLM error {type(e).__name__}: {e}")
                reasons["llm_error"] += 1
                continue
            rtok = retailer_tokens(site.name)
            for a in wanted:
                if a in have:
                    continue
                ok, why = validate(a, raw.get(a), title, text, rtok)
                if ok:
                    found[a] = ok
                elif why != "not_stated":
                    dropped[a] = {"reason": why, "raw": raw.get(a)}
            series = (found.get("series") or {}).get("value") or (attrs13.get("series") or {}).get("value") or ""
            if "brand" in found and series and norm(series) == norm(found["brand"]["value"]):
                dropped["brand"] = {"reason": "brand_is_series", "raw": found.pop("brand")}
            res = {"version": RESULT_VERSION, "url": d.get("url"), "model": args.model, "html": html_path.name,
                   "title": title, "text_chars": len(text), "requested": wanted,
                   "found": found, "dropped": dropped, "stats": stats}
            out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")

        for k in ("prompt_tokens", "output_tokens", "seconds"):
            totals[k] += res["stats"].get(k, 0)
        totals["pages"] += 1
        totals["found"] += len(res["found"])
        totals["dropped"] += len(res["dropped"])
        for v in res["dropped"].values():
            reasons[v["reason"]] += 1
        review.append((site.name, res, d))
        print(f"  [{i}/{len(jobs)}] {site.name}: +{len(res['found'])} found, "
              f"{len(res['dropped'])} dropped, {res['stats'].get('seconds', 0)}s")

    # ── hand-check sheet
    review_path = dated_path(batch.review / "llm_review.md", "%Y%m%d_%H%M%S")      # hand-check sheet: never overwritten
    with open(review_path, "w", encoding="utf-8") as f:
        f.write(f"# L4 review — model {args.model}\n\n")
        for site, res, d in review:
            f.write(f"## {site}\n{res['url']}\n\n**Title:** {res['title']}\n\n")
            f.write(f"Already found by L1-3: {', '.join(sorted((d.get('attributes') or {}).keys())) or '—'}\n\n")
            if res["found"]:
                f.write("| attribute | value | evidence | where |\n|---|---|---|---|\n")
                for a, v in res["found"].items():
                    ev = v["evidence"].replace("|", "/")
                    f.write(f"| {a} | {v['value']} | {ev} | {v['location']} |\n")
            else:
                f.write("_nothing new found_\n")
            if res["dropped"]:
                f.write("\nDropped: " + "; ".join(f"{a} ({v['reason']})" for a, v in res["dropped"].items()) + "\n")
            f.write("\n")

    # ── boilerplate: the same evidence on >=50% of a site's pages is template text, not a product fact
    site_pages, ev_count = defaultdict(int), defaultdict(int)
    for site, res, d in review:
        site_pages[site] += 1
        for a, v in res["found"].items():
            ev_count[(site, a, norm(v["evidence"]))] += 1
    boiler = {k for k, c in ev_count.items() if site_pages[k[0]] >= 5 and c / site_pages[k[0]] >= 0.5}
    if boiler:
        print(f"boilerplate evidence removed from coverage: {len(boiler)} (site, attribute) pattern(s)")

    # ── coverage: L1-3 vs L4 vs any, on the processed pages
    per_site = defaultdict(lambda: {"pages": 0, "l13": defaultdict(int), "l4": defaultdict(int)})
    for site, res, d in review:
        s = per_site[site]
        s["pages"] += 1
        have = set((d.get("attributes") or {}).keys())
        for a in ATTRS:
            if a in have:
                s["l13"][a] += 1
            elif a in res["found"] and (site, a, norm(res["found"][a]["evidence"])) not in boiler:
                s["l4"][a] += 1
    with open(batch.tables / "spec_coverage_l4.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["site", "pages", "attribute", "l13_rate", "l4_only_rate", "stated_any_rate"])
        for site, s in sorted(per_site.items()):
            n = s["pages"]
            for a in ATTRS:
                l13, l4 = s["l13"][a] / n, s["l4"][a] / n
                w.writerow([site, n, a, round(l13, 3), round(l4, 3), round(l13 + l4, 3)])

    # ── summary + estimate
    n = int(totals["pages"]) or 1
    checked = totals["found"] + totals["dropped"]
    avg_s = totals["seconds"] / n
    print("\n── summary")
    print(f"pages processed      {int(totals['pages'])}")
    print(f"values kept          {int(totals['found'])}")
    print(f"values dropped       {int(totals['dropped'])}"
          + (f"  ({totals['dropped'] / checked:.0%} of proposed)" if checked else ""))
    for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"   {r:<24}{c}")
    print(f"avg prompt tokens    {totals['prompt_tokens'] / n:.0f}")
    print(f"avg output tokens    {totals['output_tokens'] / n:.0f}")
    print(f"avg seconds / page   {avg_s:.1f}")
    print(f"eligible pages, all sites: {eligible_total} → est. full run "
          f"{eligible_total * avg_s / 3600:.1f} h at this speed")
    print(f"wall time this run   {time.monotonic() - t0:.0f}s")
    print(f"\nWrote {review_path}, {batch.tables / 'spec_coverage_l4.csv'}, <site>/specs_llm/*.json")


if __name__ == "__main__":
    main()