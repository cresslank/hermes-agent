"""Completion/finding handoff shared by CLI, TUI, gateway and quiet drains.

Routing/authentication run first at each caller. prepare_event pins source bytes;
accept_event transfers only after the destination's availability/capacity checks.
consume_metadata runs through SessionDB's existing identity-aware transcript writer.
An inbox receipt is never an inference or external-rendering receipt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, replace

from agent import supervision_store as store

# The context/policy owner may supply one already-validated, deadline-bounded
# decision consumer. No model callback, network wait or plugin types in core.
_policy = None


def set_admission_policy(owner_consumer):
    global _policy
    _policy = owner_consumer


@dataclass(frozen=True)
class CompletionDecision:
    """Already-validated owner decision. Deadline is the shared absolute monotonic bound."""
    disposition: str = 'deliver_unchanged'
    retainable: bool = False
    view_window: tuple[int, int] | None = None
    deadline: float | None = None
    selection: tuple | None = None
    comparison: tuple[int, str] | None = None
    declined: bool = False
    launch_generation: int | None = None

    def __post_init__(self):
        import math
        if self.disposition not in {'deliver_unchanged','deliver_bounded_view','retain_without_wake','defer'}:
            raise ValueError('invalid completion disposition')
        if type(self.retainable) is not bool:
            raise ValueError('retainability must be an owner boolean')
        if self.deadline is not None and (type(self.deadline) not in {int,float} or not math.isfinite(self.deadline)):
            raise ValueError('invalid absolute deadline')


@dataclass(frozen=True)
class PreparedDelivery:
    delivery_id: str | None
    state: str
    disposition: str = 'deliver_unchanged'

    @property
    def present(self):
        return self.state not in {'parked','consumed'} and self.disposition != 'defer'


def prepare_event(event, claim, target_session_id):
    """Called after owner routing but BEFORE status, render, wake or source ACK.

    Legacy non-durable events remain unchanged. Missing target authority cannot be
    made durable by creating a session here. An explicitly deleted target fails closed.
    Policy returns ``(disposition, host_retainable)``; optionality is revalidated
    against linked canonical lifecycle controls inside the writer transaction.
    """
    if event.get('type') == 'literal_source_change':
        from agent.supervision_corrections import prepare_source_event
        return PreparedDelivery(None, 'received' if prepare_source_event(event, target_session_id) else 'consumed')
    if event.get('type') != 'async_delegation' or event.get('task_failure_notice'):
        return PreparedDelivery(None, 'received')
    from tools.async_delegation import _DB_LOCK, _transaction
    decision = CompletionDecision()
    prior = None
    with _DB_LOCK, _transaction() as conn:
        prior = conn.execute('SELECT 1 FROM supervision_admissions WHERE launch_id=? AND source_id=? '
                             'AND (target_session_id=? OR lineage=?)',
                             (event['delegation_id'], event.get('finding_id') or 'final', target_session_id, target_session_id)).fetchone()
    if prior is None:
        try:
            from agent.supervision_delivery import final_decision
            value = (_policy or final_decision)(event, target_session_id)
            decision = value if isinstance(value, CompletionDecision) else CompletionDecision(*value)
        except Exception:
            pass  # absent/invalid judgment preserves normal delivery
    if decision.deadline is not None:
        import time
        if decision.deadline <= time.monotonic():
            decision = CompletionDecision()
    from contextlib import ExitStack
    from agent.supervision_delivery import persist_selection
    with ExitStack() as fences:
        selection = decision.selection
        if selection:
            runtime, proposal, registration = selection
            fences.enter_context(runtime.lock)
            fences.enter_context(registration.fence)
            failure = runtime._validate(proposal, registration)
            if failure or proposal.expires_at_monotonic != decision.deadline:
                runtime._settle(proposal, *(failure or ('expired', 'deadline')))
                decision = CompletionDecision()
        with _DB_LOCK, _transaction() as conn:
            if not conn.execute('SELECT 1 FROM async_delegations WHERE delegation_id=?', (event.get('delegation_id'),)).fetchone():
                if conn.execute('SELECT 1 FROM supervision_generations WHERE session_id=? AND revoked=1', (target_session_id,)).fetchone():
                    raise store.AdmissionError('deleted target')
                return PreparedDelivery(None, 'received')
            # Old launches with no canonical session binding retain legacy behavior;
            # they cannot participate in optional semantic retention.
            if not target_session_id or not conn.execute('SELECT 1 FROM sessions WHERE id=?', (target_session_id,)).fetchone():
                if target_session_id and conn.execute('SELECT 1 FROM supervision_generations WHERE session_id=? AND revoked=1', (target_session_id,)).fetchone():
                    raise store.AdmissionError('deleted target')
                return PreparedDelivery(None, 'received')
            # Lock/storage acquisition may consume the remaining owner budget.
            # Recheck after the final blocking boundary, before any optional write.
            if decision.selection:
                runtime, proposal, registration = decision.selection
                if runtime._validate(proposal, registration):
                    decision = replace(decision, disposition='deliver_unchanged', retainable=False,
                                       view_window=None, declined=True)
            if (decision.launch_generation is not None
                    and decision.launch_generation != store.generation(conn, target_session_id)):
                decision = replace(decision, disposition='deliver_unchanged', retainable=False,
                                   view_window=None, declined=True)
            if decision.comparison is not None:
                latest = conn.execute("SELECT id,content FROM messages WHERE session_id=? AND role='assistant' "
                                      "AND tool_calls IS NULL AND content IS NOT NULL ORDER BY id DESC LIMIT 1",
                                      (target_session_id,)).fetchone()
                if not latest or (latest[0], store.digest(latest[1])) != decision.comparison:
                    decision = replace(decision, disposition='deliver_unchanged', retainable=False,
                                       view_window=None, declined=True)
            conn.execute('SAVEPOINT optional_delivery')
            row = store.persist_admission(conn, event=event, claim=claim, target_session_id=target_session_id,
                                          disposition=decision.disposition, retainable=decision.retainable is True,
                                          view_window=decision.view_window, defer_until=decision.deadline)
            try:
                persist_selection(conn, decision, row)
            except sqlite3.Error:
                # Receipt failure must undo an optional park/view, not the required
                # final. No new judgment or deadline is allocated on this fallback.
                conn.execute('ROLLBACK TO optional_delivery')
                row = store.persist_admission(conn, event=event, claim=claim,
                    target_session_id=target_session_id, disposition='deliver_unchanged',
                    retainable=False, view_window=None, defer_until=decision.deadline)
                decision = replace(decision, declined=True)
            finally:
                conn.execute('RELEASE optional_delivery')
        if selection:
            runtime, proposal, _registration = selection
            runtime.closed_targets.add(proposal.target_id)
            if decision.declined:
                runtime._settle(proposal, 'no_op', 'admission_invalid')
    event['supervision_delivery_id'] = row['delivery_id']
    event['source_object_id'] = row['object_id']
    event['_supervision_claim'] = claim
    try:
        from agent.supervision_corrections import admit_completion
        admit_completion(event, target_session_id)
    except Exception:
        pass  # optional correction state must not suppress required/unknown F02 delivery
    if row['view_text'] is not None:
        event['_supervision_view_text'] = row['view_text']
    return PreparedDelivery(row['delivery_id'], row['state'], row['disposition'])


def presentation_text(event, original):
    view = event.get('_supervision_view_text')
    if view is None:
        return original
    return ('[Exact bounded background result; claims provisional, revalidation required; status: ' + str(event.get('status') or 'unknown')
            + '; full immutable source: ' + str(event.get('source_object_id') or '') + ']\n' + view)


def accept_event(event, *, capacity=256, destination_lease=None):
    if event.get('type') == 'literal_source_change':
        from agent.supervision_corrections import accept_source_event
        return accept_source_event(event)
    delivery_id = event.get('supervision_delivery_id')
    if not delivery_id:
        return True
    from tools.async_delegation import _DB_LOCK, _transaction
    lease = destination_lease or event.get('supervision_destination_lease') or f'{os.getpid()}:{uuid.uuid4().hex}'
    with _DB_LOCK, _transaction() as conn:
        accepted = store.reserve(conn, delivery_id, destination_lease=lease, capacity=capacity)
    if accepted:
        event['supervision_destination_lease'] = lease
    return accepted


def delivery_metadata(event):
    keys = ('supervision_delivery_id','supervision_destination_lease','delegation_id','finding_id')
    return {key: event[key] for key in keys if event.get(key)}


def accept_native_source_event(event):
    native = getattr(event, "_native_literal_source_event", None)
    if native is None:
        return True
    from agent.supervision_corrections import accept_source_event
    return event.internal is True and accept_source_event(native)


def accept_metadata(metadata, *, capacity=256):
    """Adapter acceptance gate. Mutates only host-owned internal-event metadata."""
    deliveries = metadata.get('supervision_deliveries') or ([metadata] if metadata.get('supervision_delivery_id') else [])
    if not deliveries:
        return True
    from tools.async_delegation import _DB_LOCK, _transaction
    # A group reserves all constituents in one transaction; no stranded siblings.
    class _Unavailable(Exception):
        pass
    try:
        with _DB_LOCK, _transaction() as conn:
            for item in deliveries:
                lease = item.get('supervision_destination_lease') or f'{os.getpid()}:{uuid.uuid4().hex}'
                if not store.reserve(conn, item['supervision_delivery_id'], destination_lease=lease, capacity=capacity):
                    raise _Unavailable()
                item['supervision_destination_lease'] = lease
        return True
    except _Unavailable:
        return False


def valid_hint(metadata, session_id=None):
    """Recheck a queued hint immediately before first presentation/turn promotion."""
    items = metadata.get('supervision_deliveries') or ([metadata] if metadata.get('supervision_delivery_id') else [])
    if not items:
        return True
    from tools.async_delegation import _DB_LOCK, _transaction
    try:
        with _DB_LOCK, _transaction() as conn:
            for item in items:
                row = store.get_admission(conn, item['supervision_delivery_id'])
                if not row or row['state'] not in {'accepted','consumed'}:
                    return False
                store.validate_consumption(conn, row['delivery_id'], session_id or row['target_session_id'],
                                           item.get('supervision_destination_lease'))
        return True
    except store.AdmissionError:
        return False


def consume_metadata(db, session_id, text, metadata, *, turn_lease_holder=None):
    """Append/adopt all constituent identities in ONE existing SessionDB transaction."""
    items = metadata.get('supervision_deliveries') or [metadata]
    return db.append_delegation_delivery(
        session_id, text, metadata, supervision_delivery_id=items[0]['supervision_delivery_id'],
        delivery_subtype='finding' if items[0].get('finding_id') else 'final',
        destination_lease=items[0].get('supervision_destination_lease'),
        turn_lease_holder=turn_lease_holder)


def requeue_due(target_queue, target_session_id=None):
    """Repair lost hints on an existing idle pass, only after the destination lease expires."""
    from tools.async_delegation import _DB_LOCK, _transaction
    import time
    with _DB_LOCK, _transaction() as conn:
        events = recover_events(conn, target_session_id)
        for event in events:
            row = store.get_admission(conn, event['supervision_delivery_id'])
            if not row:
                continue
            if row['disposition'] == 'defer':
                if (row['not_before'] or 0) > time.time():
                    continue
                conn.execute("UPDATE supervision_admissions SET disposition='deliver_unchanged',not_before=NULL WHERE delivery_id=?",
                             (row['delivery_id'],))
                target_queue.put(event)
                continue
            if row['updated_at'] > time.time() - 30:
                continue
            due = row['state'] == 'accepted' and ((row['lease_expires'] or 0) <= time.time()
                                                  or not store._lease_alive(row['destination_lease']))
            if row['state'] == 'persisted':
                source = conn.execute('SELECT delivery_claimed_at FROM async_delegations WHERE delegation_id=?',
                                      (row['launch_id'],)).fetchone()
                due = not source or not source[0] or source[0] < time.time() - 300
            if due:
                conn.execute('UPDATE supervision_admissions SET updated_at=? WHERE delivery_id=?',
                             (time.time(), row['delivery_id']))
                target_queue.put(event)
    return len(events)


def recover_events(conn, target_session_id=None):
    """Existing startup/idle recovery calls this; no new daemon or poll loop."""
    query = "SELECT * FROM supervision_admissions WHERE state IN ('persisted','accepted')"
    params = ()
    if target_session_id:
        query += ' AND target_session_id=?'
        params = (target_session_id,)
    cur = conn.execute(query+' ORDER BY created_at,delivery_id', params)
    names = [c[0] for c in cur.description]
    events = []
    for values in cur.fetchall():
        row = dict(zip(names, values))
        event = json.loads(row['event_json'])
        event['supervision_delivery_id'] = row['delivery_id']
        event['_supervision_recovery'] = True
        event['restored'] = True
        events.append(event)
    return events
