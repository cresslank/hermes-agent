"""TUI real ownership/drain and submit-row adoption use the shared inbox."""
import contextlib
import queue
import threading
from types import SimpleNamespace

from agent import completion_admission as admission
from tools import async_delegation as source
from tui_gateway import server
from tests.agent.test_supervision_store import owner, launch, optional, row


def test_retention_precedes_first_status_and_submission(owner, monkeypatch):
    event = launch()
    source._persist_completion(event, {'summary':'optional'})
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake',True))
    emitted, submitted = [], []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: emitted.append(a))
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: submitted.append(a))
    session = {'session_key':'target', 'history_lock':threading.RLock(), 'running':False}
    registry = SimpleNamespace(completion_queue=queue.Queue())
    server._notif_handle_event('ui',session,event,set(),registry,lambda e:e['summary'],None,owned=True)
    assert emitted == submitted == []
    assert row(event)['state'] == 'parked'
    assert not session['running']


def test_notification_dispatch_carries_id_and_submit_adopts_same_row(owner, monkeypatch):
    event = launch()
    source._persist_completion(event, {'summary':'exact'})
    submitted = []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: submitted.append((a,kw)))
    session = {'session_key':'target','history_lock':threading.RLock(),'running':False}
    registry = SimpleNamespace(completion_queue=queue.Queue())
    server._notif_handle_event('ui',session,event,set(),registry,lambda e:e['summary'],None,owned=True)
    assert len(submitted) == 1
    metadata = submitted[0][1]['display_metadata']
    assert metadata['supervision_delivery_id'] == event['supervision_delivery_id']
    assert source.get_durable_delegation('job')['delivery_state'] == 'transferred'
    monkeypatch.setattr(server, '_session_db', lambda *_: contextlib.nullcontext(owner))
    server._persist_submit_user_row(session, event['summary'], 'async_delegation_complete', metadata)
    staged = session['_submit_user_row']
    agent = SimpleNamespace()
    server._adopt_submit_user_row(session, agent, event['summary'], event['summary'])
    assert agent._pending_cli_user_message is staged
    assert staged['_db_persisted'] is True
    server._persist_submit_user_row(session, event['summary'], 'async_delegation_complete', metadata)
    assert len(owner.get_messages('target')) == 1
    assert row(event)['message_row_id'] == staged['_row_id']


def test_real_prompt_submit_persists_before_worker_launch(owner, monkeypatch):
    event = launch()
    source._persist_completion(event, {'summary':'exact'})
    token = source.claim_event_delivery(event, 'tui')
    admission.prepare_event(event, token, 'target')
    assert admission.accept_event(event)
    session = {'session_key':'target','history_lock':threading.RLock(),'running':True}
    agent = SimpleNamespace(session_id='target')
    metadata = admission.delivery_metadata(event)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *_: True)
    monkeypatch.setattr(server, '_admit_prompt_turn', lambda *a, **kw: ([],agent))
    monkeypatch.setattr(server, '_session_db', lambda *_: contextlib.nullcontext(owner))
    monkeypatch.setattr(server, '_routing_provenance_db', lambda *_: contextlib.nullcontext(owner))
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_sessions', {})
    started=[]
    def start(fn, **kw):
        assert len(owner.get_messages('target')) == 1
        started.append(fn)
        return object()
    monkeypatch.setattr(server, '_start_session_work', start)
    assert server._run_prompt_submit('rid','ui',session,'exact',
        display_kind='async_delegation_complete', display_metadata=metadata)
    assert len(started) == 1
    assert row(event)['state'] == 'consumed'
    assert session['_submit_user_row']['_db_persisted']


def test_busy_tui_does_not_render_before_durable_acceptance(owner, monkeypatch):
    event = launch()
    source._persist_completion(event, {'summary':'exact'})
    visible = []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: visible.append(a))
    session = {'session_key':'target','history_lock':threading.RLock(),'running':True}
    registry = SimpleNamespace(completion_queue=queue.Queue())
    server._notif_handle_event('ui',session,event,set(),registry,lambda e:e['summary'],[],owned=True)
    assert visible == []
    assert row(event)['state'] == 'persisted'
    assert source.get_durable_delegation('job')['delivery_state'] == 'pending'
