# Durable completion admission

Completion/finding delivery is host-owned even when no supervision plugin is loaded.
There is one `state.db`, one source ledger and the existing SessionDB transcript writer.
No decision callback writes a second transcript or starts a polling daemon.

## Ownership states

| State | Durable owner | Meaning |
| --- | --- | --- |
| received | source | No inbox receipt yet; failures cannot acknowledge the source. |
| persisted | source + inbox | Exact source bytes are pinned, source claim remains pending. |
| parked | inbox | Explicit `retained`, not delivered; no wake or status is emitted. |
| accepted | target inbox | Atomic reservation transfers the source claim; the queue contains only a hint. |
| consumed | SessionDB | The identity-aware writer appended/adopted one message row and settled its constituent source claims in the same transaction. |

Delivery IDs bind profile, original compression lineage, launch, source identity and
finding/final subtype. Grouped messages retain ordered constituent IDs. Destination
leases arbitrate competing surfaces, while source attempt fences remain on the inbox.
Findings never claim or acknowledge the eventual final batch.

The four drains (gateway, CLI, TUI and quiet CLI) call admission before their first
presentation or submission. Gateway reservation runs after handler/route/queue checks;
busy FIFO entries carry the IDs. CLI string-compatible `TimelineNotification` and TUI
submit metadata carry them to ordinary turn staging. Quiet callbacks receive the same
carrier, not ACK-then-joined unowned strings. API delivery can consume directly without
a model wake. Ordinary turn leases and session-generation guards remain authoritative.

`append_delegation_delivery` is the sole identity-aware message writer. It accepts
optional `supervision_delivery_id`, `delivery_subtype`, `destination_lease` and
`turn_lease_holder` keywords, preserving its legacy three-positional-argument API.
`metadata.supervision_deliveries` contains ordered constituent metadata for a grouped
row. A crash after append adopts that row; it never appends the same logical delivery
again. This does **not** promise exactly-once inference, tool effects or external UI.

## Source bytes and retention

`_persist_completion` and `record_unit_child` write immutable source objects in the
same source transaction as their aggregate bookkeeping. A later final aggregate cannot
change a child's object. `commit_finding` stores exact UTF-8 bytes, an independent
receipt and inbox row before publishing a queue hint. Verification is an explicit
source-owner assertion, never inferred from child prose.

Optional retention requires owner validation and every linked canonical child control
remaining optional, with nonempty closed consumer coverage, an attested read-only effect
policy, completed process/effect reconciliation and no pending cleanup/handoff/effect pin.
`async_delegations.control_child_ids` is identity linkage, never a second mutable control
authority. Missing/partial/foreign linkage and required siblings preserve delivery. It is bounded to
4 MiB per object and 64 MiB protected bytes per profile. Capacity failure preserves
ordinary delivery; unresolved pins are never evicted to admit another optional item.
Both ledger count caps and its age pruning exclude unresolved source objects. Existing
maintenance skips protected sessions; settled unpinned objects can expire after seven
days. Explicit session deletion removes source objects/inbox rows and writes a generation
revocation in the same owner transaction. Reset parks accepted work; compression alone
preserves its logical lineage.

Startup recovery and existing idle passes scan persisted/accepted receipts. No new
polling thread is introduced. A dead or expired destination lease can be recovered;
consumed receipts are adopted through existing unanswered-turn mechanisms. Disabling
the optional decision consumer does not disable this recovery.

## Integration APIs

- `dispatch_async_delegation(..., control_children=actual_children)` and its batch
  counterpart extract each actual `child._subagent_id` and persist the unit's ordered
  `control_child_ids` before scheduling. Lifecycle must already have created canonical
  `delegation_controls` rows. The launch caller supplies its own actual unit members,
  not proposal IDs or a task's requested supervision metadata. Omitted IDs remain legacy
  unknown and cannot enable optional retention.
- `tools.async_delegation.get_launch_control(delegation_id)` reads a detached projection
  of those canonical rows; it does not regrant live cancellation authority after restart.
  Admission rereads them in its source writer transaction. Only the lifecycle owner's
  `SQLiteControlStore` mutates controls; no async launch-control setter or shadow CAS
  exists. Canonical schema reconciliation creates its table/index without a private
  adapter bootstrap. Explicit parent deletion removes controls and refuses late creation;
  automatic retention preserves active/unsettled/cleanup-pending controls and their bytes.
- `tools.async_delegation.commit_finding(delegation_id, *, finding_id, source_receipt,
  payload: bytes, verified=False) -> dict`: explicit committed-owner-fact producer.
  The receipt must resolve to a same-launch immutable child/tool object whose exact
  committed summary equals `payload`. A string or `verified=True` is not proof.
  Claims remain provisional. This API never mines reasoning or ACKs the final batch.
- `completion_admission.prepare_event(event, claim, target_session_id)` pins/disposes;
  `accept_event`/`accept_metadata` reserve; `consume_metadata` invokes SessionDB.
- The default scoped `supervision_delivery.final_decision` binds admission to the
  routed session's live runtime and original launch work/instruction identity. It reads
  the immutable new result and a canonical final message committed while that launch
  was outstanding, then schedules F02 outside database locks. The only wait is the
  original completion-admission allowance (at most 150 ms; an active round's earlier
  deadline wins). No runtime/source/final/permission means unchanged delivery.
  `set_admission_policy` remains a host-only ready-decision compatibility seam.
- `supervision.final-actions.v1` negotiates exactly `bounded_view: final_bounded_view`
  and `deliver: deliver_final`, with distinct grants. Neither aliases early finding
  delivery nor generic advice. A provisional material comparison only preserves the
  already-authorized original final delivery for main-agent revalidation; it does not
  certify a correction. A bounded view preserves exact bytes, status and source handle,
  and labels claims provisional. Required/unknown obligations cannot be clipped.
- `record_unit_child` and the actual completed child tool-batch path offer F03 only
  after source commit. The latter compares an actual persisted tool row to its exact
  read-owner result. Canonical child-control membership plus an exact goal/resource
  and current work-map/todo link establish the pending consumer and next action.
  No link, missing bytes, job finality, changed plan, expired grant or no ready proposal
  leaves early delivery inactive; the required final remains outstanding.
  A parent safe point commits a distinct finding admission and queues only its hint;
  normal route/auth/reservation/next-turn staging owns delivery, never mid-token append.
- `CompletionDecision` supports unchanged delivery, owner-validated exact byte windows,
  optional retention and a defer using the existing absolute monotonic deadline (at
  most 150 ms remaining). Expired/invalid/absent decisions preserve ordinary delivery.
  Deferred work is revisited by the existing idle scheduler, not a new inference turn.
- `supervision_store.pin_result_effect(conn, object_id, receipt_ref)` and
  `settle_result_effect` retain bounded explicit effect/cleanup references; only the
  actual effect owner may attest settlement. A pending reference protects bytes after
  transcript consumption and blocks optional suppression.
- `supervision_store.read_retained(conn, id, session_id=...)` retrieves exact bytes in
  the original lineage. `promote_parked(conn, id, expected_generation=...)` is only for
  an authorized later material-change owner action; ordinary recovery never promotes.

Tests use real disposable SQLite/SessionDB paths, all four surface drains, source claim
and target lease races, actual process exit, both retention caps, exact byte budgets,
immutable partial/final reconciliation, deletion, maintenance and fallback decisions.
`test_supervision_delivery_native.py` additionally exercises the full standalone registry,
strict mock HTTP, public source launch, finished child/tool facts, actual CLI drain and
native turn staging, including early/final ordering and lost queue hints.

## Compact work and effect receipts

Canonical `SCHEMA_SQL` also owns `supervision_work` and `supervision_receipts`.
`SupervisionRuntime._settle` remains the shared settlement seam. Receipts contain scope,
revision, generation, finite action/status/reason, evidence IDs, original deadline and
owner outcome only—never raw projections, provider probabilities or control capabilities.
The runtime reserves a durable accepted receipt before queueing an optional proposal,
and a selected receipt before the owner effect. Missing/busy storage or capacity (256
work identities / 4096 receipts per profile) preserves baseline without waiting or eviction.
Selection is not consumption. Final/finding selection links to the admission in the same
source transaction; the normal SessionDB append stamps `consumed` in its transaction.
If the optional receipt write fails at this boundary, a savepoint undoes the optional
park/view and preserves unchanged final admission with the same original deadline.
The evidence-owner acknowledgment protocol can transition selected work to
`accepted` / `owner_selected`; only its exact owner acknowledgment stamps `consumed`
with the validated effect digest. An owner veto remains `rejected`, never applied.
The common store binds receipt updates to the original revision, target, action,
incident, plugin generation, owner, evidence IDs, deadline and session. Cross-owner
protocol integration is qualified separately from these common-store transitions.
Parked optional results are explicitly retained, never delivered. Generic owners still
own their actual applied/no-op receipt calls; an outcome lost after selection remains
unresolved, not permission to repeat an external effect.

Existing maintenance prunes terminal receipts after seven days; unresolved selections
and admission obligations remain protected. User deletion removes work and receipt rows
through canonical triggers. These metadata receipts never restore grants, replay model
calls, or claim exactly-once external rendering/inference/tool execution.
