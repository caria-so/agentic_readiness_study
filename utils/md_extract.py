"""
md_extract.py — microdata + RDFa(-lite) extraction, stdlib only.
================================================================

Turns  <div itemscope itemtype="https://schema.org/Product"> …  (microdata)
and    <div vocab="https://schema.org/" typeof="Product"> …     (RDFa lite)
into the same dict shape JSON-LD produces, e.g.

    {"@type": "Product", "name": "…",
     "offers": {"@type": "Offer", "price": "899.00", "priceCurrency": "EUR"},
     "additionalProperty": [{"@type": "PropertyValue", "name": "Larghezza", "value": "80 cm"}]}

so product_sample.py can audit microdata sites with the same code it uses for
JSON-LD. Single-pass html.parser; tolerant of unclosed tags.

Value rules follow the microdata spec, plus one pragmatic addition Google also
honours: a `content` attribute on ANY element wins over its text
(<span itemprop="price" content="899.00">€ 899,00</span> is ubiquitous in
PrestaShop themes).
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# tags that imply a word break in rendered text
BREAKING = {"br", "p", "div", "li", "dd", "dt", "tr", "td", "th", "h1", "h2", "h3",
            "h4", "h5", "h6", "section", "article", "ul", "ol", "dl", "table", "hr"}
SKIP_TEXT = {"script", "style", "noscript", "template"}

URL_ATTR = {"a": "href", "area": "href", "link": "href",
            "img": "src", "audio": "src", "video": "src", "source": "src",
            "iframe": "src", "embed": "src", "track": "src",
            "object": "data"}
VALUE_ATTR = {"data": "value", "meter": "value", "time": "datetime"}


def _norm_name(p: str) -> str:
    """'http://schema.org/name' | 'schema:name' | 'og:x' -> 'name' | 'x'."""
    p = p.strip()
    if "/" in p:
        p = p.rstrip("/").rsplit("/", 1)[-1]
    if "#" in p:
        p = p.rsplit("#", 1)[-1]
    if ":" in p:
        p = p.rsplit(":", 1)[-1]
    return p


def _norm_types(t: str) -> Any:
    ts = [_norm_name(x) for x in (t or "").split() if x.strip()]
    if not ts:
        return None
    return ts[0] if len(ts) == 1 else ts


class _Frame:
    __slots__ = ("tag", "item", "text_props", "text_target", "buf")

    def __init__(self, tag: str):
        self.tag = tag
        self.item: Optional[dict] = None
        self.text_props: List[str] = []
        self.text_target: Optional[dict] = None
        self.buf: List[str] = []


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[_Frame] = []
        self.top: List[dict] = []
        self._skip = 0

    # nearest enclosing item
    def _cur_item(self) -> Optional[dict]:
        for f in reversed(self.stack):
            if f.item is not None:
                return f.item
        return None

    @staticmethod
    def _add(item: dict, prop: str, value: Any):
        # everything is stored as a list while parsing; _collapse() unwraps singletons
        item.setdefault(prop, []).append(value)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag in SKIP_TEXT:
            self._skip += 1
        if tag in BREAKING:
            self.handle_data(" ")

        is_scope = "itemscope" in a or "typeof" in a
        raw_props = a.get("itemprop") or a.get("property") or ""
        props = [_norm_name(p) for p in raw_props.split() if p.strip()]
        parent = self._cur_item()

        f = _Frame(tag)
        if is_scope:
            new: Dict[str, Any] = {}
            t = _norm_types(a.get("itemtype") or a.get("typeof") or "")
            if t:
                new["@type"] = [t] if isinstance(t, str) else [t]
            if props and parent is not None:
                for p in props:
                    self._add(parent, p, new)
            else:
                self.top.append(new)
            f.item = new
            # itemid / resource / about as @id
            ident = a.get("itemid") or a.get("resource") or a.get("about")
            if ident:
                new["@id"] = [ident]
        elif props and parent is not None:
            val = None
            if "content" in a:
                val = a["content"]
            elif tag in URL_ATTR and a.get(URL_ATTR[tag]):
                val = a[URL_ATTR[tag]]
            elif tag in VALUE_ATTR and a.get(VALUE_ATTR[tag]):
                val = a[VALUE_ATTR[tag]]
            elif tag == "meta":
                val = a.get("content", "")
            if val is not None:
                for p in props:
                    self._add(parent, p, val.strip())
            else:
                f.text_props = props
                f.text_target = parent

        if tag in VOID:
            return            # no end tag will come; nothing to push
        self.stack.append(f)

    def handle_startendtag(self, tag, attrs):
        # <meta ... /> etc: treat as start of a void element
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TEXT and self._skip:
            self._skip -= 1
        if tag in BREAKING:
            self.handle_data(" ")
        # pop up to and including the nearest matching open tag; ignore strays
        if not any(fr.tag == tag for fr in self.stack):
            return
        while self.stack:
            fr = self.stack.pop()
            self._close(fr)
            if fr.tag == tag:
                break

    def _close(self, fr: _Frame):
        if fr.text_props and fr.text_target is not None:
            text = " ".join("".join(fr.buf).split())
            for p in fr.text_props:
                self._add(fr.text_target, p, text)

    def handle_data(self, data):
        if self._skip:
            return
        for fr in self.stack:
            if fr.text_props:
                fr.buf.append(data)

    def close(self):
        super().close()
        while self.stack:
            self._close(self.stack.pop())


def _collapse(node: Any) -> Any:
    """Single-valued lists -> scalars, recursively; drop internal markers."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k.startswith("__"):
                continue
            if isinstance(v, list):
                vals = [_collapse(x) for x in v]
                out[k] = vals[0] if len(vals) == 1 else vals
            else:
                out[k] = _collapse(v)
        return out
    if isinstance(node, list):
        return [_collapse(x) for x in node]
    return node


def extract_items(html: str) -> List[dict]:
    """All top-level microdata / RDFa items in the page, as JSON-LD-shaped dicts."""
    p = _Parser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # html.parser is very tolerant; if it still fails, return what we have
        pass
    return [_collapse(i) for i in p.top]