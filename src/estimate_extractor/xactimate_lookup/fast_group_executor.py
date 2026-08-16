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
    #: Every source shadow-plan row that produced this one physical
    #: execution item, in source order. Length 1 for the ordinary case;
    #: length 2 only for a proven remove/base pair collapsed into a single
    #: submission (see _remove_base_pair_reason()) -- review/audit code can
    #: always recover full source provenance from this without needing a
    #: separate schema.
    source_line_item_ids: tuple[str, ...]
    source_descriptions: tuple[str, ...]
    #: Parallel to source_line_item_ids/source_descriptions -- each source
    #: row's own original quantity, never combined. `quantity` above is the
    #: single value actually sent to Xactimate (the first/remove row's own
    #: quantity for a collapsed pair); this preserves what each source row
    #: originally said, even when they disagree.
    source_quantities: tuple[float | None, ...]
    #: True only when a collapsed pair's two source quantities differ --
    #: never set for an ordinary, single-source item.
    quantity_disagreement: bool = False
    #: True whenever a human should confirm the executed quantity before
    #: trusting it blindly -- currently set exactly when
    #: quantity_disagreement is True, kept as its own field since a future
    #: reason for requiring review need not imply a quantity disagreement.
    human_review_required: bool = False
    #: None for an ordinary, uncollapsed item; a short machine-readable
    #: reason (e.g. "paired_remove_base_same_identity") when this item
    #: represents two source rows collapsed into one submission.
    collapse_reason: str | None = None
    #: Number of Tab presses execute_fast_items() sends between typing the
    #: selector and typing the quantity. Standard resolved items expose an
    #: Act control (Sel -> Act -> Desc -> Qty, 3 tabs); catalog identities
    #: whose authoritative catalog_description starts with "Tear off",
    #: "Demolish", "Remove", "Scrape off", "Haul debris", or "Abatement",
    #: or contains "tear out" anywhere -- all live-proven removal-only, no
    #: traversable Act control -- get 2 (Sel -> Desc -> Qty). Deliberately
    #: narrow: "Strip ...", "Scrape {V} & prep for paint", "Detach &
    #: reset ...", plain "Paint ...", and "Additional charge ..." are all
    #: live-confirmed to still expose a real Act and must stay at 3.
    #: Computed once at compile time from the mapper's own
    #: catalog_description only -- never from original_description,
    #: source_action, collapse_reason, CAT, SEL, or quantity, so a source
    #: row's own "Remove ..." wording (e.g. a remove/base pair's remove
    #: side) can never trigger this by itself; only the resolved catalog
    #: identity's own description can. Unused by DOR/BIDITM items, which
    #: stay on execute_fast_bid_item()'s own separate, fixed sequence.
    quantity_tab_count: int = 3

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


def _normalized_description_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _remove_base_pair_reason(remove_row: dict[str, Any], base_row: dict[str, Any]) -> str | None:
    """Prove two ADJACENT, same-group source rows are a remove/base pair
    that Xactimate's own catalog represents as one physical line item split
    across two source rows (a "Remove X" row immediately followed by its
    paired "X" row) -- not two arbitrary rows that merely happen to share a
    CAT/SEL. Returns a short machine-readable collapse reason only when
    every proof holds, else None:

    - same group;
    - both resolved (never an unresolved/DOR-BIDITM fallback);
    - identical normalized CAT and SEL;
    - remove_row's description is exactly "Remove " + base_row's
      description, after conservative whitespace/case normalization.

    Quantity is deliberately NOT part of this predicate: the "Remove X" /
    "X" split is an explicit structural relationship in how the source
    describes the item, proven by identity and description shape alone --
    the two source quantities may legitimately differ (e.g. a remove
    measurement vs. an install measurement) without that meaning they are
    not the same paired item. Quantity handling (which value is actually
    submitted, and flagging a human-review need when they disagree) is the
    caller's responsibility once a pair is proven here.

    Deliberately generalized: no trade code, selector, or description
    fragment is ever hardcoded here. Only forward order (remove row first,
    base row second) is recognized -- no real evidence of the reverse
    orientation has been observed, so it is left alone rather than guessed
    at (see the audit in this feature's implementation report).
    """
    if remove_row.get("group") != base_row.get("group"):
        return None
    if remove_row.get("resolution") != "resolved" or base_row.get("resolution") != "resolved":
        return None
    remove_cat = (remove_row.get("execution_category") or remove_row.get("category") or "").strip().upper()
    base_cat = (base_row.get("execution_category") or base_row.get("category") or "").strip().upper()
    if not remove_cat or remove_cat != base_cat:
        return None
    remove_sel = (remove_row.get("execution_selector") or remove_row.get("selector") or "").strip().upper()
    base_sel = (base_row.get("execution_selector") or base_row.get("selector") or "").strip().upper()
    if not remove_sel or remove_sel != base_sel:
        return None
    remove_desc = _normalized_description_text(remove_row["original_description"])
    base_desc = _normalized_description_text(base_row["original_description"])
    if remove_desc != f"remove {base_desc}":
        return None
    return "paired_remove_base_same_identity"


def compile_executable_group_plan(shadow: dict[str, Any]) -> ExecutableGroupPlan:
    """Freeze a shadow plan into ordered, mapper-free live payloads.

    A proven remove/base source pair (see _remove_base_pair_reason()) is
    collapsed into ONE physical execution item here, before the executor
    ever sees it -- Xactimate raises its own "Duplicate Item(s)" dialog if
    both are submitted separately, and the keyboard hot loop stays entirely
    unaware this collapsing ever happens.
    """
    rows_by_id = {row["line_item_id"]: row for row in shadow["items"]}
    seen: set[str] = set()
    groups: list[ExecutableGroupBatch] = []
    for group_spec in shadow["group_first_future_layout"]:
        batch: list[ExecutableQuickEntryItem] = []
        line_ids = group_spec["line_item_ids"]
        index = 0
        while index < len(line_ids):
            line_id = line_ids[index]
            if line_id in seen or line_id not in rows_by_id:
                raise ValueError(f"shadow plan has missing or duplicate line item {line_id!r}")
            row = rows_by_id[line_id]

            paired_line_id: str | None = None
            paired_row: dict[str, Any] | None = None
            collapse_reason: str | None = None
            if index + 1 < len(line_ids):
                candidate_id = line_ids[index + 1]
                if candidate_id not in seen and candidate_id in rows_by_id:
                    candidate_row = rows_by_id[candidate_id]
                    reason = _remove_base_pair_reason(row, candidate_row)
                    if reason:
                        paired_line_id, paired_row, collapse_reason = candidate_id, candidate_row, reason

            seen.add(line_id)
            category = row.get("execution_category") or row.get("category")
            selector = row.get("execution_selector") or row.get("selector")
            if not category or not selector:
                raise ValueError(f"line item {line_id!r} has no executable CAT/SEL payload")
            is_bid = (category, selector) == ("DOR", "BIDITM")
            catalog_description = row.get("catalog_description")
            normalized_catalog_description = (catalog_description or "").strip().casefold()
            no_act = (
                normalized_catalog_description.startswith("tear off")
                or "tear out" in normalized_catalog_description
                or normalized_catalog_description.startswith("demolish")
                or normalized_catalog_description.startswith("remove")
                or normalized_catalog_description.startswith("scrape off")
                or normalized_catalog_description.startswith("haul debris")
                or normalized_catalog_description.startswith("abatement")
            )
            quantity_tab_count = 2 if no_act else 3
            pricing = dict(row.get("source_pricing") or {})
            source_line_item_ids: tuple[str, ...] = (line_id,)
            source_descriptions: tuple[str, ...] = (row["original_description"],)
            source_quantities: tuple[float | None, ...] = (row.get("quantity"),)
            quantity_disagreement = False
            if paired_row is not None:
                seen.add(paired_line_id)
                source_line_item_ids = (line_id, paired_line_id)
                source_descriptions = (row["original_description"], paired_row["original_description"])
                paired_quantity = paired_row.get("quantity")
                source_quantities = (row.get("quantity"), paired_quantity)
                # Quantity is not part of _remove_base_pair_reason()'s proof
                # (a remove measurement and an install measurement may
                # legitimately differ) -- the pair still collapses to one
                # physical submission using the remove row's own quantity,
                # but a disagreement must never be silently absorbed.
                remove_qty = row.get("quantity")
                if remove_qty is None or paired_quantity is None or format(remove_qty, "g") != format(paired_quantity, "g"):
                    quantity_disagreement = True
                index += 1  # the paired row is consumed here, never emitted on its own
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
                source_line_item_ids=source_line_item_ids, source_descriptions=source_descriptions,
                source_quantities=source_quantities, quantity_disagreement=quantity_disagreement,
                human_review_required=quantity_disagreement, collapse_reason=collapse_reason,
                quantity_tab_count=quantity_tab_count,
            ))
            index += 1
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
    def accept_expected_group_local_duplicate(self) -> dict[str, float]: ...
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
        time.sleep(0.2)
        focus_method, focus_elapsed = "skipped_after_verified_group_transition", 0.0
        focus_seconds.append(focus_elapsed)

        normal = batch.normal_items
        for item in batch.items:
            if item.quantity is None:
                raise RuntimeError(f"item {item.line_item_id!r} has no quantity; batch aborted before input")
        item_timings = []
        # Group-local: same effective (CAT, SEL) identity seen earlier in
        # THIS group's own source order triggers Xactimate's "Duplicate
        # Item(s)" confirmation on commit, for a resolved item exactly as
        # much as for the DOR/BIDITM fallback -- unresolved items always
        # share the identical ("DOR", "BIDITM") identity, so this one set
        # covers both without distinguishing them. Reset fresh per group
        # (declared inside this loop), so the same identity recurring in a
        # later, different group is never treated as a duplicate of it.
        seen_identities: set[tuple[str, str]] = set()
        duplicate_acceptances: list[dict[str, Any]] = []
        for item in batch.items:
            identity = (item.category.strip().upper(), item.selector.strip().upper())
            already_seen = identity in seen_identities
            if item.is_normal:
                item_timings.extend(execute_fast_items(
                    ui.keyboard,
                    [FastEntryItem(
                        item.category, item.selector, item.quantity, item.line_item_id,
                        quantity_tab_count=item.quantity_tab_count,
                    )],
                    clock=clock,
                ))
            else:
                item_timings.append(execute_fast_bid_item(
                    ui.keyboard,
                    FastBidItem(item.original_description, item.quantity, item.line_item_id),
                    clock=clock,
                ))
            if already_seen:
                duplicate_acceptances.append({
                    "line_item_id": item.line_item_id,
                    **ui.accept_expected_group_local_duplicate(),
                })
            seen_identities.add(identity)
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
            "expected_duplicate_acceptances": duplicate_acceptances,
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

    #: Bounded settling window for the post-click selection boundary to
    #: become observable. Xactimate does not always finish rendering a
    #: selection change synchronously with the click that caused it -- the
    #: older select_group() has its own fixed-sleep repaint wait for the
    #: same underlying reason (see its docstring). Generalized, bounded,
    #: fail-closed values, not tuned to any specific observed group,
    #: geometry, or claim -- the same _poll() bounded-retry pattern
    #: create_group() already uses, just with its own named constants.
    _SELECTION_BOUNDARY_SETTLE_TIMEOUT_S = 2.0
    _SELECTION_BOUNDARY_SETTLE_POLL_INTERVAL_S = 0.05

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

    def create_group(self, group: str) -> dict[str, Any]:
        """Request creation of `group` via the New Group dialog. Confirms
        only that the create OPERATION itself completed -- the dialog
        opened, accepted the typed name, and closed, with nothing blocking
        left behind -- never that Xactimate rendered/OCR's the arbitrary
        new name a particular way.

        Group identity is safety-critical only immediately before that
        group's own items are entered, and is proven there, independently,
        by select_group_lightweight()'s own fresh identity/boundary/
        context verification (unchanged). Proving it a second time here,
        right after creation, is redundant: nothing downstream consumes
        this method's return value for anything but reporting (see
        execute_group_first_plan(), which only spreads it into a report
        entry) -- verify_all_groups_created() always re-derives its own
        complete-set inventory from a fresh snapshot taken after every
        group has been created, never from anything recorded here.

        Live-caught: this method used to independently re-prove the same
        identity a second time via structural physical-delta/hierarchy
        reconciliation. A single Tesseract letter substitution
        ("Ext_Surfaces" read as "Ext_Surtaces") repeatedly, stably
        rejected an otherwise correctly created, correctly parented,
        uniquely-caused new row -- and an earlier version of that same
        reconciliation additionally required locating Xactimate's Subtotal
        column header, which calibration may reasonably demand be visible
        but production creation must not. Removing this redundant gate
        does not weaken safety: it moves identity-proving entirely to
        selection time, where it already, independently, has to happen
        before any item can be typed."""
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

        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast group creation refused: blocking UI appeared after creation")

        return {
            "creation_state": "created", "verification_method": "new_group_dialog_submitted_and_closed",
            "new_group_command_seconds": command_seconds, "group_name_input_seconds": input_seconds,
            "attach_to_dialog_close_seconds": attach_seconds,
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
        attempts: int | None = None, elapsed_seconds: float | None = None,
    ) -> None:
        """Best-effort diagnostic capture for a select_group_lightweight()
        verification failure -- purely additive evidence; any error here is
        swallowed and never changes, replaces, or delays the actual
        failure. `stage` names which specific check refused (geometry,
        OCR identity, blocking UI, missing boundary, or post-click
        context). `attempts`/`elapsed_seconds` (set only by the boundary
        settling loop) and a saved final frame close the observability gap
        the Ext_Surfaces failure exposed -- the failure JSON previously
        contained only derived facts, no timing and no image."""
        try:
            payload: dict[str, Any] = {"requested_group": group, "failure_stage": stage}
            if header is not None:
                payload["header_rect"] = list(header)
            if index is not None:
                payload["physical_row"] = index
            if attempts is not None:
                payload["verification_attempts"] = attempts
            if elapsed_seconds is not None:
                payload["elapsed_settle_seconds"] = elapsed_seconds
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
            if image is not None:
                try:
                    screenshot_path = evidence_root / f"group_selection_verification_failure_{slug}.png"
                    image.save(screenshot_path)
                    payload["screenshot"] = str(screenshot_path)
                except Exception:
                    pass
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

        # Identity was already independently proven above (the fresh
        # pre-click OCR reread) -- this loop's only job is waiting for
        # Xactimate's own selection repaint to become observable. It never
        # re-checks identity: a fresh frame is captured each attempt purely
        # to look for the boundary (and to fail fast on a blocking dialog),
        # not to re-derive which row is which.
        settle_started = time.perf_counter()
        attempt_count = 0
        last_frame = before

        def _settled_frame():
            nonlocal attempt_count, last_frame
            attempt_count += 1
            frame = self.adapter._capture_client_image(hwnd)
            last_frame = frame
            if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
                self._report_selection_verification_failure(
                    group=group, stage="unexpected_dialog_or_dropdown", image=frame, header=header, index=index,
                    attempts=attempt_count, elapsed_seconds=time.perf_counter() - settle_started,
                )
                raise RuntimeError("fast group selection refused: blocking UI appeared")
            if self.adapter._group_tree_row_has_selection_boundary(frame, header, index):
                return frame
            return None

        after = self._poll(
            _settled_frame,
            timeout_s=self._SELECTION_BOUNDARY_SETTLE_TIMEOUT_S,
            interval_s=self._SELECTION_BOUNDARY_SETTLE_POLL_INTERVAL_S,
        )
        if after is None:
            self._report_selection_verification_failure(
                group=group, stage="selection_boundary_absent", image=last_frame, header=header, index=index,
                attempts=attempt_count, elapsed_seconds=time.perf_counter() - settle_started,
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

    def accept_expected_group_local_duplicate(self) -> dict[str, float]:
        """Accept only a caller-predicted repeated group-local item identity
        -- a resolved (CAT, SEL) or the unresolved (DOR, BIDITM) fallback,
        both handled identically here since Xactimate raises the same
        Duplicate Item(s) confirmation for either after its normal commit.

        Window-title presence is a cheap non-OCR synchronization point. Live
        calibration observed the dialog at 22.75 ms; 100 ms preserves a
        bounded margin while the 5 ms poll avoids a fixed sleep.
        """
        started = time.perf_counter()
        deadline = started + 0.1
        while self.adapter._find_window_by_title(self.adapter._DUPLICATE_ITEM_DIALOG_TITLE) is None:
            if time.perf_counter() >= deadline:
                raise RuntimeError(
                    "expected repeated group-local item identity did not present Duplicate Item(s) within 100 ms"
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
