"""Phase 2: normalization and Xactimate-candidate mapping.

Consumes a Phase 1 ``canonical_estimate.json`` (never the source PDF) and
produces, per line item: a normalized description (action/trade/component/
material/attributes), and zero or more candidate Xactimate line-item
mappings with conservative confidence and an explicit status. This stage
never fabricates a CAT/SEL code and never silently guesses -- see
docs/mapping-engine.md.
"""
