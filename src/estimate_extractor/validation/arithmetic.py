"""Decimal-safe arithmetic reconciliation checks.

Every check here is best-effort: it only runs when all the inputs it needs
are present, and a carrier's presentation choices (e.g. Allstate's estimate
detail table has no per-line tax column) explain a chunk of the intentional
skips rather than being treated as failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from estimate_extractor.config import ValidationConfig
from estimate_extractor.models.canonical import CanonicalEstimate, LineItem, SummaryType
from estimate_extractor.models.validation import Issue, IssueCode, Severity


@dataclass(frozen=True, slots=True)
class ToleranceResult:
    within_tolerance: bool
    difference: Decimal


def within_tolerance(expected: Decimal, actual: Decimal, config: ValidationConfig) -> ToleranceResult:
    diff = abs(expected - actual)
    if diff <= config.money_absolute_tolerance:
        return ToleranceResult(True, diff)
    if expected != 0:
        pct = diff / abs(expected)
        if pct <= config.percentage_tolerance:
            return ToleranceResult(True, diff)
    return ToleranceResult(False, diff)


def _d(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def check_line_item_arithmetic(
    line_items: list[LineItem], config: ValidationConfig, issue_ids
) -> list[Issue]:
    issues: list[Issue] = []
    for li in line_items:
        qty, price, tax, oap, rcv = (
            _d(li.quantity),
            _d(li.unit_price),
            _d(li.tax),
            _d(li.overhead_and_profit),
            _d(li.replacement_cost_value),
        )
        if qty is not None and price is not None and rcv is not None:
            expected = qty * price + (tax or Decimal("0")) + (oap or Decimal("0"))
            result = within_tolerance(expected, rcv, config)
            if not result.within_tolerance:
                issues.append(
                    Issue(
                        issue_id=next(issue_ids),
                        severity=Severity.WARNING,
                        code=IssueCode.LINE_ITEM_ARITHMETIC_MISMATCH,
                        page=li.source.page_start,
                        line_item_id=li.line_item_id,
                        field="replacement_cost_value",
                        message=(
                            f"quantity*unit_price+tax+O&P = {expected} but RCV = {rcv} "
                            f"(difference {result.difference})."
                        ),
                        suggested_action=f"Review source page {li.source.page_start}.",
                    )
                )

        rcv, dep, acv = _d(li.replacement_cost_value), _d(li.depreciation_amount), _d(li.actual_cash_value)
        if rcv is not None and dep is not None and acv is not None:
            expected_acv = rcv - dep
            result = within_tolerance(expected_acv, acv, config)
            if not result.within_tolerance:
                issues.append(
                    Issue(
                        issue_id=next(issue_ids),
                        severity=Severity.WARNING,
                        code=IssueCode.RCV_DEPRECIATION_ACV_MISMATCH,
                        page=li.source.page_start,
                        line_item_id=li.line_item_id,
                        field="actual_cash_value",
                        message=(
                            f"RCV - depreciation = {expected_acv} but ACV = {acv} "
                            f"(difference {result.difference})."
                        ),
                        suggested_action=f"Review source page {li.source.page_start}.",
                    )
                )

        if rcv is not None and acv is not None and acv > rcv + config.money_absolute_tolerance:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.ERROR,
                    code=IssueCode.ACV_GREATER_THAN_RCV,
                    page=li.source.page_start,
                    line_item_id=li.line_item_id,
                    field="actual_cash_value",
                    message=f"ACV ({acv}) is greater than RCV ({rcv}).",
                    suggested_action=f"Review source page {li.source.page_start}.",
                )
            )
        if rcv is not None and dep is not None and dep > rcv + config.money_absolute_tolerance:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.ERROR,
                    code=IssueCode.DEPRECIATION_GREATER_THAN_RCV,
                    page=li.source.page_start,
                    line_item_id=li.line_item_id,
                    field="depreciation_amount",
                    message=f"Depreciation ({dep}) is greater than RCV ({rcv}).",
                    suggested_action=f"Review source page {li.source.page_start}.",
                )
            )
    return issues


def check_section_totals(
    canonical: CanonicalEstimate, config: ValidationConfig, issue_ids
) -> list[Issue]:
    issues: list[Issue] = []
    rcv_by_section: dict[str, Decimal] = {}
    for li in canonical.line_items:
        if li.section_id is None or li.replacement_cost_value is None:
            continue
        rcv_by_section[li.section_id] = rcv_by_section.get(li.section_id, Decimal("0")) + _d(
            li.replacement_cost_value
        )

    for st in canonical.summary_totals:
        if st.summary_type != SummaryType.SECTION_TOTAL or st.section_id is None:
            continue
        if st.replacement_cost_value is None or st.section_id not in rcv_by_section:
            continue
        expected = Decimal(str(st.replacement_cost_value))
        actual = rcv_by_section[st.section_id]
        result = within_tolerance(expected, actual, config)
        if not result.within_tolerance:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.WARNING,
                    code=IssueCode.SECTION_TOTAL_MISMATCH,
                    page=st.source_page,
                    line_item_id=None,
                    field="replacement_cost_value",
                    message=(
                        f"Section '{st.label}': sum of line-item RCV ({actual}) does not match "
                        f"reported section total RCV ({expected}); difference {result.difference}."
                    ),
                    suggested_action=f"Review source page {st.source_page}.",
                )
            )
    return issues


def compute_reconciliation(
    canonical: CanonicalEstimate, config: ValidationConfig
) -> tuple[float | None, float | None, float | None, float | None, bool | None, str | None]:
    """Returns (reported, calculated, paid_when_incurred_total, difference,
    within_tolerance, note).

    Code-upgrade items (``flags.is_code_upgrade``) are carrier-settled as
    "payable when incurred" and are excluded from the *primary carrier
    total* the document itself prints -- confirmed against two independent
    fixtures (Wei Tang, Odom): in both, the exact dollar gap between the
    naive sum-of-everything and the document's reported total is precisely
    the sum of that document's code-upgrade-flagged items' RCV. They are
    summed separately here as ``paid_when_incurred_total`` and excluded from
    ``calculated_line_item_total`` so the comparison is apples-to-apples --
    but they are NOT removed from ``canonical.line_items`` itself; every
    extracted line item is still preserved in the canonical output.
    """
    code_upgrade_items = [li for li in canonical.line_items if li.flags.is_code_upgrade]
    normal_items = [li for li in canonical.line_items if not li.flags.is_code_upgrade]

    calculated = sum((_d(li.replacement_cost_value) or Decimal("0")) for li in normal_items)
    paid_when_incurred = sum((_d(li.replacement_cost_value) or Decimal("0")) for li in code_upgrade_items)

    reported = None
    for st in canonical.summary_totals:
        if st.summary_type == SummaryType.LINE_ITEM_TOTAL and st.replacement_cost_value is not None:
            reported = Decimal(str(st.replacement_cost_value))
            break
    if reported is None:
        cov_sum = sum(
            (Decimal(str(c.summary.replacement_cost_value)) for c in canonical.coverages if c.summary.replacement_cost_value is not None),
            Decimal("0"),
        )
        if cov_sum > 0:
            reported = cov_sum

    paid_when_incurred_out = float(paid_when_incurred) if code_upgrade_items else None

    note = None
    if not canonical.line_items:
        note = "No line items were extracted; reconciliation could not be attempted."
        return None, float(calculated), paid_when_incurred_out, None, None, note

    if reported is None:
        note = "No document-reported line-item/coverage RCV total was found to reconcile against."
        return None, float(calculated), paid_when_incurred_out, None, None, note

    result = within_tolerance(reported, calculated, config)
    if not result.within_tolerance:
        has_paid_when_incurred = any(c.paid_when_incurred for c in canonical.coverages)
        multi_coverage = len(canonical.coverages) > 1
        if code_upgrade_items:
            note = (
                f"Calculated total excludes {len(code_upgrade_items)} code-upgrade "
                f"line item(s) totaling ${paid_when_incurred} (see "
                "reconciliation.paid_when_incurred_total), but a difference remains. "
                "This may indicate additional coverage-attribution ambiguity beyond "
                "the code-upgrade exclusion -- see 'issues' for details."
            )
        elif has_paid_when_incurred or multi_coverage:
            note = (
                "The document reports separate totals per coverage (and/or a "
                "'Paid When Incurred' sub-total for code-upgrade items), but line "
                "items are not reliably attributable to a single coverage from the "
                "printed layout alone (see docs/canonical-schema.md 'Known "
                "limitations'). The calculated total sums every extracted line "
                "item across all coverages, so it will not match a single "
                "coverage's reported 'Line Item Total' when the document has more "
                "than one coverage bucket."
            )
        else:
            note = (
                "Calculated line-item total does not match the document's reported "
                "total within tolerance; see 'issues' for any parsing warnings on "
                "individual line items that may explain the gap."
            )
    elif code_upgrade_items:
        note = (
            f"Reconciled after excluding {len(code_upgrade_items)} code-upgrade line "
            f"item(s) totaling ${paid_when_incurred} from the primary total (see "
            "reconciliation.paid_when_incurred_total). These items are still present "
            "in line_items; they are carrier-settled as 'payable when incurred' and "
            "excluded only from this comparison."
        )
    return (
        float(reported),
        float(calculated),
        paid_when_incurred_out,
        float(result.difference),
        result.within_tolerance,
        note,
    )
