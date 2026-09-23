"""Public configured delegate -> native executor/read -> actual worker cleanup.

The conversation is a strict script in place of a model/provider, not a full
AIAgent/model round. Scheduler, _run_single_child, native dispatch, read_file,
SQLite writer contention and _ChildRun.cleanup/_close_child/finish are real.
"""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.plugins_loader import _plugin_home_scope
from tools.delegate_tool import _run_single_child as native_run
from tests.agent.test_owned_delegation_policy import factory as factory


@pytest.mark.parametrize('collision', ['dispatch', 'finish', 'both'])
@pytest.mark.parametrize('revoke', [False, True])
def test_native_read_result_and_finalization_survive_writer(factory, monkeypatch, collision, revoke):
    from tools import delegate_tool as dt
    from agent import tool_executor as executor
    from model_tools import handle_function_call

    n = factory.make()
    path = n.home / 'readable.txt'
    path.write_text('completed owned read')
    blocker = sqlite3.connect(n.db.db_path, timeout=0, check_same_thread=False)
    read_done, finish_allowed = threading.Event(), threading.Event()
    results = []
    build = dt._build_child_preserving_parent_tools

    def hold_writer():
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute('UPDATE sessions SET input_tokens=input_tokens+1 WHERE id=?', (n.agent.session_id,))

    def scripted_build(**kwargs):
        child = build(**kwargs)
        child.quiet_mode, child.verbose_logging = True, False
        child.tool_progress_callback = child.tool_start_callback = None
        child._checkpoint_mgr = SimpleNamespace(enabled=False)
        child._tool_guardrails = SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True))
        child._touch_activity = lambda *a: None

        def run_conversation(**kwargs):
            child.started.set()
            assert child.release.wait(3)
            args = {'path': str(path)}
            ref = executor._ToolCallRef('read_file', args, child.session_id, 'owned-read', [])
            state = executor._ManagedToolResult(None, args, [], False, False)

            def read(final_args):
                result = handle_function_call('read_file', final_args, task_id=child.session_id)
                assert 'completed owned read' in result
                if collision in ('dispatch', 'both'):
                    hold_writer()  # only after actual successful read/admission
                return result

            try:
                result = executor._dispatch_authorized_once(child, state, ref, execute=read,
                    scope_block=None, display_index=None, begin_execution=None, authorization_gate=None)
                results.append(result)
            finally:
                read_done.set()
            assert finish_allowed.wait(3)
            return dict(completed=True, final_response=result, messages=[])

        child.run_conversation = run_conversation
        return child

    monkeypatch.setattr(dt, '_build_child_preserving_parent_tools', scripted_build)
    monkeypatch.setattr(dt, '_run_single_child', native_run)
    job = factory.launch(n)
    try:
        job.child.release.set()
        assert read_done.wait(3)
        assert results and 'completed owned read' in results[0]
        if collision in ('dispatch', 'both'):
            pending = n.owner._store.read(job.handle.child_id)
            assert pending['inflight'] == 1 and not pending['settled']
        if collision == 'dispatch':
            blocker.commit()  # actual normal finish repairs the earlier exit
        elif collision == 'finish':
            hold_writer()
        if revoke:
            n.config['supervision']['enabled'] = False
            (n.home / 'config.yaml').write_text(json.dumps(n.config))
            with _plugin_home_scope(n.home):
                assert not n.owner.current()
        finish_allowed.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert job.child.closed  # real _ChildRun.cleanup -> _close_child
        entry = job.reply[0]['results'][0]
        assert entry['status'] == 'completed' and 'completed owned read' in entry['summary']
        if collision == 'dispatch':
            durable = n.owner._store.read(job.handle.child_id)
            assert durable['settled'] and durable['inflight'] == 0  # finish, not status, repaired exit
        else:
            before = n.owner._store.read(job.handle.child_id)
            assert not before['settled']
            blocker.commit()
        # Native status reconciles only retained completed facts, even revoked.
        final = n.owner.status(job.handle)
        assert final == n.owner._store.read(job.handle.child_id)
        assert final['worker_finished'] and final['inflight'] == 0
        assert final['settled'] and final['processes_stopped'] and final['effects_reconciled']
        for _ in range(3):
            assert n.owner.reconcile_lifecycle(job.handle)
            n.owner.finish(job.handle)
            assert n.owner.status(job.handle) == final  # no duplicate CAS/decrement
        if revoke:
            with _plugin_home_scope(n.home):
                assert not n.owner.current()
    finally:
        blocker.rollback()
        blocker.close()
        finish_allowed.set()
        job.child.release.set()
        job.thread.join(3)


@pytest.mark.parametrize('nested_dispatch', [False, True])
def test_completed_exits_do_not_settle_parallel_or_nested_work(factory, nested_dispatch):
    n = factory.make()
    job = factory.launch(n)
    path = n.home / 'readable.txt'
    path.write_text('read')
    first = n.owner.dispatch(job.handle, 'read_file', {'path': str(path)})
    second = n.owner.dispatch(job.handle, 'delegate_task' if nested_dispatch else 'read_file',
                              {} if nested_dispatch else {'path': str(path)})
    first.__enter__()
    second.__enter__()
    blocker = sqlite3.connect(n.db.db_path, timeout=0)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        first.__exit__(None, None, None)
        assert n.owner._store.read(job.handle.child_id)['inflight'] == 2
        blocker.commit()
        job.child.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        pending = n.owner.status(job.handle)
        assert pending['worker_finished'] and pending['inflight'] == 1 and not pending['settled']
        assert not pending['effects_reconciled'] and not pending['processes_stopped']
        assert bool(pending['handoffs']) == nested_dispatch
        for _ in range(3):
            n.owner.reconcile_lifecycle(job.handle)
            assert n.owner.status(job.handle) == pending
    finally:
        blocker.rollback()
        blocker.close()
        second.__exit__(None, None, None)
    final = n.owner.status(job.handle)
    assert final['inflight'] == 0 and final['settled'] and not final['handoffs']


def test_nested_child_detach_retries_without_settling_live_sibling(factory, monkeypatch):
    n = factory.make()
    parent = factory.launch(n)
    nested_parent = SimpleNamespace(home=n.home, agent=parent.child)
    first = factory.launch(nested_parent)
    second = factory.launch(nested_parent)
    assert first.handle and second.handle
    parent.child.release.set()
    parent.thread.join(3)
    assert not parent.thread.is_alive()
    blocker = sqlite3.connect(n.db.db_path, timeout=0, check_same_thread=False)
    commit = n.owner._store.compare_and_swap_finalizer
    collided = threading.Event()

    def detach_collision(snapshot, revision):
        commit(snapshot, revision)
        if snapshot['child_id'] == first.handle.child_id and snapshot['settled']:
            blocker.execute('BEGIN IMMEDIATE')
            blocker.execute('UPDATE sessions SET input_tokens=input_tokens+1 WHERE id=?', (n.agent.session_id,))
            collided.set()  # child is durable; parent detach now collides

    monkeypatch.setattr(n.owner._store, 'compare_and_swap_finalizer', detach_collision)
    try:
        first.child.release.set()
        first.thread.join(3)
        assert not first.thread.is_alive() and collided.is_set()
        before = n.owner._store.read(parent.handle.child_id)
        assert set(before['handoffs']) == {first.handle.child_id, second.handle.child_id}
        blocker.commit()
        after = n.owner.status(parent.handle)
        assert after['handoffs'] == [second.handle.child_id] and not after['settled']
        assert not after['processes_stopped'] and not after['effects_reconciled']
        for _ in range(3):
            n.owner.reconcile_lifecycle(first.handle)
            assert n.owner.status(parent.handle) == after
        second.child.release.set()
        second.thread.join(3)
        assert not second.thread.is_alive()
        final = n.owner._store.read(parent.handle.child_id)
        assert final['settled'] and not final['handoffs'] and final['inflight'] == 0
        assert final['processes_stopped'] and final['effects_reconciled']
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize('loss', ['wal', 'session', 'conflict'])
def test_unknown_generation_or_cas_never_replays_completed_facts(factory, loss):
    from tests.agent.test_owned_delegation_wal_fences import changed_sidecar
    from contextlib import nullcontext

    n = factory.make()
    job = factory.launch(n)
    path = n.home / 'readable.txt'
    path.write_text('read')
    dispatch = n.owner.dispatch(job.handle, 'read_file', {'path': str(path)})
    dispatch.__enter__()
    context = changed_sidecar(n.db, '-wal', 'replace') if loss == 'wal' else nullcontext()
    with context:
        if loss == 'session':
            n.db._conn.execute('UPDATE sessions SET started_at=started_at+1 WHERE id=?', (n.agent.session_id,))
            n.db._conn.commit()
        elif loss == 'conflict':
            with sqlite3.connect(n.db.db_path) as writer:
                writer.execute('UPDATE delegation_controls SET control_revision=control_revision+1 WHERE child_id=?',
                               (job.handle.child_id,))
        dispatch.__exit__(None, None, None)
    # Restoring a pathname is not permission to replay an uncertain completion.
    job.child.release.set()
    job.thread.join(3)
    assert not job.thread.is_alive()
    for _ in range(3):
        assert not n.owner.reconcile_lifecycle(job.handle)
    final = n.owner._store.read(job.handle.child_id)
    assert final['inflight'] == 1 and not final['settled']
    assert not final['effects_reconciled'] and not final['processes_stopped']
    assert job.reply[0]['results'][0]['result'] == 'Synthetic result preserved'
