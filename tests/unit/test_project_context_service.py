from __future__ import annotations

from estimate_extractor.ui import project_context_service as pcs


def test_default_context_is_unconfirmed_and_empty(tmp_path):
    project_dir = tmp_path / "proj"
    context = pcs.get_project_context(project_dir)
    assert context["confirmed"] is False
    assert all(context[f] is None for f in pcs.CONTEXT_FIELDS)


def test_suggest_price_list_from_canonical_never_marks_confirmed():
    canonical = {"claim": {"price_list": {"value": "TXDF8X_JUL26"}}}
    suggested = pcs.suggest_price_list_from_canonical(canonical)
    assert suggested == "TXDF8X_JUL26"


def test_suggest_price_list_returns_none_when_absent():
    assert pcs.suggest_price_list_from_canonical({}) is None
    assert pcs.suggest_price_list_from_canonical({"claim": {}}) is None


def test_set_project_context_requires_explicit_confirm_flag(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)

    pcs.set_project_context(project_dir, {"price_list": "TXDF8X_JUL26"}, "tester", confirmed=False)
    context = pcs.get_project_context(project_dir)
    assert context["price_list"] == "TXDF8X_JUL26"
    assert context["confirmed"] is False
    assert context["confirmed_by"] is None


def test_set_project_context_confirmed_records_reviewer_and_timestamp(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)

    pcs.set_project_context(
        project_dir,
        {"profile": "State Auto", "project_type": "Estimate", "price_list": "TXDF8X_JUL26", "timezone_label": "Mountain Standard Time"},
        "tester",
        confirmed=True,
    )
    context = pcs.get_project_context(project_dir)
    assert context["confirmed"] is True
    assert context["confirmed_by"] == "tester"
    assert context["confirmed_at"] is not None
    assert context["profile"] == "State Auto"
    assert context["timezone_label"] == "Mountain Standard Time"


def test_context_persists_across_reload(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)
    pcs.set_project_context(project_dir, {"project_name": "ARANDAGENARO"}, "tester", confirmed=False)

    reloaded = pcs.get_project_context(project_dir)
    assert reloaded["project_name"] == "ARANDAGENARO"
