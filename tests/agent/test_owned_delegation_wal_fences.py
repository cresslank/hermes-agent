"""Fresh sidecar authority through public configured launch and native F01 apply.

Synthetic homes, strict fake HTTP/model execution from the ordinary-path fixture;
no production predicates, deadlines, SQLite implementation or clocks substituted.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil

import pytest

from hermes_cli.plugins_loader import _plugin_home_scope
from tests.agent.test_owned_delegation_policy import factory  # noqa: F401


@contextmanager
def changed_sidecar(db, suffix, change):
    sidecar = Path(str(db.db_path) + suffix)
    saved = sidecar.with_name(sidecar.name + '.retained-original')
    recorded = db._db_sidecar_identity.copy()
    if change == 'unknown':
        db._db_sidecar_identity.pop(suffix)
    else:
        sidecar.rename(saved)
        if change == 'replace':
            shutil.copyfile(saved, sidecar)
            assert sidecar.stat().st_ino != saved.stat().st_ino
    try:
        assert not db._db_wal_generation_lost
        yield
    finally:
        db._db_sidecar_identity = recorded
        if change != 'unknown':
            sidecar.unlink(missing_ok=True)
            saved.rename(sidecar)


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
@pytest.mark.parametrize('change', ['replace', 'loss', 'unknown'])
@pytest.mark.parametrize('boundary', ['lookup', 'admission', 'commit'])
def test_native_cancel_fences_actual_sidecar_generation(factory, monkeypatch, suffix, change, boundary):
    n = factory.make()
    job = factory.launch(n)
    with _plugin_home_scope(n.home):
        assert n.owner.current()
    factory.progress(n, job, 'First supplementary section')
    first = n.owner.status(job.handle)
    assert first['semantic_observation'] and not first['cancel_requested'] and first['priority'] == 0
    assert list(n.runtime.receipts.values())[-1].status == 'no_op'
    original = n.runtime.children.apply
    authority = n.owner._store._authority
    observed, lookups = [], []

    def apply(proposal):
        if boundary == 'lookup':
            with changed_sidecar(n.db, suffix, change):
                lookups.append((n.db.control_session_generation(n.agent.session_id), n.owner.current()))
                observed.append(original(proposal))
                return observed[-1]
        # Introduce replacement only AFTER the regular owner lookup, at the
        # real transaction's admission or final revalidation before commit.
        calls = 0
        context = None

        def transaction_authority(conn, deadline=None):
            nonlocal calls, context
            if deadline is not None:
                calls += 1
                if calls == (1 if boundary == 'admission' else 2):
                    context = changed_sidecar(n.db, suffix, change)
                    context.__enter__()
            return authority(conn, deadline)

        monkeypatch.setattr(n.owner._store, '_authority', transaction_authority)
        try:
            observed.append(original(proposal))
            assert context is not None
            return observed[-1]
        finally:
            monkeypatch.setattr(n.owner._store, '_authority', authority)
            if context is not None:
                context.__exit__(None, None, None)

    monkeypatch.setattr(n.runtime.children, 'apply', apply)
    factory.progress(n, job, 'Second supplementary section')
    assert len(observed) == 1 and observed[0] != 'applied'
    if boundary == 'lookup':
        assert lookups == [(None, False)]
    assert not n.owner.status(job.handle)['cancel_requested']
    assert not n.owner._store.read(job.handle.child_id)['cancel_requested']
    assert not job.child.stopped.is_set()
    with _plugin_home_scope(n.home):
        assert n.owner.current()  # restoration for synthetic fixture cleanup only
