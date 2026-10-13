"""
Label vocabulary for spec extraction: normalisation, junk filter, canonical
attributes, page exclusions. Every rule is generic; nothing keys on a site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────────────
# Labels
# ─────────────────────────────────────────────────────────────────────

def display_label(label: str) -> str:
    """Trimmed label as shown: no bullets, trailing colons or doubled whitespace."""
    label = re.sub(r"\s+", " ", label).strip()
    label = re.sub(r"^[•\-–*·]+\s*", "", label)
    return re.sub(r"\s*:+\s*$", "", label).strip()


def norm_label(label: str) -> str:
    return display_label(label).lower()


def label_words(label: str) -> int:
    """Word count without parenthesised units, so "Larghezza (in cm)" is one word."""
    return len(re.sub(r"\([^)]*\)", " ", label).split())


# ─────────────────────────────────────────────────────────────────────
# Junk filter (L2 and L3). Rule names are what spec_dropped.jsonl records.
# ─────────────────────────────────────────────────────────────────────

TEMPLATE_SHARE = 0.8
TEMPLATE_MIN_PAGES = 5

PRICE_LABEL_RE = re.compile(
    r"prezzo|price|prima era|sconto|\brisparmi(a|ate)?\b|offerta|\biva\b", re.I)
CURRENCY_RE = re.compile(r"€|\beur\b|\d+[.,]\d{2}\s*€", re.I)
AMOUNT_RE = re.compile(r"€|\beur\b|[\d.,]+|[\s%+\-–/()]|\b(iva|vat|tax|incl|escl|inclusa|esclusa|inclusi|esclusi)\b\.?", re.I)
SHIPPING_LABEL_RE = re.compile(
    r"spedizion|consegna|delivery|lieferung|tempistic|tempo di consegna|disponibil|"
    r"\breso\b|\bresi\b|pagament|assistenza|lunedì|\borari|elaboriamo|magazzino", re.I)
UI_LABEL_RE = re.compile(
    r"^back to$|recension|review|\bnota$|esaurito|aggiungi|carrello|wishlist|condividi|"
    r"^(nome|cognome|email|e-mail|telefono|messaggio)$", re.I)
JUNK_LABEL_RE = re.compile(
    r"^enabled$|^attivato$|^abilitato$|stato prodotto|visibilità|venditore|seller|sconti|"
    r"promozioni|attenzione|livraison", re.I)
NAME_LABEL_RE = re.compile(r"\d+\s*(cm|mm|lt|l|kg)\b|\d+\s*x\s*\d+", re.I)
MAX_LABEL_WORDS = 5
MIN_LABEL_CHARS, MAX_LABEL_CHARS = 2, 40
MAX_VALUE_WORDS = 15           # an unmatched label with a longer value is a prose paragraph
CODE_LABEL_RE = re.compile(r"^(?=.*[\d-])[A-Z0-9-]{6,}$")   # a code has a digit or a hyphen
MAX_LABEL_REPEATS = 3          # a label repeated more often on one page is a list
INTERNAL_LABEL_RE = re.compile(r"^(codice padre|id)$", re.I)


def is_invalid_label(label: str) -> bool:
    """Valid label: starts with a letter, 2-40 chars, at most 5 words, not an all-caps code."""
    lab = display_label(label)
    return (not (MIN_LABEL_CHARS <= len(lab) <= MAX_LABEL_CHARS) or not re.match(r"[^\W\d_]", lab)
            or label_words(lab) > MAX_LABEL_WORDS or bool(CODE_LABEL_RE.match(lab)))


def is_currency_value(value: str) -> bool:
    """Mostly a money amount: has a currency marker and almost nothing else."""
    return bool(CURRENCY_RE.search(value)) and len(AMOUNT_RE.sub("", value)) <= 3


def _nl(s: str) -> str:
    return re.sub(r"\s*:\s*", ": ", re.sub(r"\s+", " ", s)).strip().lower()


def _link_match(label: str, value: str, links: Iterable[str]) -> bool:
    """Label, or the whole "label: value" line, is the text of a link in the product area."""
    ls = {_nl(x) for x in links}
    return norm_label(label) in ls or _nl(f"{display_label(label)}: {value}") in ls


def is_family_name_label(label: str, value: str) -> bool:
    """One all-caps word repeated inside its value: a product tile ("KUNGSBACKA: Dispenser KUNGSBACKA …")."""
    lab = display_label(label)
    return (len(lab) >= 4 and " " not in lab and lab.isupper() and canonical(lab) is None
            and re.search(rf"\b{re.escape(lab)}\b", value) is not None and len(value.split()) > 1)


def is_internal_code(label: str, value: str) -> bool:
    k = norm_label(label)
    return bool(INTERNAL_LABEL_RE.search(k)) or (k == "title" and value.strip().lower() == "default title")


def junk_reason(label: str, value: str, title: str = "",
                links: Iterable[str] = ()) -> Optional[str]:
    """Page-level junk rules 2-6; the template rule needs the whole site (see template_pairs)."""
    k = norm_label(label)
    if PRICE_LABEL_RE.search(k) or is_currency_value(value) or is_currency_value(label):
        return "price_promo"
    if SHIPPING_LABEL_RE.search(k):
        return "shipping_service"
    if UI_LABEL_RE.search(k) or not re.search(r"[^\W\d_]", k):
        return "interface_text"
    if JUNK_LABEL_RE.search(k):
        return "junk_label"
    known = canonical(label) is not None     # a synonym match overrides the validity rules
    if not known and is_invalid_label(label):
        return "invalid_label"
    if not known and re.search(r"[,!]", label):
        return "sentence_label"
    if not known and len(value.split()) > MAX_VALUE_WORDS:
        return "prose_value"
    if (NAME_LABEL_RE.search(k) or (not known and label_words(k) > MAX_LABEL_WORDS)
            or (title and k == title.strip().lower()) or _link_match(label, value, links)
            or is_family_name_label(label, value)):
        return "product_name_label"
    if is_internal_code(label, value):
        return "internal_code"
    return None


def pair_key(label: str, value: str) -> Tuple[str, str]:
    return norm_label(label), re.sub(r"\s+", " ", value).strip().lower()


def template_pairs(pages_pairs: List[Iterable[Tuple[str, str]]]) -> set:
    """(label, value) keys present on >= 80% of a site's pages: theme text, not specs."""
    n = len(pages_pairs)
    if n < TEMPLATE_MIN_PAGES:
        return set()
    counts: dict = {}
    for pairs in pages_pairs:
        for key in {pair_key(k, v) for k, v in pairs}:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, c in counts.items() if c / n >= TEMPLATE_SHARE}


# ─────────────────────────────────────────────────────────────────────
# Canonical attributes. Order matters: first match wins, so packed-box labels
# come before plain dimensions, "codice ean" before "codice", "tipo attacco" before "tipo".
# ─────────────────────────────────────────────────────────────────────

COMPLIANCE = "compliance_eu_contact"   # EU responsible person / manufacturer: reported apart from specs
ID_ATTRS = ("gtin", "mpn", "sku")

CANONICAL_SYNONYMS: List[Tuple[str, List[str]]] = [
    (COMPLIANCE,        [r"persona responsabile", r"operatore economico", r"fabbricante", r"manufacturer"]),
    ("packaged_weight", [r"prodotto imballato: peso", r"peso (dell')?imballo", r"peso confezione"]),
    ("packaged_dims",   [r"prodotto imballato: (larghezza|altezza|profondità|lunghezza)",
                         r"dimensioni (dell')?imballo", r"imballo", r"confezione", r"numero di colli"]),
    ("gtin",            [r"ean13", r"ean", r"gtin\d*", r"codice ean", r"codice a barre"]),
    ("mpn",             [r"mpn", r"codice produttore", r"cod\. ?produttore", r"part number"]),
    ("sku",             [r"sku", r"codice( prodotto| articolo)?", r"cod\. ?(articolo|prodotto)", r"art\. ?n",
                         r"riferimento", r"le code", r"cod"]),
    ("brand",           [r"marca", r"marchio", r"brand", r"produttore", r"marke", r"hersteller", r"marque"]),
    ("series",          [r"series?", r"kollektion", r"linea", r"collezione", r"modello", r"model", r"collection", r"designer",
                         r"nome collezione"]),
    ("installation",    [r"installazione", r"montaggio", r"fissaggi?o?( compresi)?", r"tipo( di)? attacco",
                         r"posa", r"mobile sospeso", r"assemblato", r"kit( di)? fissaggio",
                         r"numero di pezzi", r"assemblaggio", r"luogo di utilizzo",
                         r"braccio di sostegno", r"attacchi?"]),
    ("opening",         [r"apertura", r"chiusura", r"sistema di apertura", r"maniglia", r"anta"]),
    ("material",        [r"materiale( principale)?", r"materiali", r"struttura", r"strutture", r"tessuto",
                         r"composizione", r"material", r"cristallo", r"vetro", r"legno", r"matériau"]),
    ("finish",          [r"finitura", r"finiture", r"effetto", r"superficie", r"finish", r"oberfläche", r"finition",
                         r"ausführung", r"finishes", r"metodo di colorazione"]),
    ("colour",          [r"colore( principale)?", r"colori", r"colour", r"color", r"farbe", r"ral",
                         r"couleur", r"famiglia colore"]),
    ("width",           [r"larghezza", r"width", r"breite"]),
    ("height",          [r"altezza", r"height", r"höhe"]),
    ("depth",           [r"profondità", r"depth", r"tiefe"]),
    ("length",          [r"lunghezza", r"length", r"länge"]),
    ("dimensions",      [r"dimensioni", r"dimensione", r"misure", r"misura", r"formato", r"ingombro",
                         r"dimensions", r"maße", r"size"]),
    ("thickness",       [r"spessore", r"spessori( fianchi| frontali)?", r"thickness"]),
    ("weight",          [r"peso( netto)?", r"weight", r"gewicht", r"poids"]),
    ("shape_style",     [r"forma", r"stile", r"tipologia", r"tipo", r"design"]),
    ("warranty",        [r"garanzia", r"warranty", r"garantie"]),
    ("certification",   [r"certificazion[ei]", r"classe di resistenza", r"norma", r"ce"]),
    ("origin",          [r"prodotto in", r"made in", r"paese di origine", r"paese di fabbricazione",
                         r"herkunft"]),
    ("electrical",      [r"potenza( watt)?", r"lumen", r"protezione ip", r"ip", r"voltaggio",
                         r"tensione", r"watt", r"kelvin", r"classe energetica", r"classe", r"dati elettrici"]),
    ("plumbing",        [r"foro troppopieno", r"foro miscelatore", r"interasse", r"rubinetteria",
                         r"posizione lavabo", r"scarico", r"kit scarico", r"sifone"]),
    ("water",           [r"portata", r"cartuccia", r"getti", r"risparmio acqua", r"pressione"]),
    ("care",            [r"manutenzione", r"pulizia", r"cura", r"candeggio", r"asciugatrice", r"stiratura",
                         r"lavaggio", r"istruzioni", r"lavare", r"consigli per il lavaggio"]),
]
BRAND_IS_RETAILER_NAME = "brand_is_retailer_name"           # brand value is the retailer itself; reported apart from brand
CANONICAL = [c for c, _ in CANONICAL_SYNONYMS] + [BRAND_IS_RETAILER_NAME]
SPEC_ATTRS = [c for c in CANONICAL if c != COMPLIANCE]
_CANON_RES = [(c, re.compile(r"^(?:" + "|".join(alts) + r")(?![a-zà-ÿ])", re.I))
              for c, alts in CANONICAL_SYNONYMS]
TYPE_PREFIX_RE = re.compile(r"^tipo( di| del| della| dello)? (?=\S)", re.I)


def _match(k: str) -> Optional[str]:
    for c, rx in _CANON_RES:
        if rx.match(k):
            return c
    return None


def canonical(label: str) -> Optional[str]:
    """Canonical attribute for a label (prefix match on the normalised label), or None.

    "Tipo di X" is matched on X first ("tipo di apertura" -> opening), else on the whole label.
    """
    k = norm_label(label)
    rest = TYPE_PREFIX_RE.sub("", k)
    return (_match(rest) if rest != k else None) or _match(k) or (typo_match(k) or (None, ""))[0]


def typo_of(label: str) -> Optional[Tuple[str, str]]:
    """(canonical, synonym) only when the label matches *solely* through the one-letter rule."""
    k = norm_label(label)
    rest = TYPE_PREFIX_RE.sub("", k)
    if (rest != k and _match(rest)) or _match(k):
        return None
    return typo_match(k)


# A one-letter slip in a single-word label ("Msure", "Missure" for "misure") still matches.
# Only plain-word synonyms of 6+ letters take part, so short words never collide.
_PLAIN_SYNONYMS = [(c, w) for c, alts in CANONICAL_SYNONYMS for w in alts if re.fullmatch(r"[a-zà-ÿ]{6,}", w)]
TYPO_MIN_LABEL = 5


def _one_edit(a: str, b: str) -> bool:
    """Levenshtein distance exactly 1 (insert, delete or substitute one letter)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def typo_match(k: str) -> Optional[Tuple[str, str]]:
    """(canonical, synonym) when the normalised label is one edit away from a plain synonym."""
    if len(k) < TYPO_MIN_LABEL or not re.fullmatch(r"[a-zà-ÿ]+", k):
        return None
    hits = {(c, w) for c, w in _PLAIN_SYNONYMS if _one_edit(k, w)}
    return next(iter(hits)) if len({c for c, _ in hits}) == 1 else None


# ─────────────────────────────────────────────────────────────────────
# Package sections: a dimension or weight under a packaging heading is the packed box.
# ─────────────────────────────────────────────────────────────────────

PACKAGE_HEADING_RE = re.compile(r"pacc|pacchi|imballo|confezione|\bcoll[oi]\b", re.I)
QUANTITY_HEADING_RE = re.compile(r"^\s*\d+\s*[x×]\s*\S")     # "1 × MITTPLAN": N units of an article
PACKAGED_OF = {"weight": "packaged_weight", "width": "packaged_dims", "height": "packaged_dims",
               "depth": "packaged_dims", "length": "packaged_dims", "dimensions": "packaged_dims"}


def packaged(attr: Optional[str], heading: str) -> Optional[str]:
    if attr in PACKAGED_OF and heading and (PACKAGE_HEADING_RE.search(heading)
                                            or QUANTITY_HEADING_RE.match(heading)):
        return PACKAGED_OF[attr]
    return attr


# ─────────────────────────────────────────────────────────────────────
# Value checks (all layers). Platforms fill some fields automatically:
# zero weights, the shop's own name as brand, one default weight for every product.
# ─────────────────────────────────────────────────────────────────────

WEIGHT_ATTRS = {"weight", "packaged_weight"}
DIMENSION_ATTRS = {"width", "height", "depth", "length", "dimensions", "thickness", "packaged_dims"}
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
DEFAULT_SHARE = 0.4               # weights and dimensions
SCHEMA_DEFAULT_SHARE = 0.8        # any other schema.org attribute
DEFAULT_MIN_PAGES = 5
GENERIC_NAME_WORDS = {"italia", "italy", "it", "store", "shop", "online", "spa", "srl", "official"}


def _first_number(value: str) -> Optional[float]:
    m = NUMBER_RE.search(value)
    return float(m.group().replace(",", ".")) if m else None


def invalid_value(attr: str, value: str) -> bool:
    """Weight must be a number > 0; a dimension must contain a number."""
    if attr in WEIGHT_ATTRS:
        n = _first_number(value)
        return n is None or n <= 0
    if attr in DIMENSION_ATTRS:
        return _first_number(value) is None
    if attr == COMPLIANCE:
        return bool(UNAVAILABLE_RE.search(value.strip())) or not re.search(r"[^\W\d_]{3}", value)
    return False


GTIN_LENGTHS = (8, 12, 13, 14)


def valid_gtin(code: str) -> bool:
    """8, 12, 13 or 14 digits with a correct GS1 check digit."""
    if not code.isdigit() or len(code) not in GTIN_LENGTHS:
        return False
    body, check = code[:-1], int(code[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def gtin_candidates(value: str) -> List[str]:
    """Digit strings in a GTIN value: whole parts of "a, b | c" lists, else runs of 8-14 digits."""
    out = []
    for part in re.split(r"[,;|/]", value):
        digits = re.sub(r"[\s-]", "", part)
        out += [digits] if digits.isdigit() else re.findall(r"\d{8,14}", part)
    return out


def has_valid_gtin(value: str) -> bool:
    return any(valid_gtin(c) for c in gtin_candidates(value))


def norm_id(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.lower())


UNAVAILABLE_RE = re.compile(
    r"non\s+(è|e')\s+attualmente\s+disponibile|non\s+disponibile|not\s+available|^n/?a$|^-+$", re.I)
EU_CONTACT_RE = re.compile(
    r"(?:persona responsabile|operatore economico)[^:\n]{0,30}:\s*(?P<v>[^\n]{0,300})", re.I)


GPSR_TERMS_RE = re.compile(
    r"persona responsabile|operatore economico|informazioni sulla sicurezza|sicurezza del prodotto|gpsr|"
    r"responsible person|fabbricante|dati del produttore|informazioni sul produttore", re.I)
# A manufacturer / operator data block = a company name AND a postal address, under a matching heading.
MANUFACTURER_HEADING_RE = re.compile(
    r"dati del produttore|informazioni sul produttore|elementi di conformit|conformit\w* del prodotto|"
    r"informazioni sulla sicurezza", re.I)
COMPANY_RE = re.compile(
    r"\b(?:s\.?\s?r\.?\s?l\.?|s\.?\s?p\.?\s?a\.?|s\.?\s?n\.?\s?c\.?|s\.?\s?a\.?\s?s\.?|s\.?\s?a\.?\s?r\.?\s?l\.?|"
    r"gmbh|ltd|limited|llc|inc|b\.?v\.?|kg|ug|oy|n\.?v\.?|spółka|sp\.? ?z ?o\.? ?o\.?|s\.r\.o\.?)(?![\w])|(?-i:\b(?:AG|SA|SL|AB)\b)", re.I)
ADDRESS_RE = re.compile(
    r"\b(?:via|viale|v\.le|piazza|p\.zza|corso|strada|largo|vicolo|localit\w*|loc\.|rue|avenue|calle|street|road)\b"
    r"[^\n]{0,40}?\d"                                      # street with a number
    r"|\b\w+(?:stra(?:ß|ss)e|str\.)\s*\d"                    # Albert-Einstein-Straße 7
    r"|[A-Za-zÀ-ÿ'][\wÀ-ÿ' ]{2,},?\s*\([A-Z]{2}\)", re.I)       # city (PG)
POSTCODE_RE = re.compile(r"(?<![\w.,\[-])(?:\d{2}-\d{3}|\d{4,6})(?![\w-])(?![.,]\d)"     # 67-120, 6136, 42929, 077005
                         r"(?!\s?(?:w|v|kg|g|cm|mm|m|l|hz|ml|mah|lm|k)\b)", re.I)


EU_PERSON_LABEL_RE = re.compile(r"(?:persona responsabile|operatore economico|responsible person)[^:\n]{0,30}:", re.I)


def is_manufacturer_block(body: str) -> bool:
    """A company name and a postal address in the same section, before any "Persona responsabile
    nell'UE :" label. What follows that label is the EU responsible person, already counted as
    the EU contact, so it must not be counted a second time as a manufacturer block."""
    m = EU_PERSON_LABEL_RE.search(body)
    if m:
        body = body[:m.start()]
    if not COMPANY_RE.search(body):
        return False
    if ADDRESS_RE.search(body):
        return True
    return any(not re.fullmatch(r"(?:19|20)\d{2}", m.group()) for m in POSTCODE_RE.finditer(body))


def eu_contact_state(text: str) -> str:
    """'contact' (a real value), 'unavailable' (label present, value says not available) or ''."""
    seen = False
    for m in EU_CONTACT_RE.finditer(text):
        seen = True
        if not invalid_value(COMPLIANCE, m.group("v").strip()):
            return "contact"
    return "unavailable" if seen else ""


def find_eu_contact(text: str) -> str:
    """Value after "Persona responsabile ...:" / "Operatore economico ...:" in the page text,
    or "" when absent or stated as unavailable. Text blocks that "Label: value" lines miss."""
    for m in EU_CONTACT_RE.finditer(text):
        if not invalid_value(COMPLIANCE, m.group("v").strip()):
            return re.sub(r"\s+", " ", m.group("v")).strip()
    return ""


def _name_words(s: str) -> List[str]:
    return re.findall(r"[0-9a-zà-ÿ]+", s.lower())


@dataclass(frozen=True)
class Retailer:
    words: Tuple[str, ...] = ()     # retailer name without generic words ("IKEA Italia" -> ikea)
    domain: str = ""                # first label of the host ("mondoconv")

    @classmethod
    def from_site(cls, company: str, base_url: str) -> "Retailer":
        words = tuple(w for w in _name_words(company) if w not in GENERIC_NAME_WORDS)
        host = urlparse(base_url).netloc.lower().removeprefix("www.")
        return cls(words, re.sub(r"[^0-9a-z]", "", host.split(".")[0]))


def is_own_brand(value: str, retailer: Optional[Retailer]) -> bool:
    """Brand value starts with the retailer's name words, or is its domain name."""
    if retailer is None:
        return False
    bw = _name_words(value)
    if not bw:
        return False
    if retailer.words and bw[:len(retailer.words)] == list(retailer.words):
        return True
    return bool(retailer.domain) and (bw[0] == retailer.domain or "".join(bw) == retailer.domain)


def repeated_defaults(pages_values: List[Iterable[Tuple[str, str]]], share: float = DEFAULT_SHARE,
                      attrs: Optional[set] = None) -> set:
    """(attr, value) repeated on >= `share` of a site's pages (min 5 pages). By default only
    weights/dimensions at 40%: one number for a WC seat, a hair dryer and a flush plate is a
    platform default. With attrs=None replaced by an explicit set, any attribute can be checked."""
    n = len(pages_values)
    attrs = WEIGHT_ATTRS | DIMENSION_ATTRS if attrs is None else attrs
    counts: dict = {}
    for values in pages_values:
        for key in {(a, v.strip().lower()) for a, v in values if a in attrs}:
            counts[key] = counts.get(key, 0) + 1
    return {k for k, c in counts.items() if c >= DEFAULT_MIN_PAGES and c / n >= share}


# ─────────────────────────────────────────────────────────────────────
# Page exclusions
# ─────────────────────────────────────────────────────────────────────

GIFT_RE = re.compile(r"gift.?card|buono regalo|carta regalo|e-?gift", re.I)
MIN_PAIRS_WITHOUT_L1 = 2


def exclusion_reason(title: str, url: str, l1_has_price: bool, l1_has_sku: bool,
                     n_block_line_pairs: int) -> Optional[str]:
    if GIFT_RE.search(title or "") or GIFT_RE.search(url or ""):
        return "gift_card"
    if not l1_has_price and not l1_has_sku and n_block_line_pairs < MIN_PAIRS_WITHOUT_L1:
        return "not_a_product"
    return None
