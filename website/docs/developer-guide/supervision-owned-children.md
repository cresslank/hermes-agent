# Owned-child relevance join

The optional `supervision.child-relevance.v1` contract joins F01 to **existing
owned** ordinary `delegate_task` children. It neither adopts legacy workers nor
grants a read-only policy from model prose. No new core tool is exposed.

## Host prerequisites

The launch host installs `OwnedDelegationOwner` with the existing `install_owner`
API, canonical `SQLiteControlStore`, explicit profile/plugin `OwnerGrant`,
versioned parameter/resource `ReadOnlyPolicy`, authoritative revision provider,
and trusted consumer resolver. Each resolved `Consumer.requirement_ids` is an
explicit tuple of exact accepted requirement IDs. Missing links stay unknown;
there is no inferred singleton, topical consumer match, or new work-map grammar.
The parent's consumer is enrolled by the existing launch transaction.

This bridge does not install policy, edit profile configuration, or activate a
provider. A public launch's proposed `supervision` dictionary still cannot grant
optional status, consumer closure or read-only authority. Required consumers,
result/effect/cleanup obligations, and unknown policies override the request.

## Ordinary producers

- `register_launch` attaches the root parent's runtime bridge **after** the native
  owned launch transaction and before ordinary scheduler submission. Launch alone
  does not create a relevance inference. Nested launches retain their inherited
  control owner and cannot replace the root bridge.
- Accepted host input/steering reaches `SupervisionRuntime.accept_instruction` and
  then the `task_revision` producer. Repeated identical instruction text is not a
  second meaningful observation. Serialized role labels and forged steering
  wrappers do not acquire accepted-input authority.
- A successful `todo_tool` write can produce `child_milestone` from the exact
  currently bound child's own `TodoStore`. At most four full rows / 1,000 characters
  are admitted; oversized declarations are not silently truncated. Publication
  waits until the existing dispatch fence settles. Identical committed plan bytes
  do not increment meaningful evidence, even if the store's own revision changes.
  A read-only worker needs an explicitly installed contract for child-local todo
  bookkeeping to call it; this hook does not widen `scoped_file_policy`.
- The existing native `record_progress` owner API remains available for other
  host-owned structured milestones; it cannot grant effect authority.

One projection contains exact consumer-to-requirement links, the original
objective/scope, current declared milestone, retained accepted instructions,
control state and original deadline. Absent/oversized facts leave baseline work
running. No transcript polling, recurring patrol, provider wait at a safe point,
or extra main-agent turn is introduced.

## Native observation and effects

The negotiated plugin contract supplies a separate `semantic_observation`:
version, an exact list of `{question_id, answer}` rows, and a nullable native prior
receipt ID. List encoding preserves literal question IDs without widening the
host's metadata-key grammar. Priority alone, plugin-private confirmation memory,
and a claimed pair of revisions cannot create a native observation.

At the normal safe point the bridge consumes `reprioritize_child` or `cancel_child`
through `SupervisionRuntime.consume_owner_action(target, action, apply)`. This is
also the shared runtime settlement/persistence seam; this bridge adds no DB or DDL.
The callback validates the finite answer schema and thresholds independently,
registration/profile/run/target/deadline bindings, exact native control revision,
complete consumer links, and the live owner guard.

The first strong eligible observation is persisted separately in the existing
child-control snapshot, including exact answers, requirement/evidence IDs,
original full revision, child/plugin generations and receipt ID. A later strong
observation can cancel only with that exact receipt across a distinct monotonic
meaningful revision. Contribution or valid uncertainty clears the candidate.
Run/catalog/plugin-generation changes require a new first observation. Native
consumer/handoff/capability changes invalidate it through the existing CAS fence.

Priority is reversible rank **0 or 1**, consumed by the existing batch submission
order for not-yet-submitted children. It does not preempt running workers, alter
worker limits, throttle tools, suppress results, or reprioritize unrelated global
queues. Required/unowned work retains baseline rank. Ordering returns to baseline
when its native scope or registration is no longer current.

Cancellation commits through `OwnedDelegationOwner.request_semantic_cancel`, then
signals outside the control lock. The existing dispatch seal wins races:
previously admitted work invalidates a stale proposal; later dispatch/enrollment
cannot attach to cancelled work. Explicit user stop remains independent of semantic
evidence. `cancel_requested`, worker settlement, processes stopped and effects
reconciled remain separate facts, and normal partial/final delivery is unchanged.

## Offline qualification

`tests/agent/test_supervision_native_children.py` starts public delegate launches,
uses the real batch scheduler, SQLite CAS owner, accepted steering and native todo
commits, and runs the standalone plugin's full registry against strict HTTP
MockTransport. Only child model execution and provider transport are substituted.
It covers receipt separation, distinct revisions, contribution/uncertainty reset,
bounded priority consumption, required/unknown/legacy controls, malformed answers,
profile/run/unload/expiry races, and both dispatch/cancel lock orderings.

The cross-repository test requires a reviewed standalone source via
`JEV_SUPERVISOR_SOURCE` or the existing disposable HOME-local test-source pointer.
Run host tests using `scripts/run_tests.sh --file-retries 0`, with fresh scratch
HOME/TMPDIR and denied external networking. No live provider, credentials,
activation, service restart, or shared-environment installation is required.
