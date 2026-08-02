"""Validates the completed selector catalog against the build spec's
completion requirements. Used both by `selectors validate` and by the
pipeline's own completion gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from estimate_extractor.selector_catalog.models import SelectorRecord, VALID_SCREENSHOT_STATUSES, ScreenshotManifestEntry


@dataclass(slots=True)
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str


@dataclass(slots=True)
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors


def _issue(issues: list[ValidationIssue], severity: str, code: str, message: str) -> None:
    issues.append(ValidationIssue(severity=severity, code=code, message=message))


def validate_records(records: list[SelectorRecord]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_keys: set[tuple[str, str]] = set()
    for r in records:
        if not r.category:
            _issue(issues, "error", "EMPTY_CATEGORY", f"Record with selector {r.selector!r} has an empty category.")
        if not r.selector:
            _issue(issues, "error", "EMPTY_SELECTOR", f"Record in category {r.category!r} has an empty selector.")
        if not r.description_original:
            _issue(issues, "error", "EMPTY_DESCRIPTION", f"{r.category}/{r.selector} has an empty description.")
        if not r.source_images:
            _issue(issues, "error", "MISSING_PROVENANCE", f"{r.category}/{r.selector} has no source_images provenance.")
        key = (r.category, r.selector)
        if key in seen_keys:
            _issue(issues, "error", "DUPLICATE_KEY", f"Duplicate (category, selector) key: {key!r}.")
        seen_keys.add(key)
    return issues


def validate_manifest_coverage(
    manifest_entries: list[ScreenshotManifestEntry],
    all_relative_paths: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    manifest_paths = {e.relative_path for e in manifest_entries}
    missing = all_relative_paths - manifest_paths
    for path in sorted(missing):
        _issue(issues, "error", "SCREENSHOT_NOT_IN_MANIFEST", f"Screenshot {path!r} was never recorded in the manifest.")

    for e in manifest_entries:
        if e.status not in VALID_SCREENSHOT_STATUSES:
            _issue(issues, "error", "INVALID_STATUS", f"{e.relative_path}: unknown status {e.status!r}.")
        if e.status == "failed" and not e.error:
            _issue(issues, "warning", "FAILED_WITHOUT_ERROR", f"{e.relative_path}: status=failed but no error message recorded.")
        if e.status == "skipped" and not e.skip_reason:
            _issue(issues, "warning", "SKIPPED_WITHOUT_REASON", f"{e.relative_path}: status=skipped but no skip_reason recorded.")
    return issues


def validate_catalog(
    records: list[SelectorRecord],
    manifest_entries: list[ScreenshotManifestEntry],
    all_relative_paths: set[str],
) -> ValidationReport:
    issues = validate_records(records)
    issues += validate_manifest_coverage(manifest_entries, all_relative_paths)
    return ValidationReport(issues=issues)
