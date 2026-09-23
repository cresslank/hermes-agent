# Direct child control: native lifecycle contract

The v2 native controller lives in `supervision_children_direct.py`; the finite policy and bounded input fingerprint in `owned_delegation_direct.py`; sealed stop signalling in `owned_delegation_stop.py`. Legacy v1 semantics are unchanged unless the owner supplies an authenticated `direct_control` v1 launch record.

## Installation prerequisite — not yet wired

**Configured ordinary launch installation is not implemented by this commit.** An implementation edit batch for `owned_delegation_policy.py` and `tools/delegate_tool.py` was blocked before execution and must be reauthorized separately. Consequently existing production configuration cannot yet install this controller or preserve v2 stop scopes across steering. The new native lower lifecycle contract, provider codec and recurring controller are source-owned building blocks, not deployment-complete coverage.

The configured owner must:

1. Opt in to exactly the policy below, retaining existing read roots, profile grant, registration/session/database generation and explicit-revocation fences.
2. Set `direct_enabled` and install v2 records only for existing natively restricted read-only children, or an explicit `tasks[].read_only=true` capability request that the owner actually fences. Never infer capability from goal prose or silently attenuate ordinary editing workers. Native parent-only return topology, not model-provided supervision metadata, establishes the closed consumer set. Unknown consumers stay unknown.
3. Persist `direct_control`, `replaceable`, `input_mode`, `original_input_ref`, `current_input_ref`, `obligation_ref`, `obligation_state`, `partial_refs`, and `duplicate_refs` at launch. Required work uses `obligation_state=open`; stopping does not satisfy it or relaunch anything. Input mode is explicitly `current`, `pinned`, or `unspecified`.
4. Preserve scoped stop authority keyed by exact owned handle plus profile/lineage/work and canonical session/database/plugin generation across continuation steering. Fresh execution and stale-result acceptance remain separate permissions. Do not adopt stored workers.
5. Allow lifecycle observations after `finish_turn`, but not after explicit `revoke`. The runtime/dispatch hooks in this commit support that distinction; the configured owner's policy-currentness check still needs its matching installation change.

```json
{
  "version": "supervision.direct-control.v1",
  "enabled": true,
  "interval_seconds": 2,
  "cancellation_rule": "restart-biased.v1",
  "choice_min": 0.8,
  "confidence_min": 0.8
}
```

This object belongs under `supervision.plugins.<plugin>.owned_delegation.direct_control`. It is a closed versioned policy, not tunable ad-hoc thresholds. Existing optional read-only policy fields remain necessary. Do not deploy before installation and native production-path tests are completed.

## Projection/egress

V2 top-level fact fields: `target_id`, `changed`, `evidence_refs`, `child_relevance_contract`, `direct_control`, `observation_id`, `current_instructions`, `main_progress`, `sibling_progress`, `job`.

Native local classes required: `task_text`, `project_excerpt`, `history_excerpt`. Remote field classifications and provenance sources must be explicitly approved by the deployment's existing egress policy. The controller sends accepted task text, bounded native todo progress and owned sibling milestones/identities, never hidden reasoning, prompts, environment, credentials or raw tool arguments. Oversized task/qualifier sets abstain rather than truncate. Input identities are content hashes; raw input files do not reach the provider.

The scoped background thread ticks every two seconds while live children remain, independently of the main model and idle parent. It revalidates actual filesystem content at unchanged HEAD (tracked/untracked/deleted/renamed inputs), bounded to 4096 files/64MiB and .5s per census. Unknown/oversized/symlink/racy inputs remain unknown. A change is evidence for semantic relevance, not automatic supersession. A periodic observation does not bump the shared evidence revision and therefore does not spuriously cancel a sibling's inference. The provider additionally bounds one in-flight request per child and two HTTP calls per profile, bypassing caching for recurring v2 opportunities.

## Lifecycle effects

A timely qualifying v2 judgment atomically seals `cancel_requested`, invalidates result applicability, leaves the obligation open and persists the decision attribution. Existing dispatch fences immediately prevent further dispatch. A running read-only tool exits before signal; inference expiry never erases an already sealed command. Cleanup, handoff, unknown capability and unknown consumers remain fences.

`supervisor_control_for_child(child)` exports native, bounded attribution only for a live bound handle. States are `pending_stop`, `signalling`, `requested`, `signal_failed`, `stopped`, and `already_finished`. `requested` is not process death. Signal failures retry at most three times, spaced two seconds, against the same owned generation. Completion wins conservatively if it beats signal confirmation. SQLite CAS and existing finalizer generation authorization own durable transitions.

## Verification scope

`tests/agent/test_owned_delegation_direct.py` covers the real lower owner, SQLite persistence, dispatch sealing, pending stop, required open obligation, signal failure/completion arbitration, pinned/cleanup/capability fences, and actual Git same-HEAD dirty input identities. It deliberately labels its fixture-installed direct record: **this is not ordinary configured launch end-to-end coverage**. Existing configured v1 suites remain passing. F21's accepted-instruction invalidation remains intact, but a new F21 selected-dependency-to-child binding is not supplied here.
