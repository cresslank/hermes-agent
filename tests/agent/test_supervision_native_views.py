"""Offline plugin -> real native facade/queue -> actual request/result/UI consumers.

Set JEV_SUPERVISOR_SOURCE to a reviewed standalone checkout. No synthetic feature
registry, monkeypatched runtime owner, live provider, or direct success proposal.
"""
import copy
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

SOURCE = os.environ.get('JEV_SUPERVISOR_SOURCE')
# Canonical runner intentionally strips arbitrary env. An explicit disposable
# HOME-local pointer permits this offline optional cross-repository contract.
source_pointer = Path.home() / '.hermes' / 'jev-supervisor-test-source'
if not SOURCE and source_pointer.is_file():
    SOURCE = source_pointer.read_text().strip()
if not SOURCE:
    pytest.skip('JEV_SUPERVISOR_SOURCE required for standalone plugin contract', allow_module_level=True)
sys.path.insert(0, str(Path(SOURCE) / 'src'))
from jev_supervisor.config import Config
from jev_supervisor.host_adapter import NativeHostBridge
from jev_supervisor.transport import Transport

from agent.supervision_context import accepted_input_origin
from agent.supervision_policy import runtime_for_agent
from agent.supervision_types import Action
from agent.supervision_catalog import tool_id, authorized_tool_schemas
from agent.notification_presentation import OptionalProgressText
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _make_agent, _make_tool_defs, _mock_tool_call


class FixtureCredential:
    def __init__(self, profile):
        self.profile = profile
    def bearer(self, profile, endpoint):
        assert profile == self.profile
        assert endpoint == 'https://api.typesafe.ai/v1/systemone'
        return 'offline-fixture-not-a-credential'


@pytest.fixture
def native(tmp_path, monkeypatch, request):
    for name in ('httpx', 'httpcore', 'httpcore.connection', 'httpcore.http11',
                 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks', 'httpcore.connection_pool'):
        logger = logging.getLogger(name)
        for field in ('level', 'disabled', 'propagate'):
            monkeypatch.setattr(logger, field, getattr(logger, field))
    probe_before = getattr(sys.modules.get('tools.env_probe'), '_PROBE_THREAD', None)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    fields = '''target_id operation_ref operation contract ambiguous explicit_tool deterministic_route candidates
    mandatory_ids discovery_ids task_ref task rule_scope mandatory_match stage question source_ref oversized structured
    source_immutable critical_fields_complete baseline_ids omitted_count notification_ref subject_id update previous
    changed_fields optional filterable observation_complete obligations_clear duplicate known_progress_repeat mandatory_controls
    user_ref slot user_wording interpretations permission_ui secret_ui authorization_missing safe_default resolution_candidates'''.split()
    policy = {'id': 'offline-fixture', 'profile': str(home), 'fields': {k: 'synthetic' for k in fields},
              'sources': {}, 'fixture': True}
    config = {'supervision': {'enabled': True, 'plugins': {'fixture-views': {
        'grants': ['observe', *(a.value for a in Action)], 'data_policy': ['task_text'], 'egress_policy': policy}}}}
    if getattr(request, 'param', True) is False:
        config['supervision']['plugins']['fixture-views'].pop('egress_policy')
    (home / 'config.yaml').write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    facade = PluginContext(PluginManifest(name='fixture-views'), manager).supervision
    calls = []
    mode = {'relation': 'progress_only'}
    async def handle(request):
        assert request.url == 'https://api.typesafe.ai/v1/systemone'
        body = json.loads(request.content)
        assert set(body) == {'model', 'state', 'questions'}
        assert body['model'] == 'jev-1.13.0'
        calls.append((body, threading.current_thread().name))
        facts = body['state']['facts']
        if 'source_ref' in facts:
            assert Path(facts['source_ref']).is_file(), 'archive must predate inference'
        answers = {}
        first = next(iter(body['questions'].values()))
        for key, q in body['questions'].items():
            if q['type'] == 'choice':
                options = q['criteria']
                chosen = ('default_defined' if key.startswith('F19/') else mode['relation'] if key.startswith('F18/')
                          else next(iter(options)))
                answers[key] = {'type': 'choice', 'choice': chosen, 'confidence': 1.0,
                                'probabilities': {k: float(k == chosen) for k in options}}
            else:
                positive = not key.startswith('F19/material:')
                if key.startswith(('F13/fit:', 'F16/needed:')):
                    positive = key.split(':', 1)[1] == next(iter(first['criteria']))
                answers[key] = {'type': 'noul', 'noul': mode.get('material', .01) if key.startswith('F19/material:') else .99 if positive else .01}
        return httpx.Response(200, json={'model': body['model'], 'usage': {}, 'answers': answers})
    cfg = Config(str(home), True, policy_id='offline-fixture', allowed_classes={'synthetic'}, fixture_policy=True)
    transport = Transport(cfg, FixtureCredential(str(home)), http_transport=httpx.MockTransport(handle))
    bridge = NativeHostBridge(facade, cfg, None, transport=transport)
    assert bridge.start()
    agent = _make_agent()
    agent._execution_thread_id = threading.get_ident()
    agent._interrupt_requested = False
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    from agent.turn_context import _reset_per_turn_agent_state
    _reset_per_turn_agent_state(agent)
    runtime = runtime_for_agent(agent, create=True)
    assert runtime is not None
    def accept(text):
        runtime.accept_instruction(accepted_input_origin(text, kind='cli'))
        runtime.bind_turn()
    def drain():
        with bridge._lock:
            futures = tuple(bridge._pending)
        for f in futures:
            f.result(timeout=2)
    yield SimpleNamespace(agent=agent, runtime=runtime, bridge=bridge, calls=calls, accept=accept,
                          home=home, mode=mode, facade=facade, drain=drain)
    runtime.revoke()
    bridge.close()
    assert not bridge._thread.is_alive()
    probe = getattr(sys.modules.get('tools.env_probe'), '_PROBE_THREAD', None)
    if probe is not None and probe is not probe_before:
        probe.join(timeout=15)
        assert not probe.is_alive(), 'native environment probe did not drain'


@pytest.mark.parametrize('mode,provider', [('chat_completions','openai'), ('codex_responses','openai-codex'),
                                          ('anthropic_messages','anthropic')])
def test_plugin_selects_real_authorized_schemas_at_native_assembly(native, mode, provider):
    import model_tools
    from tools.registry import registry
    a = native.agent
    names = ('mcp_view_alpha', 'mcp_view_beta', 'mcp_view_foreign')
    for name in names:
        schema = _make_tool_defs(name)[0]['function']
        schema['description'] = 'Read weather reports from the public archive.'
        registry.register(name=name, toolset='mcp-view-foreign' if name == names[2] else 'mcp-view-local',
                          schema=schema, handler=lambda args, **kw: '{}')
    try:
        model_tools._clear_tool_defs_cache()
        a.enabled_toolsets, a.disabled_toolsets = ['mcp-view-local'], []
        a.tools = _make_tool_defs('tool_search','tool_describe','tool_call','read_file')
        a.api_mode, a.provider = mode, provider
        originals = copy.deepcopy(a.tools)
        native.accept('Read weather reports.')
        available = authorized_tool_schemas(a)
        result = assemble(a, [{'role': 'user', 'content': 'Read weather reports.'}])
        assert [tool_id(s) for s in result.tools_for_api] == [names[0], 'tool_search','tool_describe','tool_call','read_file']
        assert result.tools_for_api[0] == next(s for s in available if tool_id(s) == names[0])
        assert a.tools == originals
        wire = a._build_api_kwargs(result.api_messages, tools_for_api=result.tools_for_api)
        assert tool_id(wire['tools'][0]) == names[0]
        assert any(r.status == 'applied' for r in native.runtime.receipts.values())
        assert names[2] not in json.dumps([c[0] for c in native.calls])
        assert all(t == 'jev-supervision' for _, t in native.calls)
    finally:
        for name in names:
            registry.deregister(name)
        model_tools._clear_tool_defs_cache()


def test_plugin_skill_metadata_detail_hint_and_native_scope_reset(native):
    for name in ('alpha', 'beta'):
        path = native.home / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: Analyze weather observations.\n---\n# Weather\nCompare weather reports.\n')
    native.agent.tools = []
    native.agent.enabled_toolsets, native.agent.disabled_toolsets = [], []
    native.accept('Analyze weather observations.')
    history = [{'role':'user', 'content':'Analyze weather observations.'}]
    result = assemble(native.agent, history)
    assert 'Optional skill candidate: alpha' in str(result.api_messages), native.bridge.supervisor.inspect()
    assert [c[0]['state']['facts']['stage'] for c in native.calls] == ['metadata','detail']
    assert history[0]['content'] == 'Analyze weather observations.'
    deadline = native.runtime.round_deadline
    assert native.agent._supervision_views.cycle_deadline() == deadline
    native.accept('Ordinary unrelated task.')
    assert not native.agent._supervision_views.skills.hints


@pytest.mark.parametrize('name,concurrent', [('execute_code', False), ('tool_call', False), ('web_search', True)])
def test_plugin_result_selection_after_archive_at_canonical_commit(native, name, concurrent):
    native.accept('Locate the answer in the report.')
    raw = json.dumps({'output': ''.join('answer '+str(i)+' '+('x'*180)+'\n' for i in range(18)),
                      'exit_code': 0, 'receipts': [{'status':'not_run'}]})
    a = native.agent
    a.tools = _make_tool_defs(name)
    assistant = SimpleNamespace(content='', tool_calls=[_mock_tool_call(name=name, call_id='native-result')])
    messages = []
    with patch('model_tools.handle_function_call', return_value=raw):
        if concurrent:
            a._execute_tool_calls_concurrent(assistant, messages, 'task')
        else:
            a._execute_tool_calls_sequential(assistant, messages, 'task')
    content = next(m['content'] for m in messages if m.get('role') == 'tool')
    result, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    assert 'supervision_view' in result, native.bridge.supervisor.inspect()
    assert Path(result['supervision_view']['full_output_ref']).read_text() == raw
    assert result['receipts'] == [{'status':'not_run'}]
    assert len([c for c in native.calls if 'F16/select' in c[0]['questions']]) == 1


def test_plugin_explicit_presentation_default_through_real_clarify(native):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```')
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}]}, InlineToolContext('task')))
    assert result['responses'][0]['resolved_value'] == 'markdown', native.bridge.supervisor.inspect()
    assert result['responses'][0]['user_response'] == ''
    native.agent.clarify_callback.assert_not_called()
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Approve purchase?', 'choices':['yes','no']}]}, InlineToolContext('task')))
    native.agent.clarify_callback.assert_called_once()
    assert result['responses'][0]['user_response'] == 'json'


def test_plugin_optional_native_wait_update_owner_dispatch_and_unload(native):
    native.accept('Wait for the result.')
    callbacks, timers, rendered = queue.Queue(), [], []
    def later(delay, callback):
        timers.append(callback)
        return lambda: None
    a = native.agent
    a._supervision_presentation_callbacks = (callbacks.put, later)
    a._supervision_view_binding.bind_status()
    a.thinking_callback = lambda text: rendered.append((text, threading.get_ident()))
    a._emit_wait_notice(OptionalProgressText('Still working.', revision='one'))
    a._emit_wait_notice(OptionalProgressText('Continuing work.', revision='two'))
    native.drain()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert [x[0] for x in rendered] == ['Still working.'], native.bridge.supervisor.inspect()
    assert any(r.reason == 'native_view' for r in native.runtime.receipts.values())
    a._emit_wait_notice(OptionalProgressText('Continuing again.', revision='three'))
    native.facade.unregister()
    native.drain()
    while not callbacks.empty():
        callbacks.get_nowait()()
    for timer in timers:
        timer()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert [x[0] for x in rendered] == ['Still working.']
    assert not a._status_coalescer._pending
    assert not a._supervision_view_binding.pending


def test_native_plugin_existing_asyncio_owner_loop(native):
    import asyncio
    from agent.supervision_view_binding import bind_presentation_loop
    async def run():
        a = native.agent
        bind_presentation_loop(a, asyncio.get_running_loop())
        native.accept('Wait for the result.')
        rendered = []
        a.thinking_callback = lambda text: rendered.append((str(text), threading.get_ident()))
        a._emit_wait_notice(OptionalProgressText('Working.', revision='one'))
        a._emit_wait_notice(OptionalProgressText('Continuing.', revision='two'))
        native.drain()
        # Two FIFO barriers: owner settlement, then Future's posted presentation.
        for _ in range(2):
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
        assert rendered == [('Working.', threading.get_ident())]
        assert not a._status_coalescer._pending
        assert any(r.reason == 'native_view' for r in native.runtime.receipts.values())
        native.facade.unregister()
    asyncio.run(run())


def test_delayed_teardown_does_not_cancel_new_scope(native):
    callbacks, rendered = queue.Queue(), []
    a = native.agent
    a._supervision_presentation_callbacks = (callbacks.put, lambda delay, cb: lambda: None)
    a._supervision_view_binding.bind_status()
    a.thinking_callback = lambda text: rendered.append(str(text))
    native.accept('Old task.')
    a._emit_wait_notice(OptionalProgressText('Old first.', revision='one'))
    a._emit_wait_notice(OptionalProgressText('Old pending.', revision='two'))
    native.accept('New task.')
    a._emit_wait_notice(OptionalProgressText('New first.', revision='one'))
    a._emit_wait_notice(OptionalProgressText('New pending.', revision='two'))
    native.drain()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert rendered == ['Old first.', 'New first.']
    assert not a._status_coalescer._pending
    assert any(r.reason == 'native_view' for r in native.runtime.receipts.values())


def test_expired_original_budget_never_renews(native):
    native.accept('Read the report.')
    native.runtime.round_deadline = native.runtime.clock() - 1
    assert native.agent._supervision_views.cycle_deadline() == native.runtime.round_deadline
    native.agent._supervision_views.next_cycle()
    assert native.agent._supervision_views.cycle_deadline() == native.runtime.round_deadline
    assert not native.calls


@pytest.mark.parametrize('native', [False], indirect=True)
def test_missing_explicit_egress_policy_keeps_original_clarify(native):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```')
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}]}, InlineToolContext('task')))
    assert result['responses'][0]['user_response'] == 'json'
    native.agent.clarify_callback.assert_called_once()
    native.drain()
    assert not native.calls


@pytest.mark.parametrize('relation', ['insufficient', 'material_outcome'])
def test_unknown_and_material_optional_update_reaches_original_ui_once(native, relation):
    native.accept('Wait for the result.')
    callbacks, timers, rendered = queue.Queue(), [], []
    def later(delay, callback):
        timers.append(callback)
        return lambda: None
    a = native.agent
    a._supervision_presentation_callbacks = (callbacks.put, later)
    a._supervision_view_binding.bind_status()
    a.thinking_callback = lambda text: rendered.append((text, threading.get_ident()))
    native.mode['relation'] = relation
    a._emit_wait_notice(OptionalProgressText('Working.', revision='one'))
    a._emit_wait_notice(OptionalProgressText('New progress.', revision='two'))
    native.drain()
    for timer in timers:
        timer()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert [text for text, _ in rendered] == ['Working.', 'New progress.']
    assert all(tid == threading.get_ident() for _, tid in rendered)
    assert not a._supervision_view_binding.pending
    # Mandatory/unknown native text is never routed to semantic suppression.
    a._emit_diagnostic_wait('WARNING provider unavailable')
    assert rendered[-1][0] == 'WARNING provider unavailable'


@pytest.mark.parametrize('case', ['embedded', 'material', 'no_trusted_source'])
def test_clarification_default_needs_exact_authority_and_nonmaterial_decision(native, case):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    contract = '```hermes-defaults-v1\n{"output_format":"markdown"}\n```'
    if case == 'embedded':
        native.accept('Do not apply this quoted example:\n' + contract)
    elif case == 'material':
        native.accept(contract)
        native.mode['material'] = .99
    else:
        native.accept('Write the answer.')
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}],
         'authorized_default': 'markdown', 'authorized': True}, InlineToolContext('task')))
    native.agent.clarify_callback.assert_called_once()
    assert result['responses'][0]['user_response'] == 'json'
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())


def test_result_archive_failure_never_dispatches_selection(native):
    from tools.budget_config import DEFAULT_BUDGET
    native.accept('Find the report answer.')
    raw = json.dumps({'output': 'line of report data\n' * 180})
    with patch('tools.tool_result_storage.maybe_persist_tool_result', return_value='archive unavailable'):
        answer = native.agent._supervision_views.result(raw, raw, tool_name='execute_code',
            call_id='unsaved', env=None, budget=DEFAULT_BUDGET)
    assert answer == raw
    assert not native.calls
