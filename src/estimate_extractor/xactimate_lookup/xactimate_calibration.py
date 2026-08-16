"""Machine/layout-specific geometry for the experimental fast executor.

Calibration is deliberately non-destructive: it reads the current Estimate
Items frame and Win32 geometry, but never clicks or types in Xactimate.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.1"
DEFAULT_CALIBRATION_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClaimExtractor" / "xactimate_calibrations"
LAYOUT_ERROR = "Xactimate layout differs from saved calibration — recalibrate"
CALIBRATION_GROUP_NAMES = ("CAL_ROW_ALPHA", "CAL_ROW_BRAVO", "CAL_ROW_CHARLIE")
GROUP_ROW_SPACING_TOLERANCE_PX = 2.0


@dataclass(frozen=True, slots=True)
class XactimateCalibration:
    schema_version: str
    profile_id: str
    machine_id: str
    created_at: str
    executable_path: str | None
    executable_version: str | None
    window_class: str
    window_title: str
    outer_rect: tuple[int, int, int, int]
    client_rect: tuple[int, int, int, int]
    client_screen_origin: tuple[int, int]
    monitor_rect: tuple[int, int, int, int]
    work_area_rect: tuple[int, int, int, int]
    dpi: int
    window_state: str
    landmarks: dict[str, list[int]]
    geometry: dict[str, Any]
    confidence: dict[str, str]
    validation_state: str
    unresolved: tuple[str, ...]

    @property
    def client_width(self) -> int:
        return self.client_rect[2] - self.client_rect[0]

    @property
    def client_height(self) -> int:
        return self.client_rect[3] - self.client_rect[1]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "XactimateCalibration":
        value = dict(value)
        for key in ("outer_rect", "client_rect", "client_screen_origin", "monitor_rect", "work_area_rect", "unresolved"):
            value[key] = tuple(value[key])
        return cls(**value)


def machine_identifier() -> str:
    raw = "|".join((socket.gethostname(), platform.system(), platform.machine()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def profile_path(directory: Path = DEFAULT_CALIBRATION_DIR, machine_id: str | None = None) -> Path:
    return directory / f"{machine_id or machine_identifier()}.json"


def save_calibration(profile: XactimateCalibration, directory: Path = DEFAULT_CALIBRATION_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = profile_path(directory, profile.machine_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _profile_transaction(path: Path, operation):
    """Restore the exact prior profile bytes if recalibration fails."""
    previous = path.read_bytes() if path.exists() else None
    try:
        return operation()
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            temporary = path.with_suffix(".restore.tmp")
            temporary.write_bytes(previous)
            temporary.replace(path)
        raise


def load_calibration(directory: Path = DEFAULT_CALIBRATION_DIR) -> XactimateCalibration:
    path = profile_path(directory)
    if not path.exists():
        raise RuntimeError("No Xactimate calibration exists for this machine — calibrate Xactimate")
    profile = XactimateCalibration.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if profile.schema_version != SCHEMA_VERSION or profile.machine_id != machine_identifier():
        raise RuntimeError(LAYOUT_ERROR)
    return profile


def _rect(value) -> tuple[int, int, int, int]:
    return tuple(int(part) for part in value)


def measure_group_row_pitch(
    ocr_data: dict[str, list[Any]], *, region_width: int, header_bottom: int = 0,
) -> dict[str, Any]:
    """Measure pitch only from plausible text in the Group name column.

    Confidence requires at least three physical row clusters and two mutually
    consistent consecutive spacings. A lone gap is retained diagnostically but
    can never authorize fast group selection.
    """
    boxes: list[dict[str, Any]] = []
    for values in zip(
        ocr_data.get("text", []), ocr_data.get("conf", []), ocr_data.get("left", []),
        ocr_data.get("top", []), ocr_data.get("width", []), ocr_data.get("height", []), strict=False,
    ):
        text, conf, left, top, width, height = values
        raw = {"text": str(text), "confidence": float(conf), "left": int(left), "top": int(top),
               "width": int(width), "height": int(height)}
        raw["center_y"] = raw["top"] + raw["height"] / 2
        alphanumeric = "".join(ch for ch in raw["text"] if ch.isalnum())
        reasons = []
        if len(alphanumeric) < 2: reasons.append("not_meaningful_text")
        if raw["height"] < 5: reasons.append("glyph_height_too_small")
        # Glyph antialiasing can begin only 2-3 px below the header's OCR
        # bounding bottom after the tree repaints. The header itself begins
        # above this boundary, so requiring top >= header_bottom separates it
        # without discarding a genuine first/root row.
        if raw["top"] < header_bottom: reasons.append("not_below_header")
        if raw["left"] < 0 or raw["left"] + raw["width"] > region_width: reasons.append("outside_group_name_column")
        raw["accepted"] = not reasons
        raw["rejection_reasons"] = reasons
        boxes.append(raw)

    accepted = sorted((box for box in boxes if box["accepted"]), key=lambda box: box["center_y"])
    clusters: list[list[dict[str, Any]]] = []
    for box in accepted:
        center = box["center_y"]
        if not clusters or center - sum(item["center_y"] for item in clusters[-1]) / len(clusters[-1]) > 5:
            clusters.append([box])
        else:
            clusters[-1].append(box)
    centers = [sum(box["center_y"] for box in cluster) / len(cluster) for cluster in clusters]
    row_tops = [min(box["top"] for box in cluster) for cluster in clusters]
    spacings = [b - a for a, b in zip(centers, centers[1:])]
    plausible = [spacing for spacing in spacings if 12 <= spacing <= 50]
    chosen = None
    inliers: list[float] = []
    if plausible:
        candidates = sorted(plausible)
        best: list[float] = []
        for candidate in candidates:
            current = [value for value in plausible if abs(value - candidate) <= max(2.0, candidate * 0.08)]
            if len(current) > len(best):
                best = current
        inliers = best
        if inliers:
            chosen = sum(inliers) / len(inliers)
    if chosen is not None and len(centers) >= 3 and len(inliers) >= 2:
        state, rejection = "measured_confident", None
    elif chosen is not None:
        state, rejection = "measured_low_confidence", "fewer than two consistent consecutive row spacings"
    else:
        state, rejection = "unresolved", "fewer than two measurable group-tree rows"
    return {
        "candidate_ocr_boxes": boxes, "accepted_row_centers": centers, "accepted_row_tops": row_tops,
        "calculated_spacings": spacings, "inlier_spacings": inliers,
        "chosen_pitch": round(chosen, 3) if chosen is not None else None,
        "confidence_state": state, "rejection_reason": rejection,
    }


#: How far (within the fallback crop) a candidate boundary box's vertical
#: center may differ from Group's own vertical center and still count as
#: the SAME header row -- separates a genuine next-column header fragment
#: (however garbled) from unrelated text at a different y-band inside the
#: same bounded crop (e.g. a breadcrumb bleeding in from the right pane).
_HEADER_ROW_BAND_TOLERANCE_PX = 6.0


def _locate_group_column_headers(adapter, image) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int], str]:
    """Locate Group and the Group-name column's right boundary.

    The boundary only needs the left edge of whatever renders next in the
    same header row -- it does not require OCR to correctly transcribe the
    literal word "Subtotal". Live-caught: the Group column can render
    noticeably wider than its content needs (observed to persist across
    project states, not just while a long name is present), pushing
    "Subtotal" hard against the panel's own right edge so only 2-3 glyphs
    of it ever render (e.g. "Sul"). A literal-word match then finds
    nothing even though the column boundary is still perfectly well
    defined by that same truncated fragment's position.
    """
    group = adapter._locate_group_tree_header(image)
    if group is None:
        raise RuntimeError("Group header is unavailable")
    subtotal = adapter._locate_label(image, "Subtotal", prefer="topmost")
    if subtotal is not None and subtotal[0] > group[2]:
        return tuple(group), tuple(subtotal), "exact_label"
    # Bound the search to the same 35px header band immediately right of
    # Group -- never a broader guess. Two tiers within it, most specific
    # first: a legible "subtot..." prefix, then (only if none is legible
    # at all) any other header-row content immediately right of Group.
    # The second tier's own text is never trusted as a row/group name --
    # only its position is used, to set the column boundary.
    crop_left = max(0, group[0] - 20)
    crop_top = max(0, group[1] - 10)
    crop_right = min(image.width, group[0] + 300)
    crop_bottom = min(image.height, group[1] + 35)
    data = adapter._ocr_data(image.crop((crop_left, crop_top, crop_right, crop_bottom)), config="--psm 6")
    group_center_y = (group[1] + group[3]) / 2
    prefix_matches: list[tuple[int, int, int, int]] = []
    row_aligned_candidates: list[tuple[int, int, int, int]] = []
    for text, left, top, width, height in zip(
        data.get("text", []), data.get("left", []), data.get("top", []),
        data.get("width", []), data.get("height", []), strict=False,
    ):
        normalized = "".join(ch for ch in str(text).casefold() if ch.isalnum())
        if not normalized:
            continue
        rect = (crop_left + int(left), crop_top + int(top),
                crop_left + int(left) + int(width), crop_top + int(top) + int(height))
        if rect[0] <= group[2]:
            continue
        if normalized.startswith("subtot"):
            prefix_matches.append(rect)
        candidate_height = rect[3] - rect[1]
        candidate_center_y = (rect[1] + rect[3]) / 2
        if 4 <= candidate_height <= 24 and abs(candidate_center_y - group_center_y) <= _HEADER_ROW_BAND_TOLERANCE_PX:
            row_aligned_candidates.append(rect)
    if len(prefix_matches) == 1:
        return tuple(group), tuple(prefix_matches[0]), "bounded_header_prefix_ocr"
    if prefix_matches:
        raise RuntimeError(f"Subtotal header fallback found {len(prefix_matches)} bounded candidate(s)")
    if row_aligned_candidates:
        boundary = min(row_aligned_candidates, key=lambda rect: rect[0])
        return tuple(group), tuple(boundary), "bounded_header_row_boundary"
    raise RuntimeError("Subtotal header fallback found 0 bounded candidate(s)")


def _group_column_inventory(adapter, image) -> dict[str, Any]:
    """Read physical group-name rows from the independently bounded column."""
    from .fast_group_executor import normalize_planned_group_identity

    group, subtotal, boundary_method = _locate_group_column_headers(adapter, image)
    region = (group[0], group[1], subtotal[0], image.height)
    data = adapter._ocr_data(image.crop(region))
    measured = measure_group_row_pitch(
        data, region_width=region[2] - region[0], header_bottom=group[3] - group[1],
    )
    accepted = [box for box in measured["candidate_ocr_boxes"] if box["accepted"]]
    clusters: list[list[dict[str, Any]]] = []
    for box in sorted(accepted, key=lambda value: value["center_y"]):
        if not clusters:
            clusters.append([box])
            continue
        center = sum(value["center_y"] for value in clusters[-1]) / len(clusters[-1])
        if box["center_y"] - center > 5:
            clusters.append([])
        clusters[-1].append(box)
    rows = []
    for cluster in clusters:
        text = " ".join(box["text"] for box in sorted(cluster, key=lambda value: value["left"]))
        center = sum(box["center_y"] for box in cluster) / len(cluster)
        rows.append({"raw_text": text, "normalized_text": normalize_planned_group_identity(text),
                     "center_y": region[1] + center, "relative_center_y": center,
                     "top": region[1] + min(box["top"] for box in cluster)})
    return {"header": list(group), "subtotal_header": list(subtotal), "boundary_method": boundary_method,
            "source_region": list(region),
            "ocr_diagnostics": measured, "rows": rows}


def _calibration_name_needle(name: str) -> str:
    from .fast_group_executor import normalize_planned_group_identity

    return normalize_planned_group_identity(name)


def _exact_calibration_name_matches(
    inventory: dict[str, Any], needle: str, *, exclude: frozenset[int] = frozenset(),
) -> list[tuple[int, dict[str, Any]]]:
    return [(index, row) for index, row in enumerate(inventory["rows"])
            if needle and needle in row["normalized_text"] and index not in exclude]


def _exact_calibration_row_map(inventory: dict[str, Any], names=CALIBRATION_GROUP_NAMES) -> dict[str, dict[str, Any]]:
    result = {}
    used: set[int] = set()
    for name in names:
        needle = _calibration_name_needle(name)
        matches = _exact_calibration_name_matches(inventory, needle)
        if len(matches) != 1:
            raise RuntimeError(f"calibration group {name!r} has {len(matches)} exact physical row match(es)")
        index, row = matches[0]
        if index in used:
            raise RuntimeError("two calibration names resolved to the same physical row")
        used.add(index)
        result[name] = row
    return result


#: Live-caught: OCR can drop an internal character from a genuinely
#: correct calibration sentinel name (observed: CAL_ROW_CHARLIE read as
#: "Cal_ROW_CHARLE)"). The three sentinel names are deliberately far
#: apart (minimum true pairwise normalized Levenshtein distance is 5;
#: see docs/calibration-sentinel-audit notes), so a small bounded
#: distance here cannot cross-match them. Scoped ONLY to the three known
#: calibration sentinel names -- never used for production group names.
CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE = 2


def _levenshtein(a: str, b: str) -> int:
    """Standard edit distance. Small local helper -- no new dependency;
    used only by the bounded calibration-sentinel matcher below."""
    if a == b:
        return 0
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    previous = list(range(n + 1))
    for i in range(1, m + 1):
        current = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[n]


def _min_substring_edit_distance(text: str, needle: str) -> int:
    """Edit distance between `needle` and its best-aligned substring of
    `text` -- free start/end within `text`, so surrounding OCR noise
    (glyph fragments merged into the same clustered row, e.g. the "fy"
    prefix seen live before a correctly-read sentinel) never inflates the
    distance, matching the tolerance the exact-substring check already
    has. Only genuine corruption WITHIN the aligned span counts."""
    m, n = len(needle), len(text)
    if m == 0:
        return 0
    if n == 0:
        return m
    previous = [0] * (n + 1)  # dp[0][j] = 0: free start anywhere in text
    for i in range(1, m + 1):
        current = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if needle[i - 1] == text[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return min(previous)  # free end: needle fully consumed, any remaining suffix ignored


def _calibration_name_guarded_matches(
    inventory: dict[str, Any], name: str, names=CALIBRATION_GROUP_NAMES,
    *, max_edit_distance: int = CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE,
) -> list[tuple[int, dict[str, Any]]]:
    """Physical rows attributable to `name`: exact normalized-substring
    containment first (identical semantics to _exact_calibration_row_map
    -- if any exact match exists, that is the answer, unfiltered by the
    fuzzy guards below). Only when there are ZERO exact matches does the
    bounded, calibration-only fuzzy tier run: a row qualifies only if its
    best-aligned substring edit distance to `name` is within
    `max_edit_distance` AND it is not simultaneously that close to any
    OTHER calibration sentinel (never guess between two plausible
    sentinels). Hierarchy/indentation is never consulted here -- not an
    identity predicate."""
    needle = _calibration_name_needle(name)
    exact = _exact_calibration_name_matches(inventory, needle)
    if exact:
        return exact
    other_needles = [_calibration_name_needle(other) for other in names if other != name]
    fuzzy: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(inventory["rows"]):
        distance = _min_substring_edit_distance(row["normalized_text"], needle)
        if distance > max_edit_distance:
            continue
        if any(_min_substring_edit_distance(row["normalized_text"], other_needle) <= max_edit_distance
               for other_needle in other_needles):
            continue  # ambiguous against another sentinel -- never guess
        fuzzy.append((index, row))
    return fuzzy


def _guarded_calibration_row_map(
    inventory: dict[str, Any], names=CALIBRATION_GROUP_NAMES,
    *, max_edit_distance: int = CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE,
) -> dict[str, dict[str, Any]]:
    """Like _exact_calibration_row_map, but each sentinel with zero exact
    matches falls back to the bounded, calibration-only fuzzy tier (see
    _calibration_name_guarded_matches). Still requires exactly one
    physical row per sentinel and fails closed on 0, 2+, or a row already
    claimed by an earlier sentinel in this call."""
    result = {}
    used: set[int] = set()
    for name in names:
        matches = [(index, row) for index, row in _calibration_name_guarded_matches(inventory, name, names, max_edit_distance=max_edit_distance)
                   if index not in used]
        if len(matches) != 1:
            raise RuntimeError(f"calibration group {name!r} has {len(matches)} exact/guarded physical row match(es)")
        index, row = matches[0]
        used.add(index)
        result[name] = row
    return result


#: Two physical rows are the "same" row across a before/after capture if
#: their measured centers land within this tolerance -- matches the row-
#: clustering tolerance _group_column_inventory() already uses, and is
#: deliberately position-only (never OCR text, which is exactly the
#: imperfect signal the causal-delta check below exists to route around).
_PHYSICAL_ROW_RECONCILE_TOLERANCE_PX = 5.0


def _new_physical_rows(
    before: dict[str, Any], after: dict[str, Any], *, tolerance: float = _PHYSICAL_ROW_RECONCILE_TOLERANCE_PX,
) -> list[dict[str, Any]]:
    """Rows present in `after` with no positional counterpart in
    `before`. Reconciled by physical Y position only -- never by OCR text
    (the imperfect signal being routed around) and never by absolute
    list index (nesting/insertion can shift indices without any row
    actually moving)."""
    before_centers = [row["center_y"] for row in before["rows"]]
    return [row for row in after["rows"]
            if not any(abs(row["center_y"] - center) <= tolerance for center in before_centers)]


def confident_known_group_measurement(
    inventory: dict[str, Any], names=CALIBRATION_GROUP_NAMES,
    *, tolerance: float = GROUP_ROW_SPACING_TOLERANCE_PX,
) -> dict[str, Any]:
    """Measure two spacings from three known calibration rows, identified
    by exact text first and the bounded calibration-only fuzzy tier only
    when a sentinel has no exact match (see _guarded_calibration_row_map)."""
    try:
        mapping = _guarded_calibration_row_map(inventory, names)
    except RuntimeError as exc:
        return {"calibration_group_names": list(names), "detected_row_centers": {}, "measured_spacings": [],
                "chosen_pitch": None, "tolerance_px": tolerance, "confidence_state": "unresolved",
                "rejection_reason": str(exc), "inventory": inventory}
    centers = {name: float(mapping[name]["center_y"]) for name in names}
    spacings = [centers[names[1]] - centers[names[0]], centers[names[2]] - centers[names[1]]]
    if any(value <= 0 for value in spacings):
        state, pitch, rejection = "unresolved", None, "calibration rows are not in expected physical order"
    elif abs(spacings[0] - spacings[1]) > tolerance:
        state, pitch, rejection = "measured_low_confidence", None, "known-row spacings disagree beyond tolerance"
    else:
        state, pitch, rejection = "measured_confident", round(sum(spacings) / 2, 3), None
    return {"calibration_group_names": list(names), "detected_row_centers": centers,
            "measured_spacings": spacings, "chosen_pitch": pitch, "tolerance_px": tolerance,
            "confidence_state": state, "rejection_reason": rejection, "inventory": inventory}


def _settled_group_column_inventory(adapter, hwnd, *, timeout_s: float = 5.0, poll_interval_s: float = 0.05) -> dict[str, Any]:
    """_group_column_inventory(), retried across a bounded settle window.

    Live-caught: immediately after a group-tree mutation (a just-created
    row still settling into its selected/highlighted render), a single
    header-locate attempt can transiently fail even though the identical
    frame succeeds moments later -- the same class of repaint delay this
    function's own row-confirmation polling below already accounts for.
    Never unbounded; still fails closed by re-raising the underlying
    locator error once the deadline passes without ever seeing a stable
    frame -- this never loosens what counts as a valid header/boundary.
    """
    deadline = time.perf_counter() + timeout_s
    while True:
        try:
            return _group_column_inventory(adapter, adapter._capture_client_image(hwnd))
        except RuntimeError:
            if time.perf_counter() >= deadline:
                raise
            time.sleep(poll_interval_s)


def _create_one_calibration_group(adapter, profile: XactimateCalibration, name: str) -> dict[str, Any]:
    """Use the fast New Group primitives, always from the live root row."""
    # The group tree has its own independent scroll position that a busy
    # project can easily drift past the root row -- see
    # _scroll_group_tree_to_top()'s own docstring. Called defensively
    # here for the same reason it is called at every other group-tree
    # entry point in windows_adapter.py: never assumed unnecessary.
    adapter._scroll_group_tree_to_top(adapter._ensure_main_window())
    hwnd = adapter._ensure_main_window()
    inventory = _settled_group_column_inventory(adapter, hwnd)
    root_rows = [row for row in inventory["rows"] if profile.window_title.casefold() in row["raw_text"].casefold()]
    if len(root_rows) != 1:
        raise RuntimeError("calibration could not uniquely locate the project root row")
    header = tuple(inventory["header"])
    root_top_offset = round(root_rows[0]["top"] - header[1])
    adapter._GROUP_TREE_ROW_TEXT_TOP_DY = root_top_offset
    adapter._GROUP_TREE_CLICK_DY_OFFSET = max(2, round(root_rows[0]["center_y"] - root_rows[0]["top"]))
    items = adapter._open_group_tree_context_menu(hwnd, header, 0)
    adapter._click_group_menu_item(items, int(profile.geometry["group_context_menu_new_index"]))
    deadline = time.perf_counter() + 5
    dialog = None
    while dialog is None and time.perf_counter() < deadline:
        dialog = adapter._find_window_by_title("New Group")
        if dialog is None: time.sleep(0.05)
    if dialog is None:
        raise RuntimeError("New Group dialog did not appear")
    adapter._click_client(dialog, *profile.geometry["new_group_dialog_name_click"])
    adapter._select_all_and_delete()
    adapter._type_keybdevent(name, char_interval_s=0.005)
    adapter._click_client(dialog, *profile.geometry["new_group_dialog_attach_click"])
    deadline = time.perf_counter() + 5
    while adapter._find_window_by_title("New Group") is not None and time.perf_counter() < deadline:
        time.sleep(0.05)
    if adapter._find_window_by_title("New Group") is not None:
        raise RuntimeError("New Group dialog did not close")
    # Confirmation binds on the causal physical delta first (exactly one
    # new physical row versus the `inventory` captured before this
    # creation began), not on OCR identity alone -- OCR can drop an
    # internal character from a genuinely correct sentinel (live-caught:
    # CAL_ROW_CHARLIE read as "Cal_ROW_CHARLE)"). The one new row is then
    # required to be OCR-compatible with the intended sentinel under the
    # same bounded, calibration-only guard _guarded_calibration_row_map
    # uses, so a coincidental unrelated new row can never be bound simply
    # because a delta of exactly one appeared.
    deadline = time.perf_counter() + 5
    while time.perf_counter() < deadline:
        try:
            current = _group_column_inventory(adapter, adapter._capture_client_image(hwnd))
        except RuntimeError:
            time.sleep(0.05)
            continue
        new_rows = _new_physical_rows(inventory, current)
        if len(new_rows) == 1:
            candidate = new_rows[0]
            needle = _calibration_name_needle(name)
            other_needles = [_calibration_name_needle(other) for other in CALIBRATION_GROUP_NAMES if other != name]
            distance = _min_substring_edit_distance(candidate["normalized_text"], needle)
            competing = any(
                _min_substring_edit_distance(candidate["normalized_text"], other_needle) <= CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE
                for other_needle in other_needles
            )
            if distance <= CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE and not competing:
                return {"name": name, "state": "created_and_exactly_observed",
                        "physical_delta": 1, "match_distance": distance}
        time.sleep(0.05)
    raise RuntimeError(f"new calibration group {name!r} was not exactly observed")


def complete_interactive_group_row_calibration(
    adapter, profile: XactimateCalibration, *, directory: Path = DEFAULT_CALIBRATION_DIR,
    group_creator=None, evidence_dir: Path | None = None,
) -> tuple[XactimateCalibration, dict[str, Any]]:
    """Boundedly create three empty groups and measure their exact rows."""
    if profile.geometry.get("group_row_pitch_state") == "measured_confident":
        return profile, {"triggered": False, "reason": "row pitch already confident"}
    if adapter._unexpected_dialog_present() or adapter._find_dropdown_window() is not None:
        raise RuntimeError("interactive row calibration refused: blocking dialog/dropdown is present")
    initial = _group_column_inventory(adapter, adapter._capture_client_image(adapter._ensure_main_window()))
    for name in CALIBRATION_GROUP_NAMES:
        try:
            _exact_calibration_row_map(initial, (name,))
        except RuntimeError as exc:
            if "0 exact" in str(exc):
                continue
            raise RuntimeError(f"interactive row calibration refused: {exc}") from exc
        raise RuntimeError(f"interactive row calibration refused: calibration name {name!r} already exists")
    creator = group_creator or (lambda name: _create_one_calibration_group(adapter, profile, name))
    creation = [creator(name) for name in CALIBRATION_GROUP_NAMES]
    image = adapter._capture_client_image(adapter._ensure_main_window())
    final_inventory = _group_column_inventory(adapter, image)
    measurement = confident_known_group_measurement(final_inventory)
    evidence_root = evidence_dir or directory / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    screenshot = evidence_root / f"{profile.profile_id}-group-rows.png"
    image.save(screenshot)
    measurement.update({"triggered": True, "creation": creation, "screenshot": str(screenshot),
                        "cleanup": "not_attempted_existing_delete_group_is_not_deterministic"})
    if measurement["confidence_state"] != "measured_confident":
        failed_geometry = dict(profile.geometry)
        failed_geometry.update({"group_row_height": None,
                                "group_row_pitch_state": measurement["confidence_state"],
                                "group_row_diagnostics": measurement})
        save_calibration(replace(profile, geometry=failed_geometry), directory)
        raise RuntimeError("Group-row geometry requires calibration: " + str(measurement["rejection_reason"]))
    geometry = dict(profile.geometry)
    root_identity = "".join(ch for ch in profile.window_title.casefold() if ch.isalnum())
    root_matches = [row for row in final_inventory["rows"] if root_identity and root_identity in row["normalized_text"]]
    if len(root_matches) != 1:
        raise RuntimeError("Group-row geometry requires calibration: project root row is not unique")
    known_mapping = _guarded_calibration_row_map(final_inventory)
    click_offsets = [known_mapping[name]["center_y"] - known_mapping[name]["top"] for name in CALIBRATION_GROUP_NAMES]
    geometry.update({"group_row_height": measurement["chosen_pitch"],
                     "group_row_pitch_state": "measured_confident",
                     "group_row_text_top_offset": root_matches[0]["top"] - final_inventory["header"][1],
                     "group_click_y_offset": round(sum(click_offsets) / len(click_offsets)),
                     "group_row_diagnostics": measurement})
    confidence = dict(profile.confidence); confidence["group_row_height"] = "measured_confident"
    unresolved = tuple(value for value in profile.unresolved if value != "Group-row geometry requires calibration")
    completed = replace(profile, geometry=geometry, confidence=confidence, unresolved=unresolved,
                        validation_state="ready_for_fast_execution")
    save_calibration(completed, directory)
    return completed, measurement


#: Shown verbatim (via calibrate_xactimate()'s caller re-raising this
#: message) when the group tree positively has none of the three
#: calibration rows and the caller has not granted creation permission --
#: actionable, and explicit that creation (not calibration itself) is
#: what's gated, matching the checkbox's now-narrow meaning.
CREATION_PERMISSION_REQUIRED_MESSAGE = (
    "Group-row geometry unresolved — enable temporary calibration-group creation and run calibration again."
)


def _complete_or_recover_group_rows(
    adapter, profile: XactimateCalibration, *, directory: Path,
    evidence_dir: Path | None, allow_creation: bool = False,
) -> tuple[XactimateCalibration, dict[str, Any]]:
    """Inspect the live tree first, always -- never gated by creation
    permission. Recovery only ever needs read access, so a caller with
    creation permission OFF can still recover three already-existing
    rows; permission is consulted only in the genuine 0/3 case, where it
    decides between bootstrap creation and an actionable refusal."""
    # See _create_one_calibration_group()'s identical comment: the group
    # tree's scroll position is independent of everything else and a
    # busy project can easily drift the calibration rows out of the
    # captured client area, which would otherwise misreport them as
    # absent and route into (redundant, failure-prone) creation.
    adapter._scroll_group_tree_to_top(adapter._ensure_main_window())
    inventory = _group_column_inventory(adapter, adapter._capture_client_image(adapter._ensure_main_window()))
    # Presence uses the same exact-then-guarded-fuzzy policy recovery
    # itself will use -- a sentinel that already, genuinely exists but
    # OCR'd imperfectly (live-caught: CAL_ROW_CHARLIE -> "CHARLE)") must
    # still route to read-only recovery, not a redundant/wrong creation
    # attempt. This is distinct from complete_interactive_group_row_
    # calibration()'s own pre-creation "does this already exist" guard,
    # which stays exact-only deliberately (see that function).
    present = []
    for name in CALIBRATION_GROUP_NAMES:
        matches = _calibration_name_guarded_matches(inventory, name)
        if len(matches) == 1:
            present.append(name)
        elif len(matches) > 1:
            raise RuntimeError(
                f"interactive row calibration refused: calibration group {name!r} has "
                f"{len(matches)} exact/guarded physical row match(es)"
            )
    if len(present) == len(CALIBRATION_GROUP_NAMES):
        return recover_existing_group_row_calibration(
            adapter, profile, directory=directory, evidence_dir=evidence_dir,
        )
    if present:
        missing = [name for name in CALIBRATION_GROUP_NAMES if name not in present]
        raise RuntimeError(
            "interactive row calibration refused: partial calibration group set; "
            f"present={present}, missing={missing}"
        )
    if not allow_creation:
        raise RuntimeError(CREATION_PERMISSION_REQUIRED_MESSAGE)
    return complete_interactive_group_row_calibration(
        adapter, profile, directory=directory, evidence_dir=evidence_dir,
    )


def recover_existing_group_row_calibration(
    adapter, profile: XactimateCalibration, *, directory: Path = DEFAULT_CALIBRATION_DIR,
    evidence_dir: Path | None = None,
) -> tuple[XactimateCalibration, dict[str, Any]]:
    """Read-only recovery using three already-existing calibration rows."""
    if adapter._unexpected_dialog_present() or adapter._find_dropdown_window() is not None:
        raise RuntimeError("row calibration recovery refused: blocking dialog/dropdown is present")
    # See _create_one_calibration_group()'s identical comment: the root
    # row this function looks up below can be scrolled out of the
    # captured client area in a busy project.
    adapter._scroll_group_tree_to_top(adapter._ensure_main_window())
    image = adapter._capture_client_image(adapter._ensure_main_window())
    inventory = _group_column_inventory(adapter, image)
    measurement = confident_known_group_measurement(inventory)
    evidence_root = evidence_dir or directory / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    screenshot = evidence_root / f"{profile.profile_id}-existing-group-rows.png"
    image.save(screenshot)
    measurement.update({"triggered": True, "mode": "read_only_existing_rows", "creation": [],
                        "screenshot": str(screenshot), "cleanup": "not_requested"})
    if measurement["confidence_state"] != "measured_confident":
        failed = dict(profile.geometry)
        failed.update({"group_row_height": None, "group_row_pitch_state": measurement["confidence_state"],
                       "group_row_diagnostics": measurement})
        save_calibration(replace(profile, geometry=failed), directory)
        raise RuntimeError("Group-row geometry requires calibration: " + str(measurement["rejection_reason"]))
    mapping = _guarded_calibration_row_map(inventory)
    root_identity = "".join(ch for ch in profile.window_title.casefold() if ch.isalnum())
    root_matches = [row for row in inventory["rows"] if root_identity and root_identity in row["normalized_text"]]
    if len(root_matches) != 1:
        raise RuntimeError("Group-row geometry requires calibration: project root row is not unique")
    click_offsets = [mapping[name]["center_y"] - mapping[name]["top"] for name in CALIBRATION_GROUP_NAMES]
    geometry = dict(profile.geometry)
    geometry.update({"group_row_height": measurement["chosen_pitch"], "group_row_pitch_state": "measured_confident",
                     "group_row_text_top_offset": root_matches[0]["top"] - inventory["header"][1],
                     "group_click_y_offset": round(sum(click_offsets) / len(click_offsets)),
                     "group_row_diagnostics": measurement})
    confidence = dict(profile.confidence); confidence["group_row_height"] = "measured_confident"
    unresolved = tuple(value for value in profile.unresolved if value != "Group-row geometry requires calibration")
    # Group creation can repaint/canonicalize the content pane, moving the
    # Group and grid anchors together. Refresh the same read-only landmarks
    # from this measurement frame so the saved profile describes the frame it
    # actually measured rather than a pre-creation scroll position.
    group = tuple(inventory["header"])
    grid_cat = adapter._locate_label(image, "Cat", prefer="bottommost")
    items = adapter._locate_items_tab(image)
    search = adapter._items_search_pane_field(image)
    if grid_cat is None or items is None or search is None:
        raise RuntimeError("Group-row geometry requires calibration: core Items/grid landmarks are unavailable")
    offset = (grid_cat[0] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[0],
              grid_cat[1] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[1])
    quick_cat = adapter._shifted_anchor("quick_entry_cat_value", offset)
    grid_header = adapter._shifted_anchor("grid_header", offset)
    grid_row_1 = adapter._shifted_anchor("grid_row_1", offset)
    landmarks = dict(profile.landmarks)
    landmarks.update({"group_tree_header": list(group), "grid_header_cat": list(grid_cat),
                      "items_tab": list(items), "items_search": list(search),
                      "quick_entry_cat": list(quick_cat), "grid_header": list(grid_header),
                      "grid_row_1": list(grid_row_1)})
    geometry["group_tree_bounds"] = [max(0, group[0] - 4), group[3], inventory["subtotal_header"][0], image.height]
    geometry["items_grid_bounds"] = [grid_header[0], items[3], grid_header[2], image.height]
    recovered = replace(profile, geometry=geometry, landmarks=landmarks,
                        confidence=confidence, unresolved=unresolved,
                        validation_state="ready_for_fast_execution")
    save_calibration(recovered, directory)
    return recovered, measurement


def calibrate_xactimate(
    adapter, *, directory: Path = DEFAULT_CALIBRATION_DIR,
    allow_interactive_group_rows: bool = False, evidence_dir: Path | None = None,
) -> tuple[XactimateCalibration, Path]:
    """Detect and persist the current non-destructive Estimate Items layout."""
    if not adapter.verify_application() or not adapter.verify_project():
        raise RuntimeError("Calibration refused: the expected Xactimate project is not active")
    if adapter._unexpected_dialog_present() or adapter._find_dropdown_window() is not None:
        raise RuntimeError("Calibration refused: an unexpected blocking dialog/dropdown is present")
    found = adapter._find_main_window()
    if found is None:
        raise RuntimeError("Calibration refused: Xactimate main window was not found")
    hwnd, title = found
    win32gui = adapter._win32gui()
    window_rect = _rect(win32gui.GetWindowRect(hwnd))
    client_rect = _rect(win32gui.GetClientRect(hwnd))
    origin = tuple(adapter._get_client_origin(hwnd))
    image = adapter._capture_client_image(hwnd)
    group = adapter._locate_group_tree_header(image)
    grid_cat = adapter._locate_label(image, "Cat", prefer="bottommost")
    items = adapter._locate_items_tab(image)
    search = adapter._items_search_pane_field(image)
    if group is None or grid_cat is None or items is None or search is None:
        missing = [name for name, value in (("group_tree_header", group), ("grid_header_cat", grid_cat), ("items_tab", items), ("items_search", search)) if value is None]
        raise RuntimeError("Calibration refused: missing required landmark(s): " + ", ".join(missing))
    if not adapter._tab_is_active(image, items):
        raise RuntimeError("Calibration refused: navigate Xactimate to the active Estimate Items screen")

    ctypes, wintypes = adapter._win32()
    user32 = ctypes.windll.user32
    dpi = int(user32.GetDpiForWindow(hwnd))
    monitor = user32.MonitorFromWindow(hwnd, 2)
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise RuntimeError("Calibration refused: monitor geometry is unavailable")
    monitor_rect = (info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)
    work_rect = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
    state = "maximized" if user32.IsZoomed(hwnd) else "minimized" if user32.IsIconic(hwnd) else "restored"

    offset = (grid_cat[0] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[0], grid_cat[1] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[1])
    quick_cat = adapter._shifted_anchor("quick_entry_cat_value", offset)
    grid_header = adapter._shifted_anchor("grid_header", offset)
    grid_row_1 = adapter._shifted_anchor("grid_row_1", offset)
    landmarks = {
        "group_tree_header": list(_rect(group)), "grid_header_cat": list(_rect(grid_cat)),
        "items_tab": list(_rect(items)), "items_search": list(_rect(search)),
        "quick_entry_cat": list(_rect(quick_cat)), "grid_header": list(_rect(grid_header)),
        "grid_row_1": list(_rect(grid_row_1)),
    }
    row_diagnostics: dict[str, Any]
    try:
        subtotal = adapter._locate_label(image, "Subtotal", prefer="topmost")
        if subtotal is None or subtotal[0] <= group[2]:
            raise RuntimeError("Subtotal header did not establish the right edge of the Group name column")
        tree_left, tree_top, tree_right = group[0], group[1], subtotal[0]
        tree_crop = image.crop((tree_left, tree_top, tree_right, image.height))
        row_diagnostics = measure_group_row_pitch(
            adapter._ocr_data(tree_crop), region_width=tree_right - tree_left,
            header_bottom=group[3] - group[1],
        )
        row_diagnostics["source_region"] = [tree_left, tree_top, tree_right, image.height]
    except Exception as exc:
        row_diagnostics = {
            "candidate_ocr_boxes": [], "accepted_row_centers": [], "accepted_row_tops": [], "calculated_spacings": [],
            "inlier_spacings": [], "chosen_pitch": None, "confidence_state": "unresolved",
            "rejection_reason": f"group-tree row measurement unavailable: {exc}",
        }
    row_state = row_diagnostics["confidence_state"]
    row_height = row_diagnostics["chosen_pitch"] if row_state == "measured_confident" else None
    accepted_tops = row_diagnostics["accepted_row_tops"]
    row_top_offset = accepted_tops[0] if accepted_tops and row_height is not None else None
    geometry = {
        "group_tree_bounds": [max(0, group[0] - 4), group[3], min(image.width, group[0] + adapter._GROUP_TREE_TEXT_WIDTH), image.height],
        "group_row_text_top_offset": row_top_offset,
        "group_row_height": row_height,
        "group_row_pitch_state": row_state,
        "group_row_diagnostics": row_diagnostics,
        "group_click_x_offset": adapter._GROUP_TREE_CLICK_DX,
        "group_click_y_offset": adapter._GROUP_TREE_CLICK_DY_OFFSET,
        "group_text_x_offset": adapter._GROUP_TREE_TEXT_DX,
        "group_text_width": adapter._GROUP_TREE_TEXT_WIDTH,
        "group_row_crop_margin_top": adapter._GROUP_TREE_ROW_CROP_MARGIN_TOP,
        "group_row_crop_height": adapter._GROUP_TREE_ROW_CROP_HEIGHT,
        "selection_min_overlap_run": adapter._GROUP_TREE_SELECTION_MIN_OVERLAP_RUN,
        "items_grid_bounds": [grid_header[0], items[3], grid_header[2], image.height],
        "grid_to_quick_cat": [quick_cat[i] - grid_cat[i] for i in range(4)],
        "new_group_dialog_name_click": [185, 18],
        "new_group_dialog_attach_click": [305, 75],
        "group_context_menu_new_index": adapter._GROUP_MENU_NEW_INDEX,
        "group_tree_scroll_point": [max(1, group[0] + 100), max(1, group[1] + 40)],
    }
    confidence = {name: "detected_current_frame" for name in landmarks}
    confidence.update({
        "group_row_height": row_state,
        "selection_geometry": "existing_relative_pixel_structure",
        "new_group_dialog_geometry": "legacy_fixed_not_non_destructively_detectable",
        "group_context_menu_index": "uia_structural_semantics",
    })
    exe_path = None
    try:
        _, pid = win32gui.GetWindowThreadProcessId(hwnd)
        import psutil
        exe_path = psutil.Process(pid).exe()
    except Exception:
        pass
    machine = machine_identifier()
    stamp = datetime.now(timezone.utc).isoformat()
    profile = XactimateCalibration(
        schema_version=SCHEMA_VERSION, profile_id=f"{machine}-{dpi}-{client_rect[2]}x{client_rect[3]}",
        machine_id=machine, created_at=stamp, executable_path=exe_path, executable_version=None,
        window_class=str(win32gui.GetClassName(hwnd)), window_title=title,
        outer_rect=window_rect, client_rect=client_rect, client_screen_origin=origin,
        monitor_rect=monitor_rect, work_area_rect=work_rect, dpi=dpi, window_state=state,
        landmarks=landmarks, geometry=geometry, confidence=confidence,
        validation_state="valid_non_destructive_core_landmarks",
        unresolved=tuple(value for value in (
            None if row_state == "measured_confident" else "Group-row geometry requires calibration",
            "New Group dialog controls require a bounded interactive calibration",
        ) if value),
    )
    path = profile_path(directory, profile.machine_id)
    if row_state != "measured_confident":
        # Presence-check/recovery must run unconditionally here -- it is
        # read-only until 0/3 calibration rows are positively proven, and
        # only THAT branch needs permission. allow_interactive_group_rows
        # therefore means "permission to create", not "permission to look",
        # and must never suppress recovering three rows that already exist.
        profile, _measurement = _profile_transaction(
            path,
            lambda: _complete_or_recover_group_rows(
                adapter, profile, directory=directory, evidence_dir=evidence_dir,
                allow_creation=allow_interactive_group_rows,
            ),
        )
    else:
        # Row pitch can already be confident here without ever touching
        # CAL_ROW_* -- measured directly from real existing group rows.
        # That is one of the two valid calibration end states, so it must
        # be marked ready the same way the interactive/recovery paths do,
        # not left stuck at the pre-row-measurement validation state.
        profile = replace(profile, validation_state="ready_for_fast_execution")
        save_calibration(profile, directory)
    return profile, path


def validate_calibration(
    adapter, profile: XactimateCalibration, *, position_tolerance: int = 8,
    require_safe_group_rows: bool = True,
) -> dict[str, Any]:
    """Lightweight fail-closed validation before experimental execution."""
    found = adapter._find_main_window()
    reasons: list[str] = []
    if found is None:
        reasons.append("Xactimate window missing")
        return {"ok": False, "reasons": reasons}
    hwnd = found[0]
    win32gui = adapter._win32gui()
    client = _rect(win32gui.GetClientRect(hwnd))
    ctypes, _ = adapter._win32()
    dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
    if (client[2] - client[0], client[3] - client[1]) != (profile.client_width, profile.client_height):
        reasons.append("client size mismatch")
    if dpi != profile.dpi:
        reasons.append("DPI mismatch")
    if require_safe_group_rows and profile.geometry.get("group_row_pitch_state") != "measured_confident":
        reasons.append("Group-row geometry requires calibration")
    image = adapter._capture_client_image(hwnd)
    observed = {
        "group_tree_header": adapter._locate_group_tree_header(image),
        "grid_header_cat": adapter._locate_label(image, "Cat", prefer="bottommost"),
        "items_tab": adapter._locate_items_tab(image),
    }
    for name, rect in observed.items():
        expected = profile.landmarks[name]
        if rect is None:
            reasons.append(f"missing landmark: {name}")
        elif abs(rect[0] - expected[0]) > position_tolerance or abs(rect[1] - expected[1]) > position_tolerance:
            reasons.append(f"landmark moved: {name}")
    if adapter._items_search_pane_field(image) is None:
        reasons.append("active Items/Search context missing")
    return {"ok": not reasons, "reasons": reasons, "client_rect": client, "dpi": dpi, "landmarks": {k: list(v) if v else None for k, v in observed.items()}}


def apply_fast_geometry(adapter, profile: XactimateCalibration) -> None:
    """Apply only fast-path relative geometry; production defaults stay intact."""
    if profile.geometry.get("group_row_pitch_state") != "measured_confident":
        raise RuntimeError("Group-row geometry requires calibration")
    mapping = {
        "_GROUP_TREE_ROW_TEXT_TOP_DY": "group_row_text_top_offset",
        "_GROUP_TREE_ROW_HEIGHT": "group_row_height", "_GROUP_TREE_CLICK_DX": "group_click_x_offset",
        "_GROUP_TREE_CLICK_DY_OFFSET": "group_click_y_offset", "_GROUP_TREE_TEXT_DX": "group_text_x_offset",
        "_GROUP_TREE_TEXT_WIDTH": "group_text_width", "_GROUP_TREE_ROW_CROP_MARGIN_TOP": "group_row_crop_margin_top",
        "_GROUP_TREE_ROW_CROP_HEIGHT": "group_row_crop_height", "_GROUP_TREE_SELECTION_MIN_OVERLAP_RUN": "selection_min_overlap_run",
        "_GROUP_MENU_NEW_INDEX": "group_context_menu_new_index",
    }
    for attribute, key in mapping.items():
        setattr(adapter, attribute, int(profile.geometry[key]))
