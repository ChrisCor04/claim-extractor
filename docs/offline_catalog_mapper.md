# Offline Xactimate catalog mapper

The mapper in `estimate_extractor.xactimate_lookup.offline_catalog_mapper` is
read-only and is not connected to Xactimate or the production execution runner.
It accepts either a description string or `SourceLineContext` containing group,
section, activity, unit, quantity, trade hint, and pricing context.

Deterministic lexical/context retrieval has no optional dependencies. Local
semantic reranking is opt-in:

```powershell
pip install -e ".[semantic]"
```

`SentenceTransformerSemanticReranker` requires a sentence-transformer model
already present on local disk and loads it with `local_files_only=True`. It
stores catalog vectors in a caller-selected derived `.npz` cache; the cache
contains the SHA-256 fingerprint of the immutable catalog and is rebuilt when
that fingerprint changes. No cache is written beside or into the source CSV
unless the caller explicitly chooses such a path.

If the optional model/dependencies are unavailable, omit the semantic reranker;
the deterministic mapper remains fully functional. A future LLM reranker can
implement `CandidateChoiceReranker`, which returns only an index into the
supplied real-catalog candidate list or abstains. `restricted_candidate_choice`
rejects any out-of-range response, preventing free CAT/SEL generation.
