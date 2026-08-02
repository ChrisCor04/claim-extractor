"""Coverage-level financial summary extraction.

Looks for "Summary for <Coverage Name>" blocks (and the unlabeled
"<Coverage Name> Paid When Incurred" variant some carriers print) across
every metadata-eligible page's text, concatenated in page order so a block
that is split across a page break (seen in both the USAA and Farmers
fixtures) is parsed as one continuous block.
"""

from __future__ import annotations

import re
from decimal import Decimal

from estimate_extractor.models.canonical import Coverage, CoverageSummaryFinancials
from estimate_extractor.models.page import ParsedDocument, PageRecord
from estimate_extractor.normalization.money import parse_decimal
from estimate_extractor.parsing.metadata import is_metadata_eligible

COVERAGE_CODE_RE = re.compile(r"^(?:Coverage|Cov\.?)\s+([A-Za-z0-9]+)\s*-\s*(.+)$", re.IGNORECASE)
# Bare "XX-Name" form (no "Coverage"/"Cov" word), e.g. Allstate's
# "AA-Dwelling" / "BB-Other Structures".
BARE_CODE_RE = re.compile(r"^([A-Z]{1,3})-([A-Za-z].*)$")

# (label prefix, CoverageSummaryFinancials field name), most specific first.
FINANCIAL_LABEL_PREFIXES: list[tuple[str, str]] = [
    ("total recoverable depreciation", "recoverable_depreciation"),
    ("less non - recoverable depreciation", "nonrecoverable_depreciation"),
    ("less non-recoverable depreciation", "nonrecoverable_depreciation"),
    ("total line item depreciation", "total_depreciation"),
    ("total depreciation", "total_depreciation"),
    ("less depreciation", "total_depreciation"),
    ("net claim if depreciation is recovered", "net_claim_if_depreciation_recovered"),
    ("total amount of claim if incurred", "net_claim_if_depreciation_recovered"),
    ("net actual cash value payment", "net_claim"),
    ("net claim", "net_claim"),
    ("less deductible", "deductible"),
    ("material sales tax", "material_sales_tax"),
    ("comm repr/remod tax", "material_sales_tax"),
    ("cleaning mtl tax", "cleaning_material_tax"),
    ("cleaning sales tax", "cleaning_sales_tax"),
    ("total paid when incurred", "replacement_cost_value"),
    ("replacement cost value", "replacement_cost_value"),
    ("actual cash value", "actual_cash_value"),
    ("line item total", "line_item_total"),
    ("subtotal", "subtotal"),
    ("overhead", "overhead"),
    ("profit", "profit"),
]


def _match_financial_label(low: str) -> str | None:
    for prefix, field in FINANCIAL_LABEL_PREFIXES:
        if low.startswith(prefix):
            return field
    return None


def _split_coverage_name(raw: str) -> tuple[str | None, str, str | None]:
    m = COVERAGE_CODE_RE.match(raw)
    code = None
    remainder = raw
    if m:
        code = m.group(1)
        remainder = m.group(2).strip()
    else:
        m2 = BARE_CODE_RE.match(raw)
        if m2:
            code = m2.group(1)
            remainder = m2.group(2).strip()
    parts = [p.strip() for p in remainder.split(" - ") if p.strip()]
    name = parts[0] if parts else remainder
    subcoverage = " - ".join(parts[1:]) if len(parts) > 1 else None
    return code, name, subcoverage


def _match_opener(combined: list[tuple[int, str]], i: int) -> tuple[str, bool, int] | None:
    n = len(combined)
    line = combined[i][1].strip()
    low = line.lower()

    if low.startswith("summary for"):
        rest = line[len("summary for") :].strip()
        j = i + 1
        if not rest:
            while j < n and not combined[j][1].strip():
                j += 1
            if j >= n:
                return None
            rest = combined[j][1].strip()
            j += 1
        if rest.lower() == "all items":
            return None

        # A coverage name occasionally wraps across two physical PDF lines
        # (e.g. "... - 35 Windstorm and" / "Hail"). If the captured name
        # ends on a dangling conjunction/preposition, merge in the next
        # short fragment before treating it as complete.
        if rest.split()[-1].lower() in {"and", "or", "the", "of", "for", "a", "an", "&", "-"}:
            j2 = j
            while j2 < n and not combined[j2][1].strip():
                j2 += 1
            if j2 < n:
                frag = combined[j2][1].strip()
                if (
                    frag
                    and len(frag.split()) <= 3
                    and not frag.lower().startswith("summary for")
                    and not frag.lower().endswith("paid when incurred")
                    and _match_financial_label(frag.lower().rstrip(":")) is None
                ):
                    rest = f"{rest} {frag}"
                    j = j2 + 1

        k = j
        while k < n and not combined[k][1].strip():
            k += 1
        if k < n and combined[k][1].strip().lower() == "summary for all items":
            k += 1
        return rest, False, k

    if low.endswith("paid when incurred") and len(line) < 90:
        name = line[: -len("paid when incurred")].strip(" -")
        if name:
            return name, True, i + 1

    return None


def _parse_financial_block(
    combined: list[tuple[int, str]], start_i: int
) -> tuple[dict[str, Decimal], set[int], int]:
    values: dict[str, Decimal] = {}
    pages: set[int] = set()
    j = start_i
    n = len(combined)
    misses = 0
    while j < n and misses < 4:
        page_num, raw = combined[j]
        line = raw.strip()
        if not line:
            j += 1
            continue
        if _match_opener(combined, j) is not None:
            break
        low = line.lower().rstrip(":")
        field = _match_financial_label(low)
        if field:
            pages.add(page_num)
            value_text = None
            if ":" in line:
                _, _, after = line.partition(":")
                if after.strip():
                    value_text = after.strip()
            if value_text is None:
                k = j + 1
                while k < n and not combined[k][1].strip():
                    k += 1
                if k < n:
                    value_text = combined[k][1].strip()
                    pages.add(combined[k][0])
                    j = k
            dec = parse_decimal(value_text) if value_text else None
            if dec is not None:
                values.setdefault(field, dec)
            misses = 0
            j += 1
            continue
        misses += 1
        j += 1
    return values, pages, j


def extract_coverages(document: ParsedDocument, page_records: list[PageRecord], id_factory) -> list[Coverage]:
    eligible = sorted((pr for pr in page_records if is_metadata_eligible(pr)), key=lambda p: p.page)
    combined: list[tuple[int, str]] = []
    for pr in eligible:
        page = document.pages[pr.page - 1]
        for line in page.raw_text.splitlines():
            combined.append((pr.page, line))

    coverages: list[Coverage] = []
    i = 0
    n = len(combined)
    while i < n:
        opener = _match_opener(combined, i)
        if opener:
            name, paid, next_i = opener
            values, pages, end_i = _parse_financial_block(combined, next_i)
            code, short_name, subcoverage = _split_coverage_name(name)
            financials = CoverageSummaryFinancials(**{k: float(v) for k, v in values.items()})
            pages.add(combined[i][0])
            coverages.append(
                Coverage(
                    coverage_id=id_factory.next("coverage"),
                    code=code,
                    carrier_code=None,
                    name=short_name,
                    subcoverage=subcoverage,
                    deductible=financials.deductible,
                    policy_limit=None,
                    paid_when_incurred=paid,
                    summary=financials,
                    source_pages=sorted(pages),
                    confidence=0.9,
                )
            )
            i = max(end_i, i + 1)
            continue
        i += 1
    return coverages
