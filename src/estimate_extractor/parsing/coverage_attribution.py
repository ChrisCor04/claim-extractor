"""Conservative coverage attribution for multi-coverage documents.

When a document has exactly one coverage, every section unambiguously
belongs to it (handled elsewhere, in state_machine.py's default_coverage_id).
When a document has more than one coverage, the printed estimate-detail
text does not reliably state which coverage a given section rolls up into
-- see docs/canonical-schema.md "Known limitations" #1. Rather than guess,
this module only assigns `coverage_id` to a section (and cascades it to
that section's line items and, when uniform, its area) when a UNIQUE exact
partition of sections into coverage-matching groups exists within a tight
money tolerance. If no such partition exists -- which is common, since most
real documents in the fixture set do NOT reconcile this cleanly -- every
`coverage_id` stays `null` and a human-readable explanation is returned for
the validation report, exactly as before this module existed.

This is deliberately conservative: false silence (leaving coverage_id null
when it could theoretically be inferred) is preferred over false confidence
(assigning a coverage_id that turns out to be wrong).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from estimate_extractor.models.canonical import Area, Coverage, LineItem, Section, SummaryTotal, SummaryType

# Safety valve: exact partition search is exponential in the number of
# sections in the worst case (pruned, but still bounded here defensively).
# Every fixture in the development set has at most 9 sections; 16 gives
# headroom without risking a pathological search on a much larger document.
_MAX_SECTIONS_FOR_SEARCH = 16


@dataclass(frozen=True, slots=True)
class AttributionResult:
    assigned_section_ids: frozenset[str]
    notes: list[str]


def _section_rcv_totals(
    sections: list[Section], line_items: list[LineItem], summary_totals: list[SummaryTotal]
) -> dict[str, Decimal]:
    """Each section's RCV total: prefer its own printed "Totals: X" row
    (SECTION_TOTAL summary), fall back to summing its line items' RCV."""
    totals: dict[str, Decimal] = {}
    printed: dict[str, Decimal] = {}
    for st in summary_totals:
        if st.summary_type == SummaryType.SECTION_TOTAL and st.section_id and st.replacement_cost_value is not None:
            printed[st.section_id] = Decimal(str(st.replacement_cost_value))

    summed: dict[str, Decimal] = {}
    for li in line_items:
        if li.section_id and li.replacement_cost_value is not None:
            summed[li.section_id] = summed.get(li.section_id, Decimal("0")) + Decimal(str(li.replacement_cost_value))

    for section in sections:
        if section.section_id in printed:
            totals[section.section_id] = printed[section.section_id]
        elif section.section_id in summed:
            totals[section.section_id] = summed[section.section_id]
    return totals


def _find_unique_partition(
    items: list[tuple[str, Decimal]], targets: list[Decimal], tolerance: Decimal
) -> list[int] | None:
    """Assign each item to one of len(targets) buckets so each bucket's sum
    is within tolerance of its target. Returns the assignment (bucket index
    per item, by position in `items`) only if EXACTLY ONE such assignment
    exists; None if zero or more than one (ambiguous)."""
    n = len(items)
    k = len(targets)
    assignment = [-1] * n
    solutions: list[list[int]] = []

    def backtrack(i: int, running: list[Decimal]) -> None:
        if len(solutions) > 1:
            return  # already ambiguous; stop searching
        if i == n:
            if all(abs(running[b] - targets[b]) <= tolerance for b in range(k)):
                solutions.append(assignment.copy())
            return
        _name, value = items[i]
        for b in range(k):
            if running[b] + value <= targets[b] + tolerance:
                assignment[i] = b
                running[b] += value
                backtrack(i + 1, running)
                running[b] -= value
        assignment[i] = -1

    backtrack(0, [Decimal("0")] * k)
    return solutions[0] if len(solutions) == 1 else None


def attribute_coverages(
    coverages: list[Coverage],
    areas: list[Area],
    sections: list[Section],
    line_items: list[LineItem],
    summary_totals: list[SummaryTotal],
    tolerance: Decimal,
) -> AttributionResult:
    """Mutates `coverage_id` on `sections`, `line_items`, and (when uniform
    across all of an area's sections) `areas` in place. Returns which
    section IDs were assigned plus explanatory notes for anything that
    could not be resolved."""
    notes: list[str] = []

    if len(coverages) <= 1:
        return AttributionResult(frozenset(), notes)

    value_counts: dict[Decimal, int] = {}
    for c in coverages:
        if c.summary.replacement_cost_value is None:
            continue
        v = Decimal(str(c.summary.replacement_cost_value))
        if v > 0:
            value_counts[v] = value_counts.get(v, 0) + 1
    ambiguous_values = {v for v, count in value_counts.items() if count > 1}

    usable_coverages = [
        c
        for c in coverages
        if c.summary.replacement_cost_value is not None
        and Decimal(str(c.summary.replacement_cost_value)) > 0
        and Decimal(str(c.summary.replacement_cost_value)) not in ambiguous_values
    ]
    if len(usable_coverages) < 2:
        if ambiguous_values:
            notes.append(
                "Coverage attribution skipped: multiple coverages report the same "
                "total, so a section-to-coverage match would be ambiguous by amount alone."
            )
        else:
            notes.append(
                "Coverage attribution skipped: fewer than two coverages have a usable "
                "(positive, unambiguous) reported total to match sections against."
            )
        return AttributionResult(frozenset(), notes)

    targets = [Decimal(str(c.summary.replacement_cost_value)) for c in usable_coverages]

    section_totals = _section_rcv_totals(sections, line_items, summary_totals)
    items = [(section_id, total) for section_id, total in section_totals.items()]

    if not items:
        notes.append("Coverage attribution skipped: no section has a computable RCV total.")
        return AttributionResult(frozenset(), notes)
    if len(items) > _MAX_SECTIONS_FOR_SEARCH:
        notes.append(
            f"Coverage attribution skipped: {len(items)} sections exceeds the safe "
            f"exact-partition search bound ({_MAX_SECTIONS_FOR_SEARCH})."
        )
        return AttributionResult(frozenset(), notes)

    assignment = _find_unique_partition(items, targets, tolerance)
    if assignment is None:
        notes.append(
            "Coverage attribution skipped: no unique exact partition of sections into "
            "the reported coverage totals was found within tolerance (the printed "
            "layout does not state which coverage each section belongs to, and no "
            "unambiguous arithmetic match exists -- see docs/canonical-schema.md "
            "'Known limitations')."
        )
        return AttributionResult(frozenset(), notes)

    section_to_coverage: dict[str, str] = {
        items[i][0]: usable_coverages[assignment[i]].coverage_id for i in range(len(items))
    }

    sections_by_id = {s.section_id: s for s in sections}
    for section_id, coverage_id in section_to_coverage.items():
        sections_by_id[section_id].coverage_id = coverage_id

    for li in line_items:
        if li.section_id in section_to_coverage:
            li.coverage_id = section_to_coverage[li.section_id]

    sections_by_area: dict[str, list[Section]] = {}
    for section in sections:
        if section.area_id:
            sections_by_area.setdefault(section.area_id, []).append(section)
    for area in areas:
        member_sections = sections_by_area.get(area.area_id, [])
        member_coverage_ids = {s.coverage_id for s in member_sections}
        if member_sections and len(member_coverage_ids) == 1 and None not in member_coverage_ids:
            area.coverage_id = member_coverage_ids.pop()

    return AttributionResult(frozenset(section_to_coverage.keys()), notes)
