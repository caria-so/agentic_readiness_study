"""
Where things live. One definition of the layout, used by every stage.

  leads/{industry}/{language}.csv                    retailer list, e.g. leads/bathroom/ita.csv
  results/{industry}/{language}/{YYYYMMDD_HHMM}/     one folder per batch
      sites/<site>/        discovery, products/, specs/, specs_llm/  (all stages write here, resumable)
      results.csv, product_audit.csv, processed_urls.json           (discovery / sampler state)
      tables/              coverage, checks, headline, feed check ...
      review/              files a person marks: precision samples, llm_review (never overwritten)
      logs/                one log per stage
      batch.json           what was run, with what, when

A rerun or a resume goes into the same batch folder; a new batch is a new folder, so nothing
ever needs merging.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parents[1]
LEADS = PROJECT / "leads"
RESULTS = PROJECT / "results"

# language (3-letter code used in folder names) -> (iso-639-1, region, Accept-Language header)
LANGS = {
    "ita": ("it", "IT", "it-IT,it;q=0.9,en;q=0.5"),
    "deu": ("de", "DE", "de-DE,de;q=0.9,en;q=0.5"),
    "fra": ("fr", "FR", "fr-FR,fr;q=0.9,en;q=0.5"),
    "spa": ("es", "ES", "es-ES,es;q=0.9,en;q=0.5"),
    "eng": ("en", "GB", "en-GB,en;q=0.9"),
    "nld": ("nl", "NL", "nl-NL,nl;q=0.9,en;q=0.5"),
}


def lang_info(lang: str) -> tuple[str, str, str]:
    if lang not in LANGS:
        sys.exit(f"unknown language {lang!r}; known: {', '.join(LANGS)} (add it to utils/batch.py LANGS)")
    return LANGS[lang]


def leads_file(industry: str, lang: str) -> Path:
    return LEADS / industry / f"{lang}.csv"


@dataclass(frozen=True)
class Batch:
    root: Path

    @property
    def sites(self) -> Path:
        return self.root / "sites"

    @property
    def tables(self) -> Path:
        return self.root / "tables"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def manifest(self) -> Path:
        return self.root / "batch.json"

    def ensure(self) -> "Batch":
        for d in (self.root, self.sites, self.tables, self.review, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self


def batches(industry: str, lang: str) -> list[Path]:
    base = RESULTS / industry / lang
    return sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def new_batch(industry: str, lang: str, now: Optional[datetime] = None) -> Batch:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    root = RESULTS / industry / lang / stamp
    if root.exists():                                  # two batches in one minute: never reuse
        root = RESULTS / industry / lang / f"{stamp}{datetime.now().strftime('%S')}"
    return Batch(root).ensure()


def add_batch_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", help="batch folder, or a batch id such as 20261003_1200 "
                                    "(then --industry and --lang are needed); default: the latest")
    ap.add_argument("--industry", help="e.g. bathroom (with --lang, picks results/<industry>/<lang>/)")
    ap.add_argument("--lang", help="e.g. ita, deu")


def resolve_batch(args: argparse.Namespace) -> Batch:
    """The batch folder named by --batch (a path) or by --industry/--lang [--batch <id>]; latest by default."""
    given = getattr(args, "batch", None)
    if given and Path(given).is_dir():
        return Batch(Path(given).resolve())
    industry, lang = getattr(args, "industry", None), getattr(args, "lang", None)
    if not (industry and lang):
        sys.exit("name the batch: --batch <folder>, or --industry <x> --lang <y> [--batch <id>]")
    if given:
        root = RESULTS / industry / lang / given
        if not root.is_dir():
            sys.exit(f"no batch {root}")
        return Batch(root)
    found = batches(industry, lang)
    if not found:
        sys.exit(f"no batch under {RESULTS / industry / lang}; run run_batch.py first")
    return Batch(found[-1])
