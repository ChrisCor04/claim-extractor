"""Phase 3.7 selector recommendation layer.

Consumes the Phase 3.6 canonical selector catalog (master_selectors.db) to
recommend candidate Xactimate CAT/SEL pairs for normalized Phase 2 line
items. This is a read-only, human-in-the-loop suggestion layer -- see
docs/selector-recommendation.md "Core rule": a database result never
becomes an approved mapping merely because it ranks first.
"""
