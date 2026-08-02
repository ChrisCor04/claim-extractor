from __future__ import annotations

import json

import pytest

from estimate_extractor.ui.project_service import (
    DuplicateSourceError,
    ProjectError,
    ProjectService,
    sha256_bytes,
)

PDF_BYTES_A = b"%PDF-1.4 fake pdf content A"
PDF_BYTES_B = b"%PDF-1.4 fake pdf content B"


def test_create_project_writes_source_and_manifest(tmp_path):
    service = ProjectService(tmp_path / "projects")
    record = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)

    assert record.slug == "aranda-insurance"
    assert record.source_sha256 == sha256_bytes(PDF_BYTES_A)
    assert service.source_pdf_path(record.slug).read_bytes() == PDF_BYTES_A
    assert service.manifest_path(record.slug).exists()
    for sub in ("source", "extraction", "mapping", "review", "exports", "logs"):
        assert (service.project_dir(record.slug) / sub).is_dir()


def test_duplicate_upload_by_hash_is_detected_even_with_different_filename(tmp_path):
    service = ProjectService(tmp_path / "projects")
    service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)

    with pytest.raises(DuplicateSourceError) as excinfo:
        service.create_project("Aranda Insurance (copy).pdf", PDF_BYTES_A)
    assert excinfo.value.existing_slug == "aranda-insurance"


def test_duplicate_upload_allow_new_version_creates_second_project(tmp_path):
    service = ProjectService(tmp_path / "projects")
    first = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)
    second = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A, allow_new_version=True)

    assert first.slug != second.slug
    assert second.slug.startswith("aranda-insurance-v")
    assert len(service.list_projects()) == 2


def test_different_content_same_filename_gets_a_unique_slug(tmp_path):
    service = ProjectService(tmp_path / "projects")
    first = service.create_project("estimate.pdf", PDF_BYTES_A)
    second = service.create_project("estimate.pdf", PDF_BYTES_B)

    assert first.slug != second.slug
    assert service.find_by_source_hash(sha256_bytes(PDF_BYTES_A)).slug == first.slug
    assert service.find_by_source_hash(sha256_bytes(PDF_BYTES_B)).slug == second.slug


def test_load_project_round_trips(tmp_path):
    service = ProjectService(tmp_path / "projects")
    created = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)
    loaded = service.load_project(created.slug)
    assert loaded == created


def test_load_missing_project_raises(tmp_path):
    service = ProjectService(tmp_path / "projects")
    with pytest.raises(ProjectError):
        service.load_project("does-not-exist")


def test_malformed_manifest_is_skipped_by_list_projects_not_crashed(tmp_path):
    service = ProjectService(tmp_path / "projects")
    good = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)

    broken_dir = service.project_dir("broken-project")
    broken_dir.mkdir(parents=True)
    (broken_dir / "project.json").write_text("{not valid json", encoding="utf-8")

    records = service.list_projects()
    slugs = {r.slug for r in records}
    assert good.slug in slugs
    assert "broken-project" not in slugs


def test_malformed_manifest_load_raises_project_error(tmp_path):
    service = ProjectService(tmp_path / "projects")
    broken_dir = service.project_dir("broken-project")
    broken_dir.mkdir(parents=True)
    (broken_dir / "project.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ProjectError):
        service.load_project("broken-project")


def test_clear_generated_outputs_keeps_source_pdf(tmp_path):
    service = ProjectService(tmp_path / "projects")
    record = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)
    project_dir = service.project_dir(record.slug)

    (project_dir / "extraction" / "canonical_estimate.json").write_text("{}", encoding="utf-8")
    (project_dir / "review" / "review_state.json").write_text("{}", encoding="utf-8")

    service.clear_generated_outputs(record.slug)

    assert service.source_pdf_path(record.slug).read_bytes() == PDF_BYTES_A
    assert not (project_dir / "extraction" / "canonical_estimate.json").exists()
    assert not (project_dir / "review" / "review_state.json").exists()
    assert (project_dir / "extraction").is_dir()  # recreated empty


def test_delete_project_removes_everything(tmp_path):
    service = ProjectService(tmp_path / "projects")
    record = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)
    project_dir = service.project_dir(record.slug)
    assert project_dir.exists()

    service.delete_project(record.slug)

    assert not project_dir.exists()
    assert service.list_projects() == []


def test_mark_processed_updates_manifest(tmp_path):
    service = ProjectService(tmp_path / "projects")
    record = service.create_project("Aranda Insurance.pdf", PDF_BYTES_A)
    assert record.last_processed_at is None

    service.mark_processed(record.slug)
    reloaded = service.load_project(record.slug)
    assert reloaded.last_processed_at is not None

    manifest_data = json.loads(service.manifest_path(record.slug).read_text(encoding="utf-8"))
    assert manifest_data["last_processed_at"] == reloaded.last_processed_at
