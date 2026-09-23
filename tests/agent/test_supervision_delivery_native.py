"""Real full plugin/strict HTTP -> public source -> CLI -> native turn append.

All sources, config, identities, and HTTP answers here are synthetic fixtures.
"""
import json
import logging
import queue
import sqlite3
from concurrent.futures import Future
from types import SimpleNamespace

import httpx
import pytest

from jev_supervisor.config import Config
from jev_supervisor.host_adapter import NativeHostBridge
from jev_supervisor.transport import Transport
from agent import completion_admission as admission
from agent import supervision_store as store
from agent.turn_context import _stage_turn_user_message
from agent.supervision_types import Action
from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
from tools import async_delegation as source
from tools.process_registry import process_registry
from tests.agent.supervision_test_support import rig, accept, commit_plan
from tests.agent.test_supervision_store import control_snapshot, write_control


class Credential:
    def __init__(self, profile): self.profile = profile
    def bearer(self, profile, endpoint):
        assert profile == self.profile and endpoint == 'https://api.typesafe.ai/v1/systemone'
        return 'offline-test-not-a-secret'


class Executor:
    def __init__(self): self.jobs = []
    def submit(self, job):
        future = Future()
        self.jobs.append((job, future))
        return future
    def finish(self):
        job, future = self.jobs.pop(0)
        job()
        future.set_result(None)


@pytest.fixture
def native(rig, monkeypatch):
    for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2','httpcore.proxy','httpcore.socks','httpcore.connection_pool'):
        logger = logging.getLogger(name)
        for key in ('level','disabled','propagate'):
            monkeypatch.setattr(logger, key, getattr(logger, key))
    fields = 'target_id changed evidence_refs result conclusion requirements findings finding decisions'.split()
    policy = {'id':'delivery-fixture','profile':str(rig.home),'fields':{k:'synthetic' for k in fields},'sources':{},'fixture':True}
    rig.config['supervision']['plugins']['fixture-supervisor']['egress_policy'] = policy
    (rig.home/'config.yaml').write_text(json.dumps(rig.config))
    calls, mode = [], {'relation':'corroborates'}
    async def handle(request):
        assert str(request.url) == 'https://api.typesafe.ai/v1/systemone'
        body = json.loads(request.content)
        assert set(body) == {'model','state','questions'} and body['model'] == 'jev-1.13.0'
        calls.append(body)
        answers = {}
        for key, q in body['questions'].items():
            if q['type'] == 'choice':
                selected = mode['relation'] if key.startswith('F02') else 'blocks_next_step'
                assert selected in q['criteria']
                answers[key] = {'type':'choice','choice':selected,'confidence':1.0,
                    'probabilities':{k:float(k == selected) for k in q['criteria']}}
            else:
                assert q['type'] == 'noul'
                answers[key] = {'type':'noul','noul':1.0}
        if mode.get('callback'): mode['callback']()
        return httpx.Response(200,json={'model':body['model'],'answers':answers,'usage':{}})
    cfg = Config(str(rig.home), True, policy_id='delivery-fixture', allowed_classes={'synthetic'}, fixture_policy=True)
    bridge = NativeHostBridge(rig.facade, cfg, None, transport=Transport(cfg, Credential(str(rig.home)), http_transport=httpx.MockTransport(handle)))
    assert bridge.start()
    executor = Executor()
    monkeypatch.setattr(source, '_get_executor', lambda *_: executor)
    monkeypatch.setattr(source, '_records', {})
    monkeypatch.setattr(source, '_db_path', lambda: rig.home/'state.db')
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    monkeypatch.setattr(admission, '_policy', None)
    cli = CLIProcessNotificationsMixin()
    cli.session_id, cli._session_db, cli._pending_input = rig.agent.session_id, rig.agent._session_db, queue.Queue()
    yield SimpleNamespace(rig=rig, bridge=bridge, calls=calls, mode=mode, executor=executor, cli=cli)
    while executor.jobs: executor.finish()
    bridge.close()


def controls(native, ids):
    with source._transaction() as conn:
        for child in ids:
            value = control_snapshot(child)
            value['parent_session_id'] = native.cli.session_id
            value['consumers'][0]['ref'] = 'parent:' + native.cli.session_id
            write_control(conn, value)
    return [SimpleNamespace(_subagent_id=child) for child in ids]


def final_launch(native, *, required=False):
    rt = accept(native.rig.agent, '- Check `target.py`.')
    child = controls(native, ['child']) if not required else []
    handle = source.dispatch_async_delegation(goal='Check `target.py`',context=None,toolsets=[],role='default',model=None,
        session_key=native.cli.session_id,parent_session_id=native.cli.session_id,control_children=child,
        runner=lambda: {'status':'completed','summary':'Exact provisional finding for target.py'})
    assert handle['status'] == 'dispatched'
    native.rig.agent._session_db.append_message(native.cli.session_id, 'assistant', 'Previously delivered conclusion.')
    native.executor.finish()
    rt.finish_turn()
    return rt, handle['delegation_id']


def rows(native):
    with source._transaction() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM supervision_receipts ORDER BY created_at')]


def consume(native):
    item = native.cli._pending_input.get_nowait()
    # Ordinary CLI promotion and actual native turn staging own the consumption.
    text, _, _ = native.cli._tui_unwrap_input(item)
    result, _ = _stage_turn_user_message(native.rig.agent, text, None, None, None, None, item.supervision_metadata)
    return result, item


def test_final_retention_is_native_persisted_park_not_ack(native):
    rt, launch = final_launch(native)
    native.cli._drain_process_notifications('fixture-cli')
    assert len(native.calls) == 1, json.dumps(native.bridge.supervisor.inspect())
    assert native.cli._pending_input.empty(), json.dumps(native.bridge.supervisor.inspect())
    assert source.get_durable_delegation(launch)['delivery_state'] == 'retained'
    receipt, = rows(native)
    assert receipt['status'] == 'applied' and receipt['reason'] == 'admission_parked'
    with source._transaction() as conn:
        item = store.get_admission(conn, receipt['delivery_id'])
        assert item['state'] == 'parked'
        assert b'Exact provisional' in store.read_retained(conn, item['delivery_id'], session_id=native.cli.session_id)
    assert len(native.rig.agent._session_db.get_messages(native.cli.session_id)) == 1
    assert rt.closed  # classification never reopens the finished agent task


def test_final_bounded_view_selected_then_native_consumed(native):
    native.mode['relation'] = 'fills_open_gap'
    _rt, launch = final_launch(native)
    native.cli._drain_process_notifications('fixture-cli')
    assert len(native.calls) == 1, json.dumps(native.bridge.supervisor.inspect())
    assert rows(native), json.dumps(native.bridge.supervisor.inspect())
    receipt, = rows(native)
    assert receipt['action'] == 'final_bounded_view' and receipt['status'] == 'selected'
    assert source.get_durable_delegation(launch)['delivery_state'] == 'transferred'
    result, item = consume(native)
    assert 'Exact bounded background result' in str(item)
    assert 'provisional' in str(item)
    assert result['_db_persisted'] is True
    assert rows(native)[0]['status'] == 'consumed'
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
    again, _ = _stage_turn_user_message(native.rig.agent, item, None, None, None, None, item.supervision_metadata)
    assert again['_row_id'] == result['_row_id']


@pytest.mark.parametrize('condition', ['required','no_grant','unload','expired','storage','capacity','stale','conclusion_changed','object_cap'])
def test_final_missing_authority_preserves_ordinary_delivery(native, monkeypatch, condition):
    rt, launch = final_launch(native, required=condition == 'required')
    if condition == 'no_grant':
        native.rig.facade._registration.grants = frozenset({'observe'})
    if condition == 'object_cap': monkeypatch.setattr(store, 'OBJECT_LIMIT', 1)
    if condition == 'conclusion_changed':
        native.mode['callback'] = lambda: native.rig.agent._session_db.append_message(native.cli.session_id, 'assistant', 'Newer final supersedes comparison.')
    if condition == 'unload': native.bridge.close()
    if condition == 'expired': native.mode['callback'] = lambda: setattr(rt, 'clock', lambda: float('inf'))
    if condition == 'storage':
        from agent import supervision_receipts
        monkeypatch.setattr(supervision_receipts, 'record', lambda *a, **k: False)
    if condition == 'capacity':
        from agent import supervision_receipts
        monkeypatch.setattr(supervision_receipts, 'MAX_RECEIPTS', 0)
    if condition == 'stale':
        from agent.supervision_context import steer_from_user
        native.mode['callback'] = lambda: steer_from_user(native.rig.agent, '- Replacement requirement')
    native.cli._drain_process_notifications('fixture-cli')
    assert not native.cli._pending_input.empty()
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
    assert all(r['reason'] != 'admission_parked' for r in rows(native))


@pytest.mark.parametrize('final_first', [False, True])
def test_actual_finished_child_produces_finding_before_final_and_next_turn_consumes(native, final_first):
    rt, _, _ = commit_plan(native.rig, native.rig.home/'plan.md')
    children = controls(native, ['first','second'])
    handle = source.dispatch_async_delegation_batch(goals=['Inspect `target.py`','Inspect `target.py`'],context=None,
        toolsets=[],role='default',model=None,session_key=native.cli.session_id,parent_session_id=native.cli.session_id,
        control_children=children,runner=lambda: {'results':[{'task_index':0,'status':'completed','summary':'Exact child finding'}]})
    assert handle['status'] == 'dispatched'
    launch = handle['delegation_id']
    source.record_unit_child(launch, {'task_index':0,'status':'completed','summary':'Exact child finding'})
    with native.bridge._lock: pending = tuple(native.bridge._pending)
    for future in pending: future.result(timeout=2)
    assert any('finding' in c['state']['facts'] for c in native.calls), json.dumps(native.bridge.supervisor.inspect())
    assert not process_registry.completion_queue.qsize()  # acceptance has not queued delivery
    rt.drain_at_safe_point()
    assert process_registry.completion_queue.qsize() == 1
    assert source.get_durable_delegation(launch)['delivery_state'] == 'pending'
    if final_first:
        finding_event = process_registry.completion_queue.get_nowait()
        native.executor.finish()
        native.cli._drain_process_notifications('fixture-cli')
        final, _ = consume(native)
        assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
        process_registry.completion_queue.put(finding_event)
    native.cli._drain_process_notifications('fixture-cli')
    first, item = consume(native)
    assert 'final batch remains outstanding' in str(item)
    assert rows(native)[0]['status'] == 'consumed'
    if not final_first:
        assert source.get_durable_delegation(launch)['delivery_state'] == 'pending'
        native.executor.finish()
        native.cli._drain_process_notifications('fixture-cli')
        final, _ = consume(native)
    assert first['_row_id'] != final['_row_id']
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


def test_deadline_expiring_during_admission_lock_preserves_required_final(native, monkeypatch):
    from contextlib import contextmanager
    rt, launch = final_launch(native)
    original = source._transaction
    @contextmanager
    def transaction():
        with original() as conn:
            if conn.execute("SELECT 1 FROM supervision_receipts WHERE status='selected'").fetchone():
                monkeypatch.setattr(rt, 'clock', lambda: float('inf'))
            yield conn
    monkeypatch.setattr(source, '_transaction', transaction)
    native.cli._drain_process_notifications('fixture-cli')
    assert len(native.calls) == 1
    assert rows(native)[0]['status'] == 'no_op'
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


def test_receipt_failure_at_admission_rolls_back_optional_park_not_final(native, monkeypatch):
    from agent import supervision_delivery
    rt, launch = final_launch(native)
    attempts = []
    original = store.persist_admission
    def persist(*args, **kwargs):
        attempts.append((kwargs.get('disposition'), kwargs.get('defer_until')))
        return original(*args, **kwargs)
    def unavailable(*args):
        raise sqlite3.OperationalError('synthetic receipt storage unavailable')
    monkeypatch.setattr(store, 'persist_admission', persist)
    monkeypatch.setattr(supervision_delivery, 'persist_selection', unavailable)
    native.cli._drain_process_notifications('fixture-cli')
    assert len(attempts) == 2
    assert attempts[0][0] == 'retain_without_wake'
    assert attempts[1][0] == 'deliver_unchanged'
    assert attempts[0][1] == attempts[1][1] and attempts[0][1] is not None
    assert len(native.calls) == 1
    assert rows(native)[0]['status'] == 'no_op'
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


def test_forged_receipt_never_commits_or_wakes(native):
    _rt, launch = final_launch(native)
    with pytest.raises(store.AdmissionError):
        source.commit_finding(launch, finding_id='fabricated', source_receipt='trust-me', payload=b'claim', verified=True)
    with source._transaction() as conn:
        assert not conn.execute("SELECT 1 FROM delegation_result_objects WHERE subtype='finding'").fetchone()


def test_provisional_material_comparison_delivers_final_not_finding_or_advice(native):
    native.mode['relation'] = 'corrects_material_error'
    _rt, launch = final_launch(native)
    native.cli._drain_process_notifications('fixture-cli')
    receipt, = rows(native)
    assert receipt['action'] == 'deliver_final' and receipt['status'] == 'selected'
    _, item = consume(native)
    assert 'ASYNC DELEGATION FINDING' not in str(item)
    assert rows(native)[0]['status'] == 'consumed'
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


@pytest.mark.parametrize('boundary', ['after_commit_before_enqueue','after_accept_before_append','after_append'])
def test_native_delivery_restart_crash_points_no_reclassification(native, monkeypatch, boundary):
    from hermes_state import SessionDB
    native.mode['relation'] = 'fills_open_gap'
    _rt, launch = final_launch(native)
    if boundary == 'after_commit_before_enqueue':
        original = admission.accept_event
        monkeypatch.setattr(admission, 'accept_event', lambda *a, **kw: False)
        native.cli._drain_process_notifications('fixture-cli')
        monkeypatch.setattr(admission, 'accept_event', original)
    else:
        native.cli._drain_process_notifications('fixture-cli')
    prior = consume(native)[0] if boundary == 'after_append' else None
    assert rows(native)[0]['status'] == ('consumed' if prior else 'selected')
    # Lose every process-local queue hint and disable the classifier before recovery.
    process_registry.completion_queue = queue.Queue()
    native.cli._pending_input = queue.Queue()
    native.bridge.close()
    with source._transaction() as conn:
        conn.execute('UPDATE supervision_admissions SET lease_expires=0')
    source.restore_undelivered_completions(process_registry.completion_queue)
    if prior:
        assert process_registry.completion_queue.empty()
        return
    native.cli._drain_process_notifications('fixture-cli')
    reopened = SessionDB(native.rig.home/'state.db')
    native.rig.agent._session_db = reopened
    try:
        consume(native)
        assert rows(native)[0]['status'] == 'consumed'
        assert len(native.calls) == 1
        assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
    finally:
        native.rig.agent._session_db = native.cli._session_db
        reopened.close()


@pytest.mark.parametrize('change', ['job_finished','consumer_changed','deleted','unloaded','storage_cap','deadline_during_commit'])
def test_early_finding_rechecks_actual_owner_before_enqueue(native, monkeypatch, change):
    rt, _, _ = commit_plan(native.rig, native.rig.home/'plan.md')
    children = controls(native, ['first','second'])
    handle = source.dispatch_async_delegation_batch(goals=['Inspect `target.py`','Other job'],context=None,toolsets=[],role='default',model=None,
        session_key=native.cli.session_id,parent_session_id=native.cli.session_id,control_children=children,
        runner=lambda: {'results':[{'task_index':0,'status':'completed','summary':'Exact finding'}]})
    launch = handle['delegation_id']
    source.record_unit_child(launch, {'task_index':0,'status':'completed','summary':'Exact finding'})
    with native.bridge._lock: pending = tuple(native.bridge._pending)
    for future in pending: future.result(timeout=2)
    assert len(native.calls) == 1
    if change == 'job_finished': native.executor.finish()
    elif change == 'consumer_changed':
        from tools.todo_tool import todo_tool
        todo_tool([{'id':'step','content':'Resolved target','status':'completed'}], store=native.rig.agent._todo_store)
    elif change == 'deleted': native.rig.agent._session_db.delete_session(native.cli.session_id)
    elif change == 'storage_cap': monkeypatch.setattr(store, 'PROFILE_LIMIT', 0)
    elif change == 'deadline_during_commit':
        from contextlib import contextmanager
        original = source._transaction
        @contextmanager
        def transaction():
            with original() as conn:
                monkeypatch.setattr(rt, 'clock', lambda: float('inf'))
                yield conn
        monkeypatch.setattr(source, '_transaction', transaction)
    else: native.bridge.close()
    rt.drain_at_safe_point()
    with source._transaction() as conn:
        assert not conn.execute("SELECT 1 FROM supervision_admissions WHERE subtype='finding'").fetchone()
    if change == 'job_finished': assert process_registry.completion_queue.qsize() == 1  # required final only


def test_actual_child_read_tool_committed_row_produces_early_finding(native, monkeypatch):
    from agent.supervision_policy import runtime_for_agent
    from agent.supervision_context import accepted_input_origin
    from tests.agent.test_tool_call_incremental_persistence import _make_agent, _make_tool_defs, _mock_tool_call
    from agent.tool_executor import execute_tool_calls_segmented
    monkeypatch.chdir(native.rig.home)
    monkeypatch.setattr('agent.title_generator.maybe_auto_title', lambda *a, **kw: None)
    (native.rig.home/'target.py').write_text('exact tool source')
    rt, _, _ = commit_plan(native.rig, native.rig.home/'plan.md')
    children = controls(native, ['first','second'])
    handle = source.dispatch_async_delegation_batch(goals=['Inspect `target.py`','Other job'],context=None,toolsets=[],role='default',model=None,
        session_key=native.cli.session_id,parent_session_id=native.cli.session_id,control_children=children,
        runner=lambda: {'results':[]})
    child = _make_agent()
    child.session_id, child._subagent_id, child._session_db = 'child-session', 'first', native.rig.agent._session_db
    child._session_db.create_session(child.session_id, 'subagent', parent_session_id=native.cli.session_id)
    child._session_db_created, child._persist_disabled = True, False
    child._last_flushed_db_idx = 0
    child._flushed_db_message_ids = set()
    child.tools = _make_tool_defs('read_file')
    runtime = runtime_for_agent(child, create=True)
    runtime.accept_instruction(accepted_input_origin('- Inspect `target.py`',kind='cli'))
    call = _mock_tool_call(name='read_file', arguments=json.dumps({'path':'target.py'}), call_id='owned-read')
    messages = [{'role':'assistant','content':None,'tool_calls':[{'id':'owned-read','type':'function',
        'function':{'name':'read_file','arguments':json.dumps({'path':'target.py'})}}]}]
    execute_tool_calls_segmented(child, SimpleNamespace(content=None, tool_calls=[call]), messages, 'default')
    with native.bridge._lock: pending = tuple(native.bridge._pending)
    for future in pending: future.result(timeout=2)
    assert any('finding' in c['state']['facts'] for c in native.calls), json.dumps(native.bridge.supervisor.inspect())
    rt.drain_at_safe_point()
    native.cli._drain_process_notifications('fixture-cli')
    consume(native)
    assert rows(native)[0]['status'] == 'consumed'
    assert source.get_durable_delegation(handle['delegation_id'])['delivery_state'] == 'pending'
    child._session_db = None
    child.close()
