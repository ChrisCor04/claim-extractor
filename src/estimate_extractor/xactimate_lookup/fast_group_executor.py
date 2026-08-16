"""Experimental Phase 4 group-first Quick Entry executor.

This module is intentionally independent of the production execution runner.
It consumes a completed Phase 3 shadow plan, freezes all live payloads before
UI work begins, creates every group, and only then populates groups in order.
The per-item hot loop delegates to :mod:`fast_quick_entry` and performs no
mapping, catalog access, screenshots, OCR, or verification.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol, Sequence

from .fast_quick_entry import (
    FastBidItem, FastEntryItem, KeyboardIO, WindowsKeyboardIO,
    execute_fast_bid_item, execute_fast_items,
)


FAST_KEY_HOLD_SECONDS = 0.005


def normalize_planned_group_identity(value: str) -> str:
    """Exact fast-plan identity; tolerate formatting, never edit letters/digits."""
    return "".join(re.findall(r"[a-z0-9]+", value.casefold()))


def exact_planned_group_rows(rows: Sequence[str], group: str) -> list[int]:
    """Return rows containing the complete normalized planned name."""
    needle = normalize_planned_group_identity(group)
    if not needle:
        return []
    return [
        index for index, observed in enumerate(rows)
        if needle in normalize_planned_group_identity(observed)
    ]


def reconcile_complete_group_inventory(
    rows: Sequence[str], groups: Sequence[str],
) -> dict[str, int]:
    """Require one distinct physical row for every distinct planned name."""
    normalized = [normalize_planned_group_identity(group) for group in groups]
    if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
        raise RuntimeError("fast group executor refused: planned group identities are empty or duplicate")
    result: dict[str, int] = {}
    used_rows: set[int] = set()
    for group in groups:
        matches = exact_planned_group_rows(rows, group)
        if len(matches) != 1:
            raise RuntimeError(
                f"fast group executor refused: planned group {group!r} has {len(matches)} exact physical row match(es)"
            )
        if matches[0] in used_rows:
            raise RuntimeError("fast group executor refused: two planned groups resolve to the same physical row")
        used_rows.add(matches[0])
        result[group] = matches[0]
    return result


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


@dataclass(frozen=True, slots=True)
class GroupInventoryEntry:
    normalized_identity: str
    displayed_name: str
    physical_row: int
    row_center: tuple[int, int]


@dataclass(frozen=True, slots=True)
class GroupInventory:
    window_rect: tuple[int, int, int, int]
    header_rect: tuple[int, int, int, int]
    entries: tuple[GroupInventoryEntry, ...]

    def entry(self, group: str) -> GroupInventoryEntry:
        identity = normalize_planned_group_identity(group)
        matches = [entry for entry in self.entries if entry.normalized_identity == identity]
        if len(matches) != 1:
            raise RuntimeError(f"verified inventory has {len(matches)} entries for {group!r}")
        return matches[0]


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
    def prepare_group_creation(self, groups: Sequence[str]) -> str: ...
    def create_group(self, group: str) -> dict[str, Any]: ...
    def verify_all_groups_created(self, groups: Sequence[str]) -> str: ...
    def select_group_lightweight(self, group: str) -> str: ...
    def focus_quick_entry_cat(self) -> str: ...
    def assert_batch_settled(self) -> None: ...
    def capture_group_evidence(self, group: str) -> str: ...
    def capture_final_evidence(self) -> str: ...
    def accept_expected_biditem_duplicate(self) -> dict[str, float]: ...
    def normalize_window(self) -> dict[str, Any]: ...


def _seconds_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "total_seconds": sum(values), "average_seconds": mean(values) if values else 0.0,
        "median_seconds": median(values) if values else 0.0,
    }


def execute_group_first_plan(
    plan: ExecutableGroupPlan, ui: GroupBatchUI, *, clock=time.perf_counter,
) -> dict[str, Any]:
    """Create all groups first, then unload each normal-item batch."""
    execution_started = clock()
    ui.verify_project_and_no_modal(plan.project)
    window_profile = ui.normalize_window()
    planned_groups = [batch.group for batch in plan.groups]
    started = clock()
    initial_inventory_method = ui.prepare_group_creation(planned_groups)
    initial_inventory_seconds = clock() - started
    creation = []
    for batch in plan.groups:
        started = clock()
        detail = ui.create_group(batch.group)
        creation.append({"group": batch.group, "seconds": clock() - started, **detail})
    started = clock()
    all_groups_verification = ui.verify_all_groups_created(planned_groups)
    final_inventory_seconds = clock() - started

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
        for item in batch.items:
            if item.quantity is None:
                raise RuntimeError(f"item {item.line_item_id!r} has no quantity; batch aborted before input")
        item_timings = []
        emitted_identities: set[tuple[str, str]] = set()
        duplicate_acceptances: list[dict[str, Any]] = []
        for item in batch.items:
            if item.is_normal:
                item_timings.extend(execute_fast_items(
                    ui.keyboard,
                    [FastEntryItem(item.category, item.selector, item.quantity, item.line_item_id)],
                    clock=clock,
                ))
            else:
                item_timings.append(execute_fast_bid_item(
                    ui.keyboard,
                    FastBidItem(item.original_description, item.quantity, item.line_item_id),
                    clock=clock,
                ))
                identity = (item.category, item.selector)
                if identity == ("DOR", "BIDITM") and identity in emitted_identities:
                    duplicate_acceptances.append({
                        "line_item_id": item.line_item_id,
                        **ui.accept_expected_biditem_duplicate(),
                    })
                emitted_identities.add(identity)
        ui.assert_batch_settled()
        evidence_path = ui.capture_group_evidence(batch.group)
        item_seconds = [timing.total_item_seconds for timing in item_timings]
        all_item_seconds.extend(item_seconds)
        group_reports.append({
            "group": batch.group, "selection_method": selection_method,
            "selection_seconds": selection_elapsed, "focus_method": focus_method,
            "focus_seconds": focus_elapsed, "normal_item_count": len(normal),
            "bid_items": [{**asdict(item), "execution_status": "fast_biditem_executed"} for item in batch.bid_items],
            "items": [
                {**asdict(timing), "line_item_id": item.line_item_id, "execution_mode": item.execution_mode}
                for item, timing in zip(batch.items, item_timings, strict=True)
            ],
            "evidence_path": evidence_path,
            "expected_biditem_duplicate_acceptances": duplicate_acceptances,
            "batch": _seconds_summary(item_seconds), "total_group_seconds": clock() - group_started,
        })
    item_summary = _seconds_summary(all_item_seconds)
    item_summary["items_per_second"] = len(all_item_seconds) / sum(all_item_seconds) if all_item_seconds else 0.0
    final_evidence_path = ui.capture_final_evidence()
    report = {
        "mode": "experimental_group_first_fast_quick_entry", "project": plan.project,
        "key_hold_seconds": FAST_KEY_HOLD_SECONDS,
        "window_profile": window_profile,
        "group_creation": {
            "groups": creation, "all_groups_verification": all_groups_verification,
            "initial_inventory_method": initial_inventory_method,
            "initial_inventory_seconds": initial_inventory_seconds,
            "final_inventory_seconds": final_inventory_seconds,
            **_seconds_summary([row["seconds"] for row in creation]),
        },
        "group_selection": _seconds_summary(selection_seconds),
        "quick_entry_focus": _seconds_summary(focus_seconds),
        "item_batch": item_summary, "groups": group_reports,
        "normal_item_count": sum(len(batch.normal_items) for batch in plan.groups),
        "bid_item_count": sum(len(batch.bid_items) for batch in plan.groups),
        "final_evidence_path": final_evidence_path,
        "total_execution_seconds": clock() - execution_started,
    }
    report["normal_item_timing"] = _seconds_summary([
        item["total_item_seconds"] for group in group_reports for item in group["items"]
        if item["execution_mode"] == "normal_quick_entry"
    ])
    report["biditem_timing"] = _seconds_summary([
        item["total_item_seconds"] for group in group_reports for item in group["items"]
        if item["execution_mode"] == "requires_biditem_sequence"
    ])
    return report


class WindowsGroupBatchUI:
    """Windows facade using existing creation and a fresh lightweight selection proof."""

    def __init__(self, project: str, evidence_dir: Path, *, calibration_dir: Path | None = None) -> None:
        from .windows_adapter import WindowsXactimateAdapter
        from .xactimate_calibration import apply_fast_geometry, load_calibration
        self.adapter = WindowsXactimateAdapter(project, evidence_dir=evidence_dir)
        self.calibration = load_calibration() if calibration_dir is None else load_calibration(calibration_dir)
        apply_fast_geometry(self.adapter, self.calibration)
        self.adapter._fast_group_tree_scroll_point = tuple(self.calibration.geometry["group_tree_scroll_point"])
        self.keyboard = WindowsKeyboardIO(key_hold_seconds=FAST_KEY_HOLD_SECONDS)
        self._initial_rows: list[str] | None = None
        self._inventory: GroupInventory | None = None

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = self.adapter._win32gui().GetWindowRect(hwnd)
        return tuple(rect)

    def prepare_group_creation(self, groups: Sequence[str]) -> str:
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        hwnd = self.adapter._ensure_main_window()
        self.adapter._scroll_group_tree_to_top(hwnd)
        self._initial_rows = self.adapter.snapshot_group_names()
        # Duplicate planned identities are invalid even before mutation.
        identities = [normalize_planned_group_identity(group) for group in groups]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities):
            raise RuntimeError("fast group executor refused: planned group identities are empty or duplicate")
        self._inventory = None
        return "one_fresh_exact_pre_creation_inventory"

    def normalize_window(self) -> dict[str, Any]:
        from .window_normalization import normalize_xactimate_window
        return normalize_xactimate_window(self.adapter, self.calibration)

    def verify_project_and_no_modal(self, project: str) -> None:
        if project != self.adapter.expected_project_name or not self.adapter.verify_application() or not self.adapter.verify_project():
            raise RuntimeError("fast group executor refused: expected project is not positively verified")
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group executor refused: blocking dialog/dropdown is present")

    @staticmethod
    def _poll(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.05):
        deadline = time.perf_counter() + timeout_s
        while True:
            result = predicate()
            if result:
                return result
            if time.perf_counter() >= deadline:
                return None
            time.sleep(interval_s)

    def _fresh_exact_group_row(self, group: str) -> tuple[int, list[str]] | None:
        """Fresh, full-tree, name-based row lookup -- deliberately
        independent of any current selection state. Live-caught
        regression: requiring the newly-created row to still be the
        selected one at verification time was found to fail closed on a
        row that was, moments later, correctly created/selected/named --
        the verification window can outlast a real but transient repaint
        delay. Re-locating by name in a fresh snapshot (the same
        mechanism verify_all_groups_created() already uses) has no such
        dependency. Two or more exact matches is genuine ambiguity, not
        something a retry can resolve -- fails closed immediately rather
        than waiting out the poll."""
        rows = self.adapter.snapshot_group_names()
        matches = exact_planned_group_rows(rows, group)
        if len(matches) > 1:
            raise RuntimeError(
                f"fast group creation refused: {len(matches)} exact physical rows match {group!r} in the group tree"
            )
        return (matches[0], rows) if matches else None

    def _report_creation_verification_failure(self, *, group: str, attempts: int, elapsed_seconds: float) -> None:
        """Best-effort diagnostic capture for a creation-verification
        failure -- purely additive evidence; any error here is swallowed
        and never changes, replaces, or delays the actual failure."""
        try:
            rows = self.adapter.snapshot_group_names()
            matches = exact_planned_group_rows(rows, group)
            payload = {
                "requested_group": group, "verification_attempts": attempts,
                "verification_elapsed_seconds": elapsed_seconds,
                "final_inventory": rows, "exact_match_indices": matches,
            }
            evidence_root = self.adapter.evidence_dir
            evidence_root.mkdir(parents=True, exist_ok=True)
            slug = normalize_planned_group_identity(group) or "unknown"
            path = evidence_root / f"group_creation_verification_failure_{slug}.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def create_group(self, group: str) -> dict[str, Any]:
        if self._initial_rows is None:
            raise RuntimeError("fast group creation refused: initial inventory was not established")
        matches = exact_planned_group_rows(self._initial_rows, group)
        if len(matches) > 1:
            raise RuntimeError(f"fast group creation refused: duplicate exact rows already match {group!r}")
        if matches:
            return {"creation_state": "already_present_exact", "verification_method": "initial_exact_inventory"}

        self._inventory = None  # any physical tree mutation invalidates reusable row coordinates
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        hwnd = self.adapter._ensure_main_window()
        image = self.adapter._capture_client_image(hwnd)
        header = self.adapter._locate_group_tree_header(image)
        if header is None:
            raise RuntimeError("fast group creation refused: group tree is unavailable")

        command_started = time.perf_counter()
        menu = self.adapter._open_group_tree_context_menu(hwnd, header, 0)
        self.adapter._click_group_menu_item(menu, self.adapter._GROUP_MENU_NEW_INDEX)
        dialog_hwnd = self._poll(lambda: self.adapter._find_window_by_title("New Group"))
        command_seconds = time.perf_counter() - command_started
        if dialog_hwnd is None:
            raise RuntimeError("fast group creation refused: New Group dialog did not appear")

        input_started = time.perf_counter()
        self.adapter._click_client(dialog_hwnd, *self.calibration.geometry["new_group_dialog_name_click"])
        self.adapter._select_all_and_delete()
        self.adapter._type_keybdevent(group, char_interval_s=FAST_KEY_HOLD_SECONDS)
        input_seconds = time.perf_counter() - input_started

        attach_started = time.perf_counter()
        self.adapter._click_client(dialog_hwnd, *self.calibration.geometry["new_group_dialog_attach_click"])
        closed = self._poll(lambda: self.adapter._find_window_by_title("New Group") is None)
        attach_seconds = time.perf_counter() - attach_started
        if not closed:
            raise RuntimeError("fast group creation refused: New Group dialog did not close")

        verify_started = time.perf_counter()
        attempt_count = 0

        def _attempt():
            nonlocal attempt_count
            attempt_count += 1
            return self._fresh_exact_group_row(group)

        observed = self._poll(_attempt)
        verify_seconds = time.perf_counter() - verify_started
        if observed is None:
            self._report_creation_verification_failure(
                group=group, attempts=attempt_count, elapsed_seconds=verify_seconds,
            )
            raise RuntimeError(
                f"fast group creation refused: {group!r} was not uniquely established in the group tree "
                f"after {attempt_count} verification attempt(s) over {verify_seconds:.2f}s"
            )
        observed_row, final_rows = observed
        return {
            "creation_state": "created", "verification_method": "dialog_close_then_fresh_exact_name_reacquisition",
            "new_group_command_seconds": command_seconds, "group_name_input_seconds": input_seconds,
            "attach_to_dialog_close_seconds": attach_seconds,
            "bounded_new_row_verification_seconds": verify_seconds, "verification_attempts": attempt_count,
            "observed_row": observed_row, "observed_display_name": final_rows[observed_row],
        }

    def verify_all_groups_created(self, groups: Sequence[str]) -> str:
        """Prove the complete set exists before the first item is typed."""
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        hwnd = self.adapter._ensure_main_window()
        self.adapter._scroll_group_tree_to_top(hwnd)
        image = self.adapter._capture_client_image(hwnd)
        header = self.adapter._locate_group_tree_header(image)
        if header is None:
            raise RuntimeError("fast group executor refused: final group tree is unavailable")
        rows = self.adapter._snapshot_group_names_from_image(image)
        mapping = reconcile_complete_group_inventory(rows, groups)
        entries = tuple(GroupInventoryEntry(
            normalized_identity=normalize_planned_group_identity(group), displayed_name=rows[row],
            physical_row=row, row_center=self.adapter._group_tree_row_xy(header, row),
        ) for group, row in mapping.items())
        self._inventory = GroupInventory(
            window_rect=self._window_rect(hwnd), header_rect=tuple(header), entries=entries,
        )
        return "one_fresh_complete_group_tree_snapshot_with_distinct_exact_planned_names"

    def _report_selection_verification_failure(
        self, *, group: str, stage: str, image=None, header=None, index: int | None = None,
    ) -> None:
        """Best-effort diagnostic capture for a select_group_lightweight()
        verification failure -- purely additive evidence; any error here is
        swallowed and never changes, replaces, or delays the actual
        failure. `stage` names which specific check refused (geometry,
        OCR identity, blocking UI, missing boundary, or post-click
        context), since select_group_lightweight()'s prior failures left
        no evidence at all to distinguish between those causes."""
        try:
            payload: dict[str, Any] = {"requested_group": group, "failure_stage": stage}
            if header is not None:
                payload["header_rect"] = list(header)
            if index is not None:
                payload["physical_row"] = index
            if image is not None and header is not None and index is not None:
                try:
                    payload["reread_text"] = self.adapter._ocr_group_tree_row_text(image, header, index)
                except Exception:
                    payload["reread_text"] = None
                try:
                    payload["has_selection_boundary"] = self.adapter._group_tree_row_has_selection_boundary(
                        image, header, index,
                    )
                except Exception:
                    payload["has_selection_boundary"] = None
            if image is not None:
                try:
                    payload["anchor_offset"] = self.adapter._anchor_offset(image)
                except Exception:
                    payload["anchor_offset"] = None
                try:
                    items_search = self.adapter._items_search_pane_field(image)
                    payload["items_search_pane_field"] = list(items_search) if items_search else None
                except Exception:
                    payload["items_search_pane_field"] = None
            evidence_root = self.adapter.evidence_dir
            evidence_root.mkdir(parents=True, exist_ok=True)
            slug = normalize_planned_group_identity(group) or "unknown"
            path = evidence_root / f"group_selection_verification_failure_{slug}.json"
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass

    def select_group_lightweight(self, group: str) -> str:
        """One fresh name proof, causal click, then fresh selection-boundary proof."""
        self.verify_project_and_no_modal(self.adapter.expected_project_name)
        hwnd = self.adapter._ensure_main_window()
        self.adapter._force_foreground(hwnd)
        if self._inventory is None:
            raise RuntimeError("fast group selection refused: verified reusable inventory is absent")
        before = self.adapter._capture_client_image(hwnd)
        header = self.adapter._locate_group_tree_header(before)
        # OCR's word box can vary by a pixel at its right/bottom edge as
        # antialiasing changes after a subtotal repaint. Row coordinates are
        # derived only from the header origin. Preserve exact window and
        # origin equality; do not mistake advisory glyph-box width for a
        # physical layout change.
        if (
            header is None
            or tuple(header[:2]) != self._inventory.header_rect[:2]
            or self._window_rect(hwnd) != self._inventory.window_rect
        ):
            self._report_selection_verification_failure(group=group, stage="geometry_invalidated", image=before, header=header)
            raise RuntimeError("fast group selection refused: reusable inventory geometry was invalidated")
        entry = self._inventory.entry(group)
        index = entry.physical_row
        reread = self.adapter._ocr_group_tree_row_text(before, header, index)
        if len(exact_planned_group_rows([reread], group)) != 1:
            self._report_selection_verification_failure(
                group=group, stage="ocr_identity_mismatch", image=before, header=header, index=index,
            )
            raise RuntimeError("fast group selection refused: clicked row failed exact independent name reread")
        self.adapter._click_client(hwnd, *entry.row_center)
        after = self.adapter._capture_client_image(hwnd)
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            self._report_selection_verification_failure(
                group=group, stage="unexpected_dialog_or_dropdown", image=after, header=header, index=index,
            )
            raise RuntimeError("fast group selection refused: blocking UI appeared")
        if not self.adapter._group_tree_row_has_selection_boundary(after, header, index):
            self._report_selection_verification_failure(
                group=group, stage="selection_boundary_absent", image=after, header=header, index=index,
            )
            raise RuntimeError("fast group selection refused: fresh selection boundary is absent")
        if self.adapter._anchor_offset(after) is None or self.adapter._items_search_pane_field(after) is None:
            self._report_selection_verification_failure(
                group=group, stage="post_click_context_unestablished", image=after, header=header, index=index,
            )
            raise RuntimeError("fast group selection refused: selected Items/grid context is not established")
        return "verified_inventory_row_then_single_row_exact_ocr_and_selection_boundary"

    def focus_quick_entry_cat(self) -> str:
        focus = self.adapter._find_main_window()
        if focus is None:
            raise RuntimeError("fast group executor refused: main window disappeared")
        hwnd, _title = focus
        image = self.adapter._capture_client_image(hwnd)
        grid_cat = self.adapter._locate_label(image, "Cat", prefer="bottommost")
        if grid_cat is None:
            self._inventory = None
            raise RuntimeError("fast group executor refused: Quick Entry CAT geometry is unavailable")
        relation = self.calibration.geometry["grid_to_quick_cat"]
        left, top, right, bottom = tuple(grid_cat[i] + relation[i] for i in range(4))
        self.adapter._click_client(hwnd, (left + right) // 2, (top + bottom) // 2)
        if not self.adapter._force_foreground(hwnd):
            self._inventory = None
            raise RuntimeError("fast group executor refused: CAT focus could not be retained")
        return "fresh_quick_entry_geometry_and_foreground_click"

    def assert_batch_settled(self) -> None:
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group batch stopped: blocking dialog/dropdown detected after batch")

    def capture_group_evidence(self, group: str) -> str:
        self.adapter.evidence_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", group).strip("_") or "group"
        path = self.adapter.evidence_dir / f"post_group_{safe}.png"
        self.adapter._capture_client_image(self.adapter._ensure_main_window()).save(path)
        return str(path)

    def capture_final_evidence(self) -> str:
        self.adapter.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.adapter.evidence_dir / "final_estimate.png"
        self.adapter._capture_client_image(self.adapter._ensure_main_window()).save(path)
        return str(path)

    def accept_expected_biditem_duplicate(self) -> dict[str, float]:
        """Accept only a caller-predicted repeated group-local BIDITM.

        Window-title presence is a cheap non-OCR synchronization point. Live
        calibration observed the dialog at 22.75 ms; 100 ms preserves a
        bounded margin while the 5 ms poll avoids a fixed sleep.
        """
        started = time.perf_counter()
        deadline = started + 0.1
        while self.adapter._find_window_by_title(self.adapter._DUPLICATE_ITEM_DIALOG_TITLE) is None:
            if time.perf_counter() >= deadline:
                raise RuntimeError(
                    "expected repeated group-local DOR/BIDITM did not present Duplicate Item(s) within 100 ms"
                )
            time.sleep(0.005)
        appeared = time.perf_counter()
        self.keyboard.press_tab()
        self.keyboard.press_tab()
        self.keyboard.press_enter()
        close_deadline = time.perf_counter() + 0.1
        while self.adapter._find_window_by_title(self.adapter._DUPLICATE_ITEM_DIALOG_TITLE) is not None:
            if time.perf_counter() >= close_deadline:
                raise RuntimeError("expected Duplicate Item(s) remained after Tab x2 -> Enter")
            time.sleep(0.005)
        return {
            "appearance_wait_seconds": appeared - started,
            "acceptance_seconds": time.perf_counter() - appeared,
        }
