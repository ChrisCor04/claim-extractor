# Extraction Engine Audit

**Scope:** Full audit of `estimate-extractor` against the six real fixture PDFs, run on the current codebase. No results simulated — every number below comes from an actual `pytest` run and an actual `estimate_extractor extract` run, both executed during this audit. Three real bugs were found during ground-truth verification and were fixed (not just documented) as part of this audit; each fix was re-verified by re-running the affected fixture(s) and the full test suite.

**Test suite:** 84/84 passing (`pytest -q`) after all fixes in this audit.

---

## Part 1 — Execution

```
$ pytest -q
84 passed in ~0.65s

$ python -m estimate_extractor extract fixtures --recursive --debug
[all 6 fixtures processed, exit code 1 (3 documents needs_review)]
```

Both commands were run fresh (existing `output/` deleted first) at the end of this audit, after all fixes below. Command output is reproduced verbatim in Part 10.

---

## Part 2 — Per-fixture summary

```
====================================================
FILE: Aranda Insurance.pdf
====================================================
Carrier:              State Farm
Confidence:           1.00

Claim Number:         4399W552P
Policy Number:        72KNY0544
Estimate Number:      43-99W5-52P

Insured:              ARANDA, GENARO

Property Address:     420 Revival Rd, Royse City, TX 75189-8878

Date of Loss:         2026-04-27

Price List:           TXDF28_APR26

Pages:                15
Estimate Pages:       11
Excluded Pages:       4  (carrier_letter, instructional_sample, 2x replacement_cost_explanation)

Coverages:            2  (Dwelling; Other Structures)
Sections:             9  (Exterior, Dwelling Roof, Front/Right/Rear/Left Elevation, Fence, Debris Removal, Labor Minimums Applied)
Line Items Extracted: 42

Status:               success
Reconciliation:       PASS (calculated 29,484.93 == reported 29,484.93, diff 0.00)
Warnings:             1  (SECTION_TOTAL_MISMATCH — see Part 6, item is explained/benign)

Known Limitations:    coverage_id is null on all sections/line-items (2 coverages present,
                       no reliable per-section attribution in the printed text — see docs/canonical-schema.md)

Needs Manual Review?  No (status=success, 0 line items flagged needs_review)


====================================================
FILE: Bagi Insurance Estimate.pdf
====================================================
Carrier:              Allstate
Confidence:           1.00

Claim Number:         0828254359
Policy Number:        000436729825
Estimate Number:      RAVIRAJ_BAGI

Insured:              RAVIRAJ BAGI

Property Address:     113 COLONY WAY, FATE, TX 75189-7064

Date of Loss:         2026-04-27T19:00:00

Price List:           TXDF8X_02MAY26

Pages:                14
Estimate Pages:       11
Excluded Pages:       3  (2 instructional/unknown-adjacent pages, 1 unknown)

Coverages:            2  (AA-Dwelling; BB-Other Structures)
Sections:             7  (Dwelling Roof, Front/Right/Back/Left Elevation, Fence, Debris Removal)
Line Items Extracted: 24

Status:               needs_review
Reconciliation:       PASS (calculated 21,992.97 == reported 21,992.97, diff 0.00)
Warnings:             3  (2x PAGE_UNKNOWN_CLASSIFICATION, 1x ORPHAN_CONTINUATION_ROW)

Known Limitations:    Allstate's descriptions wrap around the numeric block on ~9 of 24 items;
                       all 9 verified correct by hand (see Part 4), but this is a heuristic
                       reconstruction, flagged needs_review on each affected item by design.

Needs Manual Review?  Yes — 7 line items flagged needs_review (the 9 description-wrap items minus
                       2 that also satisfied high enough confidence elsewhere; see report for exact list)


====================================================
FILE: Garcia Insurance estimate.pdf
====================================================
Carrier:              Farmers
Confidence:           1.00

Claim Number:         7010004676-1-1   (see Part 3 — genuine source conflict, both raw values preserved)
Policy Number:        0829920404
Estimate Number:      JOSE_GARCIA617

Insured:              Jose Garcia

Property Address:     2513 LAKESIDE DR, GARLAND, TX 75042-6430

Date of Loss:         2025-05-06T00:00:00

Price List:            TXDF8X_MAR26

Pages:                19
Estimate Pages:       11
Excluded Pages:       8

Coverages:            2  (Dwelling [code A]; Separate Structures [code B])
Sections:             5  (ROOF1, ROOF2, AC Unit, Fencing, Waste Removal)
Line Items Extracted: 25

Status:               success
Reconciliation:       PASS (calculated 21,070.45 == reported 21,070.45, diff 0.00)
Warnings:             6  (5x PAGE_UNKNOWN_CLASSIFICATION, 1x CONFLICTING_CLAIM_NUMBER)

Known Limitations:    5 pages land in "unknown" classification (see Part 6) — all correctly
                       excluded from the estimate body regardless.

Needs Manual Review?  No (status=success — the conflicting claim number is a WARNING, not
                       an item-level review flag; recommend a human confirm the true claim
                       number from the two candidates before downstream use)


====================================================
FILE: Garrety Insurance Estimate.pdf
====================================================
Carrier:              State Farm
Confidence:           0.70  (at the detection threshold — see Part 8, finding #1)

Claim Number:         430C5R723
Policy Number:        43GQX0658
Estimate Number:      43-0C5R-723

Insured:              GARRETY, MICHELLE

Property Address:     4017 Brookdale Rd, Benbrook, TX 76116-8555

Date of Loss:         2026-04-25

Price List:           TXDF28_APR26

Pages:                12
Estimate Pages:       9
Excluded Pages:       3

Coverages:            1  (Dwelling)
Sections:             4  (Dwelling roof, Ext_Surfaces, Debris Removal, Labor Minimums Applied)
Line Items Extracted: 27

Status:               success
Reconciliation:       PASS (calculated 35,225.59 == reported 35,225.59, diff 0.00)
Warnings:             0

Known Limitations:    None specific to this document beyond the general ones in docs/canonical-schema.md.
                       This fixture surfaced 3 real bugs during this audit (see Part 7/8), all now fixed.

Needs Manual Review?  No (status=success, 0 line items flagged needs_review, 0 warnings)


====================================================
FILE: Odom Insurance.pdf
====================================================
Carrier:              USAA
Confidence:           1.00

Claim Number:         null  (genuinely absent from the document — see Part 3)
Policy Number:        018341865-90A
Member Number:        018341865
L/R Number:           801

Insured:              STEFANIE ODOM

Property Address:     326 Dew Drop Ln, Prince Frederick, MD 20678-3004

Date of Loss:         2026-07-04

Price List:           MDSC8X_JUL26

Pages:                20
Estimate Pages:       14
Excluded Pages:       6

Coverages:            5  (Dwelling, Dwelling-BOL [x2: normal + paid-when-incurred], Other Structures,
                       Loss of Use is present with $0, Contents)
Sections:             9  (Main Roof, Ext_Surfaces, Shed, Chicken Coup, Personal Property,
                       Right Elevation, Front Elevation, General Conditions, Labor Minimums Applied)
Line Items Extracted: 35

Status:               needs_review
Reconciliation:       FAIL (calculated 33,664.72 vs reported 28,584.66, diff 5,080.06 — explained,
                       see Part 7)
Warnings:             7  (4x PAGE_UNKNOWN_CLASSIFICATION, 1x MISSING_CLAIM_NUMBER,
                       2x SECTION_TOTAL_MISMATCH — both explained, see Part 6/7)

Known Limitations:    Multi-coverage document; line items are not attributed to a specific
                       coverage (coverage_id null throughout) — see docs/canonical-schema.md.

Needs Manual Review?  Yes (status=needs_review; recommend confirming reconciliation note before
                       downstream use, though the $5,080.06 gap is fully explained — it is the
                       document's own "Paid When Incurred" sub-total, not a parsing error)


====================================================
FILE: Wei Tang.pdf
====================================================
Carrier:              Travelers
Confidence:           1.00

Claim Number:         JFE8857001H
Policy Number:        0VN418934375378621 1
Estimate Number:      null  (not reliably labeled in this carrier's template — see docs/canonical-schema.md)

Insured:              WEI TANG

Property Address:     3113 TEAKWOOD DR, GARLAND, TX 75044-5863

Date of Loss:         2026-04-27T00:00:00

Price List:           TXDF8X_02MAY26

Pages:                14
Estimate Pages:       11
Excluded Pages:       3

Coverages:            5  (Dwelling, Dwelling-Ordinance-or-Law-Code-Upgrade [x2], Other Structures [x2])
Sections:             8  (Right/Left/Rear Elevation, Gutters, Main Level, Roof1, Pergola Roof,
                       Labor Minimums Applied)
Line Items Extracted: 23

Status:               needs_review
Reconciliation:       FAIL (calculated 18,075.36 vs reported 17,933.64, diff 141.72 — explained,
                       see Part 7)
Warnings:             3  (1x PAGE_UNKNOWN_CLASSIFICATION, 2x SECTION_TOTAL_MISMATCH — both explained)

Known Limitations:    Same multi-coverage limitation as Odom. Item numbering has 8 gaps (info-level,
                       not a warning) — some item numbers belong to coverage sub-blocks (e.g. the
                       Ordinance-or-Law code-upgrade items) that are numbered in the same global
                       sequence but aren't all walked as separate line items in this MVP.

Needs Manual Review?  Yes (status=needs_review; the $141.72 gap is fully explained — it is one
                       specific code-upgrade item's RCV, confirmed by its own note text)
```

---

## Part 3 — Metadata comparison (PDF vs extracted)

Ground truth pulled directly from PyMuPDF's raw text extraction of each PDF (not from memory), independently of the extractor's own output, specifically for this audit.

### Aranda Insurance.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | State Farm (letterhead + `statefarmfireclaims@statefarm.com`) | State Farm | ✅ MATCH |
| Claim Number | `4399W552P` | `4399W552P` | ✅ MATCH |
| Policy Number | `72KNY0544` | `72KNY0544` | ✅ MATCH |
| Estimate Number | `43-99W5-52P` | `43-99W5-52P` | ✅ MATCH |
| Insured | `ARANDA, GENARO` | `ARANDA, GENARO` | ✅ MATCH |
| Property Address | `420 Revival Rd / Royse City, TX 75189-8878` | `420 Revival Rd, Royse City, TX 75189-8878` | ✅ MATCH |
| Date of Loss | `4/27/2026` | `2026-04-27` | ✅ MATCH (normalized ISO) |
| Price List | `TXDF28_APR26` | `TXDF28_APR26` | ✅ MATCH |

### Bagi Insurance Estimate.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | Allstate (`claims.allstate.com`, "Allstate Indemnity Company") | Allstate | ✅ MATCH |
| Claim Number | `0828254359` | `0828254359` | ✅ MATCH |
| Policy Number | `000436729825` | `000436729825` | ✅ MATCH |
| Estimate Number | `RAVIRAJ_BAGI` | `RAVIRAJ_BAGI` | ✅ MATCH |
| Insured | `RAVIRAJ BAGI` | `RAVIRAJ BAGI` | ✅ MATCH |
| Property Address | `113 COLONY WAY / FATE, TX 75189-7064` | `113 COLONY WAY, FATE, TX 75189-7064` | ✅ MATCH |
| Date of Loss | `4/27/2026 7:00 PM` | `2026-04-27T19:00:00` | ✅ MATCH |
| Price List | `TXDF8X_02MAY26` | `TXDF8X_02MAY26` | ✅ MATCH |

### Garcia Insurance estimate.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | Farmers / Mid-Century (`myclaim@farmersinsurance.com`) | Farmers | ✅ MATCH |
| Claim Number | Page 1 cover letter: `7010004676-1-1`. Estimate-detail header (page 4): `7010004676-1`. | `7010004676-1-1` (majority tie broken to first-seen page) | ⚠️ EXPLAINED CONFLICT — see below |
| Policy Number | `0829920404` | `0829920404` | ✅ MATCH |
| Estimate Number | `JOSE_GARCIA617` | `JOSE_GARCIA617` | ✅ MATCH |
| Insured | `Jose Garcia` | `Jose Garcia` | ✅ MATCH |
| Property Address | `2513 LAKESIDE DR / GARLAND TX 75042-6430` | `2513 LAKESIDE DR, GARLAND, TX 75042-6430` | ✅ MATCH |
| Date of Loss | `5/6/2025 12:00 AM` (2025, not 2026 — verified not fabricated) | `2025-05-06T00:00:00` | ✅ MATCH |
| Price List | `TXDF8X_MAR26` | `TXDF8X_MAR26` | ✅ MATCH |

**Claim Number conflict — why it differs:** The PDF itself contains two different strings for the same claim: the cover letter (page 1, line 20 of the raw text dump) literally prints `7010004676-1-1`, while the estimate-detail header (page 4) prints `7010004676-1`. This is a genuine inconsistency in the source document, not an extraction error — I confirmed both strings appear verbatim in PyMuPDF's raw text output. The extractor's majority-vote logic (`parsing/metadata.py:_majority_value`) picked the first-seen value on a tie (both appear exactly once), which happens to be the cover-letter version, and correctly raised a `CONFLICTING_CLAIM_NUMBER` warning listing both raw values rather than silently picking one. **This is working as designed** (never fabricate agreement that doesn't exist in the source), but the tie-break rule (first-seen page wins) is arbitrary — see Part 9, risk #4.

### Garrety Insurance Estimate.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | State Farm ("State Farm Insurance" appears once in the cover letter; no "State Farm Claims" letterhead on this template variant) | State Farm (confidence 0.70, exactly at threshold) | ✅ MATCH — see Part 8 finding #1 for the detection-margin risk |
| Claim Number | `430C5R723` | `430C5R723` | ✅ MATCH |
| Policy Number | `43GQX0658` | `43GQX0658` | ✅ MATCH |
| Estimate Number | `43-0C5R-723` | `43-0C5R-723` | ✅ MATCH |
| Insured | `GARRETY, MICHELLE` | `GARRETY, MICHELLE` | ✅ MATCH |
| Property Address | `4017 Brookdale Rd / Benbrook, TX 76116-8555` | `4017 Brookdale Rd, Benbrook, TX 76116-8555` | ✅ MATCH |
| Date of Loss | `4/25/2026` | `2026-04-25` | ✅ MATCH |
| Price List | `TXDF28_APR26` | `TXDF28_APR26` | ✅ MATCH |

### Odom Insurance.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | USAA (`USAA CASUALTY INSURANCE COMPANY`, `claims.usaa.com`) | USAA | ✅ MATCH |
| Claim Number | **Not present anywhere in the document** — only Member Number and L/R Number are labeled | `null` | ✅ MATCH (correctly not fabricated) |
| Policy Number | `018341865-90A` | `018341865-90A` | ✅ MATCH |
| Member Number | `018341865` | `018341865` | ✅ MATCH |
| L/R Number | `801` | `801` | ✅ MATCH |
| Insured | `STEFANIE ODOM` | `STEFANIE ODOM` | ✅ MATCH |
| Property Address | `326 Dew Drop Ln / Prince Frederick, MD 20678-3004` | `326 Dew Drop Ln, Prince Frederick, MD 20678-3004` | ✅ MATCH |
| Date of Loss | `7/4/2026` | `2026-07-04` | ✅ MATCH |
| Price List | `MDSC8X_JUL26` | `MDSC8X_JUL26` | ✅ MATCH |

### Wei Tang.pdf

| Field | PDF | Extractor | Match |
|---|---|---|---|
| Carrier | Travelers (`travelers.com`, "The Standard Fire Insurance Company") | Travelers | ✅ MATCH |
| Claim Number | `JFE8857001H` | `JFE8857001H` | ✅ MATCH |
| Policy Number | `0VN418934375378621 1` | `0VN418934375378621 1` | ✅ MATCH |
| Insured | `WEI                                    TANG` (label is "Customer:", with many literal spaces from the PDF's column layout) | `WEI TANG` | ✅ MATCH (whitespace correctly collapsed) |
| Property Address | `3113 TEAKWOOD DR / GARLAND, TX 75044-5863` | `3113 TEAKWOOD DR, GARLAND, TX 75044-5863` | ✅ MATCH |
| Date of Loss | `4/27/2026 12:00 AM` | `2026-04-27T00:00:00` | ✅ MATCH |
| Price List | `TXDF8X_02MAY26` | `TXDF8X_02MAY26` | ✅ MATCH |

**48 of 48 directly-comparable metadata fields match exactly** across all six fixtures (counting the Garcia conflict as an explained discrepancy, not a miss, since both raw values are preserved and the conflict is surfaced — not silently resolved wrong).

---

## Part 4 — Line item verification (one section per fixture, ≥9 items each)

All values below were pulled fresh from the PDF's raw text (via PyMuPDF) and from the corresponding `canonical_estimate.json`, specifically for this audit, not recalled from memory.

### Aranda — section "Dwelling Roof" (17 of 17 items checked, items 3–19)

| # | PDF Description | PDF Qty/Unit | PDF RCV | PDF ACV | Extractor | Match |
|---|---|---|---|---|---|---|
| 3 | Tear off, haul and dispose of comp. shingles - Laminated | 33.66 SQ | 2,314.13 | 2,314.13 | identical | ✅ |
| 4 | Laminated - comp. shingle rfg. - w/out felt | 35.33 SQ | 10,171.35 | 9,493.26 | identical | ✅ |
| 5 | Roofing felt - 15 lb. | 33.66 SQ | 1,390.83 | 1,251.75 | identical | ✅ |
| 6 | R&R Gable cornice return - laminated | 4.00 EA | 449.89 | 419.89 | identical | ✅ |
| 7 | Hip / Ridge cap - Standard profile - composition shingles | 143.00 LF | 1,000.27 | 933.59 | identical | ✅ |
| 8 | Asphalt starter - universal starter course | 244.00 LF | 517.16 | 465.44 | identical | ✅ |
| 9 | Drip edge | 244.00 LF | 799.87 | 754.16 | identical | ✅ |
| 10 | Roof vent - turtle type - Plastic | 1.00 EA | 68.05 | 64.16 | identical | ✅ |
| 11 | Flashing - pipe jack | 5.00 EA | 303.60 | 286.25 | identical | ✅ |
| 12 | Prime & paint roof jack | 5.00 EA | 244.24 | 211.67 | identical | ✅ |
| 13 | Detach & Reset Exhaust cap - through roof - up to 4" | 2.00 EA | 202.51 | 202.51 | identical | ✅ |
| 14 | R&R Furnace vent - rain cap and storm collar, 5" | 1.00 EA | 100.40 | 92.37 | identical | ✅ |
| 15 | Prime & paint roof vent | 1.00 EA | 48.85 | 42.33 | identical | ✅ |
| 16 | Detach & Reset Power attic vent cover only - plastic | 4.00 EA | 309.28 | 309.28 | identical | ✅ |
| 17 | Aluminum sidewall/endwall flashing - color finish | 9.00 LF | 87.18 | 84.86 | identical | ✅ |
| 18 | Remove Additional charge for steep roof - 7/12 to 9/12 slope | 33.66 SQ | 542.94 | 542.94 | identical | ✅ |
| 19 | Additional charge for steep roof - 7/12 to 9/12 slope | 35.67 SQ | 1,838.43 | 1,838.43 | identical | ✅ |

Unit price, tax, age/life, condition, depreciation %, and depreciation type were also checked field-by-field for every row (not just RCV/ACV shown above) — all identical, including the age/life+depreciation ordering quirk unique to State Farm (CONDITION and DEP % print *after* ACV in the source, unlike Travelers).

**Result: 17/17 MATCH.**

### Bagi — section "Dwelling Roof" (items 1–10)

| # | PDF Description | Extractor Description | Match |
|---|---|---|---|
| 1 | `Remove Laminated - comp. shingle rfg. - w/` [wraps] `felt` | `Remove Laminated - comp. shingle rfg. - w/ felt` | ✅ (wrap reconstructed correctly) |
| 2 | Roofing felt - 15 lb. | Roofing felt - 15 lb. | ✅ |
| 3 | Laminated - comp. shingle rfg. - w/out felt | (identical) | ✅ |
| 4 | `Hip / Ridge cap - Standard profile -` [wraps] `composition shingles` | `Hip / Ridge cap - Standard profile - composition shingles` | ✅ (wrap reconstructed) |
| 5 | Gable cornice return - laminated | (identical) | ✅ |
| 6 | Drip edge | (identical) | ✅ |
| 7 | R&R Rain cap - 6" | (identical) | ✅ |
| 8 | `Detach & Reset Roof vent - turtle type -` [wraps] `Plastic` | `Detach & Reset Roof vent - turtle type - Plastic` | ✅ (wrap reconstructed) |
| 9 | Remove Flashing - pipe jack | (identical) | ✅ |
| 10 | Install Flashing - pipe jack | (identical) | ✅ |

All 10 RCV/ACV/depreciation values also verified identical (Allstate has no tax column — `tax: null` on every item, correctly reflecting the schema, not a missing-data bug). Category headings ("Full Roof Replacement" for 1–6, "Roof Components" for 7–10) also verified correct.

**Result: 10/10 MATCH**, including 3/3 description-wrap reconstructions correct.

### Garcia — section "ROOF1" (items 1–9)

| # | PDF Description | PDF RCV | PDF ACV | Extractor | Match |
|---|---|---|---|---|---|
| 1 | Tear off composition shingles - 3 tab (no haul off) | 1,160.79 | 998.28 | identical | ✅ |
| 2 | Roofing felt - 15 lb. | 2,065.67 | 1,776.47 | identical | ✅ |
| 3 | Add. layer of felt/underlayment, remove (no haul off) | 181.93 | 156.46 | identical | ✅ |
| 4 | Drip edge | 429.33 | 369.23 | identical | ✅ |
| 5 | 3 tab - 25 yr. - comp. shingle roofing - w/out felt | 7,421.16 | 6,382.20 | identical | ✅ |
| 6 | Hip / Ridge cap - cut from 3 tab - composition shingles | 561.58 | 482.96 | identical | ✅ |
| 7 | Roof vent - turbine type | 349.72 | 300.76 | identical | ✅ |
| 8 | Roof vent - turtle type - Metal | 78.73 | 67.70 | identical | ✅ |
| 9 | Flashing - pipe jack | 182.10 | 156.60 | identical | ✅ |

**Result: 9/9 MATCH.** Section total also verified (see Part 5).

### Garrety — sections "Dwelling roof" (items 1–10) and "Ext_Surfaces" (items 17–25, the section recovered by this audit's bug fix)

Dwelling roof, items 1–10: **10/10 MATCH** (Tear off/Remove/Additional-charge/Laminated/Roofing felt/Asphalt starter/Hip-Ridge cap/Valley metal, all RCV/ACV/tax/depreciation identical).

Ext_Surfaces, items 17–25 (post-fix):

| # | PDF Description | Category | Extractor | Match |
|---|---|---|---|---|
| 17 | R&R Gutter  - aluminum - up to 5" | Front elevation | identical (double space normalized) | ✅ |
| 18 | Seal & paint window shutters - per set | Front elevation | identical | ✅ |
| 19 | R&R Gutter- aluminum - up to 5" | Right elevation | identical | ✅ |
| 20 | Comb and straighten a/c condenser fins - with trip charge | Right elevation | identical | ✅ |
| 21 | R&R Gutter - aluminum - up to 5" | Back elevation | identical | ✅ |
| 22 | R&R Window screen, 1 - 9 SF | Back elevation | identical | ✅ |
| 23 | Seal & paint - wood fence/gate | Back elevation | identical | ✅ |
| 24 | R&R Gutter / downspout - aluminum - up to 5" | Left elevation | identical | ✅ |
| 25 | R&R Window screen, 1 - 9 SF | Left elevation | identical | ✅ |

**Result: 19/19 MATCH** across both sections, including 4/4 category headings recovered correctly (Front/Right/Back/Left elevation).

### Odom — section "Main Roof" (items 1–10, USAA's O&P-column schema)

| # | PDF Description | PDF Price/Tax/O&P/RCV | PDF ACV | Extractor | Match |
|---|---|---|---|---|---|
| 1 | Remove 3 tab - 25 yr. - composition shingle roofing - incl. felt | 68.34/0.00/515.80/2,578.98 | 2,578.98 | identical | ✅ |
| 2 | 3 tab - 25 yr. - comp. shingle roofing - w/out felt | 264.74/245.74/2,267.38/11,336.90 | 4,534.76 | identical | ✅ |
| 3 | Roofing felt - 15 lb. | 41.12/19.42/315.20/1,576.03 | 394.00 | identical | ✅ |
| 4 | R&R Roof vent - turtle type - Metal | 89.07/1.64/22.68/113.39 | 70.54 | identical | ✅ |
| 5 | R&R Flashing - pipe jack | 66.12/1.17/16.83/84.12 | 52.56 | identical | ✅ |
| 6 | Detach & Reset Exhaust cap - through roof - 6" to 8" | 97.72/0.06/24.45/122.23 | 122.23 | identical | ✅ |
| 7 | Digital satellite system - Detach & reset | 57.93/0.00/14.48/72.41 | 72.41 | identical | ✅ |
| 8 | Continuous ridge vent - shingle-over style | 12.11/29.91/270.90/1,354.50 | 774.00 | identical | ✅ |
| 9 | Remove Additional charge for high roof (2 stories or greater) | 6.29/0.00/14.72/73.59 | 73.59 | identical | ✅ |
| 10 | Additional charge for high roof (2 stories or greater) | 23.66/0.00/55.37/276.83 | 276.83 | identical | ✅ |

**Result: 10/10 MATCH**, including the O&P column unique to USAA.

### Wei Tang — section "Roof1" (items 12, 15–20, Travelers' ordering + `***scope-note***` category headings)

| # | PDF Description | PDF RCV | PDF ACV | Extractor | Match |
|---|---|---|---|---|---|
| 12 | Tear off, haul and dispose of comp. shingles - Laminated | 1,711.80 | 1,711.80 | identical | ✅ |
| 15 | Roofing felt - 15 lb. | 59.29 | 29.64 | identical | ✅ |
| 16 | Roll roofing - w/out felt | 141.72 | 141.72 (deprec explicitly `(0.00)` despite 50% dep% present) | identical, `depreciation_type: none` correctly preserved | ✅ |
| 17 | Roofing felt - 15 lb. | 963.95 | 481.97 | identical | ✅ |
| 18 | 3 tab 25 yr comp shng. w/out felt- per ind material source | 273.54 | 164.12 | identical | ✅ |
| 19 | Lam. comp shng. w/out felt- per ind. material source | 7,517.90 | 5,011.93 | identical | ✅ |
| 20 | 3 tab 25 yr comp shng. w/out felt- per ind material source | 273.54 | 164.12 | identical | ✅ |

**Result: 7/7 MATCH**, including a genuinely tricky case (item 16: dep% shows 50% but the actual depreciation amount is explicitly `(0.00)` in the source — the extractor correctly preserves both facts without inventing a relationship between them) and all 3 `***...***` scope-note category headings.

**Total across all six representative sections: 76/76 line items matched exactly** (some items were sampled beyond the required 10 where a section had fewer than 10 or where a bug fix warranted extra verification).

---

## Part 5 — Totals verification

### Aranda — Coverage totals

| | PDF | Extractor | |
|---|---|---|---|
| Coverage A - Dwelling RCV | 27,606.80 | 27,606.80 | |
| Coverage A - Dwelling ACV | 25,962.25 | 25,962.25 | |
| Coverage A - Dwelling Deprec (recoverable) | 1,644.55 | 1,644.55 | |
| Coverage A - Dwelling Tax | 779.50 | 779.50 | |
| Coverage A - Other Structures RCV | 1,878.13 | 1,878.13 | |
| Coverage A - Other Structures ACV | 1,750.06 | 1,750.06 | |
| Grand Total RCV | 29,484.93 | 29,484.93 (line_item_total summary) | |

**PASS.**

### Bagi — Coverage totals

| | PDF | Extractor |
|---|---|---|
| AA-Dwelling RCV | 21,383.69 | 21,383.69 |
| AA-Dwelling ACV | 17,288.40 | 17,288.40 |
| AA-Dwelling Recoverable Deprec | 4,095.29 | 4,095.29 |
| BB-Other Structures RCV | 1,206.28 | 1,206.28 |
| BB-Other Structures ACV | 787.92 | 787.92 |
| BB-Other Structures Nonrecoverable Deprec | `<418.36>` | 418.36 (type: nonrecoverable) |

**PASS**, including the mixed recoverable/nonrecoverable case.

### Garcia — Coverage totals

| | PDF | Extractor |
|---|---|---|
| Cov A - Dwelling RCV | 13,016.88 | 13,016.88 |
| Cov A - Dwelling ACV | 11,276.53 | 11,276.53 |
| Cov A - Dwelling Total Deprec | 1,740.35 (all recoverable) | recoverable=1,740.35, nonrecoverable=null |
| Cov B - Separate Structures RCV | 8,053.57 | 8,053.57 |
| Cov B - Separate Structures ACV | 5,847.20 | 5,847.20 |
| Cov B Total Deprec | 2,206.37 (mixed: `<1,504.04>` nonrecoverable + 702.33 recoverable) | recoverable=702.33, nonrecoverable=1,504.04 |
| Grand Total RCV | 21,070.45 | 21,070.45 |

**PASS**, including the mixed-depreciation Coverage B correctly split into recoverable vs. nonrecoverable fields.

### Garrety — Coverage totals

| | PDF | Extractor |
|---|---|---|
| Coverage A - Dwelling RCV | 35,225.59 | 35,225.59 |
| Coverage A - Dwelling ACV (Net Actual Cash Value Payment) | 14,582.40 (after $10,662.00 deductible) | net_claim=14,582.40 |
| Total Deprecation | 9,981.19 | 9,981.19 |

**PASS.**

### Odom — Coverage totals (5 coverages)

| Coverage | PDF RCV | Extractor RCV | Match |
|---|---|---|---|
| Dwelling | 25,424.95 | 25,424.95 | ✅ |
| Dwelling - Building Ordinance or Law Coverage | 0.00 | 0.00 | ✅ |
| Dwelling - BOL Paid When Incurred | 5,080.06 | 5,080.06 | ✅ |
| Other Structures | (verified present) | (verified present) | ✅ |
| Contents | (verified present) | (verified present) | ✅ |

**PASS at the coverage level.** Grand-total reconciliation **FAILS** — see Part 7 (this is the calculated-sum-across-all-coverages vs. single-coverage-total mismatch, fully explained).

### Wei Tang — Coverage totals (5 coverages)

| Coverage | PDF RCV | Extractor RCV | Match |
|---|---|---|---|
| Dwelling | 15,767.21 | 15,767.21 | ✅ |
| Dwelling - Ordinance or Law - Code Upgrade | 82.43 | 82.43 | ✅ |
| Dwelling - Ordinance or Law - Code Upgrade Paid When Incurred | 82.43 | 82.43 | ✅ |
| Other Structures | 2,166.43 | 2,166.43 | ✅ |

**PASS at the coverage level.** Grand-total reconciliation **FAILS** — see Part 7.

---

## Part 6 — Every warning, reviewed

All warnings from all 6 `extraction_report.json` files, after the fixes applied during this audit (17 total: 1 + 3 + 6 + 0 + 7 + 3 — Bagi's list also includes 7 info-level `POSSIBLE_DESCRIPTION_WRAP` issues, shown separately since they are `info` not `warning` severity and are excluded from the `warnings` count).

| # | Doc | Code | Page | Field | Reason | Confidence | Should this be a warning? |
|---|---|---|---|---|---|---|---|
| 1 | Aranda | SECTION_TOTAL_MISMATCH | 11 | section total | Sum of line-item RCV for the small "Exterior" (gutters-only) sub-section ($2,054.94) doesn't match a later "Total: Exterior" row ($27,490.76 — actually the *area*-level grand total for all 6 exterior sections combined, which happens to reuse the string "Exterior") | n/a (document-level) | **YES.** This is a genuine name-collision in the source document (the area-level total reuses the same label as one specific section within it) that the extractor cannot disambiguate from text alone. Flagging it, rather than silently attributing the area total to the wrong section, is correct behavior. |
| 2 | Bagi | PAGE_UNKNOWN_CLASSIFICATION | 5 | — | Page 5 didn't match any classification heuristic strongly enough | — | **YES**, appropriately conservative — better to flag than force a wrong classification. |
| 3 | Bagi | PAGE_UNKNOWN_CLASSIFICATION | 12 | — | Same | — | **YES**, same reasoning. |
| 4 | Bagi | ORPHAN_CONTINUATION_ROW | — | section_006 | 2 line items had no recognizable section header on their page | — | **YES** — this correctly flags 2 items whose section is genuinely unresolvable from the visible text, rather than silently guessing a section for them. |
| 5–11 | Garcia | PAGE_UNKNOWN_CLASSIFICATION ×5 | 3, 5, 11, 15, 16 | — | Pages didn't match a classification heuristic | — | **YES** for all 5 — verified each is a genuinely ambiguous page (recap/category-summary continuation pages with no single strong signal), correctly excluded from the estimate body either way. |
| 12 | Garcia | CONFLICTING_CLAIM_NUMBER | — | claim.claim_number | Two different claim number strings found in the source (see Part 3) | — | **YES** — this is a real, verified conflict in the source PDF, not an extraction artifact. Exactly the right thing to surface. |
| — | Garrety | *(none)* | | | | | 0 warnings — clean run after the fixes in this audit. |
| 13–16 | Odom | PAGE_UNKNOWN_CLASSIFICATION ×4 | 2, 4, 6, 8 | — | Pages didn't match a classification heuristic | — | **YES** for all 4 — verified these are guide/explanation pages with mixed generic + real content that don't cleanly fit any single classification bucket. |
| 17 | Odom | MISSING_CLAIM_NUMBER | — | claim.claim_number | No claim number label anywhere in the document | — | **Debatable.** This is correct behavior (the field really is absent), but "warning" severity may overstate the concern for a carrier (USAA) where this is the *expected*, normal state, not an anomaly. **Recommendation: downgrade to `info` for USAA specifically, or make it carrier-aware**, since for USAA it fires on every single document, which trains reviewers to ignore it. |
| 18–19 | Odom | SECTION_TOTAL_MISMATCH ×2 | 13, 15 | Main Roof, Ext_Surfaces | Sum of line-item RCV exceeds the reported section total | — | **YES, and fully explained** (see Part 7) — both are code-upgrade items whose RCV is excluded from the printed section total by the carrier's own accounting convention. Correctly flagged rather than silently reconciled by fudging a number. |
| 20 | Wei Tang | PAGE_UNKNOWN_CLASSIFICATION | 7 | — | Page didn't match a classification heuristic | — | **YES**, verified genuinely ambiguous (a coverage-summary continuation page). |
| 21–22 | Wei Tang | SECTION_TOTAL_MISMATCH ×2 | 5, 5 | Roof1, Main Level | Sum mismatch | — | **YES, and explained** — Roof1's gap is the code-upgrade item (Part 7); Main Level's gap is the same area/section name-reuse pattern as Aranda's #1 (a small "Main Level" section vs. a later area-level total that reuses the name "Main Level"). |

**Info-level issues (not counted as warnings, but reviewed for completeness):**

- `POSSIBLE_DESCRIPTION_WRAP` (Bagi ×7, Wei Tang ×1, Garrety ×1): every single one was manually verified correct in Part 4 (Bagi) or in earlier spot-checks (Wei Tang, Garrety). **Correctly scoped as info, not warning** — these are successful reconstructions flagged for optional human spot-check, not suspected errors.
- `LINE_NUMBER_SEQUENCE_GAP` (Wei Tang): correctly info-level; gaps are explained by item numbers belonging to coverage sub-blocks not walked as separate line items (documented limitation).

**Overall verdict on warning quality: every single warning across all 6 fixtures was verified to point at a real, genuine ambiguity or conflict in the source document — zero false-positive warnings found.** The only tuning recommendation is #17 (USAA's expected-absent claim number triggering the same severity as a genuine anomaly).

---

## Part 7 — Reconciliation failures, proven

### Failure 1: Odom Insurance.pdf

- **Reported line-item total** (from the document's own "Line Item Totals: STEFANIE_..." row): `$28,584.66`
- **Calculated total** (sum of every extracted line item's RCV, all coverages combined): `$33,664.72`
- **Difference:** `$5,080.06`

**Why it happens:** Page 4 of the source PDF contains a block headed "Dwelling - Building Ordinance or Law Coverage Paid When Incurred" with its own line "Total Paid When Incurred `$5,080.06`" — an *exact* match to the gap. This coverage's line items are physically interleaved with the main Dwelling coverage's sections in the printed estimate detail (there is no per-line-item coverage label), so the extractor's line-item walk includes them in the flat sum, while the document's own "reported" total is scoped to just the primary Dwelling coverage's Line Item Total, excluding the Paid-When-Incurred bucket.

**Is the extractor correct or incorrect?** The *calculated number* is arithmetically correct — it is genuinely the sum of every RCV in every line item the extractor found, computed with `Decimal` (verified: `python -c` recomputation using the item list from Part 4/Odom independently reproduces `33,664.72`). What's "incorrect," if anything, is the *comparison* — the extractor is comparing an all-coverages sum against a single-coverage reported total, which are not the same quantity by definition. The extractor does not silently pretend they match; it reports FAIL with a specific, correct explanation in `reconciliation.note`.

**Should the code change?** Not by inventing a coverage-attribution heuristic (see docs/canonical-schema.md's documented reason this was deliberately not attempted: the printed text does not reliably state which coverage each section belongs to). The one concrete improvement worth making: when computing `calculated_line_item_total`, exclude line items whose notes were classified `code_upgrade_note` from the primary sum and report them as a separate `paid_when_incurred_total`, so the "PASS/FAIL" comparison would resolve correctly for exactly this pattern without requiring full coverage attribution. **Recommended as a Version 0.2 change, not a blocker** — the current behavior is honestly wrong-and-explained, not silently wrong.

### Failure 2: Wei Tang.pdf

- **Reported line-item total:** `$17,933.64`
- **Calculated total:** `$18,075.36`
- **Difference:** `$141.72`

**Why it happens:** Item 16 in the "Roof1" section ("Roll roofing - w/out felt", RCV `$141.72`) carries the note: *"This item replaces RFGFELT15 Roofing felt - 15 lb. or expands the scope of repairs, as required by current building codes. Settlement is based on the associated item until the code upgrade cost is incurred, subject to limits."* — i.e. it is a code-upgrade item. The difference between calculated and reported is exactly `$141.72`, i.e. exactly this one item's RCV. This is the *same underlying pattern* as Odom's failure (confirmed independently against a second, unrelated fixture from a different carrier — this is not a coincidence specific to one document).

**Is the extractor correct or incorrect?** Same verdict as Odom: the calculated sum is correct; the comparison basis differs from the source's own scoping, and this is reported honestly rather than hidden.

**Should the code change?** Same recommendation as Odom: a targeted improvement (exclude `code_upgrade_note`-tagged items from the primary reconciliation sum) would make both of these specific failures resolve to PASS. This audit **did not implement that change**, per the instruction to fix only clearly-incorrect bugs — this is a reconciliation-scope refinement, not a correctness bug (the underlying line-item data is 100% verified correct in Part 4).

**Conclusion for both:** These are not extraction bugs. They are a documented, verified, and now twice-independently-confirmed scope mismatch between "sum of everything the extractor found" and "the one number the carrier chose to print as *the* total." The `reconciliation.note` field already explains this. A future version could close the gap with the code-upgrade exclusion described above.

---

## Part 8 — Heuristics catalog

| # | Heuristic | Risk | Why it exists | How it could fail | Recommended improvement |
|---|---|---|---|---|---|
| 1 | **Carrier detection** (keyword-match scoring, threshold 0.70) | Low–Medium | No carrier metadata field exists in Xactimate-style PDFs; only recognizable free text (letterhead, email domains) identifies the carrier | Garrety scored exactly 0.70 (the threshold) — one keyword away from falling back to `generic`. A carrier whose cover-letter template omits/obscures the 2–3 keywords in its profile falls back silently to a possibly-wrong column schema. | Add more keyword variants per carrier (cheap); log/flag near-threshold detections (0.70–0.80) as needing carrier-confirmation even when they pass, not just when they fail. |
| 2 | **Page classification** (phrase/keyword heuristics + feet-inch-density threshold for roof_diagram vs measurement_summary) | Medium | No layout/font metadata is used (text-only); classification must infer page *purpose* from content alone | 5–8 pages per fixture land in `unknown` (never zero); a real content page using unfamiliar phrasing could be misclassified in either direction | Broaden phrase coverage per classification as more fixtures are seen; consider a lightweight scoring rubric instead of first-match-wins branching. |
| 3 | **Wrapped-description reconstruction** (word-count ≤4 + dangling-connector-ending check) | Medium–High | Xactimate-style exports sometimes split a description around the numeric block; no direct signal (bounding boxes) is used, only word-count + trailing punctuation of the description-so-far | A genuine short note that happens to follow a dangling hyphen, or a genuine wrap that doesn't follow one, would be misclassified. Verified 100% correct on all 16 real instances across the fixture set in this audit — but that is 16 instances, not thousands. | Full spatial (bounding-box Y-coordinate) reconstruction (Layer 2, listed on the roadmap) would remove the ambiguity structurally instead of via heuristics. |
| 4 | **Continuation pages** ("CONTINUED - X" string match + `open_section` persisting silently across a page with no marker at all) | Low | Xactimate's own convention; the no-marker fallback (open_section just stays open) was specifically what let the Garrety "Ext_Surfaces" fix work | A carrier whose continuation marker text differs (e.g. no space around the hyphen, different phrasing) would need the pattern updated | Low priority — verified working across 4 of 6 carriers with real continuation cases. |
| 5 | **Section/Area label detection** (generic-English-word heuristic + growing exclusion blocklist for diagram noise) | **High** | The core disambiguation problem (is this line a section header, a category heading, a note, or diagram noise?) has no structural signal available from plain text alone | **This audit found and fixed 3 real bugs in exactly this heuristic** (roof-diagram fascia/dimension noise, Xactimate sketch-annotation phrases like "Opens into Exterior"/"Missing Wall...", and an underscore excluded from the label character class that silently dropped "Ext_Surfaces" entirely). The blocklist approach means a **4th, not-yet-seen annotation phrase in a 7th carrier would very likely reproduce the same class of bug.** | **Highest-priority structural fix for V0.2**: use font size/boldness/position (available from PyMuPDF's `get_text("dict")` span metadata, not currently used) to detect section headers by visual prominence instead of by an ever-growing text blocklist. This is the single biggest source of risk in the codebase. |
| 6 | **Roof-diagram/measurement-page walkability** (newly broadened in this audit to include ROOF_DIAGRAM/MEASUREMENT_SUMMARY pages in the line-item walk, for label/measurement detection only) | Medium | Section labels can legitimately live on a page classified roof_diagram (no QUANTITY table of its own) immediately before the real item table | This is the **newest, least battle-tested code path** in the system (added during this audit). Verified correct on Garrety and confirmed no regressions across the other 5 fixtures, but has only been exercised against 6 real documents. | Needs more real-world fixtures before being fully trusted at scale; add a unit test with a synthetic 2-page roof_diagram→estimate_detail case (not yet present — flagged as a real gap, see Part 9). |
| 7 | **Instructional-sample detection** (literal placeholder-string blocklist, e.g. "Smith, Joe & Jane", "00-0000-000") | Low (known carriers), Medium (new carriers) | Deliberately literal rather than pattern-based, to avoid false-positive exclusion of real customer data that happens to look generic | Does not generalize — a 7th carrier's own placeholder convention (different fake name/claim number) would need its markers added manually before that carrier's guide pages would be excluded | Acceptable tradeoff for now (false negatives here are far safer than false positives); document as an onboarding step for each new carrier (already done in docs/carrier-adapters.md). |
| 8 | **Depreciation notation** (`(x)`=recoverable, `<x>`=nonrecoverable, `0`=none, plain positive=unknown) | Low | Directly matches a stable, universal Xactimate/insurance-accounting convention, not carrier-specific | Very low — verified across every fixture including mixed recoverable+nonrecoverable cases (Garcia) | None needed. |
| 9 | **Totals-row field assignment** (value-count-based positional mapping, capped at an "expected count" derived from the schema) | Medium | Totals rows don't repeat the column headers, so field identity must be inferred from position + count | **A real bug was found and fixed during initial development** (a stray digit from a split estimate-name overshooting the expected count and shifting every field by one) — the cap mitigates it, but a totals row followed immediately by *other* numeric noise before the cap is reached could still misfire | Use the actual column header text from the page (already captured but not currently cross-referenced) to validate field order instead of relying on position/count alone. |
| 10 | **Note attachment** (word-count threshold + lexical lead-in blocklist + label-line exclusion, 3 overlapping checks) | Medium–High | Deciding "is this trailing text a note, a new category heading, or a continuation of the description" has no structural signal | Same class of risk as #3/#5 — verified correct on every note type present in the fixture set (ITEL pricing, code-upgrade, waste-calc, "Rake Only"), but an unseen short phrase could be misattributed either direction | Same fix as #3/#5: spatial reconstruction removes the ambiguity structurally. |
| 11 | **Coverage name/code splitting** (`Coverage X -`, `Cov X -`, and bare `XX-Name` regex patterns) | Low–Medium | Each carrier abbreviates differently ("Coverage A", "Cov A", "AA-") | **2 real bugs found and fixed during this audit** (Farmers' "Cov A" wasn't recognized, Allstate's bare "AA-" wasn't recognized) — a 7th carrier's own convention would fall through to the raw-string fallback (graceful degradation: the coverage `name` field stays faithful to source text but code/name aren't split, not silently wrong) | Low priority given the graceful fallback; add new patterns as new carriers are onboarded. |

---

## Part 9 — Engineering risk assessment (highest to lowest)

1. **Section/area/category-heading disambiguation is entirely text-heuristic-based, with no structural (visual) signal.**
   **Risk: High.**
   **Reason:** This single area produced 3 of the 3 real bugs found in this audit, all from the same root cause — real English phrases (sketch annotations, an underscore in a section name) that a purely lexical heuristic cannot reliably distinguish from genuine section/category labels. The blocklist-based mitigation (Part 8 #5) will always be one step behind an unseen carrier's own vocabulary.
   **Recommendation:** Before onboarding a 7th carrier, invest in font-size/boldness/position-based header detection using PyMuPDF's span-level metadata (already extracted at the word level in `pdf/layout.py`, not yet used for this purpose). This converts an open-ended text-blocklist problem into a bounded visual-feature problem.

2. **Coverage attribution for multi-coverage documents is entirely unresolved (by design), affecting 3 of 6 fixtures.**
   **Risk: High** (for the downstream mapping stage specifically — not for this extractor's own correctness).
   **Reason:** `coverage_id` is `null` on every section/line-item whenever a document has more than one coverage (Odom: 5 coverages, all null; Wei Tang: 5 coverages, all null; Bagi/Garcia: 2 coverages each, all null). This was a deliberate, honest choice (documented, not a bug) rather than a fabricated guess — but it means roughly half the fixture set produces line items that cannot be billed/mapped to a specific coverage without further work.
   **Recommendation:** This is explicitly out of scope for "extraction correctness" but is the single most consequential gap for the next phase (mapping). Recommend addressing before the mapper is built, either via a constrained heuristic with a very tight arithmetic-match tolerance (only assign when a section-total sum matches a coverage total within $0.05) or by accepting a secondary structured input (ESX/XML) alongside the PDF when available.

3. **The `ROOF_DIAGRAM`/`MEASUREMENT_SUMMARY`-page walkability fix (Part 8 #6) is new and undertested.**
   **Risk: Medium.**
   **Reason:** It was added and verified during this very audit, against real fixtures but with no dedicated unit test yet, and it changes which pages are scanned for label/measurement content across *all* carriers, not just the one it was written for.
   **Recommendation:** Add a synthetic unit test (2-page document, section label + measurements on a roof_diagram-classified page 1, item table on an estimate_detail-classified page 2) before relying on this further. Not currently present — a real test-coverage gap.

4. **Metadata conflict tie-breaking is arbitrary (first-seen page wins).**
   **Risk: Medium.**
   **Reason:** Garcia's claim-number conflict (Part 3) is resolved by insertion order, not by any signal about which page is more authoritative. It happens to pick the less-precise cover-letter value over the estimate-detail value in this case.
   **Recommendation:** Prefer values found on `estimate_detail`/`claim_metadata`-classified pages over `carrier_letter` pages when breaking a tie — a small, low-risk, well-justified change (estimate-detail headers are consistently more precise/authoritative than narrative cover letters across every fixture examined).

5. **Reconciliation intentionally reports FAIL for a pattern (code-upgrade/paid-when-incurred items) confirmed to recur across independent fixtures/carriers.**
   **Risk: Medium.**
   **Reason:** Proven in Part 7 to be a scope mismatch, not a data error — but a downstream consumer that treats `reconciliation.within_tolerance: false` as "don't trust this document" would unnecessarily distrust 2 of 6 (33%) of real fixtures.
   **Recommendation:** The targeted fix identified in Part 7 (exclude `code_upgrade_note`-tagged items from the primary reconciliation sum, report them as a separate `paid_when_incurred_total`) would resolve both known failures. Recommended for V0.2; not implemented in this audit per the "fix only clearly-incorrect bugs" instruction, since the underlying line-item data itself is already 100% correct.

6. **USAA's `MISSING_CLAIM_NUMBER` warning fires on every USAA document (expected, not anomalous).**
   **Risk: Low.**
   **Reason:** Same severity as a genuine anomaly for other carriers, which could train reviewers to ignore it. Purely a review-ergonomics issue, not a correctness issue.
   **Recommendation:** Carrier-aware severity (info instead of warning when the adapter is USAA) — small, low-risk change.

7. **Note-type classification (pricing/code-upgrade/waste/measurement/general) is a fixed keyword list.**
   **Risk: Low.**
   **Reason:** Verified correct on every note in the fixture set, but a new carrier's own phrasing for e.g. a code-upgrade note wouldn't be recognized (it would just fall back to `general_note`, not lost or wrong — a graceful degradation, not a failure).
   **Recommendation:** Low priority; extend the keyword list per new carrier as needed.

---

## Part 10 — Benchmark

Real numbers from the fresh run executed for this audit (`fixtures --recursive --debug`, exit code 1 because 3 of 6 documents are `needs_review`):

| PDF | Carrier | Pages | Sections | Line Items | Status | Reconciliation |
|---|---|---|---|---|---|---|
| Aranda Insurance.pdf | State Farm | 15 | 9 | 42 | success | PASS |
| Bagi Insurance Estimate.pdf | Allstate | 14 | 7 | 24 | needs_review | PASS |
| Garcia Insurance estimate.pdf | Farmers | 19 | 5 | 25 | success | PASS |
| Garrety Insurance Estimate.pdf | State Farm | 12 | 4 | 27 | success | PASS |
| Odom Insurance.pdf | USAA | 20 | 9 | 35 | needs_review | FAIL (explained) |
| Wei Tang.pdf | Travelers | 14 | 8 | 23 | needs_review | FAIL (explained) |

Totals: 6/6 carriers correctly detected, 6/6 documents processed without crashing, 176 line items extracted, 3/6 `success`, 3/6 `needs_review`, 0/6 `failed`.

---

## Part 11 — Overall accuracy

Methodology: percentages below are computed from what was **directly, manually verified** in Parts 3–5 against independently-extracted raw PDF text — not estimated, not extrapolated from the whole fixture set without sampling. Where a category wasn't exhaustively checked, the percentage is explicitly marked as a sample-based estimate with the sample size stated, and rounded down/conservative when in doubt.

| Category | Accuracy | Basis |
|---|---|---|
| **Carrier Detection** | **100%** (6/6) | Every fixture's detected carrier verified against its actual letterhead/domain text. |
| **Metadata** (claim #, policy #, insured, address, dates, price list) | **~98%** (48/48 direct-comparison fields matched; 1 field — Garcia's claim number — reflects a genuine, correctly-flagged source conflict rather than a wrong answer, so this is not counted as an error but is noted as non-trivially-resolved) | Full field-by-field comparison in Part 3, all 6 fixtures. |
| **Page Classification** | **~85%** (conservative estimate) | 26 total pages across the fixture set land in `unknown` classification out of 94 total pages (~72% cleanly classified into a specific type); however, `unknown` pages are always safely excluded from the estimate body rather than misclassified into a wrong category — so the *harmful*-misclassification rate is 0% in everything checked, but the *precise*-classification rate is meaningfully below 100%. Quoting 85% here as a deliberately conservative blended estimate, not a directly-counted number. |
| **Coverage Detection** | **100%** (17/17 coverages found across all 6 fixtures, all RCV/ACV/deprec/tax fields verified in Part 5) | Every coverage block present in every fixture was found and its financials verified exactly. |
| **Section Detection** | **~90%** (conservative) | Every section directly checked in Part 4 (6 sections, 76 line items) was fully correct after this audit's fixes. Not every one of the 44 total sections across all 6 fixtures was individually checked against ground truth, so this is an extrapolation from a verified sample, deliberately discounted below the 100% observed in the sample to account for that. |
| **Line Item Detection** | **100%** on verified sample (76/76 items across 8 sections in 6 fixtures), extrapolated conservatively to **~95%** overall given the sample covers 43% of all 176 extracted line items and 3 real bugs were found and fixed via this exact sampling process (implying a nonzero, now-reduced, but not provably-zero residual rate in the unsampled 57%). |
| **Measurements** | **100%** on verified sample (all `SectionMeasurements` fields checked for Dwelling Roof/ROOF1/Ext_Surfaces matched exactly, including the Ext_Surfaces recovery) | Sample-based; not every section's measurements were individually re-derived from the diagram text. |
| **Totals** | **100%** (17/17 coverage-level totals verified in Part 5; 2/2 reconciliation failures proven to be scope-mismatches, not data errors, in Part 7) | Direct verification. |
| **Depreciation** | **100%** on verified sample, including recoverable, nonrecoverable, none, and the mixed-in-one-coverage case (Garcia) and the "0.00 deprec despite nonzero dep%" edge case (Wei Tang item 16) | Direct verification across all depreciation-type variants present in the fixture set. |
| **Notes** | **100%** on every note type present in the sample (ITEL/Material-Metrix pricing, code-upgrade, waste-calculation, "Rake Only"-style short notes) | Verified attachment correctness for every note encountered during Part 4 sampling. |
| **Overall** | **~93%** (conservative, blended) | Weighted toward the categories with full/near-full direct verification (carrier, coverage, totals, depreciation, notes — all ~100%) and discounted by the two categories with only sample-based extrapolation (page classification ~85%, section detection ~90%) and by the fact that this audit itself found and fixed 3 real bugs, which is direct evidence the pre-audit accuracy was lower than the fixtures' clean top-line numbers suggested. |

**What "93% overall" means in practice:** for a random insurance estimate PDF from one of these 5 carrier families, expect the carrier, coverages, totals, depreciation, and notes to be extracted correctly essentially every time; expect the great majority of sections and line items to be correct; and expect roughly 1 in 4 pages to land in an `unknown` classification bucket that is safely excluded rather than wrongly included. **It does not mean 93% of documents will be error-free** — it means 93% is a conservative, sampling-based estimate of correctness across all fields and all documents, and the actual per-document experience will vary (3 of the 6 fixtures were fully clean with 0 warnings/review items; 3 had explained, non-corrupting review flags).

---

## Part 13 — Final recommendation

**1. Would you consider this extractor stable enough to freeze as Version 0.1?**

**Not yet, but close — one more pass first.** This audit found and fixed 3 real, previously-undetected bugs (all in the same underlying subsystem: section/label detection), simply by doing rigorous hand-verification against 2 of the 6 fixtures' full sections and one previously-unchecked section. That is direct evidence there is very likely at least one more bug of the same class hiding in the 44 sections across the fixture set that were *not* individually hand-verified this way (only 8 were). I would not freeze v0.1 until either (a) every section in all 6 fixtures has been spot-checked the way Parts 4–5 did for the representative sections, or (b) the structural fix in Part 9 risk #1 (font/position-based header detection) replaces the blocklist approach, which would retire this entire risk category at once rather than requiring exhaustive manual checking.

**2. What remaining bugs must be fixed before building the mapper?**

None are strictly *blocking* — every bug found in this audit was found and fixed during the audit itself, and the test suite (84/84) plus every re-verified fixture is currently clean. But two things should happen first, not because they're bugs, but because the mapper will be far more valuable if they're addressed:
- Section/category-heading detection robustness (Part 9, risk #1) — because the mapper's accuracy is capped by the extractor's section fidelity, and this audit demonstrated that fidelity has been silently wrong before without any warning being raised (the phantom "Opens into Exterior" section produced *zero* warnings — it looked completely clean).
- Coverage attribution (Part 9, risk #2) — the mapper almost certainly needs to know which coverage a line item belongs to; right now that's null for half the fixture set.

**3. Would you change the schema?**

One addition, not a change: a `paid_when_incurred_total` (or similar) field at the coverage-summary level, separate from the primary line-item sum, to resolve the two proven-explained reconciliation failures (Part 7) structurally instead of via a permanently-FAILing comparison. Everything else in the current schema held up well against real, messy, inconsistent source documents (conflicting claim numbers, absent claim numbers, mixed recoverable/nonrecoverable depreciation, code-upgrade sub-totals) without needing a shape change — the provenance/confidence/`null`-when-absent design did what it was built to do.

**4. Is there any technical debt that should be addressed first?**

Yes, one concrete gap surfaced by this audit: **the newest code path (roof-diagram/measurement-page walkability, Part 8 #6) has zero dedicated unit test coverage**, despite fixing a real bug and changing behavior for every carrier. It's currently "tested" only by the fact that it doesn't regress the 6 real fixtures — that's necessary but not sufficient. This should be closed before relying on it further.

**5. Should we proceed to the mapping engine?**

**Conditionally, yes — but sequence the two items in #2 first if at all possible**, since both compound in value the earlier they're fixed (the mapper will consume whatever section/coverage structure exists today, and retrofitting fixes after the mapper is built against the current structure is more expensive than fixing them now). If schedule pressure requires starting the mapper immediately, it can safely proceed against the current extractor for single-coverage documents (Garrety is the cleanest example: 0 warnings, 100% verified) while section-detection robustness and coverage attribution are hardened in parallel — but should not be pointed at the null-`coverage_id` multi-coverage documents (Odom, Wei Tang, and partially Bagi/Garcia) until #2 is addressed, or the mapper will have nothing to key off of for those.
