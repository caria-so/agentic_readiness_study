"""
L1: product attributes stated in schema.org (JSON-LD or microdata/RDFa).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import urljoin

from .product_detection import CANONICAL_RE, _node_refs, _norm, page_signals

PLACEHOLDER_VALUES = {"", "0", "-", "--", "n/a", "na", "n.d.", "nd", "none", "null",
                      "undefined", "tbd", "false", "[]", "{}"}

# schema.org property -> canonical attribute
SCHEMA_FIELDS: List[Tuple[str, str]] = [
    ("brand", "brand"), ("manufacturer", "brand"),
    ("sku", "sku"), ("productID", "sku"), ("gtin", "gtin"), ("gtin8", "gtin"),
    ("gtin12", "gtin"), ("gtin13", "gtin"), ("gtin14", "gtin"), ("mpn", "mpn"),
    ("model", "series"), ("material", "material"), ("color", "colour"),
    ("width", "width"), ("height", "height"), ("depth", "depth"),
    ("weight", "weight"), ("size", "dimensions"), ("countryOfOrigin", "origin"),
]


@dataclass
class SchemaFacts:
    pairs: List[Tuple[str, str, str]] = field(default_factory=list)  # (canonical or "", label, value)
    has_price: bool = False
    has_sku: bool = False


def to_text(v: Any) -> str:
    """Readable value: Brand/Thing -> name, QuantitativeValue -> "40 CMT", lists joined."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(dict.fromkeys(t for t in (to_text(x) for x in v) if t))
    if isinstance(v, dict):
        if "value" in v:
            unit = v.get("unitText") or v.get("unitCode") or ""
            return f"{to_text(v['value'])} {unit}".strip()
        return to_text(v.get("name") or v.get("@id") or "")
    s = str(v).strip()
    return "" if s.lower() in PLACEHOLDER_VALUES else s


def _richness(n: dict) -> int:
    return sum(1 for k, v in n.items() if not k.startswith("@") and to_text(v))


def main_node(nodes: List[Tuple[dict, bool]], page_url: str, canonical: str = "") -> Optional[dict]:
    """The Product node describing this page: url/@id match first, else the richest top-level node."""
    targets = {_norm(t) for t in (page_url, canonical) if t}
    base = page_url or canonical
    for n, _ in nodes:
        if targets and any(_norm(urljoin(base, r)) in targets for r in _node_refs(n)):
            return n
    top = [n for n, nested in nodes if not nested] or [n for n, _ in nodes]
    return max(top, key=_richness) if top else None


def _offers_have_price(node: dict) -> bool:
    offers = node.get("offers")
    for o in offers if isinstance(offers, list) else [offers]:
        if isinstance(o, dict) and (to_text(o.get("price")) or to_text(o.get("lowPrice"))
                                    or isinstance(o.get("priceSpecification"), dict)
                                    and to_text(o["priceSpecification"].get("price"))):
            return True
    return False


def schema_facts(html: str, page_url: str = "") -> SchemaFacts:
    s = page_signals(html, page_url)
    m = CANONICAL_RE.search(html)
    canonical = urljoin(page_url, m.group(1).strip()) if m else ""
    node = main_node(s.jsonld_nodes + s.microdata_nodes, page_url, canonical)
    facts = SchemaFacts()
    if node is None:
        return facts
    # A ProductGroup keeps sku/offers on its variants: fall back to the first one.
    variants = node.get("hasVariant")
    first = (variants[0] if isinstance(variants, list) and variants else variants)
    first = first if isinstance(first, dict) else {}

    def get(key: str) -> Any:
        v = node.get(key)
        return v if to_text(v) else first.get(key)

    for key, canon in SCHEMA_FIELDS:
        v = to_text(get(key))
        if v:
            facts.pairs.append((canon, key, v))
    facts.has_sku = any(c in ("sku", "gtin", "mpn") for c, _, _ in facts.pairs)
    facts.has_price = _offers_have_price(node) or _offers_have_price(first)

    ap = node.get("additionalProperty") or first.get("additionalProperty")
    for p in ap if isinstance(ap, list) else [ap]:
        if isinstance(p, dict):
            name = to_text(p.get("name") or p.get("propertyID"))
            val = to_text(p.get("value"))
            if name and val:
                facts.pairs.append(("", name, val))
    return facts
