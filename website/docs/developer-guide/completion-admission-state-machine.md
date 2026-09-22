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
  Tool/facade producers must supply the source receipt; this API does not mine reasoning.
- `completion_admission.prepare_event(event, claim, target_session_id)` pins/disposes;
  `accept_event`/`accept_metadata` reserve; `consume_metadata` invokes SessionDB.
- `set_admission_policy(owner_consumer)` installs an **already-ready, nonblocking host
  decision lookup**, not a network callback. It returns `CompletionDecision` or the
  compatible `(disposition, host_retainable)` tuple. The context/policy owner must bind
  its validated proposal consumer here; provider/network scheduling stays outside the
  SQLite transaction and outside this lane.
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
Plugin policy binding, registered read-only effect contracts and FindingV1 producer
validation belong to their respective host owners and must be integration-tested there.
