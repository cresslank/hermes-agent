# Evidence-owner mapping protocol

The optional `PluginContext.supervision` facade accepts both native immutable
`OwnerRequestV1` values and bounded evidence-owner mappings. No provider imports
are required in the host or retrieval owners.

## Admission

`negotiate("supervision.v1")` advertises `owner_capabilities` only when the calling
owner has a bound current runtime in the same profile and an active supervisor
has the observation/action grant, local data class and an explicit egress policy.
An installed but inactive facade does not replace an existing rank provider.
`owner_deadline=True` means the native host owns a bounded synchronous wait;
call on the authenticated execution thread, not a private worker. The provider
bridge schedules HTTP separately. Async owners must retain the same execution
boundary rather than using `to_thread` for the native decision.

Current owner bindings are `hermes-lcm` -> `lcm` (`history_excerpt` local class)
and `web-muxyard` -> `muxyard` (`public_source` local class). Those classes are
not remote-disclosure grants. Every emitted top-level fact, including
`target_id`, must have an explicit classification in the supervisor's configured
host `egress_policy`; private classes additionally require source grants. Missing
or foreign policy never inherits permission from source content or URL shape.

## Mapping

Exactly these keys are accepted:

- `protocol`: `supervision.v1`.
- `owner`: authenticated owner binding above.
- `event`: `retrieval_candidates` for `rank_candidates`,
  `missing_history_slot` for LCM `evaluate_relation`, or
  `oversized_structured_result` for Muxyard `select_windows`.
- `request_id`: nonempty invocation-local string, at most 128 characters.
- `deadline`: finite absolute monotonic timestamp. Host clamps this to the
  original shared round token; a subsequent call never renews that token.
- `facts`: canonical domain facts, at most eight candidates with exact `id`
  and `ref` (or `source_ref` for windows). Facts are bounded and frozen.
- `completeness`: bounded owner metadata. Exact local source enumeration may
  establish `complete`; archive/global coverage remains unknown.

Mappings cannot provide a revision, generation, data policy, grants, or an
alternate target. The host derives the revision from current execution and
binds the target to `owner:request_id`. Feature-specific facts such as
`constraints_match`, `qualifiers_complete`, currency and supersession are not
invented by the codec. Unknown owner facts remain unknown and features abstain.

Applied responses echo the literal `request_id` and contain an exact head
permutation (`candidate_ids`), a validated mandatory-preserving subset
(`window_ids`), or one `candidate_id` plus `relation="states_missing_decision"`.
No-op, malformed, stale, expired, unauthorized or unavailable decisions return
`None`; owners retain their original evidence. Rank conflict/isolation IDs are
validated against the same offered set, not treated as trust certification.

## Typed seam

`OwnerRequestV1` adds optional immutable `event`, `facts` and `evidence_refs`.
Empty defaults preserve the previous generic action event. Domain facts are
retained rather than replaced by a generic candidate list. Exact evidence refs
bind proposal evidence separately from candidate IDs. `OwnerDecisionV1.metadata`
retains bounded descriptor metadata; it does not grant authority.

## Verification and limits

`tests/agent/test_supervision_owner_protocol.py` covers native admission and
codecs without external checkouts. `test_supervision_evidence_owners.py` requires
explicit `JEV_SUPERVISOR_SOURCE`, `LCM_SUPERVISION_SOURCE`, and
`MUXYARD_SUPERVISION_SOURCE` isolated source checkouts. It uses real LCM SQLite
recall and Muxyard provider consumers, the native facade, actual Jev registry and
strict fake HTTP transport. No live provider, credential or database is required.
If the provider bridge lacks the history action codec, the F15 test records an
explicit unavailable xfail rather than fabricating an expansion.

Native-owner receipts describe admission of a view preference. The evidence
owner can still veto after a final source-byte comparison; such a receipt is not
proof of an expansion, upstream success, complete answer, or source truth.
