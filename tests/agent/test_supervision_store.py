"""Native SQLite source/inbox/SessionDB crash and retention contracts."""
import json
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import completion_admission as admission
from agent import supervision_store as store
from hermes_state import SessionDB
from tools import async_delegation as source


@pytest.fixture
def owner(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(source, '_db_path', lambda: path)
    monkeypatch.setattr(admission, '_policy', None)
    db = SessionDB(path)
    db.create_session('target', 'cli')
    yield db
    db.close()


def launch(name='job', **extra):
    event = dict(type='async_delegation', delegation_id=name, session_key='target',
                 parent_session_id='target', dispatched_at=time.time(), summary='exact result λ', status='completed', **extra)
    source._persist_dispatch(event)
    return event


def ready(name='job'):
    event = launch(name)
    source._persist_completion(event, {'summary': 'exact result λ', 'status': 'completed'})
    claim = source.claim_event_delivery(event, 'test')
    prepared = admission.prepare_event(event, claim, 'target')
    return event, prepared


def row(event):
    with source._transaction() as conn:
        return store.get_admission(conn, event['supervision_delivery_id'])


def consume(db, event, text='owned result'):
    return admission.consume_metadata(db, 'target', text, admission.delivery_metadata(event))


def test_native_persist_reserve_consume_and_reopen(owner):
    event, prepared = ready()
    assert prepared.state == 'persisted'
    assert source.get_durable_delegation('job')['delivery_state'] == 'pending'
    assert admission.accept_event(event)
    assert source.get_durable_delegation('job')['delivery_state'] == 'transferred'
    source.complete_event_delivery(event, event['_supervision_claim'])
    assert row(event)['state'] == 'accepted'
    mid = consume(owner, event)
    assert consume(owner, event) == mid
    assert row(event)['state'] == 'consumed'
    assert row(event)['message_row_id'] == mid
    assert len(owner.get_messages('target')) == 1
    reopened = SessionDB(source._db_path())
    try:
        assert consume(reopened, event) == mid
        assert len(reopened.get_messages('target')) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('boundary', ['before_commit', 'after_commit_before_enqueue', 'after_accept_before_ack', 'after_dequeue_before_append', 'after_append'])
def test_crash_boundaries(owner, boundary):
    event = launch()
    source._persist_completion(event, {'summary': 'kept'})
    claim = source.claim_event_delivery(event, 'old')
    if boundary == 'before_commit':
        with pytest.raises(RuntimeError), source._transaction() as conn:
            store.persist_admission(conn, event=event, claim=claim, target_session_id='target')
            raise RuntimeError('crash before commit')
        assert source.get_durable_delegation('job')['delivery_state'] == 'pending'
        with source._transaction() as conn:
            assert conn.execute('SELECT count(*) FROM supervision_admissions').fetchone()[0] == 0
        admission.prepare_event(event, claim, 'target')
    else:
        admission.prepare_event(event, claim, 'target')
    if boundary in {'after_accept_before_ack','after_dequeue_before_append','after_append'}:
        assert admission.accept_event(event)
    if boundary == 'after_append':
        consume(owner, event)
    with source._transaction() as conn:
        conn.execute('UPDATE supervision_admissions SET lease_expires=0')
        conn.execute('UPDATE async_delegations SET delivery_claimed_at=0')
    hints = queue.Queue()
    source.restore_undelivered_completions(hints)
    if boundary == 'after_append':
        assert hints.empty()
    else:
        recovered = hints.get_nowait()
        token = source.claim_event_delivery(recovered, 'restart')
        admission.prepare_event(recovered, token, 'target')
        assert admission.accept_event(recovered)
        consume(owner, recovered)
    assert len(owner.get_messages('target')) == 1


def test_two_surfaces_compete_using_independent_connections(owner):
    event, _ = ready()
    barrier = threading.Barrier(2)
    def reserve(surface):
        candidate = dict(event)
        barrier.wait(timeout=3)
        return candidate if admission.accept_event(candidate, destination_lease=f'{os.getpid()}:{surface}') else None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, ['tui','gateway']))
    winners = [event for event in results if event]
    assert len(winners) == 1
    consume(owner, winners[0])
    assert len(owner.get_messages('target')) == 1


def test_source_claim_fence_and_destination_lease(owner):
    event = launch()
    source._persist_completion(event, {'summary': 'exact'})
    token = source.claim_event_delivery(event, 'owner')
    with pytest.raises(store.AdmissionError):
        admission.prepare_event(event, 'wrong-claim', 'target')
    admission.prepare_event(event, token, 'target')
    assert admission.accept_event(event)
    metadata = admission.delivery_metadata(event)
    metadata['supervision_destination_lease'] = 'wrong'
    with pytest.raises(store.AdmissionError):
        admission.consume_metadata(owner, 'target', 'text', metadata)
    assert not owner.get_messages('target')


def test_active_target_turn_lease_is_never_bypassed(owner):
    event, _ = ready()
    assert admission.accept_event(event)
    assert owner.acquire_session_turn_lease('target', 'turn-owner', ttl_seconds=300)
    from hermes_state_errors import SessionTurnLeaseLostError
    with pytest.raises(Exception):
        consume(owner, event)
    assert row(event)['state'] == 'accepted'
    mid = admission.consume_metadata(owner, 'target', 'text', admission.delivery_metadata(event), turn_lease_holder='turn-owner')
    assert row(event)['message_row_id'] == mid


def control_snapshot(child_id='child', **changes):
    return dict(child_id=child_id, parent_session_id='target', generation='generation:1', control_revision=1,
                obligation='optional', consumer_set_closed=True, effect_policy_id='readonly:v1',
                effect_class='read_only', consumers=[dict(ref='parent:target', obligation='optional',
                    requires_result=False, requires_effects=False, requires_cleanup=False)],
                settled=True, processes_stopped=True, effects_reconciled=True, cleanup_pending=False,
                handoffs=[], inflight=0) | changes


def write_control(conn, snapshot):
    conn.execute("INSERT INTO delegation_controls VALUES(?,?,?,?,?,?)",
                 (snapshot['child_id'], snapshot['parent_session_id'], snapshot['generation'],
                  snapshot['control_revision'], json.dumps(snapshot), time.time()))


def optional(event):
    # Only canonical lifecycle rows grant eligibility, never async source metadata.
    with source._transaction() as conn:
        child_id = 'child:' + event['delegation_id']
        write_control(conn, control_snapshot(child_id))
        conn.execute("UPDATE async_delegations SET control_child_ids=? WHERE delegation_id=?",
                     (json.dumps([child_id]), event['delegation_id']))


def test_parked_result_never_acknowledges_delivery_and_remains_exact(owner, monkeypatch):
    event = launch()
    result = {'summary': 'bytes \u0000 λ', 'status': 'completed'}
    source._persist_completion(event, result)
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake', True))
    prepared = admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert prepared.state == 'parked' and not prepared.present
    assert not admission.accept_event(event)
    assert source.get_durable_delegation('job')['delivery_state'] == 'retained'
    with source._transaction() as conn:
        assert store.get_result_object(conn, event['source_object_id']) == json.dumps(result).encode()
    assert not owner.get_messages('target')


@pytest.mark.parametrize('budget', ['object','profile'])
def test_retention_budget_falls_back_without_eviction(owner, monkeypatch, budget):
    event = launch()
    source._persist_completion(event, {'summary': 'result'})
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake', True))
    monkeypatch.setattr(store, 'OBJECT_LIMIT' if budget == 'object' else 'PROFILE_LIMIT', 1)
    prepared = admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert prepared.present and prepared.disposition == 'deliver_unchanged'
    with source._transaction() as conn:
        assert store.get_result_object(conn, event['source_object_id'])
    assert admission.accept_event(event)
    consume(owner, event)


@pytest.mark.parametrize('policy', [lambda *_: ('retain_without_wake',True), lambda *_: ('invalid',True), lambda *_: None])
def test_unknown_obligation_and_invalid_policy_never_suppress(owner, monkeypatch, policy):
    monkeypatch.setattr(admission, '_policy', policy)
    event, prepared = ready()
    assert prepared.present


def test_storage_failure_cannot_park_or_ack(owner, monkeypatch):
    event = launch()
    source._persist_completion(event, {'summary':'result'})
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake',True))
    def fail(*a, **kw):
        raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(store, 'persist_admission', fail)
    with pytest.raises(sqlite3.OperationalError):
        admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert source.get_durable_delegation('job')['delivery_state'] == 'pending'
    assert not owner.get_messages('target')


def test_both_ledger_caps_and_age_preserve_unresolved_exact_bytes(owner, monkeypatch):
    event, _ = ready()
    monkeypatch.setattr(source, '_MAX_RETAINED_COMPLETED', 1)
    monkeypatch.setattr(source, '_MAX_DURABLE_PENDING', 1)
    expected = json.dumps({'summary':'exact result λ','status':'completed'}).encode()
    for i in range(4):
        extra = launch(f'extra-{i}')
        source._persist_completion(extra, {'summary':str(i)})
    with source._transaction() as conn:
        conn.execute('UPDATE async_delegations SET updated_at=1,completed_at=1')
    source._prune_durable_records()
    with source._transaction() as conn:
        store.prune_objects(conn, now=time.time()+store.RETENTION_SECONDS*2)
        assert store.get_result_object(conn, event['source_object_id']) == expected
    reopened = SessionDB(source._db_path())
    reopened.close()
    assert source.get_durable_delegation('job') is not None
    hints = queue.Queue()
    source.restore_undelivered_completions(hints)
    assert hints.qsize() == 5


def test_early_finding_and_final_have_independent_receipts(owner):
    event = launch()
    early = source.commit_finding('job', finding_id='child:1:rev1', source_receipt='receipt:1', payload=b'early exact source')
    source.record_unit_child('job', {'task_index':1,'summary':'completed child'})
    assert admission.accept_event(early)
    consume(owner, early, 'early exact source')
    assert source.get_durable_delegation('job')['delivery_state'] == 'pending'
    source._persist_completion(event, {'summary':'later aggregate'})
    token = source.claim_event_delivery(event, 'final')
    admission.prepare_event(event, token, 'target')
    assert event['supervision_delivery_id'] != early['supervision_delivery_id']
    assert admission.accept_event(event)
    consume(owner, event, 'later aggregate')
    with source._transaction() as conn:
        assert store.get_result_object(conn, early['source_object_id']) == b'early exact source'
    assert len(owner.get_messages('target')) == 2


@pytest.mark.parametrize('bulk', [False,True])
def test_user_deletion_revokes_generation_and_late_launch(owner, bulk):
    event, _ = ready()
    assert admission.accept_event(event)
    (owner.delete_sessions(['target']) if bulk else owner.delete_session('target'))
    with source._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM supervision_admissions').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM delegation_result_objects').fetchone()[0] == 0
        assert conn.execute('SELECT revoked FROM supervision_generations WHERE session_id=\'target\'').fetchone()[0] == 1
    source._persist_completion(event, {'summary':'old event'})
    with pytest.raises(store.AdmissionError):
        source._persist_dispatch(event)
    with pytest.raises(store.AdmissionError):
        admission.prepare_event(event, 'old', 'target')


def test_reset_parks_accepted_and_compression_preserves_lineage(owner):
    event, _ = ready()
    assert admission.accept_event(event)
    owner.end_session('target', 'session_reset')
    assert row(event)['state'] == 'parked'
    with pytest.raises(store.AdmissionError):
        consume(owner, event)


def test_process_exit_after_acceptance_recovers_without_a_second_logical_row(owner):
    import subprocess
    import sys
    event = launch()
    source._persist_completion(event, {'summary':'process crash evidence'})
    script = '''
import json, os, sys
from pathlib import Path
from tools import async_delegation as source
from agent import completion_admission as admission
source._db_path = lambda: Path(sys.argv[1])
event = json.loads(sys.argv[2])
claim = source.claim_event_delivery(event, 'crashing-process')
admission.prepare_event(event, claim, 'target')
assert admission.accept_event(event)
os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', script, str(source._db_path()), json.dumps(event)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 23, result.stderr
    hints = queue.Queue()
    source.restore_undelivered_completions(hints)
    recovered = hints.get_nowait()
    token = source.claim_event_delivery(recovered, 'new-process')
    admission.prepare_event(recovered, token, 'target')
    assert admission.accept_event(recovered)  # dead owner's PID releases the admission lease
    consume(owner, recovered)
    assert len(owner.get_messages('target')) == 1


def test_maintenance_never_deletes_protected_sessions(owner):
    event, _ = ready()
    owner.end_session('target', 'compression')
    assert owner.prune_sessions(older_than_days=None, exclude_active_write_guards=True) == 0
    assert owner.get_session('target') is not None
    assert owner.delete_session_if_empty('target') is False
    owner.delete_session('target')  # explicit deletion is different from automatic retention
    with source._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM delegation_result_objects').fetchone()[0] == 0


def test_compression_continuation_consumes_the_same_delivery(owner):
    event, _ = ready()
    assert admission.accept_event(event)
    owner.end_session('target', 'compression')
    owner.create_session('tip', 'cli', parent_session_id='target')
    mid = admission.consume_metadata(owner, 'tip', 'same result', admission.delivery_metadata(event))
    assert row(event)['message_row_id'] == mid
    assert len(owner.get_messages('tip')) == 1


def test_launch_control_is_read_only_projection_of_canonical_children(owner):
    event = launch()
    assert source.get_launch_control('job')['retainable'] is False
    optional(event)
    assert source.get_launch_control('job')['retainable'] is True
    with source._transaction() as conn:
        snapshot = control_snapshot('child:job', control_revision=2, obligation='required')
        conn.execute('UPDATE delegation_controls SET control_revision=2,snapshot_json=? WHERE child_id=?',
                     (json.dumps(snapshot), 'child:job'))
    assert source.get_launch_control('job')['obligation'] == 'required'
    assert not hasattr(source, 'update_launch_control')
