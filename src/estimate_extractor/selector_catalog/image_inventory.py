"""Builds the authoritative screenshot inventory by walking the extracted
reference library on disk -- not by trusting the library's own supplied
index CSVs (those are useful cross-reference metadata, but "process every
screenshot" means every screenshot actually present, so the filesystem is
the source of truth here).
"""

from __future__ import annotations

import re
from pathlib import Path

from estimate_extractor.selector_catalog.models import ScreenshotRef

SCREENSHOTS_DIR_NAME = "Screenshots_By_CAT"


class ImageInventoryError(Exception):
    pass


def find_screenshots_root(extracted_root: Path) -> Path:
    """The ZIP wraps everything in a single top-level directory (e.g.
    Xactimate_Reference_Library_v3/); locate Screenshots_By_CAT/ under
    whatever that top-level directory turns out to be named, rather than
    hardcoding the version-specific name."""
    direct = extracted_root / SCREENSHOTS_DIR_NAME
    if direct.is_dir():
        return direct
    matches = list(extracted_root.glob(f"*/{SCREENSHOTS_DIR_NAME}"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ImageInventoryError(
            f"Could not find a '{SCREENSHOTS_DIR_NAME}' directory under {extracted_root} "
            f"-- has the reference ZIP been extracted?"
        )
    raise ImageInventoryError(f"Found multiple '{SCREENSHOTS_DIR_NAME}' directories under {extracted_root}: {matches}")


def resolve_screenshot_path(extracted_root: Path, relative_path: str) -> Path:
    """Inverse of the relative_path computation in inventory_screenshots():
    resolves a stored relative_path (e.g. from a manifest entry or cached
    candidate) back to an absolute path under the current extracted_root,
    without assuming a specific library-version directory name."""
    screenshots_root = find_screenshots_root(extracted_root)
    return screenshots_root.parent / relative_path


def _parse_sequence(folder_category: str, filename: str) -> int:
    """Filenames are '<folder_category>_<sequence>_<uuid>.png'. Strip the
    known folder-category prefix first (folder names like
    '_NON_CAT_PROJECT_UI' themselves contain underscores, so a naive
    split(\"_\") would misparse them) and take the sequence from what's left."""
    prefix = f"{folder_category}_"
    if filename.startswith(prefix):
        remainder = filename[len(prefix) :]
    else:
        remainder = filename
    match = re.match(r"(\d+)_", remainder)
    if match:
        return int(match.group(1))
    return 0


def inventory_screenshots(extracted_root: Path) -> list[ScreenshotRef]:
    """Walks Screenshots_By_CAT/<CAT>/*.png and returns one ScreenshotRef
    per image, sorted by (folder_category, sequence, filename) for
    deterministic, reproducible processing order."""
    screenshots_root = find_screenshots_root(extracted_root)
    refs: list[ScreenshotRef] = []
    for folder in sorted(p for p in screenshots_root.iterdir() if p.is_dir()):
        folder_category = folder.name
        for image_path in sorted(folder.glob("*.png")):
            sequence = _parse_sequence(folder_category, image_path.name)
            refs.append(
                ScreenshotRef(
                    folder_category=folder_category,
                    sequence=sequence,
                    filename=image_path.name,
                    relative_path=str(image_path.relative_to(screenshots_root.parent)),
                    absolute_path=str(image_path.resolve()),
                )
            )
    refs.sort(key=lambda r: (r.folder_category, r.sequence, r.filename))
    return refs
