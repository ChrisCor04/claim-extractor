"""Claim metadata, address, and contact extraction.

Label-driven and carrier-agnostic: every carrier in the fixture set prints
the claim header as "<Label>:" either immediately followed by the value on
the same line, or with the value on the next physical line. This module
scans every page whose classification is metadata-eligible (i.e. NOT
instructional_sample/blank/unknown/attachment -- see
``models.page.NON_ESTIMATE_CLASSIFICATIONS``, minus the carve-out that
instructional_sample is the one classification we must never trust) and
collects candidate values per field, then picks the majority value so a
one-off OCR glitch or a single conflicting page doesn't win.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from estimate_extractor.models.canonical import AddressValue, ClaimData, Contact, ContactRole, FieldValue
from estimate_extractor.models.page import PageClassification, PageRecord, ParsedDocument
from estimate_extractor.normalization.dates import normalize_date
from estimate_extractor.normalization.text import normalize_whitespace

PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
CITY_STATE_ZIP_RE = re.compile(r"^(?P<city>[A-Za-z .'\-]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
NAME_SIGNATURE_RE = re.compile(r"^[A-Z][A-Za-z'\-]+,\s*[A-Z][A-Za-z'\-.() ]+$")

# field_name -> accepted label text (matched case-insensitively, colon
# stripped). Order doesn't matter; all variants are checked per line.
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "claim_number": ("claim number", "claim #"),
    "estimate_number": ("estimate",),
    "policy_number": ("policy number",),
    "insured_name": ("insured", "customer"),
    "claimant_name": ("claimant",),
    "type_of_loss": ("type of loss",),
    "cause_of_loss": ("cause of loss",),
    "date_of_loss": ("date of loss",),
    "date_contacted": ("date contacted",),
    "date_received": ("date received",),
    "date_inspected": ("date inspected",),
    "date_entered": ("date entered",),
    "date_completed": ("date completed", "date est. completed"),
    "price_list": ("price list",),
    "insurance_company": ("insurance company", "company"),
    "member_number": ("member number",),
    "lr_number": ("l/r number",),
}

DATE_FIELDS = {
    "date_of_loss",
    "date_contacted",
    "date_received",
    "date_inspected",
    "date_entered",
    "date_completed",
}

ADDRESS_LABELS = {
    "property_address": ("property",),
    "mailing_address": ("home", "mailing"),
}

ALL_KNOWN_LABELS: set[str] = {lbl for variants in FIELD_LABELS.values() for lbl in variants} | {
    lbl for variants in ADDRESS_LABELS.values() for lbl in variants
} | {"cellular", "cell", "business", "e-mail", "email", "loss email address", "deductible", "claim rep.", "estimator", "fax"}


def is_metadata_eligible(pr: PageRecord) -> bool:
    return pr.classification not in {
        PageClassification.INSTRUCTIONAL_SAMPLE,
        PageClassification.BLANK,
        PageClassification.UNKNOWN,
        PageClassification.ATTACHMENT,
    }


def _label_and_inline_value(line: str) -> tuple[str, str] | None:
    """If ``line`` is "<label>: <value>" or "<label>:", return
    (label_lower_no_colon, inline_value_or_empty)."""
    if ":" not in line:
        return None
    label, _, rest = line.partition(":")
    label_norm = normalize_whitespace(label).lower()
    return label_norm, normalize_whitespace(rest)


def _next_nonempty(lines: list[str], idx: int) -> tuple[str, int] | None:
    j = idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines):
        return lines[j].strip(), j
    return None


# Preference order when two candidate values tie on frequency: pages that
# print structured claim/estimate headers are more precise than a narrative
# cover letter, which is more prone to transcription slips (see the Garcia
# fixture: the cover letter prints "7010004676-1-1" while the estimate-detail
# header prints "7010004676-1" -- the latter is the more reliable source).
# Unlisted classifications rank last (least authoritative), same as
# CARRIER_LETTER.
_METADATA_AUTHORITY_ORDER: tuple[PageClassification, ...] = (
    PageClassification.ESTIMATE_DETAIL,
    PageClassification.ESTIMATE_DETAIL_CONTINUATION,
    PageClassification.CLAIM_METADATA,
    PageClassification.COVERAGE_SUMMARY,
    PageClassification.REPLACEMENT_COST_EXPLANATION,
    PageClassification.MEASUREMENT_SUMMARY,
    PageClassification.ROOF_DIAGRAM,
    PageClassification.TAX_RECAP,
    PageClassification.OVERHEAD_PROFIT_RECAP,
    PageClassification.RECAP_BY_ROOM,
    PageClassification.SETTLEMENT_NOTICE,
    PageClassification.FAQ,
    PageClassification.DEPRECIATION_GUIDE,
    PageClassification.CARRIER_LETTER,
)


def _authority_rank(classification: PageClassification) -> int:
    try:
        return _METADATA_AUTHORITY_ORDER.index(classification)
    except ValueError:
        return len(_METADATA_AUTHORITY_ORDER)


@dataclass
class _Candidate:
    raw: str
    page: int
    classification: PageClassification


def _collect_field_candidates(
    document: ParsedDocument, page_records: list[PageRecord]
) -> tuple[dict[str, list[_Candidate]], dict[str, list[_Candidate]]]:
    field_hits: dict[str, list[_Candidate]] = {k: [] for k in FIELD_LABELS}
    address_hits: dict[str, list[_Candidate]] = {k: [] for k in ADDRESS_LABELS}

    for pr in page_records:
        if not is_metadata_eligible(pr):
            continue
        page = document.pages[pr.page - 1]
        lines = page.raw_text.splitlines()
        for i, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                continue
            parsed = _label_and_inline_value(line)
            if parsed is None:
                continue
            label, inline_value = parsed

            for field_name, variants in FIELD_LABELS.items():
                if label in variants:
                    value = inline_value
                    if not value:
                        nxt = _next_nonempty(lines, i)
                        if nxt:
                            value = nxt[0]
                    if value and label not in {v for vs in ADDRESS_LABELS.values() for v in vs}:
                        field_hits[field_name].append(
                            _Candidate(raw=value, page=pr.page, classification=pr.classification)
                        )

            for addr_field, variants in ADDRESS_LABELS.items():
                if label in variants:
                    street = inline_value
                    start = i
                    if not street:
                        nxt = _next_nonempty(lines, i)
                        if nxt:
                            street, start = nxt
                    city_line = None
                    nxt2 = _next_nonempty(lines, start)
                    if nxt2 and CITY_STATE_ZIP_RE.match(nxt2[0]):
                        city_line = nxt2[0]
                    if street:
                        combined = f"{street} | {city_line}" if city_line else street
                        address_hits[addr_field].append(
                            _Candidate(raw=combined, page=pr.page, classification=pr.classification)
                        )
    return field_hits, address_hits


def _majority_value(candidates: list[_Candidate], normalize=None) -> tuple[str | None, float, list[int], list[str]]:
    if not candidates:
        return None, 0.0, [], []
    raw_values = [c.raw for c in candidates]
    source_pages = sorted({c.page for c in candidates})
    normed_pairs = [(normalize(c.raw) if normalize else c.raw, c) for c in candidates]
    normed_pairs = [(n, c) for n, c in normed_pairs if n]
    if not normed_pairs:
        return None, 0.3, source_pages, raw_values

    counts = Counter(n for n, _c in normed_pairs)
    max_count = max(counts.values())
    tied_values = [v for v, cnt in counts.items() if cnt == max_count]
    if len(tied_values) == 1:
        best_value = tied_values[0]
    else:
        # Tie on frequency: prefer whichever value has a supporting
        # candidate on the most authoritative page classification (an
        # estimate-detail/claim-metadata header over a narrative cover
        # letter), instead of arbitrarily keeping first-seen-page order.
        def _best_rank(value: str) -> int:
            return min(_authority_rank(c.classification) for n, c in normed_pairs if n == value)

        best_value = min(tied_values, key=_best_rank)
    best_count = counts[best_value]
    confidence = 0.98 if best_count == len(normed_pairs) else max(0.6, 0.98 * best_count / len(normed_pairs))
    return best_value, confidence, source_pages, raw_values


def _field_value(candidates: list[_Candidate], is_date: bool = False) -> FieldValue[str] | None:
    if not candidates:
        return None
    normalize = normalize_date if is_date else (lambda s: normalize_whitespace(s))
    value, confidence, source_pages, raw_values = _majority_value(candidates, normalize=normalize)
    return FieldValue[str](
        value=value, confidence=confidence, source_pages=source_pages, raw_values=raw_values, normalized=is_date
    )


def _address_value(candidates: list[_Candidate]) -> AddressValue | None:
    if not candidates:
        return None
    counts = Counter(c.raw for c in candidates)
    max_count = max(counts.values())
    tied_raws = [v for v, cnt in counts.items() if cnt == max_count]
    if len(tied_raws) == 1:
        best_raw = tied_raws[0]
    else:
        def _best_rank(value: str) -> int:
            return min(_authority_rank(c.classification) for c in candidates if c.raw == value)

        best_raw = min(tied_raws, key=_best_rank)
    best_count = counts[best_raw]
    confidence = 0.95 if best_count == len(candidates) else max(0.6, 0.95 * best_count / len(candidates))
    source_pages = sorted({c.page for c in candidates})
    raw_values = [c.raw for c in candidates]

    if "|" in best_raw:
        line1, city_line = [p.strip() for p in best_raw.split("|", 1)]
    else:
        line1, city_line = best_raw.strip(), None

    city = state = postal_code = None
    if city_line:
        m = CITY_STATE_ZIP_RE.match(city_line)
        if m:
            city, state, postal_code = m.group("city").strip(), m.group("state"), m.group("zip")

    return AddressValue(
        line1=line1 or None,
        line2=None,
        city=city,
        state=state,
        postal_code=postal_code,
        country="US",
        confidence=confidence,
        source_pages=source_pages,
        raw_values=raw_values,
    )


def extract_claim_metadata(document: ParsedDocument, page_records: list[PageRecord]) -> ClaimData:
    field_hits, address_hits = _collect_field_candidates(document, page_records)

    kwargs: dict[str, object] = {}
    for field_name in FIELD_LABELS:
        is_date = field_name in DATE_FIELDS
        kwargs[field_name] = _field_value(field_hits[field_name], is_date=is_date)
    kwargs["property_address"] = _address_value(address_hits["property_address"])
    kwargs["mailing_address"] = _address_value(address_hits["mailing_address"])
    kwargs["estimate_name"] = None  # not reliably labeled across carriers; left null rather than guessed

    return ClaimData(**kwargs)


def extract_contacts(document: ParsedDocument, page_records: list[PageRecord], id_factory) -> list[Contact]:
    contacts: list[Contact] = []
    seen: set[tuple[str, str | None]] = set()

    role_labels = {
        "claim rep.": ContactRole.CLAIM_REPRESENTATIVE,
        "claim representative": ContactRole.CLAIM_REPRESENTATIVE,
        "estimator": ContactRole.ESTIMATOR,
        "adjuster": ContactRole.ADJUSTER,
        "claim professional": ContactRole.CLAIMS_PROFESSIONAL,
    }

    for pr in page_records:
        if not is_metadata_eligible(pr):
            continue
        page = document.pages[pr.page - 1]
        lines = page.raw_text.splitlines()

        for i, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            parsed = _label_and_inline_value(line)
            if parsed is None:
                continue
            label, inline_value = parsed
            role = role_labels.get(label)
            if role is None:
                continue

            name = inline_value
            start = i
            if not name:
                nxt = _next_nonempty(lines, i)
                if nxt:
                    name, start = nxt
            if not name:
                continue

            phone = email = company = None
            j = start
            for _ in range(6):
                nxt = _next_nonempty(lines, j)
                if not nxt:
                    break
                text, j = nxt
                lbl = _label_and_inline_value(text)
                if lbl and lbl[0] in {"cellular", "cell", "business", "e-mail", "email", "loss email address"}:
                    value = lbl[1]
                    if not value:
                        nxt2 = _next_nonempty(lines, j)
                        if nxt2:
                            value, j = nxt2
                    if lbl[0] in {"e-mail", "email", "loss email address"} and value:
                        email = value
                    elif value and phone is None and PHONE_RE.search(value):
                        phone = PHONE_RE.search(value).group(0)
                    continue
                break

            key = (name, role.value)
            if key in seen:
                continue
            seen.add(key)
            contacts.append(
                Contact(
                    contact_id=id_factory.next("contact"),
                    role=role,
                    name=name,
                    company=company,
                    phone=phone,
                    email=email,
                    license_number=None,
                    source_pages=[pr.page],
                    confidence=0.9,
                )
            )

        # Bare "Lastname, Firstname" + phone signature (no explicit label),
        # seen on State Farm coverage-summary pages under the recap.
        if pr.classification == PageClassification.COVERAGE_SUMMARY:
            for i, raw_line in enumerate(lines):
                line = raw_line.strip()
                if not NAME_SIGNATURE_RE.match(line):
                    continue
                nxt = _next_nonempty(lines, i)
                if not nxt or not PHONE_RE.fullmatch(nxt[0].strip()):
                    continue
                key = (line, ContactRole.CLAIM_REPRESENTATIVE.value)
                if key in seen:
                    continue
                seen.add(key)
                contacts.append(
                    Contact(
                        contact_id=id_factory.next("contact"),
                        role=ContactRole.CLAIM_REPRESENTATIVE,
                        name=line,
                        company=None,
                        phone=nxt[0].strip(),
                        email=None,
                        license_number=None,
                        source_pages=[pr.page],
                        confidence=0.75,
                    )
                )

    return contacts
