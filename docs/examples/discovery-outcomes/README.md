# Discovery outcome samples

Example results of `radar.discovery.orchestrator.discover`, in the JSON shape produced by
`radar.discovery.serialization.outcome_to_dict`. They are a **proposal** for the HTTP API (issue #13)
and the web interface to build against; the exact wire format is not final until those owners agree it.

These files are generated from the real code against a fake network and checked by
`backend/tests/unit/discovery/test_sample_outcomes.py`, so they cannot drift silently. After an
intentional change: `UPDATE_SAMPLES=1 python -m pytest -c backend/pyproject.toml backend/tests/unit/discovery/test_sample_outcomes.py`
and review the diff. Each file is `{ description, request, outcome }`.

| File | Status | Shows |
|---|---|---|
| `validated-common-location.json` | validated | One contract, checked against an operation |
| `validated-with-referenced-documents.json` | validated | A multi-file contract; each referenced file listed with size and SHA-256 |
| `validated-provider-version-selected.json` | validated | A version hint selected one of two provider versions; the other is a rejected candidate |
| `ambiguous-two-contracts.json` | ambiguous | Two distinct contracts in `alternatives`; nothing chosen |
| `ambiguous-provider-versions.json` | ambiguous | Dated provider versions with no version requested |
| `ambiguous-resolved-by-selection.json` | validated | The same result after the user picked one alternative |
| `rejected-at-each-stage.json` | rejected | Rejections at validation, matching and reference capture |
| `inaccessible.json` | inaccessible | A source answered with an error; a contract may exist behind it |
| `not-found.json` | not_found | Clean misses only; not proof that nothing is published |

## Reading an outcome

- `status`: `validated`, `ambiguous`, `rejected`, `inaccessible` or `not_found`. Treat these as the
  distinct result states of a screen; "no result yet" (loading) is a client state, not an outcome.
- `package`: present only when `validated`. Describes the contract; it never contains the contract text.
  `root_document` and `referenced_documents` give URL, `media_type`, `size_bytes`, `sha256` and
  `retrieved_at`. `content_fingerprint` identifies the contract's content independent of formatting or
  JSON/YAML form. The bytes themselves belong to the snapshot endpoint, not this payload.
- `alternatives`: present only when `ambiguous`; each has the same shape as `package`. Choosing one needs
  no re-fetch (`select_candidate` on the server side).
- `candidates`: every document that was evaluated, accepted or not. `rejections` is a list of
  `{stage, code, reason}` where `stage` is `validation`, `matching` or `reference_capture`. Show `reason`
  to people; use `code` for logic.
- `evidence`: one entry per check. `outcome` is `match`, `mismatch`, `indeterminate` or `not_requested`
  (null for the discovery hint that led to the document). `indeterminate` and `not_requested` are not
  matches and must not be presented as confirmation.
- `attempts`: every fetch and judgement, for a detail view: `retrieved`, `not_found`, `inaccessible`,
  `blocked`, `budget_exhausted`, `not_a_contract`. A page that was fetched and then judged not to be a
  contract appears twice (`retrieved`, then `not_a_contract`).
- `limitations`: plain-language caveats to show with the result: the search is bounded, the search was cut
  short, structural-only validation, and so on. `not_found` always says it is not proof of absence.

Timestamps are ISO 8601 UTC. Nothing here involves an LLM.
