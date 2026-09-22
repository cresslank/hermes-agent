# Native efficiency supervision owners

`agent/supervision_efficiency.py` is provider-neutral. It imports no supervisor
plugin and performs no inference. Without an existing supervision runtime, its
native observation hooks are no-ops. None of these paths cancel children, change
required verification, mark requirements complete, or execute a recovery route.

## Producer and consumer boundaries

| Opportunity | Native producer | Consumer |
| --- | --- | --- |
| Overlapping file read | Authorized `tool_executor` pre-dispatch, followed by the real read-result observation | One bounded advisory; the main read still dispatches. Identical requested windows stay on the file owner's existing deterministic dedup path. |
| Delegation recommendation | `_build_children`, **after** `register_launch` and **before** scheduling | Bounded pre-launch recommendation for an existing authorized route; no new launch or cancellation. |
| Repeated failed approach | `AIAgent._append_guardrail_observation` over actual results | One distinct recurrence incident after at least three matching attempts in a 12-event window; existing hard guardrails are unchanged. |
| Verification reuse | `record_verify_run(..., supervision_check=...)`, then `propose_verification_reuse(check, current=...)` | Dedicated `reuse_receipt` admission returns the **original** successful native receipt only for an optional plugin-owned check. Main checks receive at most a hint. |
| Diminishing returns | Evidence owner calls `commit_research_pass`, then `propose_expansion` | One stop-optional-expansion advisory. No requirement or completion state changes. |
| Frozen child heartbeat | `_Heartbeat.tick`'s native stale-activity threshold branch | One owner-status diagnostic recommendation. A current owned handle supplies a real local status route when no route is already registered. Native timeout/settlement behavior remains authoritative. |
| Recovery selection | Actual tool failure with an explicit native attempt policy and currently authorized routes | One ID-bound advisory. Recheck the exact offered route, its version and live permission before consuming it. |

Ordinary safe-point draining delegates `owner="efficiency"` to this owner instead
of the generic advisory renderer. Receipt decisions use `owner="efficiency.check"`
and are not consumed by generic draining. All remote work uses the existing
shared absolute deadline. There is no fresh allowance for a second decision in
the same round, no polling loop and no extra main-model request.

## Missing metadata means baseline, not fabricated facts

The owner APIs deliberately require explicit contracts:

- `admit_read_intent(path, independent_check=...)`: unknown or independent checks
  never acquire a dedup judgment. File identity comes from native filesystem
  metadata; only a real content-bearing read result becomes a reuse candidate.
- `admit_attempt_policy(...)`: polling, registered retries, pagination, requested
  repetition and an existing deterministic error handler are vetoes. A missing
  policy is not equivalent to six false flags. Opaque shell/code commands do not
  acquire a literal resource identity from their prose.
- `admit_delegation(DelegationIntent(...))`: acceptance, available inputs,
  separability, parent step, budget and overhead categories belong to the native
  planner. A goal string alone cannot supply them.
- `register_route(Route(...))`: the native route owner supplies an existing
  implementation's identity and a cheap live predicate certifying permission,
  privacy, prerequisites and retry-after restrictions. The semantic provider
  supplies neither the predicate nor a command. Only read-only routes are
  accepted by this initial owner.
- `VerificationCheck`: every mandatory-check flag and every fingerprint is
  explicit. A native successful `kind="verify"` receipt must belong to the active
  session. A shell exit status or an arbitrary test-like command cannot mint a
  reusable verification receipt. `current()` must revalidate all owner fields at
  consumption, including obligations that became mandatory while inference ran.
- `ResearchPass`: complete pass-local accepted/rejected lists come from the
  evidence owner, not tool-call counts. Nonempty IDs must exist in retained native
  evidence. Optional research gaps are explicit and stay open after an advisory.

The current ordinary-path inventory is deliberately incomplete:

| Feature | Present ordinary hook | Missing production producer/contract |
| --- | --- | --- |
| F04 | Authorized local read and content-bearing result | No ordinary caller of `admit_read_intent`; without explicit independence metadata no semantic duplicate question is issued. No optional-operation suppression owner. |
| F05 | Constructed, owned child before scheduling | No ordinary caller of `admit_delegation`; planner acceptance, independence, overhead and parallelism remain unknown. |
| F06 / F11 | Actual post-call failure | No ordinary caller of `admit_attempt_policy` or failure-route registration; unclassified failures keep native handling. |
| F07 | Optional explicit binding in `record_verify_run`, plus `propose_verification_reuse` | No ordinary verification runner supplies `supervision_check` or calls reuse. No automatic fingerprint/claim derivation; standalone plugin codec join is separate. |
| F08 | Explicit evidence-owner API | No ordinary caller commits complete `ResearchPass` ledgers or proposes an optional expansion. Raw retrieval yields are not classified into accepted evidence. |
| F10 | Native frozen-child heartbeat branch | Requires an existing supervision runtime and owned-child binding; a real local status route is supplied when available. It cannot diagnose arbitrary unowned operations. |

The contract-supplying calls for F04/F05/F06/F07/F08/F11 currently occur in
qualification tests, not in ordinary planner/retrieval integrations. The hooks do
**not** infer absent independence, acceptance, environment identity or complete
yield ledgers. These are bounded owner implementations, not full ordinary-path
feature completion, and no savings or production accuracy has been established.

## Exact plugin codec join for receipt reuse

A plugin must not alias actual receipt reuse to `advise`.

- Native action/grant: `Action.REUSE_RECEIPT`, wire value **`reuse_receipt`**.
- Native owner: **`efficiency.check`**.
- Semantic action: **`reuse_exact_receipt`**; feature **`F07`**.
- Target: the exact `VerificationCheck.id` from `verification_proposed`.
- Evidence: `verification:<native row id>` plus the exact declared claim IDs.
- Lossless metadata: `feature_action="reuse_exact_receipt"`, `receipt_id`,
  `source_ref`, `snapshot`, `claim_ids`, `verification_waived=false`.
- Preserve the exact event revision, registration generation, owner binding and
  absolute deadline. No template text grants reuse authority.
- Add `reuse_receipt` to requested grants **only when the native host advertises
  this optional owner capability**; an older host must keep its baseline rather
  than fail its entire registration on an unknown grant. `SupervisionFacade`
  advertises `receipt_reuse="supervision.receipt-reuse.v1"` from the implemented
  native owner when negotiating the supported supervision version. This is
  protocol support, **not a grant**: registration must still request
  `reuse_receipt`, and that exact grant must be permitted by this facade's profile
  policy. `advise`, `continue` and `retain_result` cannot authorize reuse.

The owner revalidates current fingerprints, session-bound receipt identity,
claim coverage, metadata and protection flags, then returns the original receipt
without writing a new passing verification row. Timeout or stale data returns no
receipt, so the caller runs its ordinary check. Native ledger admission holds a
nonwaiting SQLite writer reservation as well as the process lock, preventing a
second WAL connection from invalidating the receipt during admission. It creates
no database or verification row and returns baseline when the ledger is busy,
missing, edited, superseded or not a successful zero-exit verification. Owner
fingerprint lookup failure also returns baseline. The host capability is supplied;
the standalone plugin mapping/conditional grant request remains a separate join.

`reuse_candidate` and `retain_plan` remain unmapped: this owner does not pretend
to implement optional-operation suppression or a plan-disposition receipt. The
file-read producer emits the main-agent `steer_once` path and never an F01
cancellation request.

## Verification scope

`tests/agent/test_supervision_efficiency.py` loads the installed real plugin entry
point, its complete registry, strict request/response parser and native facade.
HTTP is replaced only by `httpx.MockTransport`; socket networking is denied.
The tests exercise real file reads, normal child construction and SQLite launch
controls, actual heartbeat observations, real verification-ledger receipts and
native guardrail/result paths. No constructed supervision snapshot or substituted
feature registry is used for those plugin tests.

The separate receipt-admission tests use a local provider to prove the new native
`reuse_receipt` effect contract, including changed fingerprints, newly mandatory
checks, forged receipts and refusal to treat `advise` as reuse. They are explicitly
**not** a claim of plugin codec qualification. The real-plugin receipt round-trip
is a strict expected failure while that codec remains absent, and automatically
becomes a required positive test once the mapping exists. Synthetic judgments are
not measurements of remote accuracy, latency savings or production activation.
