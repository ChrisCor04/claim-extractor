"""Local project management for the Phase 3 review UI.

A "project" is a self-contained directory under ``projects/<slug>/`` that
holds one claim's source PDF plus every stage's outputs (extraction,
mapping, review, exports). This module owns project lifecycle only --
creation, listing, loading, deletion, clearing generated outputs -- and
never runs the extractor or mapper itself (see pipeline_service.py).

Duplicate uploads are detected by SHA-256 of the PDF bytes, not filename,
so a renamed copy of an already-processed document is still recognized.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.pipeline import slugify

MANIFEST_FILENAME = "project.json"

# Every project directory always has these subdirectories, even before
# anything has been processed, so the UI can rely on their existence.
SUBDIRS = ("source", "extraction", "mapping", "review", "exports", "logs")

SOURCE_PDF_FILENAME = "original.pdf"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ProjectError(Exception):
    """Base class for project-lifecycle errors."""


class DuplicateSourceError(ProjectError):
    """Raised by create_project() when the same PDF (by SHA-256) already
    has a project and the caller did not explicitly request a new version.
    The UI catches this and offers: open existing / create new version /
    cancel."""

    def __init__(self, existing_slug: str, source_sha256: str):
        super().__init__(f"A project for this exact file already exists: {existing_slug!r}")
        self.existing_slug = existing_slug
        self.source_sha256 = source_sha256


@dataclass(slots=True)
class ProjectRecord:
    slug: str
    source_filename: str
    source_sha256: str
    created_at: str
    last_processed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "source_filename": self.source_filename,
            "source_sha256": self.source_sha256,
            "created_at": self.created_at,
            "last_processed_at": self.last_processed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> ProjectRecord:
        return ProjectRecord(
            slug=data["slug"],
            source_filename=data["source_filename"],
            source_sha256=data["source_sha256"],
            created_at=data["created_at"],
            last_processed_at=data.get("last_processed_at"),
        )


class ProjectService:
    def __init__(self, projects_dir: Path):
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------
    def project_dir(self, slug: str) -> Path:
        return self.projects_dir / slug

    def manifest_path(self, slug: str) -> Path:
        return self.project_dir(slug) / MANIFEST_FILENAME

    def source_pdf_path(self, slug: str) -> Path:
        return self.project_dir(slug) / "source" / SOURCE_PDF_FILENAME

    # -- discovery -------------------------------------------------------
    def list_projects(self) -> list[ProjectRecord]:
        records: list[ProjectRecord] = []
        if not self.projects_dir.exists():
            return records
        for child in sorted(self.projects_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest = child / MANIFEST_FILENAME
            if not manifest.exists():
                continue
            try:
                records.append(ProjectRecord.from_dict(json.loads(manifest.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError):
                # A malformed manifest must not take down the whole
                # Projects list -- skip it, the UI can surface it separately.
                continue
        return records

    def find_by_source_hash(self, source_sha256: str) -> ProjectRecord | None:
        for record in self.list_projects():
            if record.source_sha256 == source_sha256:
                return record
        return None

    def project_exists(self, slug: str) -> bool:
        return self.manifest_path(slug).exists()

    def load_project(self, slug: str) -> ProjectRecord:
        manifest = self.manifest_path(slug)
        if not manifest.exists():
            raise ProjectError(f"No project found for slug {slug!r}.")
        try:
            return ProjectRecord.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ProjectError(f"Project manifest for {slug!r} is malformed: {exc}") from exc

    # -- lifecycle ---------------------------------------------------------
    def _unique_slug(self, base_slug: str) -> str:
        slug = base_slug
        n = 2
        while (self.projects_dir / slug).exists():
            slug = f"{base_slug}-v{n}"
            n += 1
        return slug

    def create_project(
        self,
        source_filename: str,
        source_bytes: bytes,
        *,
        allow_new_version: bool = False,
    ) -> ProjectRecord:
        """Create a new project directory and copy the PDF into
        source/original.pdf. Raises DuplicateSourceError if a project for
        this exact file (by content hash) already exists, unless
        allow_new_version=True."""
        source_sha256 = sha256_bytes(source_bytes)
        existing = self.find_by_source_hash(source_sha256)
        if existing is not None and not allow_new_version:
            raise DuplicateSourceError(existing.slug, source_sha256)

        base_slug = slugify(source_filename)
        slug = self._unique_slug(base_slug) if (self.projects_dir / base_slug).exists() else base_slug

        project_dir = self.project_dir(slug)
        for sub in SUBDIRS:
            (project_dir / sub).mkdir(parents=True, exist_ok=True)

        self.source_pdf_path(slug).write_bytes(source_bytes)

        record = ProjectRecord(
            slug=slug,
            source_filename=source_filename,
            source_sha256=source_sha256,
            created_at=utc_now_iso(),
        )
        self._write_manifest(record)
        return record

    def _write_manifest(self, record: ProjectRecord) -> None:
        self.manifest_path(record.slug).write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")

    def mark_processed(self, slug: str) -> None:
        record = self.load_project(slug)
        record.last_processed_at = utc_now_iso()
        self._write_manifest(record)

    def delete_project(self, slug: str) -> None:
        project_dir = self.project_dir(slug)
        if not project_dir.exists():
            raise ProjectError(f"No project found for slug {slug!r}.")
        shutil.rmtree(project_dir)

    def clear_generated_outputs(self, slug: str) -> None:
        """Remove every generated stage output but keep source/ intact --
        used to force a clean reprocess without losing the uploaded PDF."""
        project_dir = self.project_dir(slug)
        if not project_dir.exists():
            raise ProjectError(f"No project found for slug {slug!r}.")
        for sub in ("extraction", "mapping", "review", "exports", "logs"):
            target = project_dir / sub
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
