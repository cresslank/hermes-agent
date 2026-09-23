"""F22 continuation integration: public native workers and real surface admission.

Only model HTTP and Telegram SDK I/O are doubles. Sources, child identities,
completion lifecycle, admission, conversation loop and correction writes are native.
The existing original-output fixture supplies the already-qualified prerequisite.
"""
import asyncio
import json
import queue
import threading
from types import SimpleNamespace

import httpx
import openai
import pytest

from tests.agent.test_supervision_claim_uses import native  # noqa: F401
from tests.agent.test_supervision_corrections import configured, rows, sink
from tests.agent.test_supervision_original_output import setup, finalize, original_rows


@pytest.fixture
def ordinary(tmp_path, monkeypatch, request):
    from run_agent import AIAgent
    from hermes_cli import plugins
    from tools.process_registry import process_registry
    from tools import async_delegation

    from tests.agent import test_supervision_claim_uses as prerequisites
    def parent_agent():
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        config = json.loads((home / 'config.yaml').read_text(encoding='utf-8'))
        config['model'] = {'context_length': 131072}
        (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
        return AIAgent(session_id='session-A', api_key='synthetic-only',
            base_url='https://fixture.invalid/v1', provider='openai-compat', model='fixture-model',
            max_iterations=4, quiet_mode=True, enabled_toolsets=['context_engine'],
            skip_context_files=True, skip_memory=True)
    monkeypatch.setattr(prerequisites, 'Agent', parent_agent)
    prerequisite = native.__wrapped__(tmp_path, monkeypatch, request)
    n = next(prerequisite)
    configured(n)
    config = json.loads((n.home / 'config.yaml').read_text(encoding='utf-8'))
    config.update(context={'engine': n.engine.name},
                  model={'context_length': 131072},
                  compression={'enabled': False},
                  memory={'memory_enabled': False, 'user_profile_enabled': False},
                  delegation={'max_iterations': 4})
    (n.home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(plugins, '_plugin_manager', n.manager)
    monkeypatch.setitem(plugins._plugin_managers_by_home, n.home.resolve(), n.manager)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    requests = []
    release, started = threading.Event(), threading.Event()
    source_ref = []
    child_requests, children = [], []

    def respond(request, *, is_child):
        assert str(request.url) == 'https://fixture.invalid/v1/chat/completions'
        body = json.loads(request.content)
        assert body['model'] == 'fixture-model'
        requests.append(body)
        messages = body['messages']
        tools = [m for m in messages if m['role'] == 'tool']
        tool_calls = None
        content = 'The authorized source check completed.' if is_child else 'Continuation complete.'
        if is_child:
            child_requests.append(body)
            if not tools:
                started.set()
                assert release.wait(10), 'test did not release the model transport'
                assert len(source_ref) == 1
                tool_calls = [dict(id='fixture-source-read', type='function', function=dict(
                    name='lcm_evidence_pack', arguments=json.dumps(dict(
                        question='What is the mass of asset A?',
                        baseline_refs=[dict(exact_ref=source_ref[0])],
                        budgets=dict(max_retrieval_calls=0)))))]
                content = None
            else:
                text = tools[-1]['content']
                assert '{' in text, text
                result, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
                assert not result.get('error'), result
                assert source_ref[0] in tools[-1]['content'], result
        if body.get('stream'):
            delta = {'role': 'assistant', 'content': content}
            if tool_calls:
                delta['tool_calls'] = [dict(index=0, **tool_calls[0])]
            chunks = [dict(id='fixture-response', object='chat.completion.chunk', created=1,
                model=body['model'], choices=[dict(index=0, delta=delta, finish_reason=None)]),
                dict(id='fixture-response', object='chat.completion.chunk', created=1,
                model=body['model'], choices=[dict(index=0, delta={},
                    finish_reason='tool_calls' if tool_calls else 'stop')])]
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
        return httpx.Response(200, json=dict(id='fixture-response', object='chat.completion',
            created=1, model=body['model'], choices=[dict(index=0,
                message=dict(role='assistant', content=content, tool_calls=tool_calls),
                finish_reason='tool_calls' if tool_calls else 'stop')],
            usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120)))

    clients = []
    def model_client(agent, *args, **kwargs):
        if agent.platform == 'subagent' and agent not in children:
            children.append(agent)
        client = openai.OpenAI(api_key='synthetic-only', base_url='https://fixture.invalid/v1',
            http_client=httpx.Client(transport=httpx.MockTransport(
                lambda request: respond(request, is_child=agent.platform == 'subagent'))))
        clients.append(client)
        return client
    monkeypatch.setattr(AIAgent, '_create_request_openai_client', model_client)
    agent = n.agent
    agent._owns_session_db = False  # the prerequisite fixture owns this canonical connection
    yield SimpleNamespace(n=n, requests=requests, child_requests=child_requests, release=release,
                          started=started, source_ref=source_ref, children=children, events=process_registry.completion_queue)
    release.set()
    async_delegation._reset_for_tests()
    agent.close()
    with pytest.raises(StopIteration):
        next(prerequisite)
    for client in clients:
        client.close()


def original(o, monkeypatch, output, *, launch_surface=None):
    """Reuse the original prerequisite without retaining its old fake finalizer seams."""
    n = o.n
    before = dict(vars(n.agent))
    with monkeypatch.context() as scoped:
        messages, _ = setup(n, scoped)
        handle = launch(o, launch_surface) if launch_surface is not None else None
        output.original(finalize(n, messages, scoped))
    # setup/finalize's unit-finalizer conveniences must not replace a real turn.
    for name in ('_save_trajectory', '_cleanup_task_resources', '_drop_trailing_empty_response_scaffolding',
                 '_persist_session', '_apply_persist_user_message_override', '_sync_external_memory_for_turn',
                 'iteration_budget', '_file_mutation_verifier_enabled', '_turn_completion_explainer_enabled',
                 '_build_api_kwargs', '_reapply_reasoning_echo_for_provider', 'tools'):
        if name in before:
            setattr(n.agent, name, before[name])
        else:
            vars(n.agent).pop(name, None)
    assert original_rows(n)[0]['status'] == 'original_emitted'
    raw = n.engine._store._conn.execute(
        'SELECT store_id,content FROM messages WHERE session_id=? ORDER BY store_id DESC LIMIT 1',
        (n.agent.session_id,)).fetchone()
    o.source_ref[:] = [f'lcm:{raw[0]}:0-{len(raw[1])}']
    return handle


def launch(o, surface='cli'):
    from tools.delegate_tool import delegate_task
    from agent.subagent_lifecycle import bind_subagent_parent
    from gateway.session_context import set_session_vars, clear_session_vars
    from gateway.session import SessionSource, build_session_key
    from gateway.config import Platform
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-4242', chat_type='group', thread_id='17')
    tokens = set_session_vars(platform='telegram' if surface == 'telegram' else surface,
        session_id=o.n.agent.session_id, session_key=build_session_key(source) if surface == 'telegram' else o.n.agent.session_id,
        chat_id='-4242' if surface == 'telegram' else '', chat_type='group',
        thread_id='17' if surface == 'telegram' else '', ui_session_id='original-ui' if surface == 'tui' else '')
    try:
        with bind_subagent_parent(o.n.agent):
            handle = json.loads(delegate_task(goal='Check the mass source', parent_agent=o.n.agent, background=True))
    finally:
        clear_session_vars(tokens)
    assert handle['status'] == 'dispatched', handle
    assert o.started.wait(10), handle
    return handle


def continue_cli(o, event, output):
    from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    owner = SimpleNamespace(session_id=o.n.agent.session_id, _session_db=o.n.agent._session_db,
        _pending_input=queue.Queue(), conversation_history=[],
        _owns_process_notification=lambda e: e.get('session_key') == o.n.agent.session_id)
    o.events.put(event)
    CLIProcessNotificationsMixin._drain_process_notifications(owner, 'ordinary')
    pending = owner._pending_input.get_nowait()
    o.n.agent.notice_callback = output.notice
    CLIChatTurnMixin._chat_stage_user_message(owner, o.n.agent, pending)
    result = o.n.agent.run_conversation(str(pending))
    assert not result.get('failed'), result
    output.flush()
    before = output.read()
    calls = len(o.requests)
    o.events.put(event)
    CLIProcessNotificationsMixin._drain_process_notifications(owner, 'ordinary-replay')
    assert owner._pending_input.empty()
    assert len(o.requests) == calls and output.read() == before


@pytest.mark.parametrize('surface', ['cli', 'telegram', 'tui'])
def test_public_child_continuation(ordinary, monkeypatch, tmp_path, surface):
    from tools import async_delegation
    o = ordinary
    with sink(o.n, monkeypatch, tmp_path / 'sink', surface) as output:
        handle = original(o, monkeypatch, output, launch_surface=surface)
        o.release.set()
        event = o.events.get(timeout=10)
        assert event['delegation_id'] == handle['delegation_id']
        durable = async_delegation.get_durable_delegation(handle['delegation_id'])
        assert event['status'] == 'completed', durable
        with async_delegation._connect() as conn:
            child_ids = json.loads(conn.execute('SELECT control_child_ids FROM async_delegations '
                'WHERE delegation_id=?', (handle['delegation_id'],)).fetchone()[0])
        assert child_ids == [o.children[0]._subagent_id]
        assert len(child_ids) == 1 and child_ids[0].startswith('sa-0-')
        assert o.n.agent._session_db.get_session(o.children[0].session_id)['parent_session_id'] == o.n.agent.session_id
        assert len(o.child_requests) == 2
        continue_surface(o, event, output, surface)
        assert rows(o.n)[0]['status'] == 'emitted', rows(o.n)
        assert output.read().count('Correction:') == 1
        assert not o.n.calls
        with async_delegation._connect() as conn:
            admission = conn.execute('SELECT state,disposition FROM supervision_admissions WHERE launch_id=?',
                                     (handle['delegation_id'],)).fetchone()
        assert tuple(admission) == ('consumed', 'deliver_unchanged')


def continue_gateway(o, event, output):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from hermes_state import AsyncSessionDB
    from gateway.run import _format_gateway_process_notification as format_gateway_process_notification

    async def run():
        runner = GatewayRunner(GatewayConfig())
        runner._session_db = AsyncSessionDB(o.n.agent._session_db)
        runner.adapters = {Platform.TELEGRAM: output.adapter}
        from gateway.session import build_session_key
        runner._cache_session_source(build_session_key(output.source), output.source)
        received = []
        async def handler(message):
            assert message.internal is True
            received.append(message)
            if event['type'] == 'async_delegation':
                from tools import async_delegation
                with async_delegation._connect() as conn:
                    state = conn.execute('SELECT state FROM supervision_admissions WHERE launch_id=?',
                                         (event['delegation_id'],)).fetchone()[0]
                assert state == 'accepted'  # adapter acceptance is not turn consumption
            # Preserve the production callback boundary, including queued native notices.
            from gateway.run_turn_runner import TurnRunner
            from gateway.turn_context import TurnContext
            ctx = TurnContext(source=output.source, user_config={}, mute_notification_reply=False,
                _status_adapter=output.adapter, _run_still_current=lambda: True,
                _loop_for_step=asyncio.get_running_loop())
            turn = TurnRunner(runner, ctx)
            scheduled = []
            native_schedule = turn._schedule
            def track_schedule(*args, **kwargs):
                future = native_schedule(*args, **kwargs)
                scheduled.append(future)
                return future
            turn._schedule = track_schedule
            o.n.agent.notice_callback = turn._notice_callback_sync
            result = await asyncio.to_thread(o.n.agent.run_conversation, message.text,
                persist_user_display_metadata=message.metadata,
                persist_user_display_kind='literal_source_change' if event['type'] == 'literal_source_change' else 'async_delegation_complete')
            assert not result.get('failed'), result
            await asyncio.gather(*(asyncio.wrap_future(f) for f in scheduled))
        output.adapter.set_message_handler(handler)
        try:
            if event['type'] == 'literal_source_change':
                pending = queue.Queue()
                pending.put(event)
                await runner._drain_watch_notifications(pending)
                assert pending.empty(), event
            else:
                accepted = await runner._deliver_completion_notification(format_gateway_process_notification(event), event)
                assert accepted is True, (accepted, event)
            while output.adapter._background_tasks:
                await asyncio.gather(*tuple(output.adapter._background_tasks))
            assert len(received) == 1
            assert await runner._deliver_completion_notification(format_gateway_process_notification(event), event) is None
        finally:
            await runner._cancel_process_completion_batch_tasks()
    asyncio.run(run())


def continue_tui(o, event, output):
    from tui_gateway import server
    from tests.tui_gateway.test_auto_continue import _session
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import format_process_notification
    defaults = _session(agent=o.n.agent, session_key=o.n.agent.session_id,
                        profile_home=str(o.n.home), cwd=str(o.n.home), cols=2048)
    for key, value in defaults.items():
        output.session.setdefault(key, value)
    for name, callback in server._agent_cbs('original-ui').items():
        setattr(o.n.agent, name, callback)
    emitted = set()
    assert server._notif_handle_event('original-ui', output.session, event, emitted,
                                     process_registry, format_process_notification, [])
    worker = output.session.get('_run_thread')
    assert worker is not None, output.read()
    worker.join(10)
    assert not worker.is_alive()
    assert output.session['running'] is False
    assert not output.session.get('inflight_turn'), output.read()
    before, calls = output.read(), len(o.requests)
    assert server._notif_handle_event('original-ui', output.session, event, emitted,
                                     process_registry, format_process_notification, [])
    assert output.session.get('_run_thread') is worker
    assert not output.session['running']
    assert output.read() == before and len(o.requests) == calls


def continue_surface(o, event, output, surface):
    return {'cli': continue_cli, 'telegram': continue_gateway, 'tui': continue_tui}[surface](o, event, output)


@pytest.mark.parametrize('surface', ['cli', 'telegram', 'tui'])
@pytest.mark.parametrize('change', ['append', 'delete'])
def test_source_ingress_continuation(ordinary, monkeypatch, tmp_path, surface, change):
    from tests.agent.test_supervision_claim_uses import append, record
    o = ordinary
    with sink(o.n, monkeypatch, tmp_path / 'source-sink', surface) as output:
        original(o, monkeypatch, output)
        if change == 'append':
            data = record()
            data['value'] = 5
            append(o.n, data)
        else:
            o.n.engine._store.delete_session_messages(o.n.agent.session_id)
        event = o.events.get(timeout=10)
        assert event['type'] == 'literal_source_change'
        continue_surface(o, event, output, surface)
        assert len(rows(o.n)) == 1
        expected = 'emitted' if change == 'append' else 'source_unavailable'
        assert rows(o.n)[0]['status'] == expected, rows(o.n)
        assert output.read().count('Correction:') == (1 if change == 'append' else 0)
        assert not o.n.calls


@pytest.mark.parametrize('structured', [False, True])
def test_native_source_event_api_never_self_posts_or_wakes(ordinary, monkeypatch, tmp_path, structured):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session import SessionSource, build_session_key
    from tests.agent.test_supervision_claim_uses import append, record
    o = ordinary
    with sink(o.n, monkeypatch, tmp_path / 'api-source', 'cli') as output:
        original(o, monkeypatch, output)
        data = record()
        data['value'] = 5
        append(o.n, data)
        event = o.events.get(timeout=10)
        assert event['type'] == 'literal_source_change'
        runner = GatewayRunner(GatewayConfig())
        api = APIServerAdapter(PlatformConfig())
        runner.adapters = {Platform.API_SERVER: api}
        if structured:
            source = SessionSource(platform=Platform.API_SERVER, chat_id=o.n.agent.session_id, chat_type='dm')
            event['session_key'] = build_session_key(source)
            runner._cache_session_source(event['session_key'], source)
        before = output.read()
        async def denied(*a, **kw):
            pytest.fail('source events must not self-post or wake API sessions')
        monkeypatch.setattr(runner, '_self_post_api_server', denied)
        api.set_message_handler(denied)
        async def route():
            pending = queue.Queue()
            pending.put(event)
            await runner._drain_watch_notifications(pending)
            assert pending.get_nowait() is event  # retained, never acknowledged as delivered
            assert not api._background_tasks
        asyncio.run(route())
        assert not o.requests and not rows(o.n)
        assert output.read() == before


@pytest.mark.parametrize('surface', ['cli', 'telegram', 'tui'])
def test_source_event_then_public_worker_same_pair_does_not_repeat(ordinary, monkeypatch, tmp_path, surface):
    from tests.agent.test_supervision_claim_uses import append, record
    from tools import async_delegation
    o = ordinary
    with sink(o.n, monkeypatch, tmp_path / 'dedup-sink', surface) as output:
        handle = original(o, monkeypatch, output, launch_surface=surface)
        data = record()
        data['value'] = 5
        _, _, ref = append(o.n, data)
        o.source_ref[:] = [ref]
        source_event = o.events.get(timeout=10)
        assert source_event['type'] == 'literal_source_change'
        continue_surface(o, source_event, output, surface)
        assert output.read().count('Correction:') == 1, rows(o.n)
        o.release.set()
        completion = o.events.get(timeout=10)
        assert completion['delegation_id'] == handle['delegation_id']
        continue_surface(o, completion, output, surface)
        assert len(rows(o.n)) == 1, rows(o.n)
        assert output.read().count('Correction:') == 1
        # Unknown/required F02 completion still reaches the main turn, independently
        # of the correction incident already being emitted by source ingress.
        with async_delegation._connect() as conn:
            state = conn.execute('SELECT state FROM supervision_admissions WHERE launch_id=?',
                                 (handle['delegation_id'],)).fetchone()[0]
        assert state == 'consumed'
