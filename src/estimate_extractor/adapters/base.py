"""Carrier adapter interface.

The spec's ``CarrierAdapter`` protocol is deliberately preserved here
(``detect`` / ``classify_page`` / ``extract_claim_metadata`` /
``extract_coverages`` / ``extract_sections`` / ``extract_line_items``), but
``extract_sections`` and ``extract_line_items`` are thin wrappers around one
memoized document walk (see ``parsing/state_machine.py``): the two are not
actually independent passes over the document (a section spans page breaks
and its line items must be attached while walking pages in order), so
implementing them as separate un-memoized passes would mean re-walking the
document twice for no benefit. ``extract_estimate_body`` is the real,
efficient entry point; the pipeline orchestrator (``pipeline.py``) uses it
directly. See docs/carrier-adapters.md for the full rationale.

Carrier-specific behavior is expressed as data (``CarrierProfile``:
detection keywords and the line-item column schema), not as overridden
methods -- per the spec, shared parsing logic in ``parsing/*.py`` handles
the common estimate layout so it is not duplicated once per carrier.
Instructional/sample-page placeholder markers are centralized in
``classification/pages.py`` instead of per-carrier, since page
classification deliberately runs before carrier detection (see
implementation-priority ordering in the spec) and so cannot depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from estimate_extractor.classification.pages import classify_page as shared_classify_page
from estimate_extractor.models.canonical import ClaimData, Contact, Coverage
from estimate_extractor.models.page import ParsedDocument, ParsedPage, PageRecord
from estimate_extractor.parsing import coverages as coverages_mod
from estimate_extractor.parsing import metadata as metadata_mod
from estimate_extractor.parsing.line_items import ColumnSchema
from estimate_extractor.parsing.state_machine import EstimateBody, IdFactory, walk_estimate_body


@dataclass(frozen=True, slots=True)
class CarrierProfile:
    key: str
    display_name: str
    detection_keywords: tuple[str, ...]
    column_schema: ColumnSchema = field(default_factory=ColumnSchema)


class CarrierAdapter(Protocol):
    def detect(self, document: ParsedDocument) -> float: ...

    def classify_page(self, page: ParsedPage) -> PageRecord: ...

    def extract_claim_metadata(self, document: ParsedDocument, page_records: list[PageRecord]) -> ClaimData: ...

    def extract_coverages(
        self, document: ParsedDocument, page_records: list[PageRecord], id_factory: IdFactory
    ) -> list[Coverage]: ...

    def extract_estimate_body(
        self,
        document: ParsedDocument,
        page_records: list[PageRecord],
        coverages: list[Coverage],
        id_factory: IdFactory,
        boilerplate: set[str],
        low_confidence_threshold: float,
    ) -> EstimateBody: ...


class BaseCarrierAdapter:
    """Shared implementation used by every concrete carrier adapter.

    A carrier subclass typically only needs to construct a ``CarrierProfile``
    and pass it to ``__init__`` -- see adapters/state_farm.py etc.
    """

    def __init__(self, profile: CarrierProfile) -> None:
        self.profile = profile

    @property
    def key(self) -> str:
        return self.profile.key

    @property
    def display_name(self) -> str:
        return self.profile.display_name

    def detect(self, document: ParsedDocument) -> float:
        text_lower = document.full_text().lower()
        if not self.profile.detection_keywords:
            return 0.0
        hits = sum(1 for kw in self.profile.detection_keywords if kw.lower() in text_lower)
        return min(1.0, hits / len(self.profile.detection_keywords) * 1.4)

    def classify_page(self, page: ParsedPage) -> PageRecord:
        return shared_classify_page(page)

    def extract_claim_metadata(self, document: ParsedDocument, page_records: list[PageRecord]) -> ClaimData:
        return metadata_mod.extract_claim_metadata(document, page_records)

    def extract_contacts(
        self, document: ParsedDocument, page_records: list[PageRecord], id_factory: IdFactory
    ) -> list[Contact]:
        return metadata_mod.extract_contacts(document, page_records, id_factory)

    def extract_coverages(
        self, document: ParsedDocument, page_records: list[PageRecord], id_factory: IdFactory
    ) -> list[Coverage]:
        return coverages_mod.extract_coverages(document, page_records, id_factory)

    def extract_estimate_body(
        self,
        document: ParsedDocument,
        page_records: list[PageRecord],
        coverages: list[Coverage],
        id_factory: IdFactory,
        boilerplate: set[str],
        low_confidence_threshold: float = 0.80,
    ) -> EstimateBody:
        return walk_estimate_body(
            document,
            page_records,
            coverages,
            id_factory,
            self.profile.column_schema,
            boilerplate,
            low_confidence_threshold=low_confidence_threshold,
        )
