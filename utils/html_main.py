"""
Main product area of a saved product page, and its text as lines.

How the product area is found:
- Page chrome and non-content are removed by tag or role only (script, style, svg ... and
  header, footer, nav, aside, role=navigation/banner/contentinfo). A class-name filter for
  noise ("header", "search") once emptied whole Shopify pages, so none is used.
- The product area itself IS chosen by class/id: elements whose class or id matches
  CANDIDATE_RE (description, product-info, specifiche, caratteristiche ...), with the
  ancestor of the page title as the fallback (see main_area).
- Two more class/id rules refine the text: review blocks are dropped (strip_reviews, unless
  they hold the title or a spec container), and package sections are tagged "confezione"
  (PACKAGE_HOOK_RE) so a box measurement is not read as the product's size.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template"]
CHROME_TAGS = ["header", "footer", "nav", "aside"]
CHROME_ROLES = {"navigation", "banner", "contentinfo"}

CANDIDATE_RE = re.compile(
    r"description|product[-_]info|product[-_]detail|product__|product[-_]main|scheda|caratteristic|"
    r"specific|features|technical|attributes|data-sheet|tab-content", re.I)
MIN_MAIN_CHARS = 500
BLOCK_TAGS = ["p", "li", "div", "dt", "dd", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "ul", "ol", "dl", "table", "article"]
TITLE_SHARE = 0.6


def clean(el) -> str:
    text = el if isinstance(el, str) else el.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, PARSER)


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 and clean(h1):
        return clean(h1)
    og = soup.find("meta", attrs={"property": "og:title"})
    return clean(og.get("content") or "") if og else ""


def _title_heading(soup: BeautifulSoup) -> Tag | None:
    h1 = soup.find("h1")
    if h1 and clean(h1):
        return h1
    og = soup.find("meta", attrs={"property": "og:title"})
    want = clean(og.get("content") or "").lower() if og else ""
    if not want:
        return None
    for h in soup.find_all(["h2", "h3", "h4"]):
        t = clean(h).lower()
        if t and (t in want or want in t):
            return h
    return None


def full_text(soup: BeautifulSoup) -> str:
    """Whole-page text, one text node per line (call before strip_chrome)."""
    for t in soup(DROP_TAGS):
        t.decompose()
    return soup.get_text("\n")


def strip_chrome(soup: BeautifulSoup) -> None:
    """Drop non-content elements and the page chrome (header, footer, nav, aside)."""
    for t in soup(DROP_TAGS):
        t.decompose()
    for t in soup(CHROME_TAGS):
        t.decompose()
    for t in soup.find_all(attrs={"role": True}):
        if not t.decomposed and str(t.get("role", "")).lower() in CHROME_ROLES:
            t.decompose()


def _hook(el: Tag) -> str:
    cls = el.get("class") or []
    return " ".join(cls if isinstance(cls, list) else [cls]) + " " + str(el.get("id") or "")


REVIEW_RE = re.compile(r"review|recension|rating|testimonial", re.I)
SPEC_HOOK_RE = re.compile(r"specific|caratteristic|data-sheet|technical|properties|attributes|scheda", re.I)


def strip_reviews(soup: BeautifulSoup) -> None:
    """Drop review/rating blocks (class or id), except one that holds the product title or a
    spec container (CMS blocks such as "product-description-reviews" wrap both). Review-classed
    elements nested inside a kept block are still dropped on their own."""
    for el in soup.find_all(True):
        if el.decomposed or el.name in ("html", "body"):
            continue
        if REVIEW_RE.search(_hook(el)) and el.find("h1") is None and not any(
                SPEC_HOOK_RE.search(_hook(d)) and not REVIEW_RE.search(_hook(d))
                for d in el.find_all(True)):
            el.decompose()


def _outermost(tags: List[Tag]) -> List[Tag]:
    chosen = set(map(id, tags))
    return [t for t in tags if not any(id(p) in chosen for p in t.parents)]


def title_ancestor(soup: BeautifulSoup) -> Tag | None:
    """Smallest ancestor of the product title holding >= 60% of the visible text."""
    body = soup.body or soup
    total = len(clean(body))
    h = _title_heading(soup)
    if h is None or not total:
        return None
    for anc in h.parents:
        if anc.name in ("body", "html", "[document]"):
            break
        if len(clean(anc)) >= TITLE_SHARE * total:
            return anc
    return None


def main_area(soup: BeautifulSoup) -> Tuple[List[Tag], str]:
    """(roots, method). Call strip_chrome first.

    method: "candidates"; "title_ancestor" when nothing matches; "candidates+title_ancestor"
    when the candidates hold under MIN_MAIN_CHARS (screen-reader or unit-label spans).
    """
    body = soup.body or soup
    cands = [el for el in body.find_all(True)
             if el.name not in ("body", "html") and CANDIDATE_RE.search(_hook(el))]
    cands = [c for c in _outermost(cands) if clean(c)]
    if cands and sum(len(clean(c)) for c in cands) >= MIN_MAIN_CHARS:
        return cands, "candidates"
    anc = title_ancestor(soup)
    if anc is not None:
        if cands:
            return _outermost(cands + [anc]), "candidates+title_ancestor"
        return [anc], "title_ancestor"
    return (cands, "candidates") if cands else ([body], "body")


SECTION_HEADINGS = ["h2", "h3", "h4", "h5", "h6"]
_HEADING_MARK = "\x01"
_PKG_START, _PKG_END = "\x02", "\x03"


PACKAGE_HOOK_RE = re.compile(r"package|packag|pacc|imballo|confezione|(^|[-_ ])colli?($|[-_ ])", re.I)
PACKAGE_TAG = "confezione"      # appended to the heading text when the element sits in a package block


def in_package_block(el: Tag) -> bool:
    return any(PACKAGE_HOOK_RE.search(_hook(a)) for a in [el, *el.parents] if isinstance(a, Tag))


def sections_under(soup: BeautifulSoup, heading_re: "re.Pattern", max_heading: int = 80,
                   max_chars: int = 600) -> List[Tuple[str, str]]:
    """(heading text, text that follows it up to the next h1-h6 or max_chars) for every short
    element whose own text matches heading_re. Works on the whole page, so call it before
    strip_chrome when footer-ish or accordion sections matter."""
    out: List[Tuple[str, str]] = []
    seen: set = set()
    for el in soup.find_all(True):
        if el.name in {"html", "body", "script", "style"}:
            continue
        head = clean(el)
        if not head or len(head) > max_heading or not heading_re.search(head):
            continue
        parts: List[str] = []
        size = 0
        for nxt in el.next_elements:
            if isinstance(nxt, Tag):
                if nxt.name in SECTION_HEADINGS and el not in nxt.parents and nxt is not el:
                    break
                continue
            if el in nxt.parents:                 # the heading's own text
                continue
            t = re.sub(r"\s+", " ", str(nxt)).strip()
            if t:
                parts.append(t)
                size += len(t) + 1
            if size >= max_chars:
                break
        body = " ".join(parts)[:max_chars]
        key = (head.lower(), body[:60])
        if key not in seen:
            seen.add(key)
            out.append((head, body))
    return out


def section_heading(el: Tag) -> str:
    """Text of the nearest section heading (h2-h6) before el, plus "confezione" when el or an
    ancestor has a package-like class/id (a label under a heading like "1 × NAME" says nothing)."""
    h = el.find_previous(SECTION_HEADINGS)
    text = clean(h) if h is not None else ""
    return f"{text} | {PACKAGE_TAG}" if in_package_block(el) else text


def section_lines(roots: List[Tag]) -> List[Tuple[str, str]]:
    """(line, heading above it): visible text split at block boundaries, inline elements
    on one line. Mutates the roots (inserts line breaks), so read element structure first.
    """
    out: List[Tuple[str, str]] = []
    for root in roots:
        heading = section_heading(root).replace(f" | {PACKAGE_TAG}", "")
        for br in root.find_all("br"):
            br.replace_with(NavigableString("\n"))
        for h in root.find_all(SECTION_HEADINGS):
            h.insert(0, NavigableString(_HEADING_MARK))
        pkgs = _outermost([e for e in root.find_all(True) if PACKAGE_HOOK_RE.search(_hook(e))])
        for e in pkgs:
            e.insert(0, NavigableString(f"\n{_PKG_START}\n"))
            e.append(NavigableString(f"\n{_PKG_END}\n"))
        in_pkg = int(in_package_block(root))
        for el in root.find_all(BLOCK_TAGS):
            el.insert_before(NavigableString("\n"))
            el.insert_after(NavigableString("\n"))
        for ln in root.get_text(" ").splitlines():
            if ln.strip() == _PKG_START:
                in_pkg += 1
                continue
            if ln.strip() == _PKG_END:
                in_pkg -= 1
                continue
            is_heading = _HEADING_MARK in ln
            ln = re.sub(r"\s+", " ", ln.replace(_HEADING_MARK, "")).strip()
            if not ln:
                continue
            if is_heading:
                heading = ln
            out.append((ln, f"{heading} | {PACKAGE_TAG}" if in_pkg > 0 else heading))
    return out


def text_lines(roots: List[Tag]) -> List[str]:
    return [ln for ln, _ in section_lines(roots)]


def link_texts(roots: List[Tag]) -> set:
    return {clean(a).lower() for r in roots for a in r.find_all("a") if clean(a)}
