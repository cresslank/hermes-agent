# Owned-child relevance join

The optional `supervision.child-relevance.v1` contract joins F01 to **existing
owned** ordinary `delegate_task` children. It neither adopts legacy workers nor
grants a read-only policy from model prose. No new core tool is exposed.

## Host prerequisites

The ordinary accepted-input path (`runtime_for_agent(create=True)`) now installs
`ConfiguredDelegationOwner` from **host profile policy** when a registered
supervisor and the agent's canonical `SessionDB` are present. Public launch
preflight (`owner_of`) also resolves installation if the DB became available
later. Neither path calls the test-oriented `install_owner` helper. Installation
uses the existing `SQLiteControlStore` against that exact profile's `state.db`,
with existing session membership and `mode=rw`; it creates no DB/schema and never
adopts stored or legacy workers.

The policy lives under `supervision.plugins.<plugin-id>` (not plugin-owned
settings). This is an example contract, **not an activation instruction**:

```yaml
supervision:
  enabled: true
  plugins:
    your-supervisor:
      grants: [observe, reprioritize_child, cancel_child]
      # data_policy / egress_policy remain separately required by registration.
      owned_delegation:
        version: supervision.owned-delegation.v1
        allow_optional_readonly: true
        consumer_contract: authenticated-parent-only.v1
        read_roots: [/absolute/owner-approved/readonly-fixtures]
```

Exactly one registered eligible owner is required. Missing/disabled/malformed
policy, missing or foreign SessionDB, absent registration, or competing eligible
owners install nothing. This policy only permits control; it contains **no task
obligation, completion, independence or consumer-coverage facts**. The immutable
`host.owned-parent-read.v1` capability policy allows parameter-validated scoped
`read_file` and bounded child-local `todo_list` bookkeeping. Opaque commands, program
execution and unqualified connectors remain fenced at every dispatch.

### OWNER-LOCAL authenticated consumer contract

`authenticated-parent-only.v1` is deliberately narrower than arbitrary worker
sharing. A host-authenticated CLI/TUI/gateway/API submission may contain one
`hermes-owned-delegation-v1` JSON block. Model tool arguments, written plan files,
todo labels, serialized origin dictionaries and plugin scores cannot issue it.
The following **entire submission** supplies an exact same-message requirement
span (Unicode character offsets, zero-based, end exclusive):

````text
- Compare the public methods; an appendix is discretionary.
```hermes-owned-delegation-v1
{"version":1,"jobs":[{"ref":"job:appendix","goal":"Inspect supplemental fixture appendix","requirements":[{"start":0,"end":59}],"consumer_scope":"parent_only","obligation":"optional","requires_result":false,"requires_effects":false,"requires_cleanup":false,"requires_handoff":false}]}
```
````

The whole submission is bounded to 1,200 UTF-8 bytes; there are at most four jobs
and four distinct exact enumerated requirement spans per job. Unknown/duplicate
keys, duplicate refs, wrong types, invalid spans or any malformed row reject the
whole contract. Every obligation and result/effect/cleanup/handoff flag must be
explicit. `parent_only` is an authenticated declaration that this job has no
external/shared result consumers, **not global task completeness**. Do not use
it for a required independent check or a shared worker. Required/unknown
obligations and any required flag veto optionality even if the model requests it.

An ordinary `delegate_task` entry can then propose:

```json
{"goal":"Inspect supplemental fixture appendix","supervision":{"obligation":"optional","consumer_refs":["job:appendix"],"consumer_set_closed":true,"effect_policy_id":"host.owned-parent-read.v1"}}
```

The owner resolves the exact ref and goal to native consumer records, enrolling
both that job and the canonical parent consumer before scheduler publication.
Links carry the exact accepted requirement IDs, not a singleton/topic inference.
A foreign ref, changed goal, missing source or legacy launch cannot gain closed
coverage. Nested launches retain the existing fence/handoff mechanism but receive
no parent-only semantic authority; later consumer enrollment resolves unknown
and invalidates coverage. This adds **no fields to `hermes-work-map-v1`**.

Every authenticated instruction invalidates previous launch-consumer authority,
even steering without a replacement block. A replacement contract authorizes
new launches only; it cannot re-attest an existing worker. Milestone revisions
can supply the two distinct observations within the same authorized instruction
scope. Turn closure, reset, foreign served profile, changed/disabled owner policy,
registration unload/replacement and lost session membership veto semantic effects.
Existing child bindings and immutable dispatch restrictions survive those events;
cleanup/explicit stop/normal delivery remain the lifetime owner's responsibility.
No automatic reinstallation into a new lineage or policy widening occurs.

The lower-level `install_owner` API remains for explicitly owned native callers
and tests. It is not the ordinary installation path.

## Ordinary producers

- `register_launch` attaches the root parent's runtime bridge **after** the native
  owned launch transaction and before ordinary scheduler submission. Launch alone
  does not create a relevance inference. Nested launches retain their inherited
  control owner and cannot replace the root bridge.
- Accepted host input/steering reaches `SupervisionRuntime.accept_instruction` and
  then the `task_revision` producer. Repeated identical instruction text is not a
  second meaningful observation. Serialized role labels and forged steering
  wrappers do not acquire accepted-input authority.
- A successful native `todo_list` executor / `todo_tool` write can produce `child_milestone` from the exact
  currently bound child's own `TodoStore`. At most four full rows / 1,000 characters
  are admitted; oversized declarations are not silently truncated. Publication
  waits until the existing dispatch fence settles. Identical committed plan bytes
  do not increment meaningful evidence, even if the store's own revision changes.
  Todo IDs and row order are excluded from the declared content/status multiset:
  bookkeeping-only replacements cannot supply a second cancellation observation.
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

The configured ordinary path does **not** claim running-worker reprioritization.
Its genuine child milestones occur after submission, so there is no qualified
queue consumption for a priority proposal. A validated first semantic observation
is persisted separately, but the proposed priority effect settles `no_op` and
rank remains zero. Only a later eligible cancellation settles `applied`. The
lower-level explicitly installed owner retains its pre-submit two-rank API for
native callers with a real queued opportunity; that compatibility path is not
ordinary milestone-based priority qualification.

Synchronous owned delegations enter an event-driven `ChildControlWait` in the
existing join, including single-child calls. Native proposal submission and child
future completion wake the existing condition; the join consumes only its exact
owned child targets and control action kinds. It cannot run parent advisories or
other owner actions. It neither polls for semantic opportunities nor extends a
provider deadline; the existing 0.5-second interrupt timeout is unchanged. No new
single-child completion presentation, patrol, parent turn or child instruction is
added. Wait-thread authority is removed when the join leaves; child lifetime and
dispatch seals remain independently owned.

Cancellation commits through `OwnedDelegationOwner.request_semantic_cancel`, then
signals outside the control lock. The existing dispatch seal wins races:
previously admitted work invalidates a stale proposal; later dispatch/enrollment
cannot attach to cancelled work. Explicit user stop remains independent of semantic
evidence. `cancel_requested`, worker settlement, processes stopped and effects
reconciled remain separate facts, and normal partial/final delivery is unchanged.

## Offline qualification

`tests/agent/test_owned_delegation_policy.py` enters through actual standalone
`register(ctx)` and profile config, canonical SessionDB, accepted CLI submission,
public `delegate_task`, the actual `todo_list` inline executor on running child
workers, and the synchronous join's native safe-point settlement. No test-side
parent drain or pre-start milestone supplies the positive result. It never calls `install_owner` or injects a consumer resolver. Two
scratch profiles alternate A→B→A through real plugin registration and strict
MockTransport, including native first-observation/cancel and baseline no-op paths.
Required result/effect/cleanup/handoff, malformed/foreign/unknown input, missing
DB/policy, legacy launches, steering/reset/unload, and lifetime-fence cases are
negative controls. ID-only replacement changes dispatch bookkeeping but not
meaningful evidence or the native first receipt. The child model itself remains synthetic; no production
activation or arbitrary shared-consumer coverage is claimed.

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
