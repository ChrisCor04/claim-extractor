"""Xactimate group-name suggestion: maps an extracted section name to a
canonical Xactimate group name from config/xactimate_group_names.yaml,
without ever forcing the rename. A suggestion is just that -- the
original extracted section name is always preserved, and a human decides
whether to accept the suggestion, keep the original, or enter a custom
name. See docs/verified-catalog-builder.md "Group-name handling".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import yaml

FUZZY_MIN_CONFIDENCE = 0.60


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GroupNameServiceError(Exception):
    pass


@dataclass(slots=True)
class GroupNameConfig:
    groups: list[str]
    aliases: dict[str, str]  # lowercased original section name -> canonical group


@dataclass(slots=True)
class GroupSuggestion:
    original_section_name: str
    suggested_group_name: str | None
    confidence: float
    method: str  # "exact" | "alias" | "fuzzy" | "no_match"


def load_group_names(path: Path) -> GroupNameConfig:
    if not path.exists():
        return GroupNameConfig(groups=[], aliases={})
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return GroupNameConfig(groups=list(raw.get("groups") or []), aliases=dict(raw.get("aliases") or {}))


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def suggest_group_name(section_name: str, config: GroupNameConfig) -> GroupSuggestion:
    if not section_name:
        return GroupSuggestion(section_name, None, 0.0, "no_match")

    lowered = section_name.strip().lower()

    for group in config.groups:
        if group.lower() == lowered:
            return GroupSuggestion(section_name, group, 1.0, "exact")

    if lowered in config.aliases:
        return GroupSuggestion(section_name, config.aliases[lowered], 0.95, "alias")

    normalized_section = _normalize(section_name)
    best_group = None
    best_score = 0.0
    for group in config.groups:
        normalized_group = _normalize(group)
        if normalized_group and normalized_group in normalized_section:
            # e.g. "Main Dwelling Roof" contains normalized "roof"
            score = len(normalized_group) / max(len(normalized_section), 1)
            score = 0.75 + min(score, 0.2)  # substring hits are fairly confident, capped below "exact"/"alias"
            if score > best_score:
                best_score, best_group = score, group

    if best_group is not None:
        return GroupSuggestion(section_name, best_group, round(best_score, 2), "substring")

    for group in config.groups:
        ratio = SequenceMatcher(None, normalized_section, _normalize(group)).ratio()
        if ratio > best_score:
            best_score, best_group = ratio, group

    if best_group is not None and best_score >= FUZZY_MIN_CONFIDENCE:
        return GroupSuggestion(section_name, best_group, round(best_score, 2), "fuzzy")

    return GroupSuggestion(section_name, None, round(best_score, 2), "no_match")


def save_reusable_group_alias(path: Path, backups_dir: Path, original_section_name: str, group_name: str) -> None:
    """Persists a reviewer-confirmed section-name -> group-name alias so
    future documents with the same extracted section name are suggested
    this group automatically (still only a suggestion, never forced)."""
    config = load_group_names(path)
    key = original_section_name.strip().lower()
    if config.aliases.get(key) == group_name:
        return  # already saved, nothing to do

    if path.exists():
        backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        import shutil

        shutil.copy2(path, backups_dir / f"xactimate_group_names_{timestamp}.yaml")

    config.aliases[key] = group_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"groups": config.groups, "aliases": config.aliases}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Per-project group-name review overrides (review/group_name_overrides.json)
# ---------------------------------------------------------------------------


def _overrides_path(project_dir: Path) -> Path:
    return project_dir / "review" / "group_name_overrides.json"


def get_group_name_overrides(project_dir: Path) -> dict:
    path = _overrides_path(project_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def set_group_name_review(
    project_dir: Path,
    original_section_name: str,
    suggestion: GroupSuggestion,
    reviewer: str,
    *,
    reviewed_group_name: str | None = None,
    allow_custom: bool = False,
) -> dict:
    """Records the reviewer's decision for one section: accept the
    suggestion, pick a different existing group, keep the original name
    verbatim (reviewed_group_name=None, allow_custom=True), or anything in
    between. Either reviewed_group_name being set OR allow_custom=True
    marks the section as reviewed (see
    verified_catalog_service.is_automation_ready)."""
    overrides = get_group_name_overrides(project_dir)
    entry = {
        "original_section_name": original_section_name,
        "suggested_xactimate_group_name": suggestion.suggested_group_name,
        "confidence": suggestion.confidence,
        "reviewed_xactimate_group_name": reviewed_group_name,
        "allow_custom": allow_custom,
        "reviewed_by": reviewer,
        "reviewed_at": utc_now_iso(),
    }
    overrides[original_section_name] = entry
    path = _overrides_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2, default=str), encoding="utf-8")
    return entry


def is_section_reviewed(project_dir: Path, section_name: str | None) -> bool:
    if not section_name:
        return False
    overrides = get_group_name_overrides(project_dir)
    entry = overrides.get(section_name)
    if entry is None:
        return False
    return bool(entry.get("reviewed_xactimate_group_name")) or bool(entry.get("allow_custom"))
