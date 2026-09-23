"""Tracking-mutex contention retains observed facts, not permission to replay tools.

Scripted conversations use configured public launches, native executor/read_file
and actual child cleanup; the unrelated guarded file reader owns the real mutex.
"""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest

from tests.agent.test_owned_delegation_policy import factory as factory
from hermes_cli.plugins_loader import _plugin_home_scope
from tools.delegate_tool import _run_single_child as native_run


@contextmanager
def competing_registry_read(path):
    """Real offline_file_access owns the actual global lock on an unrelated file."""
    from hermes_cli.sqlite_safe_read import offline_file_access
    ready, release = threading.Event(), threading.Event()
    errors = []
    def reader():
        try:
            with offline_file_access(path):
                with path.open('rb') as f:
                    assert f.read() == b'unrelated synthetic file'
                    ready.set()
                    assert release.wait(3)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=reader)
    worker.start()
    assert ready.wait(3)
    try:
        yield
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert not errors, errors


@pytest.mark.parametrize('collision', ['dispatch', 'finish', 'both'])
@pytest.mark.parametrize('revoke', [False, True])
def test_native_completion_recovers_after_registry_contention(factory, monkeypatch, record_property, collision, revoke):
    from tools import delegate_tool as dt
    from agent import tool_executor as executor
    from model_tools import handle_function_call
    n = factory.make()
    with _plugin_home_scope(n.home):
        from tools.process_registry import process_registry
        assert process_registry is not None  # cold recovery precedes the collision
    path = n.home / 'readable.txt'
    path.write_text('independent native read result')
    other = n.home / 'unrelated.db'
    other.write_bytes(b'unrelated synthetic file')
    read_done, finish_allowed = threading.Event(), threading.Event()
    results = []
    held = []
    build = dt._build_child_preserving_parent_tools

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
            ref = executor._ToolCallRef('read_file', args, child.session_id, 'review-read', [])
            state = executor._ManagedToolResult(None, args, [], False, False)
            def read(final_args):
                result = handle_function_call('read_file', final_args, task_id=child.session_id)
                assert 'independent native read result' in result
                if collision in ('dispatch', 'both'):
                    context = competing_registry_read(other)
                    context.__enter__()
                    held.append(context)
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
        assert results and 'independent native read result' in results[0]
        if collision == 'finish':
            context = competing_registry_read(other)
            context.__enter__()
            held.append(context)
        # Generation really exists throughout; only the registry mutex is busy.
        assert n.db.control_generation_available()
        assert n.owner.storage_current() is False
        assert n.db.control_session_generation(n.agent.session_id) is None
        with _plugin_home_scope(n.home):
            assert n.owner.current() is False
        if revoke:
            n.config['supervision']['enabled'] = False
            (n.home / 'config.yaml').write_text(json.dumps(n.config))
        if collision == 'dispatch':
            for context in held:
                context.__exit__(None, None, None)
            held.clear()  # actual native finish repairs the earlier dispatch exit
        finish_allowed.set()
        job.thread.join(3)
        assert not job.thread.is_alive() and job.child.closed
        assert job.reply[0]['results'][0]['status'] == 'completed'
        before = n.owner._store.read(job.handle.child_id)
        assert before['settled'] == (collision == 'dispatch')
        assert before['worker_finished'] == (collision == 'dispatch')
        for context in held:
            context.__exit__(None, None, None)
        held.clear()
        with _plugin_home_scope(n.home):
            assert n.owner.current() is not revoke
        assert n.owner.storage_current()
        assert not n.owner._get(job.handle).finalization_failed
        final = n.owner.status(job.handle)
        assert final == n.owner._store.read(job.handle.child_id)
        assert final['settled'] and final['worker_finished'] and final['inflight'] == 0
        assert not final['dispatches'] and not final['handoffs']
        assert final['processes_stopped'] and final['effects_reconciled']
        for _ in range(3):
            assert n.owner.reconcile_lifecycle(job.handle)
            n.owner.finish(job.handle)
            assert n.owner.status(job.handle) == final
        record_property('registry_recovery', dict(collision=collision, revoked=revoke,
            completed_public_result=job.reply[0]['results'][0], before=before, durable=final))
    finally:
        for context in held:
            context.__exit__(None, None, None)
        finish_allowed.set()
        job.child.release.set()
        job.thread.join(3)


@pytest.mark.parametrize('boundary', ['admission', 'precommit'])
@pytest.mark.parametrize('loss', ['none', 'wal', 'shm', 'session', 'missing_session', 'cas', 'closed', 'main'])
def test_contended_transaction_rechecks_generation_before_recovery(factory, monkeypatch, record_property, boundary, loss):
    from contextlib import ExitStack
    import shutil
    import sqlite3
    from tests.agent.test_owned_delegation_wal_fences import changed_sidecar

    n = factory.make()
    job = factory.launch(n)
    path = n.home / 'readable.txt'
    path.write_text('read')
    other = n.home / 'unrelated.db'
    other.write_bytes(b'unrelated synthetic file')
    dispatch = n.owner.dispatch(job.handle, 'read_file', {'path': str(path)})
    dispatch.__enter__()
    before = n.owner._store.read(job.handle.child_id)
    held = ExitStack()
    connect = n.owner._store._connect
    statements = []

    def traced_connect():
        conn = connect()
        def trace(sql):
            statements.append(sql)
            if sql.startswith('UPDATE delegation_controls'):
                held.enter_context(competing_registry_read(other))
        conn.set_trace_callback(trace)
        return conn

    try:
        if boundary == 'admission':
            held.enter_context(competing_registry_read(other))
        else:
            monkeypatch.setattr(n.owner._store, '_connect', traced_connect)
        dispatch.__exit__(None, None, None)
        monkeypatch.setattr(n.owner._store, '_connect', connect)
        live = n.owner._get(job.handle)
        assert not live.finalization_failed
        assert live.completed_dispatches == set(before['dispatches'])
        assert n.owner._store.read(job.handle.child_id) == before  # UPDATE, if any, rolled back
        if boundary == 'precommit':
            assert sum(s.startswith('UPDATE delegation_controls') for s in statements) == 1
            assert 'ROLLBACK' in statements and 'COMMIT' not in statements
        # Mask an actual session loss behind the still-held mutex. The next fresh
        # check, not the transient classification, must determine authority.
        if loss == 'session':
            n.db._conn.execute('UPDATE sessions SET started_at=started_at+1 WHERE id=?', (n.agent.session_id,))
            n.db._conn.commit()
        elif loss == 'missing_session':
            n.db._conn.execute('DELETE FROM sessions WHERE id=?', (n.agent.session_id,))
            n.db._conn.commit()
        assert not n.owner.reconcile_lifecycle(job.handle)
        assert not live.finalization_failed
        held.close()
        with ExitStack() as changed:
            if loss in ('wal', 'shm'):
                changed.enter_context(changed_sidecar(n.db, '-' + loss, 'replace'))
            elif loss == 'cas':
                with sqlite3.connect(n.db.db_path) as writer:
                    writer.execute('UPDATE delegation_controls SET control_revision=control_revision+1 WHERE child_id=?',
                                   (job.handle.child_id,))
            elif loss == 'closed':
                n.db.close()
            elif loss == 'main':
                saved = n.home / 'retained-state.db'
                n.db.db_path.rename(saved)
                shutil.copyfile(saved, n.db.db_path)
                changed.callback(lambda: saved.replace(n.db.db_path))
            recovered = n.owner.reconcile_lifecycle(job.handle)
            assert recovered is (loss == 'none')
        assert live.finalization_failed is (loss != 'none')
        job.child.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        final = n.owner.status(job.handle)
        for _ in range(3):
            assert n.owner.reconcile_lifecycle(job.handle) is (loss == 'none')
            n.owner.finish(job.handle)
            assert n.owner.status(job.handle) == final
        assert final['inflight'] == (0 if loss == 'none' else 1)
        assert final['settled'] is (loss == 'none')
        assert final['processes_stopped'] is (loss == 'none')
        assert final['effects_reconciled'] is (loss == 'none')
        assert job.reply[0]['results'][0]['result'] == 'Synthetic result preserved'
        record_property('fresh_authority', dict(boundary=boundary, loss=loss, before=before, final=final))
    finally:
        monkeypatch.setattr(n.owner._store, '_connect', connect)
        held.close()
        job.child.release.set()
        job.thread.join(3)


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('invalidated', [False, True])
def test_contended_exit_preserves_original_exception_and_other_dispatch(factory, nested, invalidated):
    n = factory.make()
    job = factory.launch(n)
    path = n.home / 'readable.txt'
    path.write_text('read')
    other = n.home / 'unrelated.db'
    other.write_bytes(b'unrelated synthetic file')
    other_dispatch = n.owner.dispatch(job.handle, 'delegate_task' if nested else 'read_file',
                                     {} if nested else {'path': str(path)})
    other_dispatch.__enter__()
    if invalidated:
        n.owner.propose_capability_change(job.handle,
            expected_revision=n.owner.status(job.handle)['control_revision'])
    original = RuntimeError('original tool failure')
    held = competing_registry_read(other)
    try:
        with pytest.raises(RuntimeError) as caught:
            with n.owner.dispatch(job.handle, 'read_file', {'path': str(path)}):
                held.__enter__()
                raise original
        assert caught.value is original
    finally:
        held.__exit__(None, None, None)
    try:
        job.child.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        pending = n.owner.status(job.handle)
        assert pending['worker_finished'] and pending['inflight'] == 1 and not pending['settled']
        assert bool(pending['handoffs']) is nested
        assert not pending['processes_stopped'] and not pending['effects_reconciled']
        for _ in range(3):
            assert n.owner.reconcile_lifecycle(job.handle)
            assert n.owner.status(job.handle) == pending
    finally:
        other_dispatch.__exit__(None, None, None)
    final = n.owner.status(job.handle)
    assert final['settled'] and final['inflight'] == 0 and not final['handoffs']
    assert final['effects_reconciled'] is not invalidated
    assert final['processes_stopped'] is not invalidated


@pytest.mark.parametrize('failure', ['rollback', 'close'])
@pytest.mark.parametrize('error_code', [5, 10])  # SQLITE_BUSY and SQLITE_IOERR
def test_contended_rollback_or_close_ambiguity_is_permanent(factory, monkeypatch, failure, error_code):
    from contextlib import ExitStack
    import sqlite3

    n = factory.make()
    job = factory.launch(n)
    path = n.home / 'readable.txt'
    path.write_text('read')
    other = n.home / 'unrelated.db'
    other.write_bytes(b'unrelated synthetic file')
    dispatch = n.owner.dispatch(job.handle, 'read_file', {'path': str(path)})
    dispatch.__enter__()
    before = n.owner._store.read(job.handle.child_id)
    connect = n.owner._store._connect
    held = ExitStack()
    injected = sqlite3.OperationalError('synthetic rollback/close acknowledgement failure')
    injected.sqlite_errorcode = error_code
    observed = []

    class AmbiguousConnection:
        def __init__(self):
            self.conn = connect()
            def trace(sql):
                if sql.startswith('UPDATE delegation_controls'):
                    held.enter_context(competing_registry_read(other))
            self.conn.set_trace_callback(trace)

        def execute(self, *args):
            return self.conn.execute(*args)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            self.conn.__exit__(*args)
            observed.append(type(args[1]).__name__)
            if failure == 'rollback':
                raise injected

        def close(self):
            self.conn.close()
            if failure == 'close':
                raise injected

    try:
        monkeypatch.setattr(n.owner._store, '_connect', AmbiguousConnection)
        dispatch.__exit__(None, None, None)
        assert observed == ['ControlGenerationContended']
    finally:
        monkeypatch.setattr(n.owner._store, '_connect', connect)
        held.close()
    assert n.owner.storage_current()
    assert n.owner._get(job.handle).finalization_failed
    assert n.owner._store.read(job.handle.child_id) == before
    job.child.release.set()
    job.thread.join(3)
    assert not job.thread.is_alive()
    for _ in range(3):
        assert not n.owner.reconcile_lifecycle(job.handle)
        assert n.owner.status(job.handle) == before
