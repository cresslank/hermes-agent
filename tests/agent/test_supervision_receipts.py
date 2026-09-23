"""Canonical bounded work/settlement persistence, no raw semantic payloads."""
import json
import sqlite3
import time

import pytest

from agent.supervision_types import Action
from agent.supervision_policy import SupervisionRuntime
from agent import supervision_receipts as store
from tests.agent.supervision_test_support import rig, accept, proposal


def opportunity(rig):
    runtime = accept(rig.agent, '- Keep private test content out of receipt metadata.')
    runtime.observe('incident', {}, target_id='effect', evidence_refs=('exact:owner:ref',), actions=(Action.ADVISE,))
    event = rig.events[-1]
    return runtime, proposal(rig, event, refs=['exact:owner:ref'])


def read(rig):
    with sqlite3.connect(rig.agent._session_db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM supervision_receipts')]


def test_accepted_selected_effect_restart_no_raw_payload(rig):
    runtime, value = opportunity(rig)
    assert rig.facade.submit(value)['status'] == 'accepted'
    assert read(rig)[0]['status'] == 'accepted'
    assert runtime.drain_at_safe_point()
    row, = read(rig)
    assert row['status'] == 'applied'
    assert 'private test content' not in json.dumps(row)
    assert row['evidence_json'] == '["exact:owner:ref"]'
    # A replacement host owner adopts compact history but cannot replay the effect.
    restarted = SupervisionRuntime(rig.agent, runtime.revision.profile, runtime.revision.lineage)
    restarted.revision = runtime.revision
    result = restarted.submit(__import__('agent.supervision_types', fromlist=['InterventionProposalV1']).InterventionProposalV1(
        **{**value, 'expected': runtime.revision}), rig.facade._registration)
    assert result.status == 'applied' and not restarted.pending


@pytest.mark.parametrize('fault', ['missing','busy','cap'])
def test_unavailable_receipts_preserve_baseline_and_never_queue(rig, monkeypatch, fault):
    runtime, value = opportunity(rig)
    connection = None
    if fault == 'missing': monkeypatch.setattr(rig.agent, '_session_db', None)
    elif fault == 'cap': monkeypatch.setattr(store, 'MAX_RECEIPTS', 0)
    else:
        connection = sqlite3.connect(rig.agent._session_db.db_path)
        connection.execute('BEGIN IMMEDIATE')
    try:
        receipt = rig.facade.submit(value)
        assert receipt['status'] == 'rejected' and receipt['reason'] == 'storage_unavailable'
        assert not runtime.pending and not runtime.drain_at_safe_point()
    finally:
        if connection: connection.close()


def test_selected_not_applied_survives_restart_and_retention(rig):
    runtime, value = opportunity(rig)
    assert rig.facade.submit(value)['status'] == 'accepted'
    with runtime.lock:
        selected = runtime._take('effect', {Action.ADVISE})
    assert selected and read(rig)[0]['status'] == 'selected'
    with sqlite3.connect(rig.agent._session_db.db_path) as conn:
        store.prune(conn, time.time() + 3 * store.RETENTION_SECONDS)
    assert read(rig)[0]['status'] == 'selected'  # unresolved effects cannot be evicted
    runtime.receipts.clear()
    assert rig.facade.submit(value)['status'] == 'selected'
    assert not runtime.pending


def test_terminal_retention_and_explicit_deletion(rig):
    runtime, value = opportunity(rig)
    rig.facade.submit(value)
    runtime.drain_at_safe_point()
    runtime.finish_turn()
    with sqlite3.connect(rig.agent._session_db.db_path) as conn:
        store.prune(conn, time.time() + 3 * store.RETENTION_SECONDS)
        assert conn.execute('SELECT count(*) FROM supervision_receipts').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM supervision_work').fetchone()[0] == 0
    runtime, value = opportunity(rig)
    rig.facade.submit(value)
    rig.agent._session_db.delete_session(rig.agent.session_id)
    assert read(rig) == []
    assert not runtime.drain_at_safe_point()
    assert not store.record_work(runtime)


def test_automatic_empty_and_age_pruning_preserves_unresolved_receipts(rig):
    runtime, value = opportunity(rig)
    rig.facade.submit(value)
    with runtime.lock:
        runtime._take('effect', {Action.ADVISE})
    assert not rig.agent._session_db.delete_session_if_empty(rig.agent.session_id)
    with sqlite3.connect(rig.agent._session_db.db_path) as conn:
        conn.execute("UPDATE sessions SET ended_at=?,started_at=?,end_reason='closed' WHERE id=?",
                     (time.time()-1000000,time.time()-1000000,rig.agent.session_id))
    assert rig.agent._session_db.prune_sessions(older_than_days=0, exclude_active_write_guards=True) == 0
    assert read(rig)[0]['status'] == 'selected'
    assert rig.agent._session_db.delete_session(rig.agent.session_id)
    assert not read(rig)


def test_owner_selection_storage_loss_after_dequeue_preserves_baseline(rig, monkeypatch):
    from agent.supervision_types import OwnerRequestV1
    runtime = accept(rig.agent, '- Rank exact source refs')
    def consume(event):
        rig.facade.submit(proposal(rig, event, action='rank_candidates', template=None,
            owner='retrieval', refs=['a'], candidate_ids=['b', 'a']))
    rig.facade._registration.consumer = consume
    original = runtime._take
    def take(*args):
        entry = original(*args)
        monkeypatch.setattr(store, 'record', lambda *args, **kwargs: False)
        return entry
    monkeypatch.setattr(runtime, '_take', take)
    request = OwnerRequestV1('retrieval', 'rank', runtime.revision,
        ({'id': 'a'}, {'id': 'b'}), runtime.clock()+1, data_policy=('public_source',))
    decision = rig.facade.rank_candidates(request)
    assert not decision.applied and decision.candidate_ids == ('a', 'b')
    assert not runtime.incidents
    assert read(rig)[0]['status'] == 'selected'


@pytest.mark.parametrize('veto', [False, True])
def test_exact_owner_acknowledgment_protocol_uses_common_store(rig, veto):
    import hashlib
    runtime, value = opportunity(rig)
    assert rig.facade.submit(value)['status'] == 'accepted'
    with runtime.lock:
        selected, _ = runtime._take('effect', {Action.ADVISE})
        receipt = runtime._settle(selected, 'accepted', 'owner_selected')
        assert receipt.status == 'accepted'
        assert read(rig)[0]['reason'] == 'owner_selected'
        assert read(rig)[0]['status'] == 'accepted'
        digest = hashlib.sha256(b'exact consumed owner view').hexdigest()
        runtime._settle(selected, 'rejected' if veto else 'applied',
                        'owner_postvalidation' if veto else 'owner_consumed:' + digest)
    row, = read(rig)
    assert row['status'] == ('rejected' if veto else 'consumed')
    assert row['reason'] == ('owner_postvalidation' if veto else 'owner_consumed:' + digest)
    runtime.receipts.clear()
    assert rig.facade.submit(value)['status'] == row['status']
    assert not runtime.pending


@pytest.mark.parametrize('field,value', [('owner', 'changed'), ('incident_id', 'changed'),
    ('evidence_refs', ('different:ref',)), ('expires_at_monotonic', 99999999)])
def test_receipt_settlement_cannot_rebind_identity(rig, field, value):
    from dataclasses import replace
    runtime, wire = opportunity(rig)
    rig.facade.submit(wire)
    selected, _ = runtime._take('effect', {Action.ADVISE})
    changed = replace(selected, **{field: value})
    receipt = runtime._settle(changed, 'applied', 'owner_settlement')
    assert receipt.status == 'unknown'
    assert read(rig)[0]['status'] == 'selected'


def test_canonical_reconciliation_installs_additive_tables_and_delete_trigger():
    from hermes_state_schema import reconcile_state_schema
    conn = sqlite3.connect(':memory:')
    try:
        reconcile_state_schema(conn)
        first = conn.execute("SELECT sql FROM sqlite_master WHERE name IN ('supervision_work','supervision_receipts','supervision_work_delete') ORDER BY name").fetchall()
        reconcile_state_schema(conn)
        assert len(first) == 3
        assert first == conn.execute("SELECT sql FROM sqlite_master WHERE name IN ('supervision_work','supervision_receipts','supervision_work_delete') ORDER BY name").fetchall()
    finally: conn.close()
