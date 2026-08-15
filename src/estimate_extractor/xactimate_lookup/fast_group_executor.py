"""Experimental Phase 4 group-first Quick Entry executor.

This module is intentionally independent of the production execution runner.
It consumes a completed Phase 3 shadow plan, freezes all live payloads before
UI work begins, creates every group, and only then populates groups in order.
The per-item hot loop delegates to :mod:`fast_quick_entry` and performs no
mapping, catalog access, screenshots, OCR, or verification.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol, Sequence

from .fast_quick_entry import FastEntryItem, KeyboardIO, WindowsKeyboardIO, execute_fast_items


FAST_KEY_HOLD_SECONDS = 0.005


@dataclass(frozen=True, slots=True)
class ExecutableQuickEntryItem:
    line_item_id: str
    group: str
    original_description: str
    execution_mode: str
    category: str
    selector: str
    quantity: float | None
    unit: str | None
    price: float | None
    source_activity: str | None
    mapper_diagnostics_json: str

    @property
    def is_normal(self) -> bool:
        return self.execution_mode == "normal_quick_entry"


@dataclass(frozen=True, slots=True)
class ExecutableGroupBatch:
    group: str
    items: tuple[ExecutableQuickEntryItem, ...]

    @property
    def normal_items(self) -> tuple[ExecutableQuickEntryItem, ...]:
        return tuple(item for item in self.items if item.is_normal)

    @property
    def bid_items(self) -> tuple[ExecutableQuickEntryItem, ...]:
        return tuple(item for item in self.items if not item.is_normal)


@dataclass(frozen=True, slots=True)
class ExecutableGroupPlan:
    project: str
    groups: tuple[ExecutableGroupBatch, ...]
    source_schema_version: str


def compile_executable_group_plan(shadow: dict[str, Any]) -> ExecutableGroupPlan:
    """Freeze a shadow plan into ordered, mapper-free live payloads."""
    rows_by_id = {row["line_item_id"]: row for row in shadow["items"]}
    seen: set[str] = set()
    groups: list[ExecutableGroupBatch] = []
    for group_spec in shadow["group_first_future_layout"]:
        batch: list[ExecutableQuickEntryItem] = []
        for line_id in group_spec["line_item_ids"]:
            if line_id in seen or line_id not in rows_by_id:
                raise ValueError(f"shadow plan has missing or duplicate line item {line_id!r}")
            seen.add(line_id)
            row = rows_by_id[line_id]
            category = row.get("execution_category") or row.get("category")
            selector = row.get("execution_selector") or row.get("selector")
            if not category or not selector:
                raise ValueError(f"line item {line_id!r} has no executable CAT/SEL payload")
            is_bid = (category, selector) == ("DOR", "BIDITM")
            pricing = dict(row.get("source_pricing") or {})
            batch.append(ExecutableQuickEntryItem(
                line_item_id=line_id, group=group_spec["group"],
                original_description=row["original_description"],
                execution_mode="requires_biditem_sequence" if is_bid else "normal_quick_entry",
                category=category, selector=selector, quantity=row.get("quantity"), unit=row.get("unit"),
                price=pricing.get("unit_price"), source_activity=row.get("source_action"),
                mapper_diagnostics_json=json.dumps({
                    "resolution": row.get("resolution"), "catalog_description": row.get("catalog_description"),
                    "score": row.get("score"), "margin": row.get("margin"), "reason": row.get("reason"),
                    "top_candidates": list(row.get("top_candidates") or []),
                    "catalog_search_text": row.get("catalog_search_text"), "source_pricing": pricing,
                }, sort_keys=True),
            ))
        groups.append(ExecutableGroupBatch(group=group_spec["group"], items=tuple(batch)))
    if seen != set(rows_by_id):
        raise ValueError(f"shadow plan contains {len(set(rows_by_id) - seen)} ungrouped item(s)")
    return ExecutableGroupPlan(
        project=shadow["project"], groups=tuple(groups),
        source_schema_version=shadow.get("schema_version", "unknown"),
    )


def load_executable_group_plan(path: Path) -> ExecutableGroupPlan:
    return compile_executable_group_plan(json.loads(path.read_text(encoding="utf-8")))


class GroupBatchUI(Protocol):
    keyboard: KeyboardIO
    def verify_project_and_no_modal(self, project: str) -> None: ...
    def create_group(self, group: str) -> str: ...
    def verify_all_groups_created(self, groups: Sequence[str]) -> str: ...
    def select_group_lightweight(self, group: str) -> str: ...
    def focus_quick_entry_cat(self) -> str: ...
    def assert_batch_settled(self) -> None: ...


def _seconds_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "total_seconds": sum(values), "average_seconds": mean(values) if values else 0.0,
        "median_seconds": median(values) if values else 0.0,
    }


def execute_group_first_plan(
    plan: ExecutableGroupPlan, ui: GroupBatchUI, *, clock=time.perf_counter,
) -> dict[str, Any]:
    """Create all groups first, then unload each normal-item batch."""
    ui.verify_project_and_no_modal(plan.project)
    creation = []
    for batch in plan.groups:
        started = clock()
        method = ui.create_group(batch.group)
        creation.append({"group": batch.group, "seconds": clock() - started, "verification_method": method})
    all_groups_verification = ui.verify_all_groups_created([batch.group for batch in plan.groups])

    group_reports = []
    all_item_seconds: list[float] = []
    selection_seconds: list[float] = []
    focus_seconds: list[float] = []
    for batch in plan.groups:
        group_started = clock()
        ui.verify_project_and_no_modal(plan.project)
        started = clock()
        selection_method = ui.select_group_lightweight(batch.group)
        selection_elapsed = clock() - started
        selection_seconds.append(selection_elapsed)
        started = clock()
        focus_method = ui.focus_quick_entry_cat()
        focus_elapsed = clock() - started
        focus_seconds.append(focus_elapsed)

        normal = batch.normal_items
        for item in normal:
            if item.quantity is None:
                raise RuntimeError(f"normal item {item.line_item_id!r} has no quantity; batch aborted before input")
        item_timings = execute_fast_items(
            ui.keyboard,
            [FastEntryItem(item.category, item.selector, item.quantity, item.line_item_id) for item in normal],
            clock=clock,
        )
        ui.assert_batch_settled()
        item_seconds = [timing.total_item_seconds for timing in item_timings]
        all_item_seconds.extend(item_seconds)
        group_reports.append({
            "group": batch.group, "selection_method": selection_method,
            "selection_seconds": selection_elapsed, "focus_method": focus_method,
            "focus_seconds": focus_elapsed, "normal_item_count": len(normal),
            "bid_items": [{**asdict(item), "execution_status": "requires_biditem_sequence"} for item in batch.bid_items],
            "items": [asdict(timing) for timing in item_timings],
            "batch": _seconds_summary(item_seconds), "total_group_seconds": clock() - group_started,
        })
    item_summary = _seconds_summary(all_item_seconds)
    item_summary["items_per_second"] = len(all_item_seconds) / sum(all_item_seconds) if all_item_seconds else 0.0
    return {
        "mode": "experimental_group_first_fast_quick_entry", "project": plan.project,
        "key_hold_seconds": FAST_KEY_HOLD_SECONDS,
        "group_creation": {
            "groups": creation, "all_groups_verification": all_groups_verification,
            **_seconds_summary([row["seconds"] for row in creation]),
        },
        "group_selection": _seconds_summary(selection_seconds),
        "quick_entry_focus": _seconds_summary(focus_seconds),
        "item_batch": item_summary, "groups": group_reports,
        "normal_item_count": sum(len(batch.normal_items) for batch in plan.groups),
        "bid_item_count": sum(len(batch.bid_items) for batch in plan.groups),
    }


class WindowsGroupBatchUI:
    """Windows facade using existing creation and a fresh lightweight selection proof."""

    def __init__(self, project: str, evidence_dir: Path) -> None:
        from .windows_adapter import WindowsXactimateAdapter
        self.adapter = WindowsXactimateAdapter(project, evidence_dir=evidence_dir)
        self.keyboard = WindowsKeyboardIO(key_hold_seconds=FAST_KEY_HOLD_SECONDS)

    def verify_project_and_no_modal(self, project: str) -> None:
        if project != self.adapter.expected_project_name or not self.adapter.verify_application() or not self.adapter.verify_project():
            raise RuntimeError("fast group executor refused: expected project is not positively verified")
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group executor refused: blocking dialog/dropdown is present")

    def create_group(self, group: str) -> str:
        self.adapter.ensure_group(group)
        return "existing_fail_closed_ensure_group"

    def verify_all_groups_created(self, groups: Sequence[str]) -> str:
        """Prove the complete set exists before the first item is typed."""
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        rows = self.adapter.snapshot_group_names()
        for group in groups:
            try:
                index = self.adapter._find_unique_group_row(rows, group)
            except Exception as exc:
                raise RuntimeError(f"fast group executor refused: group {group!r} is ambiguous") from exc
            if index is None:
                raise RuntimeError(f"fast group executor refused: group {group!r} was not created")
        return "one_fresh_complete_group_tree_snapshot_with_unique_names"

    def select_group_lightweight(self, group: str) -> str:
        """One fresh name proof, causal click, then fresh selection-boundary proof."""
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        hwnd = self.adapter._ensure_main_window()
        self.adapter._force_foreground(hwnd)
        self.adapter._scroll_group_tree_to_top(hwnd)
        before = self.adapter._capture_client_image(hwnd)
        header = self.adapter._locate_group_tree_header(before)
        if header is None:
            raise RuntimeError("fast group selection refused: group tree is unavailable")
        rows = self.adapter._snapshot_group_names_from_image(before)
        index = self.adapter._find_unique_group_row(rows, group)
        if index is None or not self.adapter._group_name_matches(
            self.adapter._ocr_group_tree_row_text(before, header, index), group,
        ):
            raise RuntimeError("fast group selection refused: intended group is not uniquely established")
        self.adapter._click_client(hwnd, *self.adapter._group_tree_row_xy(header, index))
        after = self.adapter._capture_client_image(hwnd)
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group selection refused: blocking UI appeared")
        if not self.adapter._group_tree_row_has_selection_boundary(after, header, index):
            raise RuntimeError("fast group selection refused: fresh selection boundary is absent")
        if self.adapter._anchor_offset(after) is None or self.adapter._items_search_pane_field(after) is None:
            raise RuntimeError("fast group selection refused: selected Items/grid context is not established")
        return "fresh_name_ocr_then_causal_click_and_selection_boundary"

    def focus_quick_entry_cat(self) -> str:
        focus = self.adapter._find_main_window()
        if focus is None:
            raise RuntimeError("fast group executor refused: main window disappeared")
        hwnd, _title = focus
        image, offset = self.adapter._capture_and_locate(hwnd)
        if offset is None:
            raise RuntimeError("fast group executor refused: Quick Entry CAT geometry is unavailable")
        left, top, right, bottom = self.adapter._shifted_anchor("quick_entry_cat_value", offset)
        self.adapter._click_client(hwnd, (left + right) // 2, (top + bottom) // 2)
        if not self.adapter._force_foreground(hwnd):
            raise RuntimeError("fast group executor refused: CAT focus could not be retained")
        return "fresh_quick_entry_geometry_and_foreground_click"

    def assert_batch_settled(self) -> None:
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group batch stopped: blocking dialog/dropdown detected after batch")
