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
from jev_supervisor.host_adapter import NativeHostBridge, NativeRegistration, GRANTS
from jev_supervisor.transport import Transport
from jev_supervisor.policies import POLICIES

from agent.supervision_context import accepted_input_origin
from agent.supervision_policy import runtime_for_agent
from agent.supervision_types import Action
from agent.supervision_catalog import tool_id, authorized_tool_schemas
from agent.notification_presentation import OptionalProgressText
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _make_agent, _make_tool_defs, _mock_tool_call


class NegotiatedViewCodecBridge(NativeHostBridge):
    """Contract-only codec extension while the standalone adapter is unmerged.

    Real full-registry features, transport, arbiter and proposals remain unchanged.
    This is NOT qualification of the installed plugin's three missing codecs; it
    proves the newly advertised exact actions reach real native owners, so the
    standalone integrator can add these mappings without inventing host effects.
    """
    CODECS = {'present_material_once': 'present_status', 'retrieve': 'clarify_retrieve',
              'ask_material': 'clarify_ask'}

    def register_provider(self, version, supervisor):
        negotiated = self.native.negotiate(version)
        assert negotiated['view_actions_version'] == 'supervision.view-actions.v1'
        assert negotiated['view_actions'] == self.CODECS
        value = self.native.register(version=version, consumer=self.observe,
                                     requested_grants=(*GRANTS, *self.CODECS.values()))
        self._registration = NativeRegistration(self, value)
        return self._registration

    def submit(self, proposal):
        if proposal['action'] not in self.CODECS:
            return super().submit(proposal)
        assert not self._closed and not self._revoked
        owner = self._owner_for(proposal['expected'], proposal['target_id'])
        assert owner is not None
        metadata = proposal['metadata']
        result = {k: proposal[k] for k in ('proposal_id', 'plugin_generation', 'feature_id', 'incident_id',
            'expected', 'target_id', 'evidence_refs', 'expires_at_monotonic', 'template_id')}
        result.update(owner=owner, action=self.CODECS[proposal['action']],
            authority_class=POLICIES[proposal['feature_id']].action_class,
            template_args=list(proposal['template_args'].items()),
            candidate_ids=metadata.get('selected_ids', metadata.get('candidate_ids', [])),
            relation=metadata.get('relation'), metadata={'feature_action': proposal['action'], **metadata})
        return self.native.submit(result)


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
    if getattr(request, 'param', None) == 'long':
        home = home / ('long-archive-path-' + 'x' * 100)
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    fields = '''target_id operation_ref operation contract ambiguous explicit_tool deterministic_route candidates
    mandatory_ids discovery_ids task_ref task rule_scope mandatory_match stage question source_ref oversized structured
    source_immutable critical_fields_complete baseline_ids omitted_count notification_ref subject_id update previous
    changed_fields optional filterable observation_complete obligations_clear duplicate known_progress_repeat mandatory_controls
    user_ref slot user_wording interpretations permission_ui secret_ui authorization_missing safe_default resolution_candidates user_only_established retrieval_exhausted'''.split()
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
    mode: dict = {'relation': 'progress_only'}
    async def handle(request):
        assert request.url == 'https://api.typesafe.ai/v1/systemone'
        body = json.loads(request.content)
        assert set(body) == {'model', 'state', 'questions'}
        assert body['model'] == 'jev-1.13.0'
        calls.append((body, threading.current_thread().name))
        facts = body['state']['facts']
        if 'source_ref' in facts:
            # Resolve the actual host-owned binding, not a plugin filesystem path.
            owner = getattr(agent, '_supervision_views')
            source = owner._result_sources[facts['source_ref']]
            assert owner.result_source(facts['source_ref'], scope=source.scope, revision=source.revision) is source
            assert Path(source.path).read_bytes() == source.original.encode('utf-8'), 'exact archive must predate inference'
            assert owner.result_source(facts['source_ref'], scope='foreign', revision=source.revision) is None
            assert owner.result_source(facts['source_ref'], scope=source.scope, revision='stale') is None
            mode.setdefault('archives', []).append(source)
        answers = {}
        first = next(iter(body['questions'].values()))
        for key, q in body['questions'].items():
            if q['type'] == 'choice':
                options = q['criteria']
                chosen = (mode.get('resolution', 'default_defined') if key.startswith('F19/') else mode['relation'] if key.startswith('F18/')
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
    bridge_type = NegotiatedViewCodecBridge if getattr(request, 'param', None) == 'codecs' else NativeHostBridge
    bridge = bridge_type(facade, cfg, None, transport=transport)
    assert bridge.start()
    agent = _make_agent()
    agent._execution_thread_id = threading.get_ident()
    agent._interrupt_requested = False
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    from agent.turn_context import _reset_per_turn_agent_state
    _reset_per_turn_agent_state(agent)
    runtime = runtime_for_agent(agent, create=True)
    assert runtime is not None
    def accept(text, **kwargs):
        runtime.accept_instruction(accepted_input_origin(text, kind='cli', **kwargs))
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


def test_plugin_skill_metadata_detail_hint_and_native_scope_reset(native, monkeypatch):
    import tools.skills_tool as skills
    reads, replies = [], []
    read = skills._read_skill_text
    acquire = native.facade.acquire_skill_details
    def local_read(path):
        reads.append((path.parent.name, threading.get_ident()))
        return read(path)
    async def acquire_bound(request):
        monkeypatch.setattr(skills, '_read_skill_text', local_read)
        reply = await acquire(request)
        assert reply is not None
        assert {k: v for k, v in reply.items() if k != 'candidates'} == request
        assert request['deadline'] == native.runtime.round_deadline
        assert request['deadline_issued_at'] == native.runtime.round_deadline_issued_at
        assert await acquire(request) is None  # acquisition cannot be repeated
        replies.append(reply)
        return reply
    readiness = MagicMock(side_effect=AssertionError('detail acquisition cannot perform setup'))
    monkeypatch.setattr(skills, '_skill_readiness', readiness)
    monkeypatch.setattr(native.facade, 'acquire_skill_details', acquire_bound)
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
    assert reads == [('alpha', threading.get_ident())]
    readiness.assert_not_called()
    assert len(replies) == 1 and replies[0]['selected_ids'] == ['alpha']
    assert len({c[0]['state']['facts']['target_id'] for c in native.calls}) == 1
    assert not native.agent._supervision_view_binding.detail_requests
    assert not native.agent._supervision_view_binding.details
    assert history[0]['content'] == 'Analyze weather observations.'
    deadline = native.runtime.round_deadline
    assert native.agent._supervision_views.cycle_deadline() == deadline
    native.accept('Ordinary unrelated task.')
    assert not native.agent._supervision_views.skills.hints


def skill_catalog(native):
    for name in ('alpha', 'beta', 'gamma', 'delta'):
        path = native.home / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: Analyze weather observations.\n---\nCompare weather reports.\n')
    native.agent.tools = []
    native.agent.enabled_toolsets, native.agent.disabled_toolsets = [], []
    native.accept('Analyze weather observations.')


@pytest.mark.parametrize('changes', [
    {'version': 'other'}, {'event_id': 'other'}, {'incident_id': 'other'},
    {'owner': 'other'}, {'target_id': 'other'}, {'task_ref': 'other'},
    {'data_policy': {}}, {'deadline': 999999999999}, {'deadline_issued_at': -1},
    {'selected_ids': []}, {'selected_ids': ['alpha', 'beta', 'gamma', 'delta']},
    {'selected_ids': ['alpha', 'alpha']}, {'selected_ids': ['foreign']},
    {'selected_ids': [{}]}, {'expected': {}}, {'extra': True},
])
def test_skill_details_reject_changed_bindings_without_reading(native, monkeypatch, changes):
    import tools.skills_tool as skills
    skill_catalog(native)
    reads = MagicMock(side_effect=AssertionError('unbound detail read'))
    acquire, replies = native.facade.acquire_skill_details, []
    async def changed(request):
        monkeypatch.setattr(skills, '_read_skill_text', reads)
        reply = await acquire({**request, **changes})
        replies.append(reply)
        return reply
    monkeypatch.setattr(native.facade, 'acquire_skill_details', changed)
    result = assemble(native.agent, [{'role': 'user', 'content': 'Analyze weather observations.'}])
    native.drain()
    assert replies == [None]
    reads.assert_not_called()
    assert 'Optional skill candidate:' not in str(result.api_messages)
    assert [c[0]['state']['facts']['stage'] for c in native.calls] == ['metadata']
    assert not native.agent._supervision_view_binding.details
    assert not native.agent._supervision_view_binding.detail_requests


@pytest.mark.parametrize('case', ['pruned', 'oversized', 'description', 'read_error', 'malformed',
                                  'expired', 'unload', 'reset'])
def test_skill_details_fail_closed_and_do_not_read_after_revocation(native, monkeypatch, case):
    import tools.skills_tool as skills
    skill_catalog(native)
    acquire, replies = native.facade.acquire_skill_details, []
    async def selected(request):
        # Exercise the maximum acquisition size; the negative first row must
        # prevent the remaining authorized local reads and the dependent pass.
        monkeypatch.setattr(skills, '_read_skill_text', unavailable)
        reply = await acquire({**request, 'selected_ids': ['alpha', 'beta', 'gamma']})
        replies.append(reply)
        return reply
    monkeypatch.setattr(native.facade, 'acquire_skill_details', selected)
    read, reads = skills._read_skill_text, []
    def unavailable(path):
        reads.append(path.parent.name)
        if case == 'read_error':
            raise OSError('offline fixture unavailable')
        if case == 'malformed':
            return '[]'
        value = read(path)
        if case in ('pruned', 'oversized'):
            value = '[SKILL_PRUNED]' if case == 'pruned' else 'x' * 1201
        if case == 'description':
            value = value.replace('Analyze weather observations.', 'Changed catalog description.')
        if case == 'expired':
            monkeypatch.setattr(native.runtime, 'clock', lambda: native.runtime.round_deadline + 1)
        if case == 'unload':
            native.facade.unregister()
        if case == 'reset':
            native.agent._supervision_view_binding.reset()
        return value
    result = assemble(native.agent, [{'role': 'user', 'content': 'Analyze weather observations.'}])
    native.drain()
    assert reads == ['alpha']
    assert replies == [None]
    assert 'Optional skill candidate:' not in str(result.api_messages)
    assert [c[0]['state']['facts']['stage'] for c in native.calls] == ['metadata']
    assert not native.agent._supervision_view_binding.details
    assert not native.agent._supervision_view_binding.detail_requests


@pytest.mark.parametrize('case', ['shortlist', 'unserved'])
def test_skill_hint_requires_ready_proposal_over_served_ids(native, monkeypatch, case):
    skill_catalog(native)
    submit, receipts = native.facade.submit, []
    def changed(proposal):
        proposal = copy.deepcopy(proposal)
        if proposal['feature_id'] == 'F12':
            if case == 'shortlist':
                proposal['metadata']['phase'] = 'shortlist'
            else:
                proposal['candidate_ids'] = ['beta']
                proposal['metadata']['selected_ids'] = ['beta']
            receipts.append(submit(proposal))
            return receipts[-1]
        return submit(proposal)
    monkeypatch.setattr(native.facade, 'submit', changed)
    result = assemble(native.agent, [{'role': 'user', 'content': 'Analyze weather observations.'}])
    native.drain()
    assert receipts and receipts[0]['status'] == 'accepted'
    assert 'Optional skill candidate:' not in str(result.api_messages)
    assert any(r.reason == 'skill_details_required' for r in native.runtime.receipts.values())


def result_archive_text():
    return json.dumps({'output': ''.join('answer '+str(i)+' é'+('x'*179)+'\n' for i in range(22)) +
                      'WARNING partial result; cleanup not run\n',
                      'exit_code': 2, 'receipts': [{'status': 'not_run', 'mutation': 'none'}]}, ensure_ascii=False)


def commit_result(native, raw, call_id):
    a = native.agent
    a.tools = _make_tool_defs('execute_code')
    assistant = SimpleNamespace(content='', tool_calls=[_mock_tool_call(name='execute_code', call_id=call_id,
        arguments=json.dumps({'code': call_id}))])
    messages = []
    with patch('model_tools.handle_function_call', return_value=raw):
        a._execute_tool_calls_sequential(assistant, messages, 'task')
    content = next(m['content'] for m in messages if m.get('role') == 'tool')
    return json.JSONDecoder().raw_decode(content[content.index('{'):])[0]


@pytest.mark.parametrize('native', ['short', 'long'], indirect=True)
@pytest.mark.parametrize('case', ['wrong_evidence', 'wrong_metadata', 'stale_ref', 'overlong',
                                  'profile', 'owner', 'revision', 'reset', 'expired', 'unload', 'no_op'])
def test_result_reference_binding_rejects_wrong_or_stale_native_proposals(native, monkeypatch, case):
    native.accept('Locate the answer in the report.')
    raw = result_archive_text()
    stale_ref = None
    if case == 'stale_ref':
        assert 'supervision_view' in commit_result(native, raw, 'previous-result')
        stale_ref = native.calls[-1][0]['state']['facts']['source_ref']
        assert not native.agent._supervision_views._result_sources
        native.accept('Locate the answer in the report again.')
    submit, replies, proposals = native.facade.submit, [], []
    def changed(proposal):
        proposal = copy.deepcopy(proposal)
        if proposal['feature_id'] == 'F16':
            proposals.append(proposal)
            if case in ('wrong_evidence', 'stale_ref', 'overlong'):
                proposal['evidence_refs'] = [stale_ref if case == 'stale_ref' else 'x' * 257 if case == 'overlong' else 'result:unknown']
            if case in ('wrong_metadata', 'stale_ref'):
                proposal['metadata']['source_ref'] = stale_ref or 'result:unknown'
            if case == 'profile':
                proposal['expected']['profile'] += '-foreign'
            if case == 'owner':
                proposal['owner'] = 'foreign-owner'
            if case == 'revision':
                proposal['expected']['evidence'] += 1
            if case == 'reset':
                native.agent._supervision_view_binding.reset()
            if case == 'expired':
                monkeypatch.setattr(native.runtime, 'clock', lambda: native.runtime.round_deadline + 1)
            if case == 'unload':
                native.facade.unregister()
            if case == 'no_op':
                ids = native.calls[-1][0]['state']['facts']['baseline_ids']
                proposal['candidate_ids'] = ids
                proposal['metadata']['selected_ids'] = ids
        replies.append(submit(proposal))
        return replies[-1]
    monkeypatch.setattr(native.facade, 'submit', changed)
    result = commit_result(native, raw, 'native-result')
    native.drain()
    assert result == json.loads(raw)
    assert proposals and replies
    source = native.mode['archives'][-1]
    assert Path(source.path).read_bytes() == raw.encode('utf-8')
    assert not native.agent._supervision_views._result_sources
    if case != 'no_op':
        assert not any(r.status == 'applied' and r.proposal_id == proposals[-1]['proposal_id'] for r in native.runtime.receipts.values())
    if case in ('wrong_evidence', 'stale_ref'):
        assert replies[-1]['reason'] == 'unbound_evidence'
    if case == 'wrong_metadata':
        assert any(r.reason == 'view_contract' for r in native.runtime.receipts.values())
    if case == 'overlong':
        assert replies[-1] == {'status': 'rejected', 'reason': 'invalid_proposal'}


@pytest.mark.parametrize('native', ['short', 'long'], indirect=True)
@pytest.mark.parametrize('name,concurrent', [('execute_code', False), ('tool_call', False), ('web_search', True)])
def test_plugin_result_selection_after_archive_at_canonical_commit(native, name, concurrent):
    native.accept('Locate the answer in the report.')
    raw = result_archive_text()
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
    source, = native.mode['archives']
    assert result['supervision_view']['full_output_ref'] == source.path
    assert Path(source.path).read_bytes() == raw.encode('utf-8')
    assert source.original == raw and source.call_id == 'native-result' and source.tool_name == name
    if 'long-archive-path-' in str(native.home):
        assert len(source.path) > 256
    original = json.loads(raw)
    assert {k: v for k, v in result.items() if k not in ('output', 'supervision_view')} == {k: v for k, v in original.items() if k != 'output'}
    spans = result['supervision_view']['spans']
    assert result['output'] == ''.join(original['output'][s['start']:s['end']] for s in spans)
    assert 'WARNING partial result; cleanup not run' in result['output']
    assert len(result['output']) < len(original['output'])
    body, = [c[0] for c in native.calls if 'F16/select' in c[0]['questions']]
    assert set(body['state']['facts']['mandatory_ids']) <= {s['id'] for s in spans}
    ref = body['state']['facts']['source_ref']
    assert 0 < len(ref) <= 256 and ref != source.path
    assert not native.agent._supervision_views._result_sources
    assert native.agent._tool_guardrails._persisted_result_paths['native-result'] == source.path
    assert any(r.status == 'applied' and r.reason == 'native_view' for r in native.runtime.receipts.values())


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
def test_unknown_and_material_optional_update_reaches_original_ui_once(native, relation, monkeypatch):
    submit, replies = native.bridge.submit, []
    def capture(proposal):
        replies.append(submit(proposal))
        return replies[-1]
    monkeypatch.setattr(native.bridge, 'submit', capture)
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
    if relation == 'material_outcome':
        assert replies == [{'status': 'rejected', 'reason': 'action_codec_unavailable'}]
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())
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


@pytest.mark.parametrize('native', ['codecs'], indirect=True)
def test_negotiated_material_contract_presents_once_before_timeout(native):
    native.accept('Wait for the result.')
    callbacks, timers, rendered = queue.Queue(), [], []
    a = native.agent
    def later(delay, callback):
        timers.append(callback)
        return lambda: None
    a._supervision_presentation_callbacks = (callbacks.put, later)
    a._supervision_view_binding.bind_status()
    a.thinking_callback = lambda text: rendered.append((str(text), threading.get_ident()))
    native.mode['relation'] = 'material_outcome'
    a._emit_wait_notice(OptionalProgressText('Working.', revision='one'))
    a._emit_wait_notice(OptionalProgressText('Result available.', revision='two'))
    native.drain()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert [text for text, _ in rendered] == ['Working.', 'Result available.']
    assert any(r.status == 'applied' and r.reason == 'native_view' for r in native.runtime.receipts.values())
    for timer in timers:
        timer()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert len(rendered) == 2
    assert all(tid == threading.get_ident() for _, tid in rendered)


@pytest.mark.parametrize('native', ['codecs'], indirect=True)
@pytest.mark.parametrize('resolution', ['retrievable', 'user_only'])
def test_negotiated_clarification_contract_uses_exact_source_or_original_ui(native, resolution):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    native.mode['resolution'] = resolution
    native.mode['material'] = .99 if resolution == 'user_only' else .01
    if resolution == 'retrievable':
        native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```', message_id='format-source')
        native.accept('```hermes-defaults-v1\n{"output_format_ref":"format-source"}\n```', continuation=True)
    else:
        native.accept('```hermes-defaults-v1\n{"output_format":"ask"}\n```')
    a = native.agent
    a.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](a,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}]}, InlineToolContext('task')))
    if resolution == 'retrievable':
        a.clarify_callback.assert_not_called()
        assert result['responses'][0]['resolution'] == 'retrieved'
        assert result['responses'][0]['resolved_value'] == 'markdown'
        assert result['responses'][0]['evidence_ref'] == 'format-source'
        assert result['responses'][0]['user_response'] == ''
    else:
        a.clarify_callback.assert_called_once()
        assert result['responses'][0]['user_response'] == 'json'
    assert any(r.status == 'applied' and r.reason == 'native_view' for r in native.runtime.receipts.values()), native.bridge.supervisor.inspect()
    assert len(native.calls) == 1


@pytest.mark.parametrize('native', ['codecs'], indirect=True)
@pytest.mark.parametrize('resolution', ['retrievable', 'user_only'])
@pytest.mark.parametrize('guard', ['grant', 'metadata', 'unload'])
def test_dedicated_clarification_actions_keep_original_ui_when_denied(native, monkeypatch, resolution, guard):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    native.mode.update(resolution=resolution, material=.99 if resolution == 'user_only' else .01)
    if resolution == 'retrievable':
        native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```', message_id='format-source')
        native.accept('```hermes-defaults-v1\n{"output_format_ref":"format-source"}\n```', continuation=True)
    else:
        native.accept('```hermes-defaults-v1\n{"output_format":"ask"}\n```')
    submit, replies = native.facade.submit, []
    def denied(proposal):
        proposal = copy.deepcopy(proposal)
        if guard == 'grant':
            native.facade._registration.grants -= {proposal['action']}
        if guard == 'metadata':
            proposal['metadata']['feature_action'] = 'use_authorized_default'
        if guard == 'unload':
            native.facade.unregister()
        replies.append(submit(proposal))
        return replies[-1]
    monkeypatch.setattr(native.facade, 'submit', denied)
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}]}, InlineToolContext('task')))
    native.drain()
    assert len(replies) == 1
    if guard == 'grant':
        assert replies[0]['reason'] == 'grant_missing'
    native.agent.clarify_callback.assert_called_once()
    assert result['responses'][0]['user_response'] == 'json'
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())


@pytest.mark.parametrize('native', ['codecs'], indirect=True)
@pytest.mark.parametrize('case', ['foreign_work', 'evicted', 'changed_during_decision', 'wrong_question'])
def test_clarification_retrieval_never_broadens_pinned_source(native, monkeypatch, case):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    native.mode['resolution'] = 'retrievable'
    native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```', message_id='format-source')
    native.accept('```hermes-defaults-v1\n{"output_format_ref":"format-source"}\n```', continuation=case != 'foreign_work')
    if case == 'evicted':
        native.runtime.sources.pop('format-source')
    if case == 'changed_during_decision':
        submit = native.facade.submit
        def changed(proposal):
            native.runtime.sources['format-source'] = '```hermes-defaults-v1\n{"output_format":"json"}\n```'
            return submit(proposal)
        monkeypatch.setattr(native.facade, 'submit', changed)
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    result = json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question': 'Password?' if case == 'wrong_question' else 'Output format?',
                        'choices': ['markdown','plain_text','json']}]}, InlineToolContext('task')))
    native.drain()
    native.agent.clarify_callback.assert_called_once()
    assert result['responses'][0]['user_response'] == 'json'
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())
    assert len(native.calls) == (1 if case == 'changed_during_decision' else 0)


@pytest.mark.parametrize('resolution', ['retrievable', 'user_only'])
def test_unmodified_plugin_missing_clarification_codecs_remain_explicit(native, monkeypatch, resolution):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    assert type(native.bridge) is NativeHostBridge
    native.mode.update(resolution=resolution, material=.99 if resolution == 'user_only' else .01)
    if resolution == 'retrievable':
        native.accept('```hermes-defaults-v1\n{"output_format":"markdown"}\n```', message_id='format-source')
        native.accept('```hermes-defaults-v1\n{"output_format_ref":"format-source"}\n```', continuation=True)
    else:
        native.accept('```hermes-defaults-v1\n{"output_format":"ask"}\n```')
    submit, replies = native.bridge.submit, []
    def capture(proposal):
        replies.append(submit(proposal))
        return replies[-1]
    monkeypatch.setattr(native.bridge, 'submit', capture)
    native.agent.clarify_callback = MagicMock(return_value={'answers': {'q0': 'json'}})
    INLINE_TOOL_EXECUTORS['clarify'](native.agent,
        {'questions': [{'question':'Output format?', 'choices':['markdown','plain_text','json']}]}, InlineToolContext('task'))
    native.drain()
    assert replies == [{'status': 'rejected', 'reason': 'action_codec_unavailable'}]
    native.agent.clarify_callback.assert_called_once()
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
