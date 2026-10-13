# Agent guide: spec extraction from saved product pages

Working dir: `~/Desktop/deepowl/git/exploration` (conda env `exploration`).

**Ground rules:**
- **Offline only.** Read `discovery_merged/sites/*/products/*.html*` and fetch nothing.
- **Surgical patches.** Don't rewrite existing files. Put new code in `spec_extract.py`.
- **No domain-specific rules.** Every rule must be generic (markup patterns, label vocabulary), never `if site == …`.
- **Don't delete or modify** any `discovery_round_final*` folder.

## Goal

For each saved product page, find every product attribute the page **states**, and record **where** it was found. Per retailer this gives the study's main measure, *stated vs. structured*: how much of the datasheet is on the page at all, and how much of it is machine-readable.

Each attribute can come from one of four layers:

| Layer | Source | Machine-readable? |
|---|---|---|
| `L1_schema` | schema.org JSON-LD or microdata (Product, Offer, additionalProperty) | yes |
| `L2_block` | spec blocks: `<dl>` dt/dd, 2-cell `<tr>`, label/value `<div>` pairs | semi |
| `L3_lines` | "Label: value" lines in the description or bullet lists | semi |
| `L4_prose` | free text ("struttura in MDF idrorepellente…") | no (**phase 2, see below**) |

## Findings from the smoke test (`smoke_specs.py`), to design around

| Site | Finding | What it means for the extractor |
|---|---|---|
| Tecnomat | specs are `div.table-features__label` + `div.table-features__value` pairs, inside `additional-attributes-wrapper … technical-features-tab` | L2 must read **label/value div pairs**, generically (see below) |
| ShopBagno | the page is a real 490 KB Shopify page, but smoke reported **0 characters of text** | that's a bug in smoke's noise filter, not a missing page. Debug the text extraction on this site first (step 1) |
| Mondo Convenienza | no spec blocks; its GraphQL product query is empty; every "label" was a product name | specs are prose only, so it falls to L4. The product-name labels must be rejected by the junk filter |
| Deghi | only "Codice" plus price lines | thin datasheet; any remaining specs are in prose |
| Leroy Merlin | "Prodotto imballato: larghezza/altezza/profondità/peso" | these describe the **packed box**. Map them to `packaged_*`, never to product dimensions |
| Dwelli | German duplicates ("Lieferung", "Marke") | merge by canonical attribute, don't count twice |
| IKEA | labels end in a colon ("Struttura:", "Tessuto:") | normalise labels: strip a trailing colon and extra whitespace |
| Boleco | 2 gift cards in the sample | exclude non-physical items (step 5) |
| KVStore | only 3 pages saved, but they're rich | a product-detection problem in `product_sample.py` (no schema.org on its pages); **out of scope here**, list it in the report |
| Paramashop | a pharmacy shop with no spec keywords | the owner decides whether it stays in scope. Run it anyway, flag it in the report |
| All sites | "attribute-like" background responses are cookie lists, chat widgets, captcha, Trustpilot | **ignore the `*_xhr.json` files** for specs |

## Step 1: main product area + text (fixes the ShopBagno bug)

Write `main_area(soup) -> Tag`.

1. Remove `script`, `style`, `noscript`, `svg` and `iframe` elements.
2. **Don't remove elements by class names containing "header", "search" and similar.** That is what emptied ShopBagno: a wrapper class matched the noise filter. Instead, remove only these whole elements: `<header>`, `<footer>`, `<nav>`, `<aside>`, and any element whose `role` is navigation, banner or contentinfo.
3. Candidate containers are elements whose class or id contains any of: `description, product-info, product-detail, product__, product-main, scheda, caratteristic, specific, features, technical, attributes, data-sheet, tab-content`.
4. If there are no candidates, use the smallest ancestor of the product title that holds at least 60% of the remaining visible text. The product title is `h1`, or failing that `og:title` matched to a heading.
5. Return the union of the candidates, or the fallback ancestor, and record which method was used.

**Check:** ShopBagno's main area must contain more than 500 characters of text on at least 20 of its 25 pages.

## Step 2: L2 spec blocks (generic)

Inside the main area:
- **`<dl>`:** each `dt` with the `dd` that follows it is one pair, read pair by pair, never the whole list at once.
- **`<tr>`:** exactly two cells (`th`/`td`) make one pair.
- **Label/value div pairs:** an element whose class contains `label|name|key|title|attribute-label` and whose **next sibling** (or the sibling inside the same parent) has a class containing `value|data|attribute-value`. This covers Tecnomat's `table-features__label` / `table-features__value` and similar themes. Don't key on the Tecnomat class names.

## Step 3: L3 "Label: value" lines

Inside the main area:
1. Turn `br`, `p`, `li`, `div`, `dt`, `dd`, `tr` and `h*` boundaries into line breaks, then take the text, joining inline elements with spaces so that `<strong>Materiale:</strong> Nobilitato` stays on one line.
2. A line is a pair if it matches `^Label: value$`, where the label is 2–40 characters, has at most 5 words and starts with an uppercase letter.

## Step 4: junk filter (applies to L2 and L3)

Drop a pair if **any** of these holds:
1. **Template:** the same (label, value) appears on 80% or more of the site's pages (keep this rule from smoke).
2. **Price or promo:** the label matches `prezzo|price|prima era|sconto|risparmi|offerta|iva`, or the value is mainly a currency amount (`€`, `EUR`, `\d+[.,]\d{2}\s*€`).
3. **Shipping or service:** the label matches `spedizion|consegna|delivery|lieferung|tempistic|tempo di consegna|disponibil|reso|resi|pagament|assistenza|lunedì|orari|elaboriamo`.
4. **Interface text:** the label matches `^back to$|recension|review|nota$|esaurito|aggiungi|carrello|wishlist|condividi`.
5. **Product name posing as a label:** the label contains a number followed by a unit (`\d+\s*(cm|mm|lt|l|kg)\b|\d+x\d+`), or it has more than 5 words, or it matches the page's own title or a link text in the main area.
6. **Internal codes with no meaning** (`codice padre`, `id`, `title` with the value `default title`). Keep real identifiers (SKU, EAN, MPN, product code); they map to `identifier`.

Log every dropped pair with its rule to `spec_dropped.jsonl`, so the filters can be audited.

## Step 5: exclusions

Exclude a page from scoring, but keep it in the logs with `excluded_reason`, if:
- its title or URL matches `gift.?card|buono regalo|carta regalo|e-?gift`, or
- it has no price and no SKU in L1 and fewer than 2 pairs in L2+L3 (probably not a real product).

## Step 6: canonical attributes

Map raw labels to canonical attributes, matching case-insensitively on the normalised label. Synonyms cover IT, EN and DE. Labels that match nothing are kept as `other:<label>` and listed in the report.

| Canonical | Label synonyms (regex fragments) |
|---|---|
| `brand` | marca, marchio, brand, produttore, marke, hersteller |
| `series` | serie, linea, collezione, modello, model, collection |
| `identifier` | sku, ean, ean13, gtin, mpn, codice( prodotto\| articolo)?, cod\. articolo, art\. n, riferimento |
| `material` | materiale( principale)?, struttura, tessuto, composizione, material, materiali |
| `finish` | finitura, effetto, superficie, finish, oberfläche |
| `colour` | colore( principale)?, colori, colour, color, farbe |
| `width` | larghezza( \(in cm\)\| cm\| \(cm\))?, width, breite |
| `height` | altezza(…same units)?, height, höhe |
| `depth` | profondità(…)?, depth, tiefe |
| `length` | lunghezza, length, länge |
| `dimensions` | dimensioni, dimensione, misure, formato, ingombro, dimensions, maße (a combined value such as "80x110 cm") |
| `thickness` | spessore, spessori( fianchi\| frontali)?, thickness |
| `weight` | peso( netto)?, weight, gewicht |
| `packaged_dims` | prodotto imballato: (larghezza\|altezza\|profondità), imballo, confezione (dims) |
| `packaged_weight` | prodotto imballato: peso, peso imballo |
| `installation` | installazione, montaggio, fissaggi?( compresi)?, tipo attacco, posa, mobile sospeso |
| `shape_style` | forma, stile, tipologia, tipo, design |
| `warranty` | garanzia, warranty, garantie |
| `certification` | certificazion[ei], classe di resistenza, norma, ce |
| `origin` | prodotto in, made in, paese di origine, herkunft |
| `electrical` | potenza( watt)?, lumen, protezione ip, ip, voltaggio, tensione, watt |
| `water` | portata, cartuccia, getti, risparmio acqua, pressione, sifone |
| `care` | manutenzione, pulizia, cura |

Merge duplicates: one value per canonical attribute per page, preferring L1 over L2 over L3. Keep every raw source in a list.

## Step 7: outputs

**Per page:** `<site_dir>/specs/<page>.json`

```json
{"url": "...", "excluded_reason": null, "main_area_method": "candidates|title_ancestor",
 "attributes": {"material": {"value": "Nobilitato", "layer": "L3_lines",
                             "sources": [{"layer": "L3_lines", "label": "Materiale"}]}},
 "other_labels": {"Approfondimento tecnico": "…"},
 "counts": {"L1": 5, "L2": 3, "L3": 6, "dropped": 4}}
```

**Per site and attribute:** `spec_coverage.csv`

`site, pages_scored, attribute, stated_rate, l1_rate, l2_rate, l3_rate`

- `stated_rate` = share of pages where the attribute was found in any layer.
- `l1_rate` = share of pages where it was found in schema.org (the structured share).

**Per site summary:** `spec_summary.csv`

`site, pages_scored, excluded, median_attributes, median_L1, median_L2, median_L3, top_other_labels`

## Step 8: tests (`test_spec_extract.py`, on saved fixtures)

| # | Page | Must pass |
|---|---|---|
| 1 | Boleco `…1799-pensile-bagno-diadema-cm-30x75` | `material = Nobilitato` (L3); `thickness` present; `colour` = PERLA SPATOLATO (L2); no "Codice Padre" |
| 2 | any Tecnomat page | at least 5 L2 attributes from the div pairs |
| 3 | any Leroy Merlin page with "Prodotto imballato" | `packaged_dims` set; `width/height/depth` **not** taken from the packed-box labels |
| 4 | any Jysk page | no price or "Prima era" pair survives |
| 5 | any Mondo Convenienza page | no product-name labels survive; prose-only is allowed (0 L2/L3 is acceptable) |
| 6 | any Dwelli page with "Lieferung"/"Marke" | shipping dropped; `brand` counted once |
| 7 | any ShopBagno page | main area has more than 500 characters of text |
| 8 | a Boleco gift card | `excluded_reason` set |

## Phase 2 (don't run without the owner's go-ahead): L4 prose

Only after the layer 1–3 tests pass and the owner has reviewed `spec_summary.csv`:
- Send an LLM the main-area text (without the pairs already found) and ask for the canonical attributes as JSON, with the exact supporting phrase for each.
- About 25 pages × 24 sites, roughly 600 calls.
- Before running, report the expected token count and cost.

## Report back (one message)

1. A per-site table from `spec_summary.csv`, plus the 3 strongest and 3 weakest sites by median attributes.
2. The **stated vs. structured** headline per attribute: the average `stated_rate` and `l1_rate` across sites for material, dimensions (width/height/depth/dimensions), colour, finish, weight and identifier.
3. Test results 1–8.
4. The 15 most frequent `other:` labels, which are candidates for new synonyms.
5. The 10 most frequent dropped labels and which rule dropped them, so the filters can be audited.
6. Anything skipped, plus the scope notes for KVStore and Paramashop.


# to rerun
Rerun the spec extraction with these changes. All rules must be generic.

Label validity, for L2 as well as L3: the label must start with a letter, be 2–40 characters long, have at most 5 words, and not be all uppercase codes (^[A-Z0-9-]{6,}$).
Review blocks: don't parse anything inside elements whose class or id contains review|recension|rating|testimonial, unless the element contains the h1.
Junk labels, add these: ^enabled$|^attivato$|^abilitato$|stato prodotto|visibilità|venditore|seller|sconti|promozioni|attenzione|livraison.
Synonyms:
origin: paese di fabbricazione
care: candeggio|asciugatrice|stiratura|lavaggio|istruzioni
electrical: kelvin|classe energetica|classe
brand: marque
finish: finition|ausführung
material: cristallo|vetro|legno|matériau
colour: ral|couleur
installation: assemblato|kit fissaggio|numero di pezzi
New attributes:
opening = apertura|chiusura|sistema di apertura|maniglia|anta
compliance_eu_contact = persona responsabile|operatore economico|fabbricante|manufacturer. Report it separately from the specs.
Distortions from the previous review, apply them if not already done:
schema.org value checks: weight > 0, brand ≠ the retailer's name (flag it as own_brand), dimension values contain a number
identifier split: gtin, mpn and sku become separate attributes
package sections: a label under a heading containing pacc|pacchi|imballo|confezione|collo maps to packaged_*
Deghi: before the rerun, print the raw L3 lines from 2 Deghi pages and report what shape they have.
