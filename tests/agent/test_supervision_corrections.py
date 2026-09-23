"""Ordinary late native source hydration -> durable completion -> main-owned notice.

Strictly offline. Uses real declaration/adoption/original emission, source owner,
async dispatch/finalization and CLI admission; only model/SDK I/O is substituted.
"""
import asyncio
from contextlib import contextmanager
import copy
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from tests.agent.test_supervision_claim_uses import native  # noqa: F401
from tests.agent.test_supervision_original_output import setup, finalize, original_rows
from agent.supervision_corrections import POLICY
from agent.subagent_lifecycle import bind_subagent_parent


@contextmanager
def sink(n, monkeypatch, path, surface):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    from hermes_cli.cli_stream_mixin import CLIStreamMixin
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    from prompt_toolkit.input import DummyInput
    from tui_gateway import server
    from tui_gateway.transport import StdioTransport
    from gateway.config import PlatformConfig, Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.run_notifications import GatewayNotificationsMixin
    from tui_gateway.agent_callbacks import _agent_notice_update
    stream = path.open('w+', encoding='utf-8')
    calls = []
    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=len(calls))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='offline-fixture', typing_indicator=False))
    adapter._bot = Bot()
    adapter._session_store = SimpleNamespace(peek_session_id=lambda _: n.agent.session_id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-4242', chat_type='group', thread_id='17')
    event = MessageEvent(text='question', source=source, message_id='8')
    owner = SimpleNamespace(agent=n.agent, _stream_started=False, _stream_box_opened=False,
        final_response_markdown='raw', _scrollback_box_width=lambda: 2048)
    gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter)
    session = {'transport': StdioTransport(lambda: stream, threading.Lock()), 'agent': n.agent,
        'history_lock': threading.RLock()}
    monkeypatch.setitem(server._sessions, 'original-ui', session)
    async def telegram_original(text):
        await adapter.send_final_ledgered(event, 'original-session', text, {'thread_id': '17', 'notify': True}, reply_to=None)
    def emit_original(result):
        if surface == 'cli':
            turn = SimpleNamespace(result=result, use_streaming_tts=False, box_opened=False)
            CLIChatTurnMixin._chat_print_response_panel(owner, turn, result['final_response'])
        elif surface == 'telegram':
            asyncio.run(telegram_original(result['final_response']))
        else:
            server._emit('message.complete', 'original-ui', {'text': result['final_response']})
    def notice(value):
        if surface == 'cli':
            CLIStreamMixin._on_notice(owner, value)
        elif surface == 'telegram':
            asyncio.run(GatewayNotificationsMixin._deliver_platform_notice(gateway, source, value.text))
        else:
            _agent_notice_update('original-ui', value)
    def flush():
        if surface == 'cli':
            CLIStreamMixin._flush_credit_notices(owner)
    def readback():
        stream.flush()
        return path.read_text() if surface != 'telegram' else json.dumps(calls)
    with create_app_session(input=DummyInput(), output=PlainTextOutput(stream)):
        yield SimpleNamespace(original=emit_original, notice=notice, flush=flush, read=readback,
            stream=stream, calls=calls, adapter=adapter, source=source, session=session)
    stream.close()


def rows(n):
    with n.agent._session_db._lock:
        return [json.loads(r[0]) for r in n.agent._session_db._conn.execute(
            "SELECT body_json FROM supervision_owner_records WHERE kind='correction'")]


def launch_late(n, monkeypatch):
    from tools import async_delegation as jobs
    from tools.process_registry import process_registry
    from tests.agent.supervision_test_support import Agent
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    child = Agent()
    child.session_id = 'child-source'
    child.context_compressor = n.engine.clone_for_agent()
    child.context_compressor.on_session_start(child.session_id, platform='subagent')
    # The native dispatch's child slot is canonical; source custody binds to it,
    # not to a claimed ref or a relevance boolean in the summary.
    child._subagent_id = 'late-native-child'
    released, hydrated = threading.Event(), threading.Event()
    errors = []
    # B was committed by the native store, but only this later ordinary pack use
    # supplies worker custody. No synthetic SourceProposition/owner row is used.
    with n.engine._store._write_lock:
        raw = n.engine._store._conn.execute('SELECT store_id,content FROM messages WHERE session_id=? ORDER BY store_id DESC LIMIT 1',
            ('session-A',)).fetchone()
    ref = f'lcm:{raw[0]}:0-{len(raw[1])}'
    def runner():
        assert released.wait(10), 'native child release missing'
        try:
            with bind_subagent_parent(child):
                result = child.context_compressor.handle_tool_call('lcm_evidence_pack',
                    {'question': 'What is the mass of asset A?', 'baseline_refs': [{'exact_ref': ref}],
                     'budgets': {'max_retrieval_calls': 0}})
            assert not json.loads(result).get('error'), result
            return {'status': 'completed', 'summary': 'The authorized source check completed.'}
        except BaseException as exc:
            errors.append(exc)
            raise
        finally:
            hydrated.set()
    with bind_subagent_parent(n.agent):
        handle = jobs.dispatch_async_delegation(goal='Check the mass source', context=None,
            toolsets=None, role='leaf', model=None, session_key=n.agent.session_id,
            parent_session_id=n.agent.session_id, runner=runner, control_children=[child])
    assert handle['status'] == 'dispatched', handle
    return SimpleNamespace(released=released, hydrated=hydrated, errors=errors, child=child,
        handle=handle, events=process_registry.completion_queue)


def admit(n, late):
    from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
    from agent.completion_admission import consume_metadata
    event = late.events.get(timeout=10)
    assert event['delegation_id'] == late.handle['delegation_id']
    late.events.put(event)
    owner = SimpleNamespace(session_id=n.agent.session_id, _session_db=n.agent._session_db,
        _pending_input=queue.Queue(), _owns_process_notification=lambda e: e.get('session_key') == n.agent.session_id)
    CLIProcessNotificationsMixin._drain_process_notifications(owner, 'correction-fixture')
    pending = owner._pending_input.get_nowait()
    assert pending.supervision_metadata
    consume_metadata(n.agent._session_db, n.agent.session_id, str(pending), pending.supervision_metadata)
    return event, pending


def drain_source(n, event):
    from tools.process_registry import process_registry
    from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
    process_registry.completion_queue.put(event)
    owner = SimpleNamespace(session_id=n.agent.session_id, _session_db=n.agent._session_db,
        _pending_input=queue.Queue(), _owns_process_notification=lambda e: e.get('session_key') == n.agent.session_id)
    CLIProcessNotificationsMixin._drain_process_notifications(owner, 'source-fixture')
    pending = owner._pending_input.get_nowait()
    assert pending.display_kind == 'literal_source_change'
    return pending


def configured(n):
    config = json.loads((n.home / 'config.yaml').read_text())
    config['supervision']['plugins']['hermes-lcm']['literal_sources']['correction_review'] = POLICY
    (n.home / 'config.yaml').write_text(json.dumps(config))


@pytest.mark.parametrize('surface', ['cli', 'telegram', 'tui'])
@pytest.mark.parametrize('change', ['append', 'delete'])
def test_committed_source_change_enters_native_admission(native, monkeypatch, tmp_path, surface, change):
    from tools.process_registry import process_registry
    from tests.agent.test_supervision_claim_uses import append, record
    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path / 'source-sink', surface) as output:
        output.original(finalize(n, messages, monkeypatch))
        if change == 'append':
            data = record()
            data['value'] = 5
            appended = append(n, data)
        else:
            n.engine._store.delete_session_messages(n.agent.session_id)
        event = process_registry.completion_queue.get_nowait()
        assert event['type'] == 'literal_source_change' and 'delegation_id' not in event
        assert n.engine._store._conn.execute('SELECT value FROM metadata WHERE key=?',
            (event['source_event_id'],)).fetchone()
        drain_source(n, event)
        n.agent.notice_callback = output.notice
        n.rt.bind_turn()
        n.rt.finish_turn()
        output.flush()
        result = rows(n)
        assert len(result) == 1
        if change == 'append':
            assert result[0]['status'] == 'emitted', result
            assert appended[2] in result[0]['source_refs']
            assert output.read().count('Correction:') == 1
        else:
            assert result[0]['status'] == 'source_unavailable', result
            assert 'Correction:' not in output.read()
        from agent.completion_admission import prepare_event
        assert not prepare_event(event, '', n.agent.session_id).present


@pytest.mark.parametrize('failure', ['cancel', 'new-work', 'revoke', 'audience', 'before-write', 'after-send', 'source-delete'])
def test_notice_failures_never_replay_or_claim_emitted(native, monkeypatch, tmp_path, failure, request):
    from tools.process_registry import process_registry
    from tests.agent.test_supervision_claim_uses import append, record
    from agent import supervision_planning_records
    import sqlite3
    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path / 'failure-sink', 'cli') as output:
        output.original(finalize(n, messages, monkeypatch))
        data = record()
        data['value'] = 5
        append(n, data)
        event = process_registry.completion_queue.get_nowait()
        drain_source(n, event)
        n.agent.notice_callback = output.notice
        n.rt.bind_turn()
        n.rt.finish_turn()  # CLI queues; no send yet
        assert rows(n)[0]['status'] == 'contested'
        before = output.read()
        locked = None
        if failure == 'cancel':
            n.agent._active_children_lock = threading.Lock()
            n.agent._active_children = []
            request.addfinalizer(n.agent.clear_interrupt)
            n.agent.interrupt()
        elif failure == 'new-work':
            from tests.agent.supervision_test_support import accept
            accept(n.agent, 'Stop considering the previous task.', message_id='new-work')
        elif failure == 'revoke':
            n.ctx.supervision._literal_source_registration.close()
        elif failure == 'audience':
            from prompt_toolkit.application.current import get_app_session
            get_app_session()._output = __import__('prompt_toolkit.output', fromlist=['DummyOutput']).DummyOutput()
        elif failure == 'before-write':
            locked = sqlite3.connect(n.agent._session_db.db_path, timeout=0)
            locked.execute('BEGIN IMMEDIATE')
        elif failure == 'after-send':
            native_save = supervision_planning_records.save_correction
            def fail_ack(owner, row, **kwargs):
                return False if row['status'] == 'emitted' else native_save(owner, row, **kwargs)
            monkeypatch.setattr(supervision_planning_records, 'save_correction', fail_ack)
        else:
            n.engine._store.delete_session_messages(n.agent.session_id)
        try:
            output.flush()
        finally:
            if locked is not None:
                locked.rollback()
                locked.close()
        after = output.read()
        assert rows(n)[0]['status'] != 'emitted'
        assert (after.count('Correction:') == 1) is (failure == 'after-send')
        if failure != 'after-send':
            assert before == after
        output.flush()
        assert output.read() == after


@pytest.mark.parametrize('surface', ['cli', 'telegram', 'tui'])
@pytest.mark.parametrize('later_work', [False, True])
def test_late_native_completion_correction_once(native, monkeypatch, tmp_path, surface, later_work):
    n = native
    config = json.loads((n.home / 'config.yaml').read_text())
    config['supervision']['plugins']['hermes-lcm']['literal_sources']['correction_review'] = POLICY
    (n.home / 'config.yaml').write_text(json.dumps(config))
    messages, _ = setup(n, monkeypatch)
    late = launch_late(n, monkeypatch)
    try:
        with sink(n, monkeypatch, tmp_path / 'sink', surface) as output:
            result = finalize(n, messages, monkeypatch)
            output.original(result)
            originals = original_rows(n)
            assert len(originals) == 1 and originals[0]['status'] == 'original_emitted'
            assert n.ctx.supervision._literal_source_registration.provider.correction_custody
            if later_work:
                from tests.agent.supervision_test_support import accept
                accept(n.agent, 'An unrelated next question.', message_id='later-question')
                n.rt.bind_turn()
                n.rt.finish_turn()
            late.released.set()
            assert late.hydrated.wait(10) and not late.errors, late.errors
            event, pending = admit(n, late)
            assert len(rows(n)) == 1, (rows(n), getattr(n.rt, '_correction_candidates', None))
            n.agent.notice_callback = output.notice
            n.rt.bind_turn()  # ordinary admitted continuation, not authenticated user input
            n.rt.finish_turn()
            output.flush()
            found = rows(n)
            assert found[0]['status'] == 'emitted', found
            assert found[0]['truth_winner'] is None
            assert found[0]['relevance'] == 'native_whole_answer_dependency.v1'
            assert found[0]['required_answer_intent'] == 'unknown'
            assert found[0]['original_id'] == originals[0]['id']
            assert found[0]['observation']['transport_generation'] == originals[0]['observation']['transport_generation']
            actual = output.read()
            assert actual.count('Correction:') == 1 and 'does not establish which source is correct' in actual
            from agent.completion_admission import prepare_event
            prepare_event(event, event['_supervision_claim'], n.agent.session_id)
            n.rt.bind_turn()
            n.rt.finish_turn()
            output.flush()
            assert output.read() == actual
            assert not n.calls  # exact numeric invalidation requires no inference
    finally:
        late.released.set()
        assert late.hydrated.wait(10)
        late.child.context_compressor.shutdown()
