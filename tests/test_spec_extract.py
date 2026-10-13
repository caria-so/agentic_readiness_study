#!/usr/bin/env python3
"""
Acceptance tests for c_spec_extract.py (extract.md, Step 8).

Pages are copied once from results/bathroom/ita/20261003_merged/sites/ into tests/fixtures/specs/
(HTML + URL), so the tests keep working if the run folder changes. Offline only.

Run from the project root:
  python -m tests.test_spec_extract              # use fixtures, copy missing ones
  python -m tests.test_spec_extract --refresh    # re-copy from the reference batch
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Tuple

from c_spec_extract import extract_page
from utils.html_main import main_area, make_soup, section_lines, strip_chrome, strip_reviews
from utils.page_files import iter_saved_pages
from utils.spec_pairs import block_pairs, div_pairs, line_pairs
from utils.spec_rules import (
    COMPLIANCE, BRAND_IS_RETAILER_NAME, PRICE_LABEL_RE, Retailer, canonical, invalid_value, is_invalid_label,
    find_eu_contact, has_valid_gtin, is_own_brand, junk_reason, packaged, repeated_defaults,
)

SITES = Path("results/bathroom/ita/20261003_merged/sites")     # the reference batch the fixtures come from
FIXTURES = Path(__file__).parent / "fixtures" / "specs"

# (id, site dir, file-name fragment)
PAGES = [
    ("boleco_diadema", "Boleco_93c40284", "1799-pensile-bagno-diadema-cm-30x75"),
    ("tecnomat_raccord", "Tecnomat_051a633b", "raccord-ghisa-130r-tee"),
    ("leroy_imballato", "Leroy_Merlin_Italia_eacd447c", "saloon-l-67-71-cm"),
    ("jysk_lycksele", "Jysk_ab080d4f", "dispenser-sapone-lycksele"),
    ("mondo_alice", "Mondo_Convenienza_0a828b02", "alice-22e3"),
    ("dwelli_alape", "Dwelli_c293df3e", "alape-fb-me-eingebautes-waschbecken"),
    ("shopbagno_box", "ShopBagno_212db257", "box-doccia-90x90-semicircolare-profili-oro"),
    ("boleco_giftcard", "Boleco_93c40284", "gift-card_2473"),
]


def fixture(pid: str, site: str, frag: str, refresh: bool) -> Tuple[str, str]:
    html_p, meta_p = FIXTURES / f"{pid}.html", FIXTURES / f"{pid}.json"
    if refresh or not html_p.exists():
        f, url = next((f, u) for f, u in iter_saved_pages(SITES / site) if frag in f.name)
        FIXTURES.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, html_p)
        meta_p.write_text(json.dumps({"url": url, "source": str(f)}, indent=1), encoding="utf-8")
    return html_p.read_text(encoding="utf-8", errors="replace"), json.loads(meta_p.read_text())["url"]


def surviving_labels(page: dict) -> List[str]:
    """Every L2/L3 label kept on the page, attribute sources and other labels alike."""
    labels = [s["label"] for a in page["attributes"].values() for s in a["sources"]
              if s["layer"] != "L1_schema"]
    return labels + list(page["other_labels"])


def all_labels(page: dict) -> List[str]:
    return [s["label"] for a in page["attributes"].values() for s in a["sources"]] + list(page["other_labels"])


def attr(page: dict, name: str) -> dict:
    return page["attributes"].get(name) or {}


# Each check returns a list of failure messages (empty = pass).
def t1(page, dropped, html, url):
    f = []
    m = attr(page, "material")
    if not (m.get("value", "").lower() == "nobilitato" and any(s["layer"] == "L3_lines" for s in m.get("sources", []))):
        f.append(f"material should be Nobilitato from L3, got {m}")
    if "thickness" not in page["attributes"]:
        f.append("thickness missing")
    c = attr(page, "colour")
    if not (c.get("value", "").upper() == "PERLA SPATOLATO" and c.get("layer") == "L2_block"):
        f.append(f"colour should be PERLA SPATOLATO from L2, got {c}")
    if any("codice padre" in k.lower() for k in all_labels(page)):
        f.append("Codice Padre survived")
    return f


def t2(page, dropped, html, url):
    soup = make_soup(html)
    strip_chrome(soup)
    n_div = len(div_pairs(main_area(soup)[0]))
    l2_attrs = [k for k, a in page["attributes"].items() if any(s["layer"] == "L2_block" for s in a["sources"])]
    f = []
    if n_div < 5:
        f.append(f"only {n_div} label/value div pairs read")
    if len(l2_attrs) < 5:
        f.append(f"only {len(l2_attrs)} L2 attributes: {l2_attrs}")
    return f


def t3(page, dropped, html, url):
    f = []
    if "packaged_dims" not in page["attributes"]:
        f.append("packaged_dims not set")
    for a in ("width", "height", "depth"):
        bad = [s["label"] for s in attr(page, a).get("sources", []) if "imball" in s["label"].lower()]
        if bad:
            f.append(f"{a} taken from packed-box labels {bad}")
    return f


def t4(page, dropped, html, url):
    bad = [k for k in surviving_labels(page) if PRICE_LABEL_RE.search(k.lower()) or "€" in k]
    f = [f"price/promo labels survived: {bad}"] if bad else []
    if "prima era" not in html.lower():
        f.append("fixture has no 'Prima era' text, pick another page")
    return f


def t5(page, dropped, html, url):
    bad = [k for k in surviving_labels(page) if junk_reason(k, "x", page["title"]) == "product_name_label"
           or re.search(r"\d+\s*(cm|mm|lt|l|kg)\b|\d+x\d+", k, re.I)]
    return [f"product-name labels survived: {bad}"] if bad else []


def t6(page, dropped, html, url):
    f = []
    if any(re.search(r"lieferung|delivery|consegna", k, re.I) for k in surviving_labels(page)):
        f.append("shipping label survived")
    if not any(d["label"].lower() == "lieferung" for d in dropped):
        f.append("fixture has no Lieferung pair, pick another page")
    if "brand" not in page["attributes"]:
        f.append("brand missing")
    if any(k.lower() in ("marke", "marca", "brand") for k in page["other_labels"]):
        f.append("a brand label was counted outside the brand attribute")
    return f


def t7(page, dropped, html, url):
    return [] if page["main_area_chars"] > 500 else [f"main area only {page['main_area_chars']} chars"]


def t8(page, dropped, html, url):
    return [] if page["excluded_reason"] else ["gift card not excluded"]


def t9(page, dropped, html, url):
    """Value checks: zero weight, number-less dimension, own brand, platform default."""
    f = []
    if not invalid_value("weight", "0.000000 kg") or not invalid_value("weight", "KGM"):
        f.append("zero / unit-only weight accepted")
    if invalid_value("weight", "0,5 kg"):
        f.append("0,5 kg rejected")
    if not invalid_value("width", "Non applicabile") or invalid_value("width", "60 cm"):
        f.append("dimension number check wrong")
    jysk = Retailer.from_site("Jysk", "https://jysk.it/")
    if not (is_own_brand("JYSK A/S", jysk) and is_own_brand("Mondo Convenienza",
            Retailer.from_site("Mondo Convenienza", "https://www.mondoconv.it/"))):
        f.append("own brand not recognised")
    if is_own_brand("Obiettivo", Retailer.from_site("OBI Italia", "https://www.obi-italia.it/")):
        f.append("Obiettivo taken as OBI's own brand")
    page_j, _ = extract_page(html, url, retailer=jysk)
    if "brand" in page_j["attributes"] or BRAND_IS_RETAILER_NAME not in page_j["attributes"]:
        f.append(f"Jysk brand not re-tagged: {list(page_j['attributes'])}")
    pages = [[("weight", "9.0000 KGM")]] * 6 + [[("weight", "2 kg")]] * 4
    if repeated_defaults(pages) != {("weight", "9.0000 kgm")}:
        f.append(f"platform default not found: {repeated_defaults(pages)}")
    return f


SNIPPET = """<html><body><h1>Mobile bagno</h1>
<div class="product-detail">
  <h3>Scheda tecnica</h3><dl><dt>Larghezza</dt><dd>60 cm</dd></dl>
  <h3>Dati del pacco</h3><dl><dt>Larghezza</dt><dd>65 cm</dd></dl><p>Peso: 12 kg</p>
  <div class="reviews-block"><p>Materiale: ottimo prodotto</p></div>
</div></body></html>"""


def t10(page, dropped, html, url):
    """Label validity, junk labels, synonyms, id split, compliance, package sections, reviews."""
    f = []
    for lab, bad in [("Colore", False), ("CNL15T", True), ("1 pezzo", True), ("X", True),
                     ("Modalità di connessione per il collegamento 1", True)]:
        if is_invalid_label(lab) != bad:
            f.append(f"label validity wrong for {lab!r}")
    for lab, bad in [("COLORE", False), ("TIPOLOGIA", False), ("MOBILE-DESIO-180-GRIGIO", True),
                     ("GL98321", True), (", punteggio pari a 50", True)]:
        if is_invalid_label(lab) != bad:
            f.append(f"all-caps rule wrong for {lab!r}")
    if junk_reason("COLORE", "Bianco") is not None or junk_reason("TIPOLOGIA", "Sospeso") is not None:
        f.append("synonym match did not override the validity rule")
    long_lab = "Garanzia del prodotto (per la pubblicazione- in anni)"
    if junk_reason(long_lab, "2") is not None:
        f.append("long synonym label dropped")
    if junk_reason(", punteggio pari a 50", "x") != "invalid_label":
        f.append("rating fragment not dropped")
    if junk_reason("Enabled", "x") != "junk_label" or junk_reason("COLORE Venditore", "x") != "junk_label":
        f.append("junk filter must still apply to synonym matches")
    rep = "<html><body><h1>T</h1><div class='product-detail'><p>Cod. Articolo: A1</p>" + \
          "".join(f"<p>Cod. Articolo: B{i}</p>" for i in range(4)) + "<p>Materiale: legno</p></div></body></html>"
    page_r, drops_r = extract_page(rep)
    if page_r["attributes"].get("sku", {}).get("value") != "A1" or \
            sum(d["rule"] == "repeated_label_list" for d in drops_r) != 4:
        f.append(f"repeated label: first occurrence must stay, 4 repeats dropped: {page_r['attributes'].get('sku')}")
    if "material" not in page_r["attributes"]:
        f.append("non-repeated label lost")
    for lab in ("Enabled", "Stato prodotto", "Venditore", "Livraison"):
        if junk_reason(lab, "x") != "junk_label":
            f.append(f"{lab} not junk: {junk_reason(lab, 'x')}")
    want = {"Tipo di apertura": "opening", "Chiusura": "opening", "Maniglia": "opening",
            "Persona responsabile nell'UE": COMPLIANCE, "Fabbricante": COMPLIANCE,
            "Codice EAN": "gtin", "EAN13": "gtin", "Codice produttore": "mpn", "MPN": "mpn",
            "Codice": "sku", "SKU": "sku", "Paese di fabbricazione": "origin", "Ausführung": "finish",
            "RAL": "colour", "Couleur": "colour", "Classe energetica": "electrical",
            "Classe di resistenza": "certification", "Kit di fissaggio": "installation",
            "Lavaggio": "care", "Vetro": "material", "Marque": "brand", "Tipo di prodotto": "shape_style"}
    for lab, c in want.items():
        if canonical(lab) != c:
            f.append(f"{lab!r} -> {canonical(lab)}, want {c}")
    if packaged("width", "Dati del pacco") != "packaged_dims" or packaged("width", "Collocazione") != "width":
        f.append("package heading mapping wrong")

    soup = make_soup(SNIPPET)
    strip_chrome(soup)
    strip_reviews(soup)
    roots, _ = main_area(soup)
    blocks = {(k, v): h for k, v, h in block_pairs(roots)}
    lines = {(k, v): h for k, v, h in line_pairs(section_lines(roots))}
    if blocks.get(("Larghezza", "60 cm")) != "Scheda tecnica" or blocks.get(("Larghezza", "65 cm")) != "Dati del pacco":
        f.append(f"spec-block headings wrong: {blocks}")
    if lines.get(("Peso", "12 kg")) != "Dati del pacco":
        f.append(f"line heading wrong: {lines}")
    if any(k == "Materiale" for k, _ in lines):
        f.append("review block was parsed")
    keep = make_soup('<div class="product-rating"><h1>T</h1><p>Colore: blu</p></div>')
    strip_reviews(keep)
    if keep.find("h1") is None:
        f.append("review-class block holding the h1 was removed")

    page_s, _ = extract_page(SNIPPET)
    a = page_s["attributes"]
    if a.get("width", {}).get("value") != "60 cm" or "packaged_dims" not in a or "packaged_weight" not in a:
        f.append(f"package section mapping in extract_page wrong: {list(a)}")
    return f


def t11(page, dropped, html, url):
    """Contact-form, sentence, prose and link labels; new synonyms; Deghi rows; EU contact."""
    f = []
    for lab in ("Nome", "Email", "Telefono", "Cognome"):
        if junk_reason(lab, "x") != "interface_text":
            f.append(f"{lab} not dropped as form field")
    if junk_reason("Boleco, design e forza", "x") != "sentence_label" or \
            junk_reason("AVVERTENZA! Rischio di caduta", "x") != "sentence_label":
        f.append("comma/! labels not dropped")
    if junk_reason("Uso acqua & alimenti", " ".join(["parola"] * 20)) != "prose_value":
        f.append("prose paragraph not dropped")
    if junk_reason("Divani modulari", "trova quello giusto per la tua casa",
                   links={"divani modulari: trova quello giusto per la tua casa"}) != "product_name_label":
        f.append("link text (label: value) not dropped")
    want = {"Lavare": "care", "Consigli per il lavaggio": "care", "Famiglia colore": "colour",
            "Numero di colli": "packaged_dims", "Assemblaggio": "installation",
            "Luogo di utilizzo": "installation", "Size": "dimensions", "Finishes": "finish",
            "Metodo di colorazione": "finish", "Designer": "series", "Nome Collezione": "series",
            "Dati Elettrici": "electrical", "COD": "sku", "Foro troppopieno": "plumbing",
            "Foro miscelatore": "plumbing", "Interasse": "plumbing", "Rubinetteria": "plumbing",
            "Posizione lavabo": "plumbing", "Scarico": "plumbing", "Sifone": "plumbing"}
    for lab, c in want.items():
        if canonical(lab) != c:
            f.append(f"{lab!r} -> {canonical(lab)}, want {c}")
    rows = "".join(f'<li><div class="row"><div class="label-attribute"><label><strong>{k}</strong></label></div>'
                   f'<div class="col-md-6"><p><span> {v} </span></p></div></div></li>'
                   for k, v in [("Materiale", "Resina"), ("Installazione", "Appoggio"), ("Colore", "Trasparente")])
    page_d, _ = extract_page(f"<html><body><h1>T</h1><div class='product-detail'><ul>{rows}</ul></div></body></html>")
    got = {k: v["value"] for k, v in page_d["attributes"].items()}
    if got.get("material") != "Resina" or got.get("installation") != "Appoggio" or got.get("colour") != "Trasparente":
        f.append(f"label/value rows without value class not read: {got}")
    if find_eu_contact("Leroy Merlin\nPersona responsabile nell'UE :\nEBANISTERIA SRL\nVia Roma 1") != "EBANISTERIA SRL":
        f.append("EU contact text not found")
    if find_eu_contact("Persona responsabile nell'UE : Questa informazione non è attualmente disponibile sul nostro sito."):
        f.append("'not available' counted as an EU contact")
    return f


def t12(page, dropped, html, url):
    """GTIN check digit, package blocks (class and "N x NAME" heading), spec table inside a review-classed block."""
    f = []
    for code, ok in [("4006381333931", True), ("40063813339310", False), ("4006381333932", False),
                     ("96385074", True), ("012345678905", True), ("12345", False), ("00000000000000", True),
                     ("abc", False)]:
        if has_valid_gtin(code) != ok:
            f.append(f"gtin {code}: want {ok}")
    if not has_valid_gtin("4006381333932, 4006381333931") or has_valid_gtin("1234567890123"):
        f.append("gtin list handling wrong")
    pk = """<html><body><h1>Scrivania</h1><div class="product-detail"><h4>Cura</h4>
      <ul class="measurements"><li><span>Larghezza:</span> <span>80 cm</span></li>
      <li><span>Altezza:</span> <span>75 cm</span></li></ul>
      <div class="pipf-measurements-tab__package"><h4>1 \u00d7 SCRIVANIA</h4>
      <p>Larghezza: 87 cm</p><p>Peso: 20 kg</p></div>
      <h4>Istruzioni</h4><p>Lunghezza: 150 cm</p></div></body></html>"""
    page_p, _ = extract_page(pk)
    a = page_p["attributes"]
    if a.get("width", {}).get("value") != "80 cm" or a.get("packaged_dims", {}).get("value") != "87 cm" \
            or a.get("packaged_weight", {}).get("value") != "20 kg" or a.get("length", {}).get("value") != "150 cm":
        f.append(f"package block mapping wrong: { {k: v['value'] for k, v in a.items()} }")
    if packaged("weight", "1 \u00d7 MITTPLAN") != "packaged_weight" or packaged("weight", "Garanzia") != "weight":
        f.append("quantity heading rule wrong")
    rv = """<html><body><h1>T</h1><div class="cms-block-product-description-reviews"><div class="specificheTecniche">
      <table><tr><th>Materiale:</th><td>Legno</td></tr></table></div>
      <div class="product-reviews"><p>Colore: rosso</p></div></div></body></html>"""
    page_r, _ = extract_page(rv)
    ar = page_r["attributes"]
    if ar.get("material", {}).get("value") != "Legno" or "colour" in ar:
        f.append(f"review-wrapped spec table: {list(ar)}")
    return f


def t13(page, dropped, html, url):
    """New synonyms, one-letter typos, total-price lines, EU-contact states."""
    from utils.spec_rules import canonical, eu_contact_state, junk_reason, typo_match
    f = []
    for lab, want in [("Kollektion", "series"), ("Series", "series"), ("Le code", "sku"), ("Poids", "weight"),
                      ("Braccio di sostegno", "installation"), ("Attacchi", "installation"),
                      ("Msure", "dimensions"), ("Missure", "dimensions"), ("Marche", None), ("Stila", None),
                      ("Largezza", "width")]:
        if canonical(lab) != want:
            f.append(f"{lab!r}: want {want}, got {canonical(lab)}")
    from utils.spec_rules import typo_of
    if typo_of("Misure") is not None or typo_match("msure") != ("dimensions", "misure") \
            or typo_of("Materiale") is not None or typo_of("weight") is not None or typo_of("Msure") is None:
        f.append("typo rule wrong")
    if junk_reason("TOT", "43,25 \u20ac (IVA incl.)") != "price_promo" or junk_reason("Peso", "12 kg"):
        f.append("total price line rule wrong")
    nd = "Persona responsabile nell'UE : Questa informazione non \u00e8 attualmente disponibile sul nostro sito."
    if eu_contact_state(nd) != "unavailable" or eu_contact_state("Operatore economico: ACME Srl, Milano") != "contact" \
            or eu_contact_state("niente") != "":
        f.append("eu_contact_state wrong")
    return f


def t14(page, dropped, html, url):
    """Manufacturer block = company name + postal address under a matching heading."""
    from utils.html_main import make_soup, sections_under
    from utils.spec_rules import MANUFACTURER_HEADING_RE, is_manufacturer_block
    f = []
    cases = [("FIBROTUBI SRL VIA PROVINCIALE SUD 5 42011, BAGNOLO IN PIANO Italia", True),
             ("SC APLAST WOOD INDUSTRY SRL SAT CEPTURA DE JOS 077005, PRAHOVA Romania", True),
             ("OBI Group Sourcing GmbH Albert-Einstein-Stra\u00dfe 7-9 42929 Wermelskirchen DE", True),
             ("ante-holz Polska sp\u00f3\u0142ka z o.o. Sp\u00f3\u0142dzielcza 2 67-120 Ko\u017cuch\u00f3w PL", True),
             ("SEMPLE ELEKTRIK SRL, azienda italiana con sede a Pomezia (RM)", True),
             ("- - - - - IT mail@mail.com EAN(s): [ 8015345080435 ]", False),     # empty placeholder block
             ("Produttore: Vanit\u00e0&Casa Modello: Gamma Rectangular", False),    # plain spec label
             ("ACME SRL garantisce dal 2023 la qualit\u00e0", False),               # name, no address
             ("Leroy Merlin Persona responsabile nell'UE : EBANISTERIA MARELLI SRL Via Lombardia n. 100, "
              "22063 Cant\u00f9, Italia info@x.it", False),                           # the EU person line itself
             ("ARTEGRONDA SAS VIA GALILEO GALILEI 10 25010, SAN ZENO Italia Persona responsabile nell'UE : "
              "Questa informazione non \u00e8 attualmente disponibile", True)]       # manufacturer, then the EU line
    for text, want in cases:
        if is_manufacturer_block(text) != want:
            f.append(f"is_manufacturer_block({text[:40]!r}) want {want}")
    doc = ("<html><body><h2>Dati del produttore</h2><div>ELBI SPA VIA BUCCIA 9 35010, LIMENA Italia</div>"
           "<h2>Altro</h2><div>ACME SRL Via Roma 1</div></body></html>")
    got = [(h, is_manufacturer_block(b)) for h, b in sections_under(make_soup(doc), MANUFACTURER_HEADING_RE)]
    if got != [("Dati del produttore", True)]:
        f.append(f"sections_under: {got}")
    return f


TESTS: List[Tuple[int, str, Callable]] = [
    (1, "boleco_diadema", t1), (2, "tecnomat_raccord", t2), (3, "leroy_imballato", t3),
    (4, "jysk_lycksele", t4), (5, "mondo_alice", t5), (6, "dwelli_alape", t6),
    (7, "shopbagno_box", t7), (8, "boleco_giftcard", t8), (9, "jysk_lycksele", t9),
    (10, "boleco_diadema", t10), (11, "boleco_diadema", t11),
    (12, "boleco_diadema", t12),
    (13, "boleco_diadema", t13),
    (14, "boleco_diadema", t14),
]


def main() -> int:
    refresh = "--refresh" in sys.argv
    pages = {pid: (site, frag) for pid, site, frag in PAGES}
    passed = 0
    for n, pid, check in TESTS:
        html, url = fixture(pid, *pages[pid], refresh)
        page, dropped = extract_page(html, url)
        fails = check(page, dropped, html, url)
        status = "PASS" if not fails else "FAIL"
        passed += not fails
        print(f"{status} {n} {pid}" + ("" if not fails else ": " + "; ".join(fails)))
    print(f"\n{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
