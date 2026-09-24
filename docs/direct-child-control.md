# Direct child stop: one semantic authority

Jev decides whether the current owned child should stop. The plugin's finite deterministic rule converts that judgment to `cancel_owned_child`. Hermes validates the native ID/generation, current scope, registration grant, deadline, consumer ownership and lifecycle state, then seals native cancellation. **No Hermes/main-model call, reconsideration prompt, second relevance threshold, or two-observation confirmation occurs.** The arbiter provides replay/mechanical fences, not another semantic vote. The legacy F01 wire also stops on its first qualified decision; its existing optional/read-only grant is not silently widened.

## Source configuration (not installed or activated)

Configured ordinary-launch installation is implemented. An authorized deployment must set the following under its existing plugin entry, preserving its authenticated egress/provenance configuration and canonical profile/session database. This change makes no live configuration changes.

```yaml
supervision:
  enabled: true
  plugins:
    jev-supervisor:
      grants: [observe, cancel_child]
      data_policy: [task_text, project_excerpt, history_excerpt]
      owned_delegation:
        version: supervision.owned-delegation.v1
        allow_optional_readonly: false
        allow_owned_child_stop: true
        consumer_contract: authenticated-parent-only.v1
        read_roots: [/absolute/project/root]
        direct_control:
          version: supervision.direct-control.v2
          enabled: true
          interval_seconds: 2
          cancellation_rule: decisive-stop.v1
          choice_min: 0.8
          confidence_min: 0.8
```

`direct_control` is a closed versioned value, not a tunable threshold menu. Both host and plugin must support v2. `allow_owned_child_stop` explicitly grants stopping ordinary mutation-capable and required children. It does not grant execution, waive approval, classify effects as harmless, or authorize result acceptance. `allow_optional_readonly` remains separate for old explicitly restricted launches and may be false. Reprioritization is not required by the direct owner.

The native root launch establishes its parent return consumer and persists the immutable child generation before scheduling; it does not require model-written supervision metadata. Ordinary tools are not silently narrowed to read-only. Native direct children cannot create nested delegation through the tracked delegate tool; known pending handoffs/cleanup remain blockers. Untracked external activity remains unknown, not reconciled.

Scope is bound to profile/lineage/work and canonical session/database/plugin generation. Same-work steering, `finish_turn`, and a new turn do not revoke ownership of a live child; old replies remain fenced by their full request revision. Explicit runtime revoke or a new work/session/plugin generation removes semantic control authority. No persisted worker is adopted after restart.

## Judgment and dispatch boundary

The `supervision.child-relevance.v2` Choice is `current`, `superseded`, `duplicate`, `no_remaining_consumer`, or `insufficient`. Only the plugin evaluates the .8 choice/confidence rule. `superseded` additionally requires explicitly current input mode and distinct known input identities; `duplicate` requires a supplied native reference. Pinned inputs do not prohibit stopping an otherwise unnecessary instance. The configured ordinary launch uses unspecified input mode, so content drift alone cannot classify it as superseded.

A timely stop atomically persists `cancel_requested`, invalidates result applicability and leaves the obligation open. Every subsequent native child dispatch is refused. When no tool/cleanup/handoff is in progress the existing hard interrupt is delivered immediately. An already-dispatched tool is allowed to exit its ordinary cancellation boundary before signalling; the sealed fence survives inference expiry. No main-model approval is requested.

Native states distinguish `pending_stop`, `signalling`, `requested`, `signal_failed`, `stopped`, and `already_finished`. `stopped` records worker completion, not rollback of issued effects. `worker_finished`, `processes_stopped`, and `effects_reconciled` are separate explicit fields. Mutation-capable work never inherits read-only reconciliation guarantees. A blocked tool can remain pending; remote actions and arbitrary work inside an already-issued command are not magically preempted or undone. Existing required approval and secret boundaries are unchanged.

The scheduler neither creates a replacement nor retries schema repair on a sealed worker. The configured owner also refuses an exact-goal relaunch in the same work/instruction boundary after a semantic stop. A later authenticated user instruction can authorize a fresh attempt; this mechanical boundary does not infer semantic equivalence between differently worded goals. Required unfinished work remains unfinished.

## Observation, egress and lifecycle

The scoped background thread ticks every two seconds while children remain, independently of main-model activity. It sends bounded accepted task text, todo progress and sibling milestones/identities. It never sends hidden reasoning, system prompts, credentials or arbitrary tool arguments. Original/current local input identities are bounded content hashes, not transmitted file contents. Use project read roots, not the profile's changing state database.

A fresh observation ID binds each opportunity. Existing one-per-child/two-per-profile inference limits, original one-second background budget, exact codec validation, egress checks, CAS and finalizer generation authorization remain in force. Recurring inference does not mutate the shared revision merely to manufacture another vote.

The host currently has no provider terminal-scope notification join on last-child settlement, runtime revoke, or old-work retirement. Do not emit terminal closure at `finish_turn` while children remain live. The terminal-notification/512-scope retirement join is a separate runtime integration requirement; these source changes preserve the live stop scope but do not claim to repair that retirement seam.

## Offline verification

`tests/agent/test_child_stop_decisive.py` exercises configured ordinary launch through the real plugin registry, HTTP codec (in-process fixture only), recurring native consumer, SQLite owner, scheduler, child lifecycle and result presentation. It verifies actual native interrupt delivery, zero main-model reconsideration, sealed subsequent dispatch, no schema retry or exact-goal relaunch, required obligations staying open, mutation in-flight boundaries, stale generation refusal and live scope retention after turn completion. Child model execution is synthetic; no paid model or live socket is used.

The direct owner, legacy bridge, configured policy and native children suites retain mechanical authority, race, persistence failure and settlement checks under the one-decision contract.
