"""Bridge C vertical launch, durable fences and deterministic concurrency tests.

Only model execution/credentials and presentation are substituted. The positive
launch uses ordinary delegate_task -> _build_children -> owner validation -> the
real SQLite CAS adapter. No test injects a child attestation.
"""
import dataclasses
import json
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import subagent_lifecycle as lifecycle
from agent.owned_delegation import (
    CapabilityContract, Consumer, ControlDenied, OwnerGrant, ReadOnlyPolicy,
    SemanticEvidence, dispatch_fence, install_owner, scoped_file_policy,
)
from tools.delegation_control_store import SQLiteControlStore, ControlConflict

from hermes_state import SessionDB


class Child:
    def __init__(self, ident):
        self._subagent_id = ident
        self.session_id = ident
        self._delegate_depth = 1
        self._delegate_role = 'leaf'
        self.provider = 'synthetic'
        self.model = 'synthetic'
        self.tools = [{'function': {'name': n}} for n in ('read_file', 'terminal', 'execute_code', 'delegate_task')]
        self.valid_tool_names = {'read_file', 'terminal', 'execute_code', 'delegate_task'}
        self.stopped = threading.Event()
        self.closed = False

    def hard_interrupt(self, *args, **kwargs):
        self.stopped.set()

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from tools import delegate_tool as dt
    path = tmp_path / 'state.db'
    # Use the real canonical initializer now that delivery and lifecycle are joined.
    db = SessionDB(path)
    db.create_session('parent', 'cli')
    db.close()
    store = SQLiteControlStore(lambda: sqlite3.connect(path))
    parent = SimpleNamespace(session_id='parent', _delegate_depth=0)
    consumers = {'parent:parent': Consumer('parent:parent', 'optional'), 'research': Consumer('research', 'optional')}
    revision = [1, 1, 1]
    policy = scoped_file_policy((str(tmp_path),))
    owner = install_owner(parent, store=store, grant=OwnerGrant('test-profile', 'supervisor', True, True),
                          consumer_resolver=consumers.get, policy=policy, revision_provider=lambda: tuple(revision))
    children = []
    def build(**kwargs):
        child = Child('child-' + str(len(children)))
        children.append(child)
        return child
    monkeypatch.setattr(dt, '_build_child_preserving_parent_tools', build)
    monkeypatch.setattr(dt, '_load_config', lambda: {})
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a: dict(model='synthetic', provider='synthetic', base_url=None, api_key=None, api_mode=None))
    monkeypatch.setattr(dt, '_get_max_spawn_depth', lambda: 8)
    monkeypatch.setattr(dt, '_get_max_concurrent_children', lambda: 4)
    monkeypatch.setattr(dt, 'is_spawn_paused', lambda: False)
    monkeypatch.setattr(dt, '_oneshot_spawn_budget', lambda *a: None)
    monkeypatch.setattr(dt, '_announce_batch', lambda *a: None)
    monkeypatch.setattr(dt, '_capture_origin', lambda: ('', '', None, None, False))
    monkeypatch.setattr('tools.delegation_live_log.create_live_transcripts', lambda *a, **kw: (None, [], []))
    # Preserve ordinary launch and construction, stop exactly at scheduler I/O.
    batches = []
    monkeypatch.setattr(dt, '_run_batch', lambda batch, background: batches.append(batch) or json.dumps({'dispatched': True}))
    request = dict(obligation='optional', consumer_refs=['research'], consumer_set_closed=True, effect_policy_id=policy.policy_id)
    def launch(req='default', via_parent=None):
        task = {'goal': 'Inspect the public synthetic fixture'}
        if req == 'default':
            task['supervision'] = request.copy()
        elif req is not None:
            task['supervision'] = req
        result = json.loads(dt.delegate_task(tasks=[task], parent_agent=via_parent or parent))
        assert result.get('dispatched'), result
        return children[-1], owner.list_owned()[-1]
    return SimpleNamespace(owner=owner, parent=parent, store=store, consumers=consumers, revision=revision,
                           policy=policy, children=children, batches=batches, request=request, launch=launch, path=tmp_path,
                           deadline=time.monotonic() + 60)


def evidence(rig, handle, value=.01):
    return SemanticEvidence(tuple(rig.revision), tuple((c['ref'], value) for c in rig.owner.status(handle)['consumers']),
                            'no_remaining_consumer', .99, .99)


def propose(rig, handle, key, *, value=.01, feature='F01', expected=None):
    return rig.owner.request_semantic_cancel(handle, expected_revision=expected or rig.owner.status(handle)['control_revision'],
                                             evidence=evidence(rig, handle, value), idempotency_key=key, feature_id=feature,
                                             deadline=rig.deadline)


def prime(rig, handle):
    # Cancellation is decisive on its first request; advance only the test revision.
    assert not rig.owner.status(handle)['cancel_requested']
    rig.revision[2] += 1


@pytest.mark.parametrize('subset', [False, True])
def test_public_launch_links_unit_controls_before_executor_submission(rig, monkeypatch, subset):
    from tools import async_delegation as source
    from tools.delegate_tool_dispatch import _dispatch_unit

    first, _ = rig.launch()
    second, _ = rig.launch()
    tasks = [rig.batches[0].task_list[0], rig.batches[1].task_list[0]]
    children = [(0, tasks[0], first), (1, tasks[1], second)]
    unit = dataclasses.replace(rig.batches[0], task_list=tasks,
                               children=children[1:] if subset else children)
    expected_ids = [child._subagent_id for _, _, child in unit.children]
    futures = []
    observations = []
    monkeypatch.setattr(source, '_db_path', lambda: rig.path / 'state.db')
    monkeypatch.setattr(source, '_ensure_stale_monitor', lambda: None)

    class SchedulerBoundary:
        def submit(self, worker):
            # This is the scheduling boundary, not a mock source/control store.
            # The real public launch has already registered owner controls.
            with source._connect() as conn:
                row = conn.execute('SELECT control_child_ids, task_json FROM async_delegations '
                                   'WHERE delegation_id=?', ('linked-unit',)).fetchone()
                assert json.loads(row[0]) == expected_ids
                task = json.loads(row[1])
                assert task.get('task_indexes') == ([1] if subset else None)
            control = source.get_launch_control('linked-unit')
            assert [c['child_id'] for c in control['children']] == expected_ids
            assert not control['retainable']  # launching is not settlement
            observations.append(control)
            future = Future()
            futures.append(future)
            return future

    monkeypatch.setattr(source, '_get_executor', lambda _: SchedulerBoundary())
    try:
        result = _dispatch_unit(unit, 'linked-unit', None,
                                dict(session_key='parent', parent_session_id='parent'))
        assert result['status'] == 'dispatched', result
        assert len(observations) == 1
        assert SQLiteControlStore(lambda: sqlite3.connect(rig.path / 'state.db')).read(expected_ids[0])
    finally:
        for future in futures:
            future.cancel()  # release the real retirement reservation
        source._reset_for_tests()


def test_ordinary_delegate_launch_produces_durable_scoped_handle_and_metadata(rig):
    child, handle = rig.launch()
    service = lifecycle.SubagentLifecycleService(lambda: rig.parent)
    assert service.list_owned() == (handle,)
    state = service.owned_status(handle)
    assert state == rig.store.read(child._subagent_id)
    assert state['obligation'] == 'optional'
    assert state['consumer_set_closed'] is True
    assert {c['ref'] for c in state['consumers']} == {'research', 'parent:parent'}
    assert state['effect_class'] == 'read_only'
    assert state['effect_contracts'] == {'read_file': '1'}
    assert child.valid_tool_names == {'read_file', 'delegate_task'}
    assert not hasattr(handle, 'capability')
    state['obligation'] = 'required'  # snapshots are detached, not a mutation surface
    assert rig.owner.status(handle)['obligation'] == 'optional'
    with pytest.raises(ControlDenied):
        rig.owner.status(dataclasses.replace(handle, generation='forged'))
    with pytest.raises(ControlDenied):
        rig.owner.status(dataclasses.replace(handle, plugin_id='other'))


@pytest.mark.parametrize('feature', ['F01', 'F04'])
def test_native_stop_first_request_and_distinct_settlement(rig, feature):
    child, handle = rig.launch()
    expected = rig.owner.status(handle)['control_revision']
    receipt = propose(rig, handle, 'second', feature=feature)
    assert receipt.accepted and receipt.cancel_requested
    assert not receipt.settled and not receipt.processes_stopped and not receipt.effects_reconciled
    assert child.stopped.is_set()
    assert propose(rig, handle, 'second', feature=feature, expected=expected) == receipt
    with pytest.raises(ControlDenied):
        propose(rig, handle, 'second', feature=feature, expected=expected, value=.1)
    with pytest.raises(ControlDenied):
        with dispatch_fence(child, 'read_file', {'path': __file__}):
            pytest.fail('cancelled child dispatched')
    from tools.delegate_tool_child_run import _close_child
    _close_child(child, 'test cleanup')
    state = rig.store.read(handle.child_id)
    assert all(state[k] for k in ('cancel_requested', 'settled', 'processes_stopped', 'effects_reconciled'))


def test_native_stop_does_not_reinterpret_plugin_scores(rig):
    child, handle = rig.launch()
    assert propose(rig, handle, 'decided-by-plugin', value=.9).accepted
    assert child.stopped.is_set()
    assert rig.owner.status(handle)['candidate'] is None
    assert not propose(rig, handle, 'repeat').accepted


@pytest.mark.parametrize('case', ['legacy', 'required', 'unknown', 'unclosed', 'empty', 'missing', 'result', 'effects', 'cleanup'])
def test_unknown_or_required_obligations_never_semantically_cancel(rig, case):
    request = rig.request.copy()
    if case in ('required', 'unknown'):
        request['obligation'] = case
    elif case == 'unclosed':
        request['consumer_set_closed'] = False
    elif case == 'empty':
        request['consumer_refs'] = []
    elif case == 'missing':
        request['consumer_refs'] = ['not-registered']
    elif case in ('result', 'effects', 'cleanup'):
        rig.consumers['research'] = Consumer('research', 'optional', **{'requires_' + case: True})
    _, handle = rig.launch(None if case == 'legacy' else request)
    assert propose(rig, handle, 'one').reason == 'ineligible'
    rig.revision[2] += 1
    assert not propose(rig, handle, 'two', feature='F04').accepted


@pytest.mark.parametrize('name,args', [
    ('terminal', {'command': 'cat public.txt'}), ('execute_code', {'code': 'read_file(...)'}),
    ('browser_console', {'expression': 'document.title'}), ('opaque_plugin', {}),
    ('read_file', {'path': '/outside-scope'}), ('read_file', {'path': 'safe', 'expression': 'write()'}),
    ('read_file', {'path': 'safe', 'limit': True}), ('delegate_task', {'action': 'stop'}),
])
def test_every_dispatch_revalidates_capability_and_parameters(rig, name, args):
    child, _ = rig.launch()
    with pytest.raises(ControlDenied):
        with dispatch_fence(child, name, args):
            pytest.fail('forbidden effect dispatched')


def test_valid_read_dispatch_and_cancellation_wait_for_inflight(rig):
    child, handle = rig.launch()
    path = rig.path / 'public.txt'
    path.write_text('synthetic')
    entered, release = threading.Barrier(2), threading.Event()
    def work():
        with dispatch_fence(child, 'read_file', {'path': str(path)}):
            entered.wait(timeout=5)
            assert release.wait(5)
            return path.read_text()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(work)
        entered.wait(timeout=5)
        prime(rig, handle)
        assert propose(rig, handle, 'cancel').accepted
        rig.owner.finish(handle)
        assert not rig.owner.status(handle)['settled']
        release.set()
        assert future.result(5) == 'synthetic'
    assert rig.owner.status(handle)['settled']


@pytest.mark.parametrize('operation', ['enroll', 'capability', 'handoff', 'sharing', 'nested'])
@pytest.mark.parametrize('winner', ['cancel', 'change'])
def test_control_lock_serializes_both_race_winners(rig, operation, winner):
    child, handle = rig.launch()
    rig.consumers['new-required'] = Consumer('new-required', 'required', requires_result=True)
    rig.consumers['parent:' + child.session_id] = Consumer('parent:' + child.session_id, 'optional')
    prime(rig, handle)
    revision = rig.owner.status(handle)['control_revision']
    start = threading.Barrier(3)
    committed = threading.Event()
    results = {}
    def change():
        start.wait(timeout=5)
        if winner == 'cancel':
            assert committed.wait(5)
        try:
            if operation == 'enroll':
                rig.owner.enroll_consumer(handle, 'new-required', expected_revision=revision)
            elif operation == 'capability':
                rig.owner.propose_capability_change(handle, expected_revision=revision)
            elif operation == 'handoff':
                rig.owner.mark_handoff(handle, 'process-owned', expected_revision=revision)
            elif operation == 'sharing':
                rig.owner.invalidate_coverage(handle, expected_revision=revision)
            else:
                from tools import delegate_tool as dt
                response = json.loads(dt.delegate_task(tasks=[{'goal': 'Read the same synthetic fixture'}], parent_agent=child))
                if not response.get('dispatched'):
                    raise ControlDenied('Nested launch was refused')
            results['change'] = True
        except ControlDenied:
            results['change'] = False
        finally:
            if winner == 'change':
                committed.set()
    def cancel():
        start.wait(timeout=5)
        if winner == 'change':
            assert committed.wait(5)
        try:
            results['cancel'] = propose(rig, handle, 'race', expected=revision).accepted
        finally:
            if winner == 'cancel':
                committed.set()
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(change), pool.submit(cancel)
        start.wait(timeout=5)
        a.result(10)
        b.result(10)
    assert results == {'cancel': winner == 'cancel', 'change': winner == 'change'}
    assert rig.owner.status(handle) == rig.store.read(handle.child_id)


def test_nested_legacy_shape_inherits_fence_and_blocks_parent_cancel_until_settled(rig):
    child, handle = rig.launch()
    rig.consumers['parent:' + child.session_id] = Consumer('parent:' + child.session_id, 'optional')
    nested, nested_handle = rig.launch(None, via_parent=child)
    assert rig.owner.status(nested_handle)['effect_class'] == 'read_only'
    assert rig.owner.status(nested_handle)['obligation'] == 'unknown'
    with pytest.raises(ControlDenied):
        with dispatch_fence(nested, 'terminal', {'command': 'true'}):
            pass
    assert propose(rig, handle, 'nested-block').reason == 'ineligible'
    rig.owner.finish(nested_handle)
    assert not rig.owner.status(handle)['handoffs']


def test_failed_cleanup_cannot_claim_effects_or_processes_reconciled(rig):
    child, handle = rig.launch()
    child.close = Mock(side_effect=RuntimeError('synthetic cleanup failure'))
    from tools.delegate_tool_child_run import _close_child
    _close_child(child, 'synthetic close failed')
    state = rig.owner.status(handle)
    assert state['settled']
    assert not state['effects_reconciled'] and not state['processes_stopped']


def test_storage_failure_and_stale_cas_never_publish_cancellation(rig, monkeypatch):
    child, handle = rig.launch()
    prime(rig, handle)
    persisted = rig.store.read(handle.child_id)
    with pytest.raises(ControlConflict):
        rig.store.compare_and_swap(persisted, 0)
    monkeypatch.setattr(rig.store, 'compare_and_swap', Mock(side_effect=sqlite3.OperationalError('disk failed')))
    with pytest.raises(sqlite3.OperationalError):
        propose(rig, handle, 'fail')
    assert not child.stopped.is_set()
    assert not rig.owner.status(handle)['cancel_requested']


def test_durable_deadline_recheck_rolls_back_after_update(rig, monkeypatch):
    from tools import delegation_control_store as storage
    child, handle = rig.launch()
    original = rig.store.read(handle.child_id)
    candidate = dict(original, control_revision=original['control_revision'] + 1, cancel_requested=True)
    # First check before BEGIN, second after lock, third immediately before commit.
    clock = iter([10., 10., 12.])
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=time.time, monotonic=lambda: next(clock)))
    with pytest.raises(storage.ControlDeadlineExpired):
        rig.store.compare_and_swap(candidate, original['control_revision'], deadline=11.)
    assert rig.store.read(handle.child_id) == original
    assert not child.stopped.is_set()


def test_executor_submission_failure_rolls_back_and_closes(isolated_lifecycle, monkeypatch):
    service = isolated_lifecycle
    child = Child('not-submitted')
    monkeypatch.setattr('tools.delegate_tool._build_child_preserving_parent_tools', lambda **kw: child)
    monkeypatch.setattr(lifecycle, '_EXECUTOR', SimpleNamespace(submit=Mock(side_effect=RuntimeError('executor closed'))))
    with pytest.raises(RuntimeError, match='executor closed'):
        service.launch(lifecycle.SubagentLaunchRequest(goal='synthetic', correlation_id='rollback-submit'))
    assert child.closed
    assert not lifecycle._REGISTRY.records
    assert not lifecycle._REGISTRY.correlations


def test_policy_cannot_attest_opaque_execution():
    for name in ('terminal', 'execute_code', 'browser_console', 'tool_call', 'delegate_task'):
        with pytest.raises(ControlDenied):
            ReadOnlyPolicy('bad', (CapabilityContract(name, '1', lambda a: True),))


def test_restart_and_parent_reset_never_regrant_stored_authority(rig):
    _, handle = rig.launch()
    # A new registry cannot control a previously attested worker from JSON alone.
    new = install_owner(rig.parent, store=rig.store, grant=OwnerGrant('test-profile', 'supervisor', True, True),
                        consumer_resolver=rig.consumers.get, policy=rig.policy, revision_provider=lambda: tuple(rig.revision))
    assert not new.list_owned()
    with pytest.raises(ControlDenied):
        new.status(handle)


def test_real_tool_executor_fences_after_argument_rewrite(rig, monkeypatch):
    from agent import tool_executor as te
    child, _ = rig.launch()
    path = rig.path / 'read.txt'
    path.write_text('safe')
    child._tool_guardrails = SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True))
    monkeypatch.setattr(te, '_pre_tool_block', lambda agent, ref: (None, {'path': '/outside'}))
    monkeypatch.setattr(te, '_begin_tool_execution', lambda *a: None)
    monkeypatch.setattr('agent.terminal_approval_batch.prepare_current_terminal', lambda *a: None)
    monkeypatch.setattr(te, '_blocked_tool_result', lambda *a, **kw: kw['block_error_type'])
    monkeypatch.setattr(te, '_run_with_activity_heartbeat', lambda agent, name, call: call())
    run = Mock(return_value='not run')
    ref = te._ToolCallRef('read_file', {'path': str(path)}, 'task', 'call', [])
    state = te._ManagedToolResult(None, ref.args, [], False, False)
    result = te._dispatch_authorized_once(child, state, ref, execute=run, scope_block=None,
                                         display_index=None, begin_execution=None, authorization_gate=None)
    assert result == 'owned_delegation_fence'
    assert state.blocked
    run.assert_not_called()


@pytest.fixture
def isolated_lifecycle(monkeypatch):
    monkeypatch.setattr(lifecycle, '_REGISTRY', lifecycle._Registry())
    parent = SimpleNamespace(session_id='race-parent', enabled_toolsets=[])
    service = lifecycle.SubagentLifecycleService(lambda: parent)
    monkeypatch.setattr('tools.delegate_tool._run_child_lifecycle', lambda *a: {'status': 'completed', 'summary': 'synthetic'})
    return service


def test_duplicate_correlation_reserved_before_construction_real_barrier(isolated_lifecycle, monkeypatch):
    service = isolated_lifecycle
    constructed = []
    entered = threading.Barrier(2)
    release = threading.Event()
    def build(**kwargs):
        index = len(constructed)
        constructed.append(index)
        if index == 0:
            entered.wait(timeout=5)
            assert release.wait(5)
        return Child('reserved-child-' + str(index))
    monkeypatch.setattr('tools.delegate_tool._build_child_preserving_parent_tools', build)
    request = lifecycle.SubagentLaunchRequest(goal='synthetic', correlation_id='unique')
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.launch, request)
        entered.wait(timeout=5)
        try:
            with pytest.raises(lifecycle.SubagentLifecycleError, match='Duplicate correlation'):
                pool.submit(service.launch, request).result(2)
        finally:
            release.set()
        handle = first.result(5)
    assert len(constructed) == 1
    assert service.wait(handle, timeout_seconds=5).completed


def test_record_publication_has_waitable_future_before_submit_returns(isolated_lifecycle, monkeypatch):
    service = isolated_lifecycle
    monkeypatch.setattr('tools.delegate_tool._build_child_preserving_parent_tools', lambda **kw: Child('waitable-child'))
    published = threading.Barrier(2)
    release = threading.Event()
    captured = []
    def submit(fn, record, *args):
        captured.append(record)
        published.wait(timeout=5)
        assert release.wait(5)
        fn(record, *args)
        done = Future()
        done.set_result(None)
        return done
    monkeypatch.setattr(lifecycle, '_EXECUTOR', SimpleNamespace(submit=submit))
    with ThreadPoolExecutor(1) as pool:
        launch = pool.submit(service.launch, lifecycle.SubagentLaunchRequest(goal='synthetic'))
        published.wait(timeout=5)
        try:
            result = service.wait(captured[0].handle, timeout_seconds=.01)
            assert result.timed_out and not result.completed
        finally:
            release.set()
        handle = launch.result(5)
    assert service.wait(handle, timeout_seconds=1).completed


def test_ordinary_delegate_real_scheduler_cancels_and_preserves_partial_result(rig, monkeypatch):
    from tools import delegate_tool as dt
    from tools.delegate_tool_dispatch import _run_batch
    monkeypatch.setattr(dt, '_run_batch', _run_batch)
    monkeypatch.setattr(dt, '_get_child_timeout', lambda: 10)
    entered = threading.Barrier(2)
    builder = dt._build_child_preserving_parent_tools
    def build(**kwargs):
        child = builder(**kwargs)
        def conversation(**kwargs):
            entered.wait(timeout=5)
            assert child.stopped.wait(5)
            return {'interrupted': True, 'completed': False, 'final_response': 'Retained partial public finding'}
        child.run_conversation = conversation
        return child
    monkeypatch.setattr(dt, '_build_child_preserving_parent_tools', build)
    with ThreadPoolExecutor(1) as pool:
        result_future = pool.submit(dt.delegate_task, tasks=[{'goal': 'Read the public synthetic fixture',
                                                              'supervision': rig.request}], parent_agent=rig.parent)
        entered.wait(timeout=5)
        handle = rig.owner.list_owned()[0]
        prime(rig, handle)
        assert propose(rig, handle, 'scheduler-cancel').accepted
        result = json.loads(result_future.result(10))
    assert result['results'][0]['status'] == 'interrupted'
    assert 'Retained partial public finding' in result['results'][0]['summary']
    assert rig.owner.status(handle)['settled']
    assert rig.children[0].closed


def test_parent_reset_and_progress_are_bound_and_bounded(rig):
    _, handle = rig.launch()
    rig.owner.record_progress(handle, 'x' * 2000)
    assert len(rig.owner.status(handle)['latest_milestone']) == 1000
    rig.parent.session_id = 'reset-session'
    service = lifecycle.SubagentLifecycleService(lambda: rig.parent)
    with pytest.raises(ControlDenied, match='generation changed'):
        service.owned_status(handle)


def test_expired_semantic_decision_cannot_cancel(rig):
    child, handle = rig.launch()
    prime(rig, handle)
    rig.deadline = time.monotonic() - 1
    assert propose(rig, handle, 'expired').reason == 'deadline_expired'
    assert not child.stopped.is_set()


def test_explicit_stop_is_separate_and_seals_required_work(rig):
    from tools.delegate_tool_child_run import _signal_child_stop
    request = dict(rig.request, obligation='required')
    child, handle = rig.launch(request)
    assert propose(rig, handle, 'semantic').reason == 'ineligible'
    _signal_child_stop(child, 'user stop')
    assert child.stopped.is_set()
    assert rig.store.read(handle.child_id)['cancel_requested']
    with pytest.raises(ControlDenied):
        rig.owner.enroll_consumer(handle, 'research', expected_revision=rig.owner.status(handle)['control_revision'])


def test_failed_construction_rolls_back_correlation_reservation(isolated_lifecycle, monkeypatch):
    service = isolated_lifecycle
    build = Mock(side_effect=[ValueError('construction failed'), Child('retry-child')])
    monkeypatch.setattr('tools.delegate_tool._build_child_preserving_parent_tools', build)
    req = lifecycle.SubagentLaunchRequest(goal='synthetic', correlation_id='retryable')
    with pytest.raises(ValueError, match='construction failed'):
        service.launch(req)
    handle = service.launch(req)
    assert service.wait(handle, timeout_seconds=5).completed
