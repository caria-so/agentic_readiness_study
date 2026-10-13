"""
Raw (label, value) pairs from the main product area.

L2 spec blocks: <dl> dt/dd, two-cell <tr>, label/value <div> pairs.
L3 lines:       "Label: value" lines of the main-area text.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bs4 import Tag

from .html_main import clean, section_heading

Pair = Tuple[str, str, str]                       # (label, value, section heading)

LABEL_CLASS_RE = re.compile(r"label|name|key|title|attribute-label", re.I)
VALUE_CLASS_RE = re.compile(r"value|data|attribute-value", re.I)
MAX_LABEL, MAX_VALUE = 80, 300
LINE_PAIR_RE = re.compile(r"^(?P<label>[A-ZÀ-Ý][^:]{1,39}?)\s*:\s*(?P<value>\S.*)$")
MAX_LINE_LABEL_WORDS = 5


def _cls(el: Tag) -> str:
    c = el.get("class") or []
    return " ".join(c) if isinstance(c, list) else str(c)


def _pair(label_el: Tag, value_el: Tag) -> Optional[Pair]:
    k, v = clean(label_el), clean(value_el)
    if k and v and len(k) <= MAX_LABEL and len(v) <= MAX_VALUE:
        return k, v, section_heading(label_el)
    return None


def dl_pairs(roots: List[Tag]) -> List[Pair]:
    out = []
    for r in roots:
        for dt in r.find_all("dt"):
            dd = dt.find_next_sibling(["dd", "dt"])
            if dd is not None and dd.name == "dd" and (p := _pair(dt, dd)):
                out.append(p)
    return out


def table_pairs(roots: List[Tag]) -> List[Pair]:
    out = []
    for r in roots:
        for tr in r.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) == 2 and (p := _pair(*cells)):
                out.append(p)
    return out


def _value_sibling(el: Tag) -> Optional[Tag]:
    for sib in el.find_next_siblings(True):
        c = _cls(sib)
        if VALUE_CLASS_RE.search(c):
            return sib
        if LABEL_CLASS_RE.search(c):
            return None
    return None


def div_pairs(roots: List[Tag]) -> List[Pair]:
    """Element with a label-ish class followed by a sibling with a value-ish class.

    When the label sits alone inside a wrapper, the wrapper's siblings are searched.
    """
    out = []
    for r in roots:
        for el in r.find_all(True):
            if el.name in ("dt", "dd", "tr", "td", "th") or not LABEL_CLASS_RE.search(_cls(el)):
                continue
            if VALUE_CLASS_RE.search(_cls(el)):
                continue
            val = _value_sibling(el)
            if val is None and el.parent is not None and len(el.parent.find_all(True, recursive=False)) == 1:
                val = _value_sibling(el.parent)
            if val is not None and el not in val.parents and (p := _pair(el, val)):
                out.append(p)
    return out


ROW_CELL_TAGS = {"span", "strong", "b", "div", "p", "em", "label"}
MIN_REPEATED_ROWS = 3


def _row_signature(el: Tag) -> Optional[tuple]:
    """Signature of a two-cell row: exactly two leaf children with text and no links."""
    kids = el.find_all(True, recursive=False)
    if len(kids) != 2 or any(k.name not in ROW_CELL_TAGS or k.find(["div", "p", "ul", "table", "a"])
                             for k in kids):
        return None
    return (el.name, _cls(el), kids[0].name, _cls(kids[0]), kids[1].name, _cls(kids[1]))


def repeated_row_pairs(roots: List[Tag]) -> List[Pair]:
    """Two-cell rows (<span>Label</span><span>value</span>) repeated at least 3 times
    with the same markup: a spec list built from divs instead of a table."""
    groups: dict = {}
    for r in roots:
        for el in r.find_all(True):
            sig = _row_signature(el)
            if sig and el.find_parent("a") is None:      # rows inside a link are product tiles
                groups.setdefault(sig, []).append(el)
    out = []
    for rows in groups.values():
        if len(rows) < MIN_REPEATED_ROWS:
            continue
        for el in rows:
            p = _pair(*el.find_all(True, recursive=False))
            if p and len(p[0]) <= 40 and _words(p[0]) <= MAX_LINE_LABEL_WORDS:
                out.append(p)
    return out


ROW_LABEL_CLASS_RE = re.compile(r"label|attribute|(^|[-_ ])key", re.I)


def labelled_row_pairs(roots: List[Tag]) -> List[Pair]:
    """Rows of exactly two children, the first with a label-ish class and the second
    without one (no "value" class needed), repeated at least 3 times with the same markup."""
    groups: dict = {}
    for r in roots:
        for el in r.find_all(True):
            kids = el.find_all(True, recursive=False)
            if (len(kids) == 2 and ROW_LABEL_CLASS_RE.search(_cls(kids[0]))
                    and not ROW_LABEL_CLASS_RE.search(_cls(kids[1]))
                    and el.find_parent("a") is None and kids[0].name not in ("td", "th", "dt")):
                groups.setdefault((el.name, _cls(el), _cls(kids[0]), _cls(kids[1])), []).append(kids)
    out = []
    for rows in groups.values():
        if len(rows) >= MIN_REPEATED_ROWS:
            out += [p for k in rows if (p := _pair(k[0], k[1]))]
    return out


def block_pairs(roots: List[Tag]) -> List[Pair]:
    """L2: every spec-block pair, de-duplicated in document order."""
    seen, out = set(), []
    for p in dl_pairs(roots) + table_pairs(roots) + div_pairs(roots) + repeated_row_pairs(roots) \
            + labelled_row_pairs(roots):
        key = (p[0].lower(), p[1].lower())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _words(label: str) -> int:
    return len(re.sub(r"\([^)]*\)", " ", label).split())


def line_pairs(lines: List[Tuple[str, str]]) -> List[Pair]:
    """L3: "Label: value" lines, given as (line, heading); label 2-40 chars, at most 5 words,
    starts uppercase."""
    seen, out = set(), []
    for ln, heading in lines:
        m = LINE_PAIR_RE.match(ln)
        if not m:
            continue
        k, v = m.group("label").strip(), m.group("value").strip()
        if not (2 <= len(k) <= 40) or _words(k) > MAX_LINE_LABEL_WORDS or len(v) > MAX_VALUE:
            continue
        key = (k.lower(), v.lower())
        if key not in seen:
            seen.add(key)
            out.append((k, v, heading))
    return out
