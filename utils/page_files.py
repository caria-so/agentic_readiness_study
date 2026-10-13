"""Saved product pages: file naming (shared with b_product_sample.py) and lookup."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator, Tuple
from urllib.parse import urlparse


def page_file_name(url: str) -> str:
    """Stem under <site_dir>/products/ for a page URL (pages are saved as <stem>.html)."""
    return re.sub(r"[^\w.\-]", "_", urlparse(url).path.strip("/"))[-120:] or "root"


def iter_saved_pages(site_dir: Path) -> Iterator[Tuple[Path, str]]:
    """(html_path, url) for every saved product page; url is "" when it can't be recovered."""
    pdir = site_dir / "products"
    if not pdir.is_dir():
        return
    urls = {}
    pj = site_dir / "product_audit.json"
    if pj.exists():
        for p in json.loads(pj.read_text(encoding="utf-8")).get("products", []):
            if p.get("url"):
                urls[page_file_name(p["url"])] = p["url"]
    for f in sorted(pdir.glob("*.html")):
        yield f, urls.get(f.name[:-len(".html")], "")
