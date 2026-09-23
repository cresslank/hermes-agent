"""Configured control safe points must not drain unrelated accounting writes."""
from contextlib import ExitStack
from dataclasses import replace
import json
import sqlite3
import threading
import time

import pytest

from tests.agent.test_owned_delegation_policy import factory as factory


@pytest.mark.parametrize('stage', ['observation', 'cancellation'])
def test_accounting_contention_keeps_original_control_deadline(factory, monkeypatch, record_property, stage):
    n = factory.make()
    job = factory.launch(n)
    if stage == 'cancellation':
        factory.progress(n, job, 'First optional milestone')
        assert n.owner.status(job.handle)['semantic_observation']
    original = n.runtime.children.apply
    current = n.owner.current
    measurements, memberships = [], []

    def membership(**kwargs):
        started = time.monotonic()
        answer = current(**kwargs)
        memberships.append((answer, time.monotonic() - started))
        assert n.owner._lock._is_owned()
        return answer

    def contended(proposal):
        with sqlite3.connect(n.db.db_path, timeout=0) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            n.db.queue_token_counts(n.agent.session_id, input_tokens=1, source='cli')
            started = time.monotonic()
            assert n.runtime.lock._is_owned() and n.owner.registration.fence._is_owned()
            with monkeypatch.context() as scoped:
                scoped.setattr(n.owner, 'current', membership)
                answer = original(proposal)
            ended = time.monotonic()
            measurements.append((ended - started, ended - proposal.expires_at_monotonic, answer))
        return answer

    monkeypatch.setattr(n.runtime.children, 'apply', contended)
    factory.progress(n, job, 'First optional milestone under accounting contention')
    assert len(measurements) == 1
    elapsed, overrun, answer = measurements[0]
    record_property('control', dict(stage=stage, elapsed_s=elapsed, overrun_s=overrun,
                                   status=answer, membership_reads=memberships))
    # A scheduling-tolerant outer bound, not a new action budget. Production must
    # still compare against the exact original deadline before any effect.
    assert memberships and all(ok and elapsed < 2 for ok, elapsed in memberships), memberships
    assert elapsed < 2, measurements
    assert overrun < 1, measurements
    assert answer != 'applied'
    assert not n.owner.status(job.handle)['cancel_requested']
    assert not job.child.stopped.is_set()
    # No external drain drives supervision. Ordinary accounting can finish after
    # the competing writer releases; the queued delta must not be dropped.
    assert n.db.get_session(n.agent.session_id)['input_tokens'] == 1


@pytest.mark.parametrize('case', [
    'config_lock', 'control_lock', 'registry_lock', 'tracking_lock', 'session_mutex',
    'config_changed', 'config_missing', 'config_cold', 'malformed_loaded',
    'session_missing', 'session_ended', 'session_expired', 'session_generation',
    'foreign_db', 'missing_contract', 'foreign_handle', 'expired',
    'registration_revoked', 'registration_generation', 'grant_revoked',
    'session_revoked_at_cas', 'policy_revoked_at_cas',
])
def test_native_cancel_revalidates_authority_and_budget(factory, monkeypatch, record_property, case):
    from hermes_cli import config, sqlite_safe_read
    from agent import supervision_facade
    from hermes_state import SessionDB

    n = factory.make()
    job = factory.launch(n)
    factory.progress(n, job, 'First optional milestone')
    assert n.owner.status(job.handle)['semantic_observation']
    original = n.runtime.children.apply
    measurements = []

    def checked(proposal):
        with ExitStack() as cleanup, monkeypatch.context() as scoped:
            lock = {'config_lock': config._CONFIG_LOCK, 'control_lock': n.owner._lock,
                    'registry_lock': supervision_facade._lock, 'tracking_lock': sqlite_safe_read._live_lock,
                    'session_mutex': n.db._lock}.get(case)
            if lock is not None:
                acquired, release = threading.Event(), threading.Event()
                def holder():
                    with lock:
                        acquired.set()
                        release.wait(5)
                thread = threading.Thread(target=holder)
                thread.start()
                def join():
                    release.set()
                    thread.join(3)
                    assert not thread.is_alive()
                cleanup.callback(join)
                assert acquired.wait(3)
            if case.startswith('config_') and case != 'config_lock':
                path = n.home / 'config.yaml'
                if case == 'config_cold':
                    scoped.delitem(config._LOAD_CONFIG_CACHE, str(path))
                else:
                    original_text = path.read_text()
                    cleanup.callback(path.write_text, original_text)
                    if case == 'config_missing':
                        path.unlink()
                    else:
                        changed = json.loads(original_text)
                        changed['supervision']['enabled'] = False
                        path.write_text(json.dumps(changed))
            if case == 'malformed_loaded':
                path = n.home / 'config.yaml'
                original_text = path.read_text()
                cleanup.callback(path.write_text, original_text)
                path.write_text('supervision: [broken')
                # An ordinary loader's last-known-good fallback must not silently
                # authorize control from the now-corrupt current configuration.
                config.load_config_readonly()
            if case in {'session_missing', 'session_ended', 'session_expired', 'session_generation'}:
                column, value = {
                    'session_missing': ('id', 'temporarily-missing'),
                    'session_ended': ('ended_at', time.time()),
                    'session_expired': ('expiry_finalized', 1),
                    'session_generation': ('started_at', 0),
                }[case]
                def update(value, ident):
                    with sqlite3.connect(n.db.db_path, timeout=0) as conn:
                        conn.execute(f'UPDATE sessions SET {column}=? WHERE id=?', (value, ident))
                with sqlite3.connect(n.db.db_path, timeout=0) as conn:
                    old, = conn.execute(f'SELECT {column} FROM sessions WHERE id=?', (n.agent.session_id,)).fetchone()
                update(value, n.agent.session_id)
                cleanup.callback(update, old, value if column == 'id' else n.agent.session_id)
            if case == 'foreign_db':
                foreign = SessionDB(n.home / 'foreign.db')
                foreign.create_session(n.agent.session_id, 'cli')
                cleanup.callback(foreign.close)
                scoped.setattr(n.agent, '_session_db', foreign)
            if case == 'missing_contract':
                scoped.setattr(n.runtime, 'owned_consumer_contracts', {})
            if case == 'foreign_handle':
                target = n.runtime.children.targets[proposal.target_id]
                scoped.setitem(n.runtime.children.targets, proposal.target_id,
                               (replace(target[0], generation='foreign'), *target[1:]))
            if case == 'expired':
                proposal = replace(proposal, expires_at_monotonic=time.monotonic())
            if case == 'registration_revoked':
                scoped.setattr(n.owner.registration, 'active', False)
            if case == 'registration_generation':
                scoped.setattr(n.owner.registration, 'generation', 'foreign')
            if case == 'grant_revoked':
                scoped.setattr(n.owner.registration, 'grants', frozenset({'observe'}))
            if case in {'session_revoked_at_cas', 'policy_revoked_at_cas'}:
                connect = n.owner._store._connect
                def revoke_then_connect():
                    # Preflight has passed. A real independent writer commits a
                    # revocation before the control transaction's admission.
                    if case == 'session_revoked_at_cas':
                        with sqlite3.connect(n.db.db_path, timeout=0) as conn:
                            conn.execute('UPDATE sessions SET expiry_finalized=1 WHERE id=?', (n.agent.session_id,))
                    else:
                        changed = json.loads((n.home / 'config.yaml').read_text())
                        changed['supervision']['enabled'] = False
                        (n.home / 'config.yaml').write_text(json.dumps(changed))
                    return connect()
                scoped.setattr(n.owner._store, '_connect', revoke_then_connect)
            started = time.monotonic()
            answer = original(proposal)
            measurements.append((time.monotonic() - started, answer))
            return answer

    monkeypatch.setattr(n.runtime.children, 'apply', checked)
    factory.progress(n, job, 'Second optional milestone with changed authority')
    assert len(measurements) == 1
    elapsed, answer = measurements[0]
    record_property('control', dict(case=case, elapsed_s=elapsed, status=answer))
    assert elapsed < 2, measurements
    allowed = case == 'session_mutex'  # accounting mutex is not control authority
    assert (answer == 'applied') is allowed
    assert n.owner.status(job.handle)['cancel_requested'] is allowed
    assert job.child.stopped.is_set() is allowed
