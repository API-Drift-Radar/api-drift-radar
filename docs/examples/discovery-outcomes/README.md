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
| `validated-direct-url.json` | validated | The target was a specification URL; fetched directly, nothing else searched |
| `validated-with-referenced-documents.json` | validated | A multi-file contract; each referenced file listed with size and SHA-256 |
| `validated-provider-version-selected.json` | validated | A version hint selected one of two provider versions; the other is a rejected candidate |
| `ambiguous-two-contracts.json` | ambiguous | Two distinct contracts in `alternatives`; nothing chosen |
| `ambiguous-swagger-config-versions.json` | ambiguous | A Swagger initializer led to a configuration listing two versions |
| `ambiguous-provider-versions.json` | ambiguous | Dated provider versions with no version requested |
| `ambiguous-resolved-by-selection.json` | validated | The same result after the user picked one alternative |
| `rejected-at-each-stage.json` | rejected | Rejections at validation, matching and reference capture |
| `validated-via-api-catalog.json` | validated | Found through an RFC 9727 catalogue; the `trail` shows each step, an unrelated entry skipped |
| `not-found-apidoc-documentation-only.json` | not_found | apiDoc documentation data found: `artifacts` says documentation-only, no contract accepted |
| `rejected-unsupported-format.json` | rejected | A Google Discovery description: recognised, unsupported, named in `artifacts` |
| `inaccessible-authentication-required.json` | inaccessible | A location answered 401: an `authentication_required` artifact |
| `not-found-search-cut-short.json` | not_found | The search hit its limits: `coverage` names them and what was left unexamined |
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

### Optional details (backward compatible additions)

- `artifacts`: what discovery found or hit besides a contract. `category` is one of `unsupported_description` (a formal
  description Radar recognises but does not support: Swagger 2.0, Google Discovery, Smithy, AsyncAPI), `documentation_only`
  (documentation data or a collection, for example apiDoc), `authentication_required` (a source answered 401/403 so its
  contents are unknown) or `unsupported_dynamic_configuration` (a documentation viewer whose configuration is computed at
  runtime and cannot be read statically). `kind` names the format or barrier. None of these means no contract exists, and
  none is a validated package.
- `trail`: the navigation steps. Each has the `url`, the `parent_url` it was found on, the `mechanism` that produced it
  (for example `origin_root`, `documentation_navigation`, `service_desc_link`, `api_catalog`, `swagger_config_url`,
  `framework_probe`, `llm_suggestion`), its `kind`, `depth` and `outcome` (`fetched`, `failed`, `not_examined`,
  `skipped_depth`, `skipped_limit`, `skipped_unrelated`). A link only creates a lead: it never establishes that a provider
  published a contract or that it applies to the target.
- `coverage`: how complete the bounded search was. `complete` is false when a limit cut it short; `limits_reached` names
  them (`navigation_limit`, `request_limit`, `host_limit`, `deadline_exceeded`, `depth_limit`, `page_limit`, `lead_limit`,
  ...); `unexamined` lists leads that were found but never looked at. Show this beside the result: an accepted contract and
  an incomplete search can both be true, and an incomplete search is not evidence of absence.

Timestamps are ISO 8601 UTC. Nothing here involves an LLM unless the optional fallback was enabled.
