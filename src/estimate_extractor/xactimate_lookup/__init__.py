"""Phase 3.8 description-first Xactimate lookup workflow.

Adds a trusted-mapping-first, description-search-second resolution path
on top of Phase 3.6 (selector catalog) and Phase 3.7 (recommendation
layer): for each normalized line item, prefer a previously-approved
internal mapping (fast CAT/SEL path); otherwise generate a concise search
phrase, rank the resulting dropdown candidates, and require human
approval before anything is reused automatically. See
docs/xactimate-lookup.md.

Nothing in this package performs live desktop automation -- see
adapter.py's FakeXactimateAdapter and orchestrator.py's dry-run mode.
"""
