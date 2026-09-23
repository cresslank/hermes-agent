# Direct supervisor delivery

## Control outcomes

`agent.owned_delegation.supervisor_control_for_child(child)` is the sole native
control attribution source. Result construction and `delegate_task(action="list")`
project its bounded mapping as additive `supervisor_control`. They do not infer
cancellation from child prose, `interrupted`, or an interrupt request, and do not
change `status` or `exit_reason`.

The mapping carries actor, feature/action/reason, decision, state, original/current
input references, partial references and remaining obligation/replacement when
known. Asynchronous source persistence preserves it in the immutable full result
and ordinary event. Native notification text renders the fields, including the
open obligation; the compact title includes actor and state. `requested`,
`signal_failed`, `already_finished` and confirmed `stopped` stay distinct.

The result mapping is refreshed after the existing child cleanup/owner finalizer,
before the caller persists or delivers it. Deferred timeout cleanup cannot claim
that the worker stopped. The immediate `subagent.complete` progress event can
therefore show an earlier requested state than the later final result. Actual
completion-versus-stop arbitration remains the lifecycle owner's responsibility;
this presentation module has no stop or result-acceptance authority.

F02 parking/bounded views cannot suppress a control notice, nested child failure,
open obligation, cleanup, worktree preservation or process handoff. Checks run
both before semantic triage and in the admission transaction against immutable
source bytes. Replayed events cannot omit their saved control or result fields.
Control-bearing diagnostic events are treated as normal results rather than
mutable diagnostic-only notices. Required/unknown launch obligations retain their
existing nonsuppressible delivery semantics.

## F02: late results across steering

New launches persist the parent's generation alongside the original supervision
revision. A late result may compare against a changed instruction/requirements
revision only with an identical profile, lineage and work ID, exact parent session,
matching generation and complete canonical child-control linkage. It is not an
adoption into another scope. Legacy launches lacking this proof use ordinary
delivery after steering.

The relation observation carries original and current revisions and current
requirements. A current committed conclusion is pinned and rechecked at admission;
steering during inference, a changed conclusion, a changed generation, expired
judgment, missing grants and unavailable storage preserve ordinary delivery.
Qualified optional corroborating/superseded results are retained with their full
immutable original bytes, without a parent wake. New material findings follow the
ordinary delivery route and remain provisional. This does not certify that a
worker read a coherent filesystem snapshot or satisfy current verification from
old inputs.

## F03: native consumers

When no current exact work-map link exists, early-finding admission can use
canonical parent launch consumers with `requires_result=True` and exact
`requirement_ids` joined to current native requirements. The existing requested
information need is supplied as the decision; no new action, task or consumer is
invented. Source, launch binding, parent generation and current consumer links are
rechecked at the safe point. Missing/stale links remain an honest no-op.

Once-only finding admission uses an immutable source object and independent
receipt. It does not acknowledge the final result or complete a required
obligation. Sources containing protected control/failure/cleanup information do
not enter summary-only early selection; their ordinary result remains intact.

F02/F03 retain the existing bounded eligibility limits (including 1,200-byte late
source/early finding bounds and small enumerated requirements). Larger or
unsupported sources fall back to ordinary final delivery, not truncated authority.

## F18: current progress

Native `OptionalProgressText` phases (`progress`, `local_load`, `first_chunk`,
`post_chunk`, `first_event`, `reconnect`, `post_event`) deduplicate identical updates
locally and show changed progress immediately. They never dispatch a semantic
request. Other genuinely unclassified optional content can still use the bounded
show/suppress path. Explicit control/request/approval/safety/failure/cleanup
metadata bypasses optional coalescing. Clearing a wait resets its local dedupe.
