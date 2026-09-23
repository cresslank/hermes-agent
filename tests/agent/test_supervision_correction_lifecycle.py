"""Source transaction failures and native lifecycle changes never mint a notice."""
import json
import queue
import sqlite3

import pytest

from tests.agent.test_supervision_claim_uses import native, append, record  # noqa: F401
from tests.agent.test_supervision_corrections import configured, sink, drain_source, rows
from tests.agent.test_supervision_original_output import setup, finalize
from tests.agent.test_supervision_final_use import configure


@pytest.mark.parametrize('fault', ['delete-rollback', 'metadata', 'postcommit'])
def test_source_outbox_follows_native_commit(native, monkeypatch, tmp_path, fault):
    from tools.process_registry import process_registry
    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path/'source', 'cli') as output:
        output.original(finalize(n, messages, monkeypatch))
        provider = n.ctx.supervision._literal_source_registration.provider
        custody, = provider.correction_custody.values()
        store = n.engine._store
        def failed(*args, **kwargs):
            raise RuntimeError('optional callback fault')
        if fault == 'delete-rollback':
            store._conn.execute("CREATE TEMP TRIGGER refuse_source_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
            with pytest.raises(sqlite3.IntegrityError):
                store.delete_session_messages(n.agent.session_id)
            store._conn.rollback()
            assert not custody.unavailable
            assert process_registry.completion_queue.empty()
            assert store._conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'literal-correction-event:%'").fetchone()[0] == 0
            assert store.get(int(custody.record.exact_ref.split(':')[1])) is not None
            store._conn.execute('DROP TRIGGER refuse_source_delete')
        elif fault == 'metadata':
            monkeypatch.setattr('hermes_lcm.literal_source_events._prepare', failed)
        else:
            monkeypatch.setattr('agent.supervision_corrections.source_event_received', failed)
        new = record()
        new['value'] = 7
        added = append(n, new)  # completed ordinary write survives optional faults
        assert store.get(added[0])['content'] == added[1]
        if fault == 'delete-rollback':
            event = process_registry.completion_queue.get_nowait()
            drain_source(n, event)
            n.agent.notice_callback = output.notice
            n.rt.bind_turn()
            n.rt.finish_turn()
            output.flush()
            assert rows(n)[0]['status'] == 'emitted'
            n.rt.dependencies.contested_claims.clear()
            n.rt.dependencies.planning.clear()
            n.rt.dependencies.planning._load()
            assert rows(n)[0]['claim_id'] in n.rt.dependencies.contested_claims
        else:
            assert process_registry.completion_queue.empty()
            assert not rows(n)


@pytest.mark.parametrize('boundary', ['reregister', 'native-reset', 'deleted', 'retained-record-removed', 'source-reopened', 'database-reopened'])
def test_queued_correction_cannot_cross_native_lifecycle(native, monkeypatch, tmp_path, boundary):
    from tools.process_registry import process_registry
    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path/'life', 'cli') as output:
        output.original(finalize(n, messages, monkeypatch))
        new = record()
        new['value'] = 7
        append(n, new)
        event = process_registry.completion_queue.get_nowait()
        drain_source(n, event)
        n.agent.notice_callback = output.notice
        n.rt.bind_turn()
        n.rt.finish_turn()
        assert rows(n)[0]['status'] == 'contested'
        before = output.read()
        reopened = None
        if boundary == 'database-reopened':
            from hermes_state import SessionDB
            db = n.agent._session_db
            path = db.db_path
            db.close()
            reopened = n.agent._session_db = SessionDB(path)
            assert rows(n)[0]['status'] == 'contested'  # durable facts, not a renewed send lease
        elif boundary == 'reregister':
            old = n.ctx.supervision._literal_source_registration
            configure(n)  # real registration replacement, same provider/unchanged policy
            assert not old.active and not old.provider.correction_custody
        elif boundary == 'native-reset':
            assert n.agent._session_db.promote_to_session_reset(n.agent.session_id)
        elif boundary == 'deleted':
            n.agent._session_db.delete_session(n.agent.session_id)
        elif boundary == 'retained-record-removed':
            # Explicit record-retention fault; no clock or policy changes.
            n.agent._session_db._conn.execute("DELETE FROM supervision_owner_records WHERE kind='claim_delivery'")
            n.agent._session_db._conn.commit()
        else:
            n.engine.on_session_start('another', platform='cli')
            n.engine.on_session_start('session-A', platform='cli')
        output.flush()
        assert output.read() == before
        output.flush()
        assert output.read() == before
        assert not any(r['status'] == 'emitted' for r in rows(n))
        if reopened is not None:
            reopened.close()
