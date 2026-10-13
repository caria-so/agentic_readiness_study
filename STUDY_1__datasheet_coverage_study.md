# Datasheet coverage study — method and manual checks

Oct 8, 2026 · @Federico

## Objective

The study measures how much of a product's datasheet Italian home and bathroom retailers put on their product pages, and how much of it software can read without guessing.

- **The question.** For each attribute (material, colour, dimensions, finish, installation and so on), on what share of a retailer's product pages is it:
  1. stated anywhere on the page;
  2. stated in a structure a simple parser can read (a spec table, a "Label: value" line);
  3. published as schema.org markup, the machine-readable data that search engines and shopping agents read first?
- **Why it matters.** A person can read a sentence such as "struttura in MDF idrorepellente". Software reliably reads only structured data. The gap between "stated" and "machine-readable" is information the retailer already has but software cannot use.
- **Scope.** 27 retailers were listed and 23 are measured. 25 product pages are sampled per retailer. Only Italian-language pages count.
- **Not in scope.** Whether a value is correct, whether a retailer complies with the law, and how well the descriptions are written.

## Key terms: the four layers

Every attribute found on a page is assigned to the first layer where it appears. The layer says how hard it is for software to get at it.

| Layer | Where the information sits | Who can read it | How we extract it |
| --- | --- | --- | --- |
| L1 machine-readable | schema.org markup in the page code (JSON-LD or microdata) | any software, directly | read the markup |
| L2 spec block | a spec table, a definition list, or label/value boxes | a parser that knows the page layout | read label/value pairs |
| L3 label lines | lines such as "Materiale: Nobilitato" inside the description | a simple text parser | match `Label: value` |
| L4 prose only | ordinary sentences, or the product title | a person, or an LLM | local LLM with a verbatim quote check |

Three measures follow from the layers:

- **Stated** = found in any layer, L1 to L4.
- **Found by rules** = found in L1, L2 or L3, without an LLM.
- **Machine-readable** = found in L1 only.

**Example 1, Boleco wall cabinet** ([page](https://www.boleco.it/pensili-e-colonne/1799-pensile-bagno-diadema-cm-30x75.html)):

| Attribute | What the page shows | Layer |
| --- | --- | --- |
| sku, price, availability | schema.org microdata in the code | L1 |
| colour | spec list: "Colori: PERLA SPATOLATO" | L2 |
| material | description line: "Materiale: Nobilitato" | L3 |
| thickness | description line: "Spessori fianchi: 18mm" | L3 |

Material is clearly stated, but not machine-readable: Boleco publishes no material in schema.org.

**Example 2, Mondo Convenienza sofa** ([page](https://www.mondoconv.it/venere-vtnh.html)): the only size information is the sentence "VENERE DIVANO LETTO ANGOLARE CON PENISOLA SINISTRA L.290 P.170 H.99". Width 290, depth 170 and height 99 are stated, but only in L4. Without the LLM layer this site would look as if it published no dimensions at all.

## The pipeline, step by step

Each step produces files the next step reads. All of them sit under `discovery_merged/sites/<site>/`.

1. **Retailer list.** 27 retailers are listed in the files under `sites_leads/`, each with the URL of its Italian homepage.
2. **Discovery** (`a_discovery_pipeline.py`).
   - The homepage is opened in a real, visible Chrome with a saved profile. Robots.txt is respected, with at least 1 second between requests to the same site.
   - The sitemaps are found and walked.
   - Output: `index.html`, `robots.txt`, `sitemap.json`.
   - Crawl dates: 30 September to 3 October 2026.
3. **Catalogue size.**
   - If a sitemap file is named after products (`product`, `prodott`, `prod-`), every URL in it counts as a product.
   - Otherwise, a sample of sitemap pages is opened, the share that are product pages is measured, and that share is multiplied by the number of URLs.
   - Output: `product_total_available` in `sitemap.json`.
4. **Product-page sample** (`b_product_sample.py`).
   - 25 product URLs per retailer are fetched and saved.
   - Each page must pass the product-page test, which rules out category pages (section 4).
   - Output: `products/*.html` (some files end in `.html.html`) and `product_audit.json`.
5. **Smoke test** (`smoke_specs.py`). For every retailer it checks that the saved pages are real: no bot wall, a product title, enough text. It also shows where the specs sit (tables, label lines, prose).
6. **Rule-based extraction, L1 to L3** (`c_spec_extract.py`). For each saved page:
   1. find the main product area;
   2. read the schema.org data (L1), the spec blocks (L2) and the label lines (L3);
   3. drop junk pairs (prices, shipping, menus);
   4. map each label to a canonical attribute ("Materiale", "Material" and "Matériau" all become `material`).

   Output: `specs/*.json` per page, and one `spec_coverage.csv` in `discovery_merged/` for all retailers.
7. **LLM extraction, L4** (`d_llm_prose.py`).
   - A local model (qwen2.5:32b via Ollama, on the Mac's GPU) reads the product title and product text.
   - It returns every attribute it finds, each with an exact quote from the page.
   - Code then checks every quote against the page text, and counts only attributes that L1 to L3 missed.
   - Output: `specs_llm/*.json`, and `spec_coverage_l4.csv` in the project root (it includes KVStore).
8. **Quality checks** (`e_check.py`).
   - 50 random LLM values are drawn for hand marking.
   - Quotes that repeat across many pages of one site are listed, to catch template text.
9. **Aggregation.** Done by hand for now: no script computes it, and KVStore must first be dropped from `spec_coverage_l4.csv`. For each attribute, the share of pages is averaged across the 23 measured retailers, giving "stated", "found by rules" and "machine-readable".

## Every hardcoded rule, and why it exists

Every rule below was added because a real page broke the previous version. None of them names a retailer; each matches a pattern that any site could show. The "Example" column is the case that triggered the rule, so it can be checked.

### Access and discovery

| Rule | Why | Example |
| --- | --- | --- |
| Fetch pages with a real, visible Chrome, not a headless one | Many retailers block headless browsers but let a normal browser in, so a headless crawl mistakes "blocked" for "no data" | Amazon, ManoMano, Deghi, Bagnity, KVStore and Idrocentro returned 403 to the headless crawler and 200 to real Chrome in round 1 |
| Recognise bot walls (`captcha-delivery`, `cf-chl`, "Just a moment") and label the site `challenge` | A wall page is not the retailer's content, so it must never be scored | ManoMano's homepage is a 28 KB Cloudflare page; it is excluded and needs a manual visit |
| Fetch walled sitemaps through a real browser tab | The plain HTTP client gets the wall; only a tab passes the check | Tecnomat's `/pub/media/sitemap/sitemap.xml` |
| Decompress `.xml.gz` sitemaps before reading them | Otherwise a compressed sitemap is rejected as "not XML" | Paramashop's sitemap is gzipped |
| Keep only Italian sitemap files: bare `it`, or region `it-IT` | Some robots.txt files list sitemaps for every country and language, which multiplies the counts | IKEA listed `cat-de-DE`, `cat-ar-AE` and `cat-it-CH`; Mondialshop had `1_de_0`; Dwelli had `sitemap_en_…` |
| A sitemap file whose path says `product`, `prodott` or `prod-` counts as all products, **unless** it says taxonomy, tag or category | The file name is the most reliable signal, but tag listings also contain the word "product" | Bagnity `sitemap_it_product.xml` counts; Termoidraulica `sitemap-taxonomy-product_tag.xml` contains tag pages and does not |
| Without a product sitemap, estimate products = sampled product share × number of URLs | Opening every URL would take days; a sample with a confidence interval is enough for a catalogue size | Boleco: share of product pages in a sample, times 1,381 URLs |

### Is this a product page?

| Rule | Why | Example |
| --- | --- | --- |
| A page counts as a product page only if one Product in its markup describes **this** page: its url matches the page, or it is the only top-level Product. `og:type=product` also counts | Category grids mark every tile as a Product, so "has Product markup" is true for category pages too | Boleco category `/5-tutto-per-le-docce` carries 20 Product tiles |
| URLs shaped `/<number>-<slug>` with no `.html` are vetoed as category pages (PrestaShop sites only) | A category with a single tile looks exactly like a product page in the markup | Boleco `/it/859-appendini-bagno-a-parete` shows 1 tile |
| Exclude gift cards | They are "products" with no datasheet to measure | Boleco `gift-card_2473-gift-card-euro-199` |

### Rule-based extraction (L1 to L3)

| Rule | Why | Example |
| --- | --- | --- |
| The product area is picked by a class/id pattern for description containers (`description`, `product-info`, `scheda`, `caratteristiche`…) after removing `header`, `nav`, `footer` and `aside`; review blocks and package sections are also found by class/id | A class-name filter ("header", "search") once removed whole pages. Picking containers by class is safe; removing elements by class is not | ShopBagno's 490 KB product pages came out with 0 characters of text |
| L2 reads label/value `div` pairs, not only tables and definition lists | Some themes build their spec table out of divs | Tecnomat `table-features__label` / `table-features__value` |
| L3 reads `Label: value` lines (a label of 2 to 40 characters, at most 5 words, starting uppercase) | Many shops write specs as lines inside the description | Boleco "Materiale: Nobilitato" |
| A label/value pair found on 80% or more of a site's pages is template text | Shipping notes and footers repeat on every page | removed shipping and service lines |
| When a label repeats more than 3 times on one page, keep the **first** occurrence | Variant tables repeat labels; dropping them all once deleted real specs | Paramashop lost material (0.92 → 0) and Jo-Bagno dimensions (0.96 → 0.32) until this was fixed |
| Drop prices and promotions | They are not specs | Jysk "Prezzo più basso ultimi 30 giorni"; Tecnomat "TOT: 43,25 € (IVA incl.)" |
| Drop shipping and service lines | Not specs | Dwelli "Lieferung"; Arredobagno360 "Tempistiche indicative" |
| Drop interface text, reviews and form fields | Not specs | IKEA reviewer names; Arredobagno360 "Back to"; Bertani "Nome" and "Email" |
| Drop labels that are really product names or codes (a number plus a unit, or more than 5 words), and repeated codes like `MOBILE-DESIO-180-GRIGIO` | Recommended products rendered as "Name: price" look like label lines | every Mondo Convenienza "label" in the first run was a product name |
| Drop internal codes and flags | Not specs | Boleco "Codice Padre"; Termoidraulica "Enabled" |
| Match against the synonym list **before** the validity rules | Otherwise real all-caps labels were dropped as codes | Iperceramica "COLORE", "TIPOLOGIA" |
| A label under a packaging heading maps to `packaged_*`, never to product size | The box is not the product | IKEA "Pacchi" section; Leroy Merlin "Prodotto imballato: peso" |
| schema.org weight must be above 0, a weight or dimension identical on 40% or more of a site's pages counts as a platform default, and so does any other schema.org value identical on 80% or more | Shop platforms fill these fields automatically | 34 default weights removed (Arredobagno360 12, Bagnodistile 11, ShopBagno 11) and 24 default brands (Paramashop) |
| A brand equal to the retailer's name is reported as `brand_is_retailer_name`, not as brand | The shop's name in the brand field says nothing about the product | InBagno 92%, Termoidraulica 100% |
| A GTIN counts only if its check digit is valid; an MPN that copies the GTIN or the SKU is flagged | Platforms copy one identifier into another field | Arcshop MPN = GTIN on 25 of 25 pages; MPN = SKU at 8 retailers |
| An EU-contact field reading "not available" does not count as a contact | It states the absence of the information | Tecnomat "Persona responsabile nell'UE: Questa informazione non è attualmente disponibile" |
| Allow one-letter typos on long labels | Real labels contain typos | Jo-Bagno "Msure", "Missure" |

### LLM extraction (L4)

| Rule | Why | Example |
| --- | --- | --- |
| The model returns a value **plus a verbatim quote**; code keeps the value only if the quote is in the page text and contains the value (numbers checked one by one) | It makes invention detectable without re-reading every page | the model wrote "nero opaco" when the page says only "nero"; rejected |
| Ask for **all** attributes, pass the already-known values as context, count only new ones | When colour was not asked for, the model pushed colours into "finish" | Boleco Happy shower: a colour list under finish |
| State the Italian size shorthand: L = larghezza (width), P = profondità (depth), H or A = altezza (height) | The model read "L." as length | Mondo Convenienza "L.290 P.170 H.99"; Boleco "HxPxL 220x17,5x20,5" |
| Reject sizes and weights whose quote mentions packaging | Shipping data is not product data | Leroy Merlin weight 0.1 kg from "Prodotto imballato: peso" |
| Reject the statutory warranty | Every product has it by law, so it says nothing about the product | Leroy Merlin "garanzia legale di conformità … 2 anni" |
| Reject a brand that contains the retailer's name or equals the series | Not the manufacturer | Jysk "JYSK A/S"; Boleco "Jane" (a model name) |
| Use `og:title` as the product title (the shop-name suffix, such as "JYSK", stays; it does not affect the quote check) | Some h1s are interface headings | Mondo Convenienza h1 "CONFIGURA IL TUO DIVANO" |
| A quote repeated on 50% or more of a site's pages is not counted in spec\_coverage\_l4.csv (the per-page results and the review sheet still list it) | Template text, not a product fact | Paramashop "garanzia: 2 anni" on 24 of 24 pages |
| Ignore values that belong to included parts or compatible products (added after the full run, **not yet** in the current numbers) | The main remaining error type | ShopBagno "20x20" is the shower head; Ceramikeshop "1,5 m" is the hose |
| An origin value counts only if its quote says where the product was made ("made in", "realizzato in", "prodotto in", "paese di origine") | A company's address or registered office is not the country of manufacture | Tecnomat: all 10 origin values were manufacturer addresses ("FIBROTUBI SRL VIA PROVINCIALE SUD 5 … BAGNOLO IN PIANO"); OBI: 1 ("OBI Group Sourcing GmbH …"). Applied to the saved results, no new model calls |

## How to check the results by hand

Each check takes 5 to 15 minutes, needs only a browser (and a terminal for check 7), and states the expected result. A check that disagrees with the expectation points to a bug, and the rule tables in section 4 say which rule to look at.

**Where the numbers live**, per retailer folder `discovery_merged/sites/<site>/`:

| File | Contains |
| --- | --- |
| `sitemap.json` | catalogue size, the sitemap files read, the ones that failed and why |
| `products/*.html (some files end in .html.html)` | the 25 saved product pages |
| `specs/<page>.json` | the attributes found by rules, each with its layer and raw label |
| `specs_llm/<page>.json` | the attributes the LLM added, each with its quote |
| `spec_coverage_l4.csv` (project root) | shares per retailer and attribute, including the LLM layer; it includes KVStore, which the headline drops |
| spec\_coverage.csv (in discovery\_merged/) | shares per retailer and attribute from the rules only (L1–L3) |

### Check 1: product page or category page?

1. Open [boleco.it/5-tutto-per-le-docce](https://www.boleco.it/5-tutto-per-le-docce). It is a grid of many showers.
2. Open [Boleco pensile Diadema](https://www.boleco.it/pensili-e-colonne/1799-pensile-bagno-diadema-cm-30x75.html). It is one product, with a price and a cart button.
3. **Expected:** the first is never among the sampled pages; the second can be. Grids must not appear in any retailer's `products/` folder.

### Check 2: an attribute found by rules (L3)

1. On the Diadema page, use the browser's find (Cmd+F) to search for "Materiale:".
2. **Expected:** the line reads "Materiale: Nobilitato", and in `specs/` the page's file shows `material = Nobilitato` with layer `L3_lines`.
3. Open the page source (View → Developer → View Source) and search for "material". **Expected:** no hit in the schema.org data, so material is not machine-readable here.

### Check 3: a spec block (L2)

1. Open any Tecnomat product page and find the technical features tab ("Caratteristiche tecniche").
2. **Expected:** each label with its value next to it appears in the page's `specs/` file with layer `L2_block`.

### Check 4: machine-readable or not (L1)

1. Paste a product URL into [validator.schema.org](https://validator.schema.org/).
2. Look inside the Product item for `material`, `color`, `width`, `height`, `depth`, `weight`, `gtin`.
3. **Expected:** IKEA, Bagnity, Jysk and OBI show some of these. Boleco, Mondo Convenienza and most others show only name, sku, price and availability. This is the "machine-readable" column of the results.

### Check 5: an attribute only in prose (L4)

1. Open [Mondo Convenienza Venere](https://www.mondoconv.it/venere-vtnh.html) and search for "L.290".
2. **Expected:** the sentence "… L.290 P.170 H.99". The page's `specs_llm/` file shows width = L.290, depth = P.170, height = H.99, each quoting that sentence. Its `specs/` file shows no dimensions.

### Check 6: packaging is not product size

1. Open a Leroy Merlin page with a "Prodotto imballato" section (for example the one in `specs_llm/` for `lav-tess-pacch-c-bin-cass-fino-l-200`).
2. **Expected:** "Prodotto imballato: peso (in kg) 0.1" is recorded as `packaged_weight`, never as `weight`.

### Check 7: catalogue size

1. Download the Bagnity product sitemap from `https://www.bagnity.com/sitemap_it/sitemap_it_product.xml` and count its URLs: `curl -s <url> | grep -c '<loc>'`.
2. **Expected:** close to the 2,649 products in `sitemap.json`. Differences of a few percent are normal, because the catalogue changes daily.

### Check 8: precision of the LLM values

1. Open `l4_precision_sample`\_20261008\_164951.csv, the independent second sample. (l4\_precision\_sample\_20261008.csv repeats 45 of the first sample's 50 rows.) It holds 50 LLM values drawn at random.
2. For each row, open the URL and search for the quote.
3. Mark `y` only if all three hold:
   - the value appears in the quote;
   - it describes the product as a whole, not an included part or a compatible product;
   - it sits under the right attribute.
4. Worked verdicts, taken from the first sample (none of these rows is in the independent file):

| Row | Verdict | Why |
| --- | --- | --- |
| Arcshop, colour = nero, "rubinetteria Sunset nero per piletta click clack" | right | value in quote, describes the product |
| Bagnodistile, origin = Spagna, "Progettati e realizzati in Spagna" | right | value in quote |
| ShopBagno, dimensions = 20x20, "Colonna Doccia … Soffione 20x20" | wrong | 20x20 is the shower head, not the column |
| Ceramikeshop, length = 1,5 m, "tubo doccia … lunghezza di 1,5 m" | wrong | it is the hose length |
| Jo-Bagno, series = Sfera 54, "compatibile con serie Sfera 54" | wrong | the toilet the seat fits, not the seat's series |
| Mondo Convenienza, care = "Facile da pulire" | debatable | a claim, not care instructions |

## Results so far and known limitations

Retailers state most of the datasheet, but publish almost none of it in machine-readable form. Shares are the average across 23 retailers, 25 pages each.

| Attribute | Stated (L1–L4) | Found by rules (L1–L3) | Machine-readable (L1) |
| --- | --- | --- | --- |
| Material | 75% | 47% | 7.5% |
| Colour | 62% | 39% | 11% |
| Any dimension | at least 70% | n/a | about 10% |

- **"Any dimension"** takes, per retailer, the highest of width, height, depth, length and overall dimensions, so it is a lower bound.
- **`series` and `brand` are left out of the headline.** They identify a product rather than describe it, and the LLM often takes them straight from the title.

**Precision of the LLM layer:** 82% strict and 88% counting the debatable cases, over two independent draws of 50 values each (82%/90% and 82%/86%; about ±8 points), with every uncertain row settled by a person opening the page. Nearly every error gives a component's or compatible product's value to the product itself. At this rate, "stated" is overstated by about 3 points; the machine-readable column does not use the LLM and is unaffected.

**Excluded retailers:**

| Retailer | Reason |
| --- | --- |
| Amazon.it | marketplace, no public sitemap |
| ManoMano | Cloudflare challenge, also for a real browser; manual visit only |
| Idrocentro | publishes only PDF catalogues, no product pages |
| KVStore | only 3 product pages detected (no schema.org markup); too few to report |

**Known limitations:**

- **Series variants cluster in the sample.** 25 random pages can include many variants of one series, so a retailer's share can partly reflect one series (8 of Bagnodistile's 25 pages are the Bequia series).
- **"Stated" is a lower bound.** The LLM misses some values, and recall was not measured.
- **The component rule came after the full run.** The ShopBagno and Ceramikeshop error type is not removed from the current numbers.
- **Boleco's dimensions include a filter range.** "Misure Pensili e Colonne: 20-30cm, 31-100cm" is a filter bucket, not the product's size; it may inflate Boleco's dimensions share.
- **Values are not normalised** ("L.290" against "290 cm"). This is fine for measuring presence, not for comparing values.
- **Machine-readable means present, not checked.** Apart from GTIN check digits, schema.org values were not tested for correctness.

**EU product-safety information (GPSR).** Only three retailers show any of it on the page. The manufacturer block is counted separately from the EU responsible person, because a block that names the manufacturer does not name an EU contact. Presence only; no judgement on compliance.

| Retailer | EU responsible person shown | "Not available" shown | Manufacturer block |
| --- | --- | --- | --- |
| Leroy Merlin | 18 of 25 pages | 7 of 25 | 0 (the detector reports 11, but those are the EU-person line counted a second time) |
| OBI | 0 | 0 | 22 of 25 ("Dati del produttore"; one more block holds only dashes) |
| Tecnomat | 0 | 13 of 25 | 13 of 25 ("Elementi di conformità del prodotto") |

**Open items before publishing:**

- [x] EU-contact search rerun with "dati del produttore": OBI shows a manufacturer block on 23 of 25 pages (table above).
- [x] Tecnomat's origin values were manufacturer addresses; 11 such values removed (10 Tecnomat, 1 OBI). L4 now holds 1,960 values on 562 pages.
- [x] Independent second sample (l4\_precision\_sample\_20261008\_164951.csv; marked copy l4\_precision\_sample\_2\_marked.csv) checked by hand: 41 right, 7 wrong, 2 debatable. Remaining errors: compatible products (3, Jo-Bagno), package values (2, IKEA), a link label as certification (1, Tecnomat), a product name as brand (1, OBI).

* [ ] Script the headline averages (drop KVStore) so the results table is generated, not computed by hand.
* [ ] Stop the manufacturer-block detector from counting Leroy Merlin's EU-person line a second time.
* [ ] Correct the comment at the top of utils/html\_main.py, which still says the main area is "never by class name".

## Scaling to other stores and languages

The measurement design scales; the extraction rules do not. The rules in section 4 were the right way to finish this Italian study, but they would not carry to a new language or to a few hundred stores.

| Part | Scales? | Why |
| --- | --- | --- |
| The four layers, and "stated vs machine-readable" as the measure | yes | the same question applies to any store and language |
| L1 schema.org extraction | yes | the markup is the same everywhere |
| Sitemaps, robots.txt, real browser, locale filter by `xx-YY` code | yes | follows web standards, not page content |
| Statistical rules: template text repeats on 50–80% of a site's pages; keep the first repeated label | yes | they make no assumption about the language |
| LLM answers checked against verbatim quotes; hand-checked precision samples | yes | a process that works in any language |
| Synonym lists ("Materiale", "Peso", "Larghezza") | no | Italian, grown example by example |
| Junk lists (spedizione, prezzo, "Prima era") | no | every new store or language adds entries |
| Platform rules (the PrestaShop category veto) | no | one rule per platform |
| Fixes for single error patterns (IKEA package sections, Jo-Bagno compatible seats) | no | whack-a-mole |

The 82–88% precision was also measured on the same stores the rules were tuned on, so it is probably optimistic for an unseen store.

### The design that would scale

Move all language-dependent interpretation into one LLM step, and keep code only for what does not depend on language.

1. **Code collects, without interpreting:** schema.org data, spec-table and label/value pairs, and the product-area text. No label is judged at this stage.
2. **One LLM pass per page**, in any language, against the fixed attribute list. Each value comes back with its verbatim quote. The only per-language knowledge sits in the prompt: the size shorthand (L/P/H in Italian, B/T/H in German) and "describe the whole product, not its packaging, parts or compatible products".
3. **Code assigns the layer from where the quote sits on the page:**
   - inside schema.org → L1
   - in a table or label/value pair → L2
   - in a "Label: value" line → L3
   - in a sentence → L4

   This replaces the separate extractors and every synonym list.
4. **Label meanings are cached per site.** A store uses about 50–200 distinct labels. The LLM maps each one once ("Breite" → width, "Lieferzeit" → junk), and the mapping is saved, reused and can be audited.
5. **Product pages are found by URL group:** sitemap URLs are grouped by shape, about 3 pages per group are labelled by an LLM, and the label applies to the whole group. This replaces the platform rules.
6. **Quality is a process, not a rule list:**
   - for each new language or product vertical, hand-check about 50 values on stores not used for tuning;
   - accept a precision floor, for example 85%;
   - add a fix only when the same error type appears in at least 3 stores.

### What happens to the current rules

They become the test set. Every rule in section 4 came from a real page, so its example is a case the new design must get right: Boleco's category grids, IKEA's package dimensions, Mondo Convenienza's prose-only sizes, Jo-Bagno's compatible seats. A new pipeline passes when it handles those pages without any store-specific code.

This study does not need to be rebuilt: its rules are documented and its precision is reported. The design above is for applying the measure to any store, URL in, report out.
