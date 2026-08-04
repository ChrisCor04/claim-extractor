"""Writers for the CLI/UI's on-disk extraction outputs.

``cli.py`` and ``ui/pipeline_service.py`` have imported from this package
since before the earliest commit in this repository's history, but the
package itself was never created -- both entry points have been unable to
import at all as a result (Phase 5.0 finding). This package restores exactly
the six functions those two call sites already depend on, matching the
established writer pattern in ``mapping/outputs.py`` (pydantic
``model_dump(mode="json")`` -> ``json.dumps`` -> ``Path.write_text``).
"""
