# tests/fixtures/

This directory is intentionally empty in version control.

Integration tests use the real carrier PDFs in the repo-root `fixtures/`
directory (see `tests/conftest.py::fixtures_dir`), not this one. Those PDFs
contain real customer PII and must never be committed -- they are excluded
via `.gitignore`.

This directory is reserved for small, synthetic (non-PII) fixture files a
future unit test might need (e.g. a hand-built single-page PDF for a
targeted regression test). None exist yet.
