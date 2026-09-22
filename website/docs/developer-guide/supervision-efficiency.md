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
| Repeated failed approach | `AIAgent._append_guardrail_observation` over actual results | One shared failure/recurrence incident after at least three matching attempts in a 12-event window; existing hard guardrails are unchanged. |
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
| F04 | Public `SupervisionFacade.read_optional_context` executes profile-authorized supplemental reads and retains exact original bytes | Optional duplicate suppression is implemented for this narrow owner. Main `read_file` calls still have no ordinary independence supplier and are never suppressed. |
| F05 | Constructed, owned child before scheduling | No ordinary caller of `admit_delegation`; planner acceptance, independence, overhead and parallelism remain unknown. |
| F06 / F11 | Actual post-call failure | No ordinary caller of `admit_attempt_policy` or failure-route registration; unclassified failures keep native handling. |
| F07 | `run_verify` supplies closed `nativeChecks`; an installed supervisor requests them from the ordinary `pre_verify` hook and the host drains on the execution owner | JSON-object/key checks only. Arbitrary shell recipes have no complete scope contract and remain nonreusable. In-process main runners consume advisories but always execute; detached CLI processes have no parent runtime binding. |
| F08 | Explicit evidence-owner API | No ordinary caller commits complete `ResearchPass` ledgers or proposes an optional expansion. Raw retrieval yields are not classified into accepted evidence. |
| F10 | Native frozen-child heartbeat branch | Requires an existing supervision runtime and owned-child binding; a real local status route is supplied when available. It cannot diagnose arbitrary unowned operations. |

Except for the public optional-read operation and the native verification runner below,
the contract-supplying calls for F04/F05/F06/F08/F11 still occur in qualification tests, not in
ordinary planner/retrieval integrations. The hooks do
**not** infer absent independence, acceptance, environment identity or complete
yield ledgers. These are bounded owner implementations, not full ordinary-path
feature completion, and no production savings or accuracy has been established.

## Host-attested optional context reads (F04)

`SupervisionFacade.read_optional_context(request)` is a public native **operation**,
not an `admit_*` fact setter. A plugin calls it on the execution-owner thread, with
an active agent binding. It reads one bounded local UTF-8 file window. It adds no
model-visible tool, mandatory model request, automatic patrol or main-read veto.

The host profile entry, **not plugin settings**, must explicitly contain:

```yaml
supervision:
  enabled: true
  plugins:
    example-owner:
      optional_reads:
        version: supervision.optional-read.v1
        purpose: supplemental_context
        paths: [/absolute/project/source.txt]
        max_bytes: 4096
```

The purpose enum attests to a *supplemental-context-only* operation, never an
independent audit, acceptance test, mandatory read or write readback. No missing
boolean is interpreted as permission. Unknown policy keys/purposes, missing budget,
symlinks, secret paths and absent current operation-to-requirement edges reject
without reading. The exact path must resolve through a current committed
`hermes-work-map-v1` step; a singleton requirement ledger does not establish a link.
The normative work-map grammar is unchanged.

The request has exactly `version`, `path`, `objective`, `acceptance`, `offset`,
`limit`. Strings are bounded to 1,200 characters, offset/limit to 1..2,000, and the
entire source file to the smaller of the explicit byte budget and 64 KiB. The
objective and acceptance are proposed descriptions, not grants. Main `read_file`
execution is a separate owner and cannot enter the suppressible branch.

Identical windows replay locally without Jev. Differing optional windows offer
retained source receipts to F04. Only the literal negotiation
`optional_read="supervision.optional-read.v1"` enables its `reuse_candidate`
action codec; the plugin conditionally requests that exact grant. Grant omission,
expiry or an unavailable judgment executes the normal optional read. A successful
reuse returns the **original** source ref, window, bytes, snapshot and hash, not a
new read or passing verification receipt. Required/new-state work is not waived.

The owner checks current profile/registration generation, work revision, live
profile policy, exact requirement link and filesystem snapshot at consumption.
The actual read uses a bounded regular-file descriptor with `O_NOFOLLOW` and
pre/post metadata pins; this is ordinary local-filesystem currentness, not protection
against a privileged actor forging inode metadata. At most 16 receipts (1 MiB)
are held; finish/revoke/unload clears bytes. Effect settlement uses the existing
runtime owner seam, not a competing database. Durable common settlement remains
owned by the common host delivery integration.

`test_supervision_optional_reads.py` drives authentic CLI input, native todo,
committed plan, public read API, the full installed plugin registry and strict
MockTransport. It counts real source opens: the positive returns the original
bytes with one open rather than two. This is a synthetic functional avoided-read
measurement, **not** a latency, token-cost or production-quality claim.

### Remaining concrete supplier constraints

- F05: the exact work-map step grammar contains only `todo_id`,
  `requirement_indexes`, `target_refs`. It cannot express acceptance, parent input
  dependency, specialist capability or overhead/parallelism. Goals/tool lists do
  not supply those facts. `retain_plan` still has no ordinary producer/disposition
  owner; the current policy returns no proposal when no candidate passes.
- F06/F11: main post-call results do not establish user-requested repetition or
  registered retry/poll policy. Route metadata must come from its real owner,
  not a guessed false flag. Failure and loop opportunities now share a host-owned
  intervention ledger while retaining distinct opportunity targets.
- F07: arbitrary bootstrap/build/test recipes still lack a complete input, dependency
  and environment scope. Command spelling or workspace status cannot fill those
  fields. The closed native-check contract below supplies a narrow ordinary path,
  not a certification of arbitrary test commands or terminal backends.
- F08: raw retrieval results and ranking are not accepted/rejected evidence
  decisions. No ordinary research owner currently supplies that pass ledger and
  an explicit optional expansion. No stop-execution effect is claimed.

## Ordinary native verification checks (F07)

`hermes verify` recipes may add an owner-local `nativeChecks` list. Version
`hermes.verify-check.v1` deliberately supports only a **bounded UTF-8 JSON object
with specified literal top-level keys** (Python's JSON decoder semantics).
It has no shell, project imports, schema refs, custom hooks, environment reads,
network, clock, or external mutable service input. Up to eight checks, three keys
per check and 64 KiB per regular input file are supported. Secret paths and
symlink redirection are refused. Unknown check shape/version fails the native
check instead of silently making the recipe green.

Example inside `.hermes/environment.json`'s `recipe` object:

```json
{
  "name": "Metadata checks",
  "nativeChecks": [{
    "version": "hermes.verify-check.v1",
    "kind": "json_object_keys",
    "path": "metadata.json",
    "keys": ["name", "version"],
    "description": "Confirm the metadata object has name and version keys",
    "required": false,
    "external_write_readback": false,
    "acceptance_test": false,
    "independent_review": false,
    "time_sensitive": false,
    "explicit_user_check": false
  }]
}
```

Those booleans are proposed scope, **not permission**. Main `hermes verify`
execution always runs every selected native check and every original shell
command, regardless of repeated descriptions. Missing/non-boolean protection
fields leave a runnable native check nonreusable. True protection flags always
retain execution. Shell commands never acquire fingerprints from their names.

Automatic **additional supplemental** checks require separate exact profile
approval, under the registered supervisor's host policy entry:

```yaml
supervision:
  enabled: true
  plugins:
    jev-supervisor:
      optional_verification:
        version: hermes.verify-check.v1
        purpose: supplemental_confirmation
        root: /absolute/project
        recipe_sha256: <SHA-256 of the exact environment.json bytes>
```

Receipt production/reuse also requires the existing verification ledger to be
enabled (`agent.verify_on_stop: true`); otherwise checks still execute and no
receipt is claimed.

This policy does not authorize arbitrary commands. The hook runner selects only
`nativeChecks`; it cannot execute bootstrap/build/test/start. Each input path
must also resolve through a current, actually committed work-map edge. The
work-map-v1 grammar is unchanged. A changed manifest needs a new explicit policy
pin; a recipe cannot self-authorize optionality or waive main/user work.

The real ordinary call chain is:

1. `turn_stop_gates._pre_verify_nudge` invokes the normal `pre_verify` hooks.
2. The installed plugin, only after negotiating `native_verification`, queues a
   bounded request with `SupervisionFacade.request_verification_checks`.
3. The host drains after hook completion on the execution-owner thread. Plugin
   callback workers may not execute checks or consume receipts.
4. `run_verify(..., native_only=True)` calls the same native checker used by the
   CLI. It reads the actual bytes, derives assertion IDs, fingerprints scope,
   input, parser/interpreter dependencies and environment, executes the check,
   and binds the successful **original** verification-ledger row.
5. Exact optional repeat: deterministic receipt admission, no semantic question.
   Nonidentical descriptions on identical qualified scope: real F07 codec,
   `reuse_receipt` grant, common `consume_owner_action` settlement, original row.
   Unknown/stale/error/expired decisions run the baseline check if permission
   still exists; revoked permission stops only the optional supplemental work.

Fingerprints bind the exact input bytes and path; the actual key assertion set;
full runtime revision, profile/lineage/work and active registration generations;
installed evaluator/JSON parser/interpreter bytes; Python implementation,
version, byte order, integer-string limit and recursion limit. The closed
operation has no ambient environment-variable dependencies. Unavailable
fingerprints remain nonreusable. This assumes trusted in-process Python code
and ordinary local filesystem currentness, not hostile interpreter-memory or
privileged filesystem tampering. No receipt is reused across process restart.
Hashing dependencies may cost more than these small checks; no latency/cost
improvement is claimed.

Supplemental rows stay in the **existing** verification evidence DB using
`scope="supplemental"`; they never advance workspace-green state or clear its
mutation marker. Reuse is fenced under the existing nonwaiting SQLite writer
reservation and requires exact original row identity, no later verification,
no later recorded edit, and fresh owner pins. Existing workspace receipt pointers
are retained during pruning. There is no new DB, DDL, or competing supervision
settlement implementation.

Main in-process `run_verify` calls may consume an advisory into their normal
result (`checks[].advisory`, also rendered by the CLI), but still execute the
check. A **separately spawned** `hermes verify` has no active parent-agent runtime:
it executes normally, records evidence, and does not issue a semantic hint.
The parent interpreter/environment must not be presented as the child's complete
fingerprint. Covering that separate-process pre-action case needs an authenticated,
versioned runner handoff carrying actual child environment/dependency pins and
live owner scope, or an explicitly authorized in-process native runner route.
A shell command name alone is not that contract.

`test_native_verification_supplier.py` enters through authentic input, real todo
and committed file operations, the normal stop-gate hook, installed plugin entry
point/full registry/strict MockTransport, and the real CLI handler. Tests count
actual evaluator calls and compare complete original receipts, including protected
work, missing grants/links/policy, failed prior results, changed/unknown pins,
revocation, expiry, transport error, profile switching and mandatory shell work.

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
fingerprint lookup failure also returns baseline. The compatible standalone plugin
negotiates this capability and requests the exact optional grant. Hosts without
the capability preserve baseline registration and execution.

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
**not** a claim of plugin codec qualification by themselves. The installed-plugin
receipt round-trip is a required positive test, asserting negotiated support and
return of the original native receipt without waiving mandatory checks. The
standalone installed-entry suite also covers missing grants and older hosts.
The closed native runner supplies ordinary fingerprints/claims as described above;
other verification classes remain unsupported. Synthetic judgments do not measure
remote accuracy, savings or activation.
