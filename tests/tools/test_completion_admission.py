"""Owner source retention, immutable finding production and disposition fallbacks."""
import json
import queue
import sqlite3
import time

import pytest

from agent import completion_admission as admission
from agent import supervision_store as store
from tools import async_delegation as source
from tests.agent.test_supervision_store import owner, launch, ready, optional, consume, row, control_snapshot, write_control


def test_fixed_object_and_profile_budgets_never_evict_pins(owner):
    assert store.OBJECT_LIMIT == 4 * 1024 * 1024
    assert store.PROFILE_LIMIT == 64 * 1024 * 1024
    payload = b'x' * store.OBJECT_LIMIT
    with source._transaction() as conn:
        objects = [store.put_result_object(conn, launch_id=f'large-{i}', source_id='final', subtype='final',
            session_id='target', payload=payload) for i in range(16)]
        store.protect_optional(conn, objects[0])  # Exactly 64MiB is admissible.
        extra = store.put_result_object(conn, launch_id='over-profile',source_id='final',subtype='final',
            session_id='target',payload=b'x')
        with pytest.raises(store.RetentionCapacity):
            store.protect_optional(conn, extra)
        for object_id in objects:
            assert store.get_result_object(conn, object_id) == payload
        oversized = store.put_result_object(conn,launch_id='over-object',source_id='final',subtype='final',
            session_id='target',payload=payload+b'x')
        with pytest.raises(store.RetentionCapacity):
            store.protect_optional(conn,oversized)
        assert conn.execute('SELECT count(*) FROM delegation_result_objects').fetchone()[0] == 18


def test_effect_receipts_protect_after_consumption_until_owner_settlement(owner):
    event, _ = ready()
    with source._transaction() as conn:
        store.pin_result_effect(conn,event['source_object_id'],'cleanup:owned-process')
    assert admission.accept_event(event)
    consume(owner,event)
    with source._transaction() as conn:
        store.prune_objects(conn,now=time.time()+store.RETENTION_SECONDS*2)
        assert store.get_result_object(conn,event['source_object_id'])
        assert store.settle_result_effect(conn,event['source_object_id'],'cleanup:owned-process')
        store.prune_objects(conn,now=time.time()+store.RETENTION_SECONDS*2)
        with pytest.raises(store.AdmissionError):
            store.get_result_object(conn,event['source_object_id'])


def test_committed_finding_is_not_rendered_as_failure_or_final(owner):
    from tools.process_registry_notifications import format_process_notification, async_delegation_display_text
    launch()
    event=source.commit_finding('job',finding_id='early:1',source_receipt='committed:1',payload=b'exact finding')
    text=format_process_notification(event)
    assert 'ASYNC DELEGATION FINDING' in text
    assert 'final batch remains outstanding' in text
    assert 'not independently verified' in text
    assert 'did not complete successfully' not in text
    assert async_delegation_display_text(event).startswith('Subagent Finding (Provisional)')


def test_source_immutable_update_and_final_replay(owner):
    event, _ = ready()
    assert admission.accept_event(event)
    consume(owner,event)
    original = source.get_durable_delegation('job')['result']
    source._persist_completion(event,original)
    assert source.get_durable_delegation('job')['delivery_state'] == 'delivered'
    with pytest.raises(store.AdmissionError):
        source._persist_completion(event,{'summary':'replacement'})
    with pytest.raises(sqlite3.IntegrityError), source._transaction() as conn:
        conn.execute('UPDATE delegation_result_objects SET payload=? WHERE object_id=?',(b'replacement',event['source_object_id']))


def test_finding_identity_cannot_change_and_never_settles_final(owner):
    launch()
    finding = source.commit_finding('job',finding_id='one',source_receipt='receipt:1',payload=b'fact')
    assert admission.accept_event(finding)
    consume(owner,finding)
    with pytest.raises(store.AdmissionError):
        source.commit_finding('job',finding_id='one',source_receipt='receipt:1',payload=b'changed')
    assert source.get_durable_delegation('job')['delivery_state'] == 'pending'


def test_exact_bounded_view_and_unexpired_defer(owner, monkeypatch):
    event = launch()
    source._persist_completion(event,{'summary':'exact qualified finding'})
    optional(event)
    raw = json.dumps({'summary':'exact qualified finding'}).encode()
    start = raw.index(b'exact')
    monkeypatch.setattr(admission,'_policy',lambda *_: admission.CompletionDecision('deliver_bounded_view',True,(start,start+len(b'exact qualified finding'))))
    prepared = admission.prepare_event(event,source.claim_event_delivery(event,'owner'),'target')
    assert prepared.disposition == 'deliver_bounded_view'
    view = admission.presentation_text(event,'original')
    assert view.endswith('exact qualified finding') and event['source_object_id'] in view
    later = launch('deferred')
    source._persist_completion(later,{'summary':'later'})
    monkeypatch.setattr(admission,'_policy',lambda *_: admission.CompletionDecision('defer',deadline=time.monotonic()+0.14))
    prepared = admission.prepare_event(later,source.claim_event_delivery(later,'owner'),'target')
    assert prepared.disposition == 'defer' and not prepared.present
    assert not admission.accept_event(later)
    with source._transaction() as conn:
        conn.execute('UPDATE supervision_admissions SET not_before=0 WHERE delivery_id=?',(later['supervision_delivery_id'],))
    hints = queue.Queue()
    admission.requeue_due(hints,'target')
    recovered = hints.get_nowait()
    token = source.claim_event_delivery(recovered,'idle')
    assert admission.prepare_event(recovered,token,'target').present
    assert admission.accept_event(recovered)
    consume(owner,recovered)


@pytest.mark.parametrize('decision',[
    admission.CompletionDecision('defer', deadline=1.0),
    admission.CompletionDecision('deliver_bounded_view',True,(-1,100)),
    admission.CompletionDecision('deliver_bounded_view',True,(0,99999999)),
])
def test_stale_or_invalid_window_is_normal_delivery(owner,monkeypatch,decision):
    event=launch()
    source._persist_completion(event,{'summary':'data'})
    optional(event)
    monkeypatch.setattr(admission,'_policy',lambda *_:decision)
    prepared=admission.prepare_event(event,source.claim_event_delivery(event,'owner'),'target')
    assert prepared.disposition == 'deliver_unchanged'


def test_receipt_replay_does_not_reclassify(owner,monkeypatch):
    calls=[]
    monkeypatch.setattr(admission,'_policy',lambda *a:calls.append(a) or ('deliver_unchanged',False))
    event,_=ready()
    admission.prepare_event(event,event['_supervision_claim'],'target')
    assert len(calls)==1


def test_authorized_parked_promotion_keeps_identity(owner,monkeypatch):
    event=launch()
    source._persist_completion(event,{'summary':'optional'})
    optional(event)
    monkeypatch.setattr(admission,'_policy',lambda *_:('retain_without_wake',True))
    admission.prepare_event(event,source.claim_event_delivery(event,'owner'),'target')
    delivery_id=event['supervision_delivery_id']
    with source._transaction() as conn:
        assert b'optional' in store.read_retained(conn,delivery_id,session_id='target')
        store.promote_parked(conn,delivery_id,expected_generation=1)
    assert admission.accept_event(event)
    consume(owner,event)
    assert row(event)['delivery_id']==delivery_id


def test_launch_control_is_committed_before_the_real_source_dispatch(owner, monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace
    monkeypatch.setattr(source, '_records', {})
    monkeypatch.setattr(source, '_new_delegation_id', lambda: 'controlled')
    seen=[]
    class InlineExecutor:
        def submit(self, fn):
            future=Future()
            fn()
            future.set_result(None)
            return future
    monkeypatch.setattr(source, '_get_executor', lambda *_: InlineExecutor())
    snapshot = control_snapshot('actual-child-id')
    with source._transaction() as conn:
        write_control(conn, snapshot)
    def runner():
        with source._transaction() as conn:
            assert json.loads(conn.execute("SELECT control_child_ids FROM async_delegations WHERE delegation_id='controlled'").fetchone()[0]) == ['actual-child-id']
        seen.append(source.get_launch_control('controlled'))
        return {'status':'completed','summary':'read-only result'}
    handle=source.dispatch_async_delegation(goal='read',context=None,toolsets=[],role='default',model=None,
        session_key='target',parent_session_id='target',runner=runner,control_children=[SimpleNamespace(_subagent_id='actual-child-id')])
    assert handle['status']=='dispatched'
    assert seen==[{'obligation':'optional','children':[snapshot],'retainable':True}]


def test_source_persistence_failure_never_starts_worker_and_rolls_back(owner, monkeypatch):
    monkeypatch.setattr(source, '_records', {})
    monkeypatch.setattr(source, '_new_delegation_id', lambda: 'invalid-control')
    calls=[]
    def fail(*args):
        raise sqlite3.OperationalError('source unavailable')
    monkeypatch.setattr(source, '_persist_dispatch', fail)
    result=source.dispatch_async_delegation(goal='read',context=None,toolsets=[],role='default',model=None,
        session_key='target',parent_session_id='target',runner=lambda:calls.append('ran'),
        control_children=[object()])
    assert result['status']=='rejected'
    assert not calls and not source._records
    assert source.get_durable_delegation('invalid-control') is None


def test_migrated_legacy_schema_has_control_columns_and_protected_tables(owner):
    from hermes_state_schema import reconcile_state_schema
    conn=sqlite3.connect(':memory:')
    try:
        conn.execute('''CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT,
            state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
            updated_at REAL NOT NULL, event_json TEXT, result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending', delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL, owner_pid INTEGER, owner_started_at INTEGER, task_json TEXT,
            delivery_claim TEXT, delivery_claimed_at REAL, origin_session_id TEXT NOT NULL DEFAULT '')''')
        # Canonical reconciler handles additive control columns, no shadow DDL.
        reconcile_state_schema(conn)
        columns={r[1] for r in conn.execute('PRAGMA table_info(async_delegations)')}
        assert 'control_child_ids' in columns
        assert not {'launch_obligation','consumer_refs_json','control_revision'} & columns
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_delegation_controls_parent'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='supervision_admissions'").fetchone()
    finally:
        conn.close()
