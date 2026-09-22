"""Canonical child controls govern admission and retention without a shadow authority."""
import json
import sqlite3
import time

import pytest

from agent import completion_admission as admission
from agent import supervision_store as store
from tools import async_delegation as source
from tests.agent.test_supervision_store import (
    owner, launch, optional, control_snapshot, write_control, consume,
)


@pytest.mark.parametrize('change', [
    {'obligation': 'required'}, {'consumer_set_closed': False}, {'consumers': []},
    {'effect_class': 'unknown'}, {'effect_policy_id': None}, {'settled': False},
    {'processes_stopped': False}, {'effects_reconciled': False}, {'cleanup_pending': True},
    {'handoffs': ['nested']}, {'inflight': 1},
    {'consumers': [dict(ref='parent:target', obligation='optional', requires_result=True,
                       requires_effects=False, requires_cleanup=False)]},
    {'consumers': [dict(ref='other', obligation='optional', requires_result=False,
                       requires_effects=False, requires_cleanup=False)]},
])
def test_current_canonical_controls_override_host_retention_proposal(owner, monkeypatch, change):
    event = launch()
    optional(event)
    source._persist_completion(event, {'summary': 'required bytes'})
    # Simulate a cached host judgment whose dependencies changed before admission.
    def policy(*_):
        with source._transaction() as conn:
            snapshot = control_snapshot('child:job', control_revision=2, **change)
            conn.execute('UPDATE delegation_controls SET control_revision=2,snapshot_json=? WHERE child_id=?',
                         (json.dumps(snapshot), 'child:job'))
        return 'retain_without_wake', True
    monkeypatch.setattr(admission, '_policy', policy)
    result = admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert source.get_launch_control('job')['children'][0]['control_revision'] == 2
    assert result.disposition == 'deliver_unchanged'
    assert admission.accept_event(event)
    consume(owner, event)
    assert len(owner.get_messages('target')) == 1


@pytest.mark.parametrize('ids', [[], ['missing'], ['child:job', 'missing'], [None], ['child:job', 'child:job']])
def test_unknown_or_partial_linkage_is_never_optional(owner, ids):
    event = launch()
    optional(event)
    with source._transaction() as conn:
        conn.execute('UPDATE async_delegations SET control_child_ids=?', (json.dumps(ids),))
    assert not source.get_launch_control('job')['retainable']


def test_mixed_batch_keeps_required_sibling_and_unit_subset_uses_own_children(owner):
    launch(goals=['first', 'second'], control_child_ids=['one', 'two'])
    with source._transaction() as conn:
        write_control(conn, control_snapshot('one'))
        write_control(conn, control_snapshot('two', obligation='required'))
    assert source.get_launch_control('job')['obligation'] == 'required'
    with source._transaction() as conn:
        conn.execute('UPDATE async_delegations SET task_json=?,control_child_ids=?',
                     (json.dumps({'goals': ['first', 'second'], 'task_indexes': [0]}), '["one"]'))
    assert source.get_launch_control('job')['retainable']


@pytest.mark.parametrize('change', [
    {'settled': False}, {'processes_stopped': False}, {'effects_reconciled': False},
    {'cleanup_pending': True}, {'handoffs': ['nested']}, {'inflight': 1},
])
@pytest.mark.parametrize('method', ['maintenance', 'ghost', 'empty'])
def test_automatic_parent_deletion_preserves_unsettled_controls(owner, change, method):
    with source._transaction() as conn:
        write_control(conn, control_snapshot(**change))
        conn.execute("UPDATE sessions SET source='tui',started_at=1,ended_at=2,end_reason='compression' WHERE id='target'")
    if method == 'maintenance':
        assert owner.prune_sessions(older_than_days=None, exclude_active_write_guards=True) == 0
    elif method == 'ghost':
        assert owner.prune_empty_ghost_sessions() == 0
    else:
        assert owner.delete_session_if_empty('target') is False
    assert owner.get_session('target') is not None
    with source._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM delegation_controls').fetchone()[0] == 1


@pytest.mark.parametrize('bulk', [False, True])
def test_explicit_parent_delete_removes_control_and_refuses_late_recreation(owner, bulk):
    with source._transaction() as conn:
        write_control(conn, control_snapshot(settled=False))
    (owner.delete_sessions(['target']) if bulk else owner.delete_session('target'))
    with source._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM delegation_controls').fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError), source._transaction() as conn:
        write_control(conn, control_snapshot())


def test_settled_controls_do_not_block_normal_session_maintenance(owner):
    with source._transaction() as conn:
        write_control(conn, control_snapshot())
    owner.end_session('target', 'compression')
    assert owner.prune_sessions(older_than_days=None, exclude_active_write_guards=True) == 1
    with source._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM delegation_controls').fetchone()[0] == 0


@pytest.mark.parametrize('delivery_state', ['pending', 'delivered'])
def test_ledger_caps_and_age_preserve_canonical_cleanup_even_without_objects(owner, monkeypatch, delivery_state):
    event = launch()
    optional(event)
    with source._transaction() as conn:
        snapshot = control_snapshot('child:job', cleanup_pending=True)
        conn.execute('UPDATE delegation_controls SET snapshot_json=?', (json.dumps(snapshot),))
        conn.execute("UPDATE async_delegations SET state='completed',delivery_state=?,updated_at=1", (delivery_state,))
    monkeypatch.setattr(source, '_MAX_RETAINED_COMPLETED', 0)
    monkeypatch.setattr(source, '_MAX_DURABLE_PENDING', 0)
    source._prune_durable_records()
    assert source.get_durable_delegation('job') is not None


def test_canonical_cleanup_protects_consumed_bytes_after_age_horizon(owner):
    event = launch()
    optional(event)
    source._persist_completion(event, {'summary': 'exact bytes'})
    admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert admission.accept_event(event)
    consume(owner, event)
    with source._transaction() as conn:
        snapshot = control_snapshot('child:job', cleanup_pending=True)
        conn.execute('UPDATE delegation_controls SET snapshot_json=?', (json.dumps(snapshot),))
        store.prune_objects(conn, now=time.time() + store.RETENTION_SECONDS * 2)
        assert store.get_result_object(conn, event['source_object_id']) == json.dumps({'summary': 'exact bytes'}).encode()


def test_retention_horizon_starts_after_latest_canonical_reconciliation(owner):
    event = launch()
    optional(event)
    source._persist_completion(event, {'summary': 'old consumption, recent reconciliation'})
    admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert admission.accept_event(event)
    consume(owner, event)
    now = time.time()
    with source._transaction() as conn:
        conn.execute('UPDATE supervision_admissions SET updated_at=1')
        conn.execute('UPDATE delegation_result_objects SET settled_at=1')
        conn.execute('UPDATE delegation_controls SET updated_at=?', (now,))
        store.prune_objects(conn, now=now + store.RETENTION_SECONDS - 1)
        assert store.get_result_object(conn, event['source_object_id'])
        store.prune_objects(conn, now=now + store.RETENTION_SECONDS + 1)
        with pytest.raises(store.AdmissionError):
            store.get_result_object(conn, event['source_object_id'])


def test_invalid_snapshot_is_protected_and_cannot_suppress(owner):
    event = launch()
    optional(event)
    with source._transaction() as conn:
        conn.execute("UPDATE delegation_controls SET snapshot_json='not-json'")
        assert store.protected_control_sessions(conn) == {'target'}
    assert not source.get_launch_control('job')['retainable']
    assert not owner.delete_session_if_empty('target')
