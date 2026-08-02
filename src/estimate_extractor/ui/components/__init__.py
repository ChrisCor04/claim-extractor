"""Streamlit rendering functions for the Phase 3 review UI. Each module
here only renders widgets and calls into the sibling *_service.py modules
for all actual logic -- nothing in components/ is unit tested directly
(Streamlit rendering isn't testable without a browser); the service layer
it calls is fully covered by tests/unit and tests/integration instead.
"""
