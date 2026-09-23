"""Real configured F04 supplier -> canonical append and SessionDB consumption.

Both main reads execute; neither ready strings nor an unlocked census authorize
advice. Strict transport and public cross-thread native admission are used.
"""
import json
import threading
from contextvars import copy_context
from contextlib import contextmanager, nullcontext

import pytest
from tests.agent.test_owned_delegation_planning import (
    base_rig as base_rig, rig as rig, native as native, factory as factory,
)
from tests.agent.test_supervision_read_supplier import requests, read
from tests.agent.test_supervision_efficiency import feature_calls


def commit(p, args, ident, result):
    from agent.tool_executor import _commit_tool_result, _ToolCallRef
    from tools.budget_config import DEFAULT_BUDGET
    p.history.pop()
    return _commit_tool_result(p.a, p.history, _ToolCallRef('read_file', args, 'default', ident, []), result,
        budget=DEFAULT_BUDGET, tool_duration=0, is_error=False, blocked=False,
        effect_disposition=None, observed=True)


@contextmanager
def pending_native_launch(p, monkeypatch):
    """Public delegate_task owns its real admission reservation on another thread.
    Stop at synthetic credential resolution, before construction or network.
    """
    from tools import delegate_tool as dt
    entered, release = threading.Event(), threading.Event()
    replies, errors, tids = [], [], []
    def credentials(*args, **kwargs):
        tids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        raise ValueError('synthetic offline admission stop')
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', credentials)
    def launch():
        try:
            replies.append(json.loads(dt.delegate_task(parent_agent=p.a, goal='Independent current source consumer')))
        except BaseException as exc:
            errors.append(repr(exc))
    ctx = copy_context()
    thread = threading.Thread(target=lambda: ctx.run(launch))
    thread.start()
    try:
        assert entered.wait(3), errors
        assert tids == [thread.ident] and thread.ident != threading.get_ident()
        yield tids
    finally:
        release.set(); thread.join(3)
        assert not thread.is_alive() and not errors, errors
        assert 'synthetic offline admission stop' in str(replies), replies



def lock_probe(p):
    from tools import delegate_tool_registry as registry
    observations = []
    for name, lock in [('runtime', p.rt.lock), ('efficiency', p.native.owner.lock),
                       ('owner', p.owner._lock), ('native', registry._active_subagents_lock)]:
        acquired = lock.acquire(blocking=False)
        observations.append((name, acquired))
        if acquired:
            lock.release()
    assert all(value for _, value in observations), observations
    return observations


@pytest.mark.parametrize('change', [
    'none', 'source', 'configuration', 'native_admission', 'wrong_call', 'unload',
    'expiry', 'canonical_write', 'receipt_write', 'busy_config',
])
def test_exact_read_advice_only_at_confirmed_canonical_use(factory, monkeypatch, change):
    from agent import tool_executor
    p = factory(); rows = requests(p); p.commit(rows)
    first, first_bytes = read(p, rows[0])
    requirements = p.rt.requirements
    owner_thread = threading.get_ident()
    inference_locks = []
    def inference():
        assert threading.get_ident() != owner_thread
        inference_locks.extend(lock_probe(p))
    p.native.on_response.append(inference)
    args = rows[1]['operation']['arguments']
    ident, result = p.call('read_file', args, settle=False)
    assert len(feature_calls(p.native, 'F04')) == 1 and len(inference_locks) == 4
    candidate = feature_calls(p.native, 'F04')[0]['state']['facts']['candidates'][0]
    assert candidate['source_ref'] == 'tool:' + first
    assert candidate['requirement_id'] in {r.id for r in requirements}
    assert p.native.owner.read_payloads[first] == first_bytes.encode()
    assert ident != first and 'Synthetic source:' in result
    # Selection/rendering is not a persisted advisory effect.
    assert not [r for r in p.rt.receipts.values() if r.status == 'applied']
    if change == 'source':
        p.source.write_text('Synthetic source: externally changed before commit.\n')
    if change == 'configuration':
        cfg = p.native.rig.config
        cfg['supervision']['planning']['allow_discretionary_readonly_labels'] = False
        (p.native.rig.home / 'config.yaml').write_text(json.dumps(cfg))
        from hermes_cli.config import load_config_readonly
        load_config_readonly()
    if change == 'wrong_call':
        ident += '-different'
    if change == 'unload':
        p.native.bridge.close()
    if change == 'expiry':
        deadline = p.rt.opportunities[ident]['deadline']
        threading.Event().wait(max(0, deadline - p.rt.clock()) + 0.01)
    db = p.a._session_db
    if change == 'canonical_write':
        db._conn.execute("""CREATE TRIGGER fail_read_message BEFORE INSERT ON messages
            WHEN NEW.role='tool' BEGIN SELECT RAISE(ABORT, 'offline canonical write failure'); END""")
        db._conn.commit()
    if change == 'receipt_write':
        db._conn.execute("""CREATE TRIGGER fail_read_receipt BEFORE UPDATE ON supervision_receipts
            WHEN NEW.status='applied' BEGIN SELECT RAISE(ABORT, 'offline receipt write failure'); END""")
        db._conn.commit()
    original_flush = tool_executor._flush_session_db_after_tool_progress
    flush_probes = []
    def flush(*a, **kw):
        assert not [r for r in p.rt.receipts.values() if r.status == 'applied']
        errors = []
        def probe():
            try:
                flush_probes.extend(lock_probe(p))
            except BaseException as exc:
                errors.append(exc)
        t = threading.Thread(target=probe); t.start(); t.join(3)
        assert not t.is_alive() and not errors
        return original_flush(*a, **kw)
    monkeypatch.setattr(tool_executor, '_flush_session_db_after_tool_progress', flush)
    scope = pending_native_launch(p, monkeypatch) if change == 'native_admission' else nullcontext()
    from hermes_cli import config
    entered, release = threading.Event(), threading.Event()
    def hold_config():
        with config._CONFIG_LOCK:
            entered.set()
            assert release.wait(3)
    holder = None
    if change == 'busy_config':
        holder = threading.Thread(target=hold_config); holder.start(); assert entered.wait(3)
    try:
        with scope:
            value = commit(p, args, ident, result)
    finally:
        release.set()
        if holder:
            holder.join(3); assert not holder.is_alive()
    assert len(flush_probes) == 4
    assert (value is None) == (change == 'canonical_write')
    stored = db.get_messages(p.a.session_id)
    expected_advice = change in {'none', 'receipt_write'}
    assert ('Task-bound advisory' in str(stored)) == expected_advice
    receipts = list(p.rt.receipts.values())
    assert bool([r for r in receipts if r.status == 'applied']) == (change == 'none')
    durable = db._conn.execute('SELECT status FROM supervision_receipts').fetchall()
    assert bool([r for r in durable if r[0] == 'applied']) == (change == 'none')
    if change in {'canonical_write', 'receipt_write'}:
        assert receipts[0].status == 'unknown'
    assert p.rt.requirements == requirements and not p.owner.list_owned()
    assert p.native.owner.read_payloads[first] == first_bytes.encode()
    assert not p.rt.drain_at_safe_point()  # never reroute the rejected/wrong-call advice


@pytest.mark.parametrize('aba', [False, True])
def test_public_native_admission_wins_after_last_census(factory, monkeypatch, aba):
    from agent.owned_delegation_planning import _no_native_workers
    p = factory(); rows = requests(p); p.commit(rows); read(p, rows[0])
    args = rows[1]['operation']['arguments']
    ident, result = p.call('read_file', args, settle=False)
    graph = p.rt.dependencies.planning
    original = graph.read_intent
    scopes, observations = [], []
    # Arm after ordinary wrapping, immediately before the canonical consumer.
    original_content = p.a._tool_result_content_for_active_model
    armed = []
    def content(*a, **kw):
        value = original_content(*a, **kw)
        armed.append(True)
        return value
    monkeypatch.setattr(p.a, '_tool_result_content_for_active_model', content)
    def census_then_competitor(arguments, call_id):
        view = original(arguments, call_id)
        if armed and view is not None and not observations:
            scope = pending_native_launch(p, monkeypatch)
            scope.__enter__(); scopes.append(scope)
            assert not _no_native_workers(p.owner, p.a)
            assert original(arguments, call_id) is None
            if aba:
                scopes.pop().__exit__(None, None, None)
                assert _no_native_workers(p.owner, p.a)
                assert original(arguments, call_id) == view
            observations.append(call_id)
        return view  # actual observed census, not a synthetic currentness result
    monkeypatch.setattr(graph, 'read_intent', census_then_competitor)
    try:
        assert commit(p, args, ident, result) is not None
        assert observations == [ident]
        assert not [r for r in p.rt.receipts.values() if r.status == 'applied']
        assert 'Task-bound advisory' not in str(p.a._session_db.get_messages(p.a.session_id))
        assert len(feature_calls(p.native, 'F04')) == 1 and 'Synthetic source:' in result
    finally:
        for scope in scopes:
            scope.__exit__(None, None, None)
