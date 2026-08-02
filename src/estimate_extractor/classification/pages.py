"""Generic (carrier-agnostic) page classification.

Runs before carrier detection (see implementation priorities in the spec):
most of the signal that separates an instructional sample page from a real
estimate-detail page is structural, not carrier-specific -- it doesn't need
to know which carrier produced the PDF.

The most important rule in this module: instructional/sample pages must
never be treated as a source of real claim metadata or real line items.
``NON_ESTIMATE_CLASSIFICATIONS`` in ``models/page.py`` drives that; this
module only has to classify correctly.
"""

from __future__ import annotations

import re

from estimate_extractor.models.page import PageClassification, PageRecord, ParsedPage

FEET_INCHES_RE = re.compile(r"\d+'\s*\d*\"?")

INSTRUCTIONAL_PHRASES = (
    "sample estimate",
    "guide to understanding",
    "this summary guide is based on a sample estimate",
    "provided for reference only",
    "for illustrative purposes",
    "for informational purposes only",
    "this is not an actual claim",
    "example to the right depicts",
    "your estimate cover sheet",  # Travelers annotated-example page title
    "your estimate detail",
    "your estimate summary",
)

# Specific placeholder values observed in carrier-provided "guide" pages.
# These are deliberately literal (rather than generic pattern rules) because
# a false-positive here would exclude real customer data, which is worse
# than a false-negative that lets a guide page through unclassified.
INSTRUCTIONAL_PLACEHOLDER_MARKERS = (
    "smith, joe & jane",
    "1 main street",
    "anywhere, il",
    "00-0000-000",
    "00-00-0000-0",
    "guide_example",
    "john smith",
    "1234 oak street",
    "abc1234001h",
    "123456789-633-1",
)

FAQ_PHRASES = (
    "frequently asked questions",
    "commonly asked questions",
)

REPLACEMENT_COST_EXPLANATION_PHRASES = (
    "explanation of building replacement cost benefits",
    "replacement cost benefits pays the actual and necessary cost",
)

DEPRECIATION_GUIDE_PHRASES = (
    "what is depreciation",
    "recoverable depreciation is",
    "using this guide",
)

TAX_RECAP_PHRASES = ("recap of taxes",)
OVERHEAD_PROFIT_PHRASES = ("overhead and profit",)
RECAP_BY_ROOM_PHRASES = ("recap by room", "recap by category")

CLAIM_HEADER_LABELS = (
    "claim number",
    "claim #",
    "policy number",
    "insured:",
    "member number",
    "l/r number",
)

COVERAGE_SUMMARY_PHRASES = (
    "line item total",
    "replacement cost value",
    "actual cash value",
    "net claim",
)

MEASUREMENT_PHRASES = (
    "surface area",
    "total perimeter length",
    "number of squares",
    "total ridge length",
    "grand total areas",
    "sf walls",
)

SETTLEMENT_PHRASES = (
    "we are settling your claim",
    "enclosed is our settlement",
    "settlement notice",
)


def _has_any(text_lower: str, phrases: tuple[str, ...]) -> str | None:
    for p in phrases:
        if p in text_lower:
            return p
    return None


def classify_page(page: ParsedPage) -> PageRecord:
    text = page.raw_text
    text_lower = text.lower()
    reasons: list[str] = []

    if not text.strip():
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.BLANK,
            include_in_estimate=False,
            confidence=0.99,
            reasons=["Page has no extractable text."],
        )

    has_quantity_header = "quantity" in text_lower
    has_numbered_items = bool(re.search(r"(?m)^\*?\s*\d{1,3}\.\s+\S", text))
    is_continuation = bool(re.search(r"(?i)continued\s*-\s*\S", text))

    # 1. Instructional / sample content is checked first and wins over
    #    everything else, including the presence of what looks like a
    #    line-item table -- the spec's Wei Tang / Aranda / Bagi fixtures all
    #    include full fake estimate tables on their "guide" pages.
    marker = _has_any(text_lower, INSTRUCTIONAL_PLACEHOLDER_MARKERS)
    if marker:
        reasons.append(f"Contains known placeholder marker: '{marker}'")
    phrase = _has_any(text_lower, INSTRUCTIONAL_PHRASES)
    if phrase:
        reasons.append(f"Contains instructional-guide phrase: '{phrase}'")
    if marker or (phrase and (marker or "guide" in text_lower or "example" in text_lower)):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.INSTRUCTIONAL_SAMPLE,
            include_in_estimate=False,
            confidence=0.95 if marker else 0.85,
            reasons=reasons or [f"Contains instructional-guide phrase: '{phrase}'"],
        )

    # 2. Estimate detail: a numbered line-item table with a QUANTITY column.
    if has_quantity_header and has_numbered_items:
        cls = (
            PageClassification.ESTIMATE_DETAIL_CONTINUATION
            if is_continuation
            else PageClassification.ESTIMATE_DETAIL
        )
        r = ["Contains QUANTITY column header and numbered line items."]
        if is_continuation:
            r.append("Contains 'CONTINUED -' section marker.")
        return PageRecord(
            page=page.page_number, classification=cls, include_in_estimate=True, confidence=0.97, reasons=r
        )

    # 3. Recap / recap-adjacent pages (checked before generic coverage
    #    summary, since recap pages also mention RCV/ACV/tax words).
    if _has_any(text_lower, RECAP_BY_ROOM_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.RECAP_BY_ROOM,
            include_in_estimate=True,
            confidence=0.9,
            reasons=["Contains 'Recap by Room/Category' heading."],
        )
    if _has_any(text_lower, OVERHEAD_PROFIT_PHRASES) and _has_any(text_lower, TAX_RECAP_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.OVERHEAD_PROFIT_RECAP,
            include_in_estimate=True,
            confidence=0.9,
            reasons=["Contains 'Recap of Taxes, Overhead and Profit' heading."],
        )
    if _has_any(text_lower, TAX_RECAP_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.TAX_RECAP,
            include_in_estimate=True,
            confidence=0.9,
            reasons=["Contains 'Recap of Taxes' heading."],
        )

    # 4. Replacement-cost explanation (carries real claim facts in most
    #    carrier templates, so include_in_estimate stays True for metadata
    #    purposes even though it has no line items).
    if _has_any(text_lower, REPLACEMENT_COST_EXPLANATION_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.REPLACEMENT_COST_EXPLANATION,
            include_in_estimate=False,
            confidence=0.92,
            reasons=["Contains 'Explanation of Building Replacement Cost Benefits' heading."],
        )
    if _has_any(text_lower, DEPRECIATION_GUIDE_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.DEPRECIATION_GUIDE,
            include_in_estimate=False,
            confidence=0.75,
            reasons=["Contains generic depreciation-explanation language."],
        )

    # 5. FAQ.
    if _has_any(text_lower, FAQ_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.FAQ,
            include_in_estimate=False,
            confidence=0.85,
            reasons=["Contains FAQ heading language."],
        )

    # 6. Settlement notice.
    if _has_any(text_lower, SETTLEMENT_PHRASES):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.SETTLEMENT_NOTICE,
            include_in_estimate=False,
            confidence=0.85,
            reasons=["Contains settlement-notice language."],
        )

    # 7. Coverage summary: "Summary for <Coverage>" financial recap without
    #    a line-item table on this page.
    coverage_summary_hits = sum(1 for p in COVERAGE_SUMMARY_PHRASES if p in text_lower)
    if "summary for" in text_lower and coverage_summary_hits >= 2:
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.COVERAGE_SUMMARY,
            include_in_estimate=True,
            confidence=0.9,
            reasons=["Contains 'Summary for <Coverage>' financial recap."],
        )

    # 8. Roof diagram vs. measurement summary: both are dense with
    #    measurement vocabulary; roof diagrams are additionally dense with
    #    feet/inches tokens (rafter/fascia lengths from the sketch).
    measurement_hits = sum(1 for p in MEASUREMENT_PHRASES if p in text_lower)
    feet_inches_hits = len(FEET_INCHES_RE.findall(text))
    if measurement_hits >= 2 or feet_inches_hits >= 6:
        if feet_inches_hits >= 6:
            return PageRecord(
                page=page.page_number,
                classification=PageClassification.ROOF_DIAGRAM,
                include_in_estimate=True,
                confidence=0.85,
                reasons=[f"Dense with feet/inches diagram tokens ({feet_inches_hits})."],
            )
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.MEASUREMENT_SUMMARY,
            include_in_estimate=True,
            confidence=0.8,
            reasons=["Contains aggregated measurement recap vocabulary."],
        )

    # 9. Claim metadata header block (Insured/Property/Claim Number/...)
    #    without a line-item table.
    claim_hits = sum(1 for p in CLAIM_HEADER_LABELS if p in text_lower)
    if claim_hits >= 2:
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.CLAIM_METADATA,
            include_in_estimate=True,
            confidence=0.85,
            reasons=[f"Contains {claim_hits} claim-header labels (Claim/Policy/Insured/...)."],
        )

    # 10. A narrative cover letter (greeting language, no structured
    #     table), possibly still carrying real claim metadata in a header
    #     block (several carriers combine both on page 1).
    if any(w in text_lower for w in ("dear ", "we have prepared this estimate", "claims", "claim representative")):
        return PageRecord(
            page=page.page_number,
            classification=PageClassification.CARRIER_LETTER,
            include_in_estimate=claim_hits >= 1,
            confidence=0.75,
            reasons=["Narrative cover-letter language detected."],
        )

    return PageRecord(
        page=page.page_number,
        classification=PageClassification.UNKNOWN,
        include_in_estimate=False,
        confidence=0.3,
        reasons=["No page-classification heuristic matched; defaulting to unknown."],
    )


def classify_document(pages: list[ParsedPage]) -> list[PageRecord]:
    return [classify_page(p) for p in pages]
