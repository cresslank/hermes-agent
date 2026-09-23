"""Direct native set decisions: real transport, assembly and exact result consumer."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.agent.test_supervision_native_views import native
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _make_tool_defs, _mock_tool_call
from agent.supervision_catalog import authorized_tool_schemas, tool_id
from agent.supervision_views import canonical_result_view, _blocks
from tools.budget_config import DEFAULT_BUDGET


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('native set tests prohibit live sockets')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


@pytest.mark.parametrize('fault', [None, 'stale', 'unavailable', 'malformed', 'denied', 'oversize'])
def test_full_roster_request_schema_selection(native, monkeypatch, fault, roster_size=64, useful_count=1):
    import model_tools
    from tools.registry import registry
    a = native.agent
    names = [f'mcp_set_{i:03}' for i in range(roster_size - 4)]
    foreign = 'mcp_set_foreign'
    for name in [*names, foreign]:
        schema = _make_tool_defs(name)[0]['function']
        # Different vocabulary defeats the former lexical gate, >1200 exercises
        # complete descriptions without truncation or duplicated question copies.
        schema['description'] = ('Archive capability. ' * 80 if name == names[0]
                                 else 'Independent capability description.')
        registry.register(name=name, toolset='mcp-set-foreign' if name == foreign else 'mcp-set-local',
                          schema=schema, handler=lambda args, **kw: '{}')
    try:
        model_tools._clear_tool_defs_cache()
        a.enabled_toolsets, a.disabled_toolsets = ['mcp-set-local'], []
        a.tools = _make_tool_defs('tool_search', 'tool_describe', 'tool_call', 'read_file')
        task = 'Use mcp_set_000 and its companion to investigate the observation.'
        native.accept(task)
        available = authorized_tool_schemas(a)
        # Ordinary full schema baseline; selected definitions must be these exact
        # objects/bytes, not generated schemas or appended expanded duplicates.
        a.tools = available
        original = copy.deepcopy(available)
        native.mode['selected_ids'] = names[1:useful_count + 1]  # explicit 000 must survive anyway
        submit = native.facade.submit
        def change(proposal):
            if fault == 'stale':
                proposal = {**proposal, 'expected': {**proposal['expected'], 'evidence': proposal['expected']['evidence'] + 1}}
            if fault == 'malformed':
                proposal = {**proposal, 'candidate_ids': [foreign],
                    'metadata': {**proposal['metadata'], 'selected_ids': [foreign]}}
            return submit(proposal)
        monkeypatch.setattr(native.facade, 'submit', change)
        if fault == 'unavailable':
            native.facade.unregister()
        if fault == 'denied':
            monkeypatch.setattr(native.bridge.supervisor, 'config', replace(native.bridge.supervisor.config, allowed_classes=frozenset({'task_text'})))
        if fault == 'oversize':
            for schema in available:
                schema['function']['description'] = 'X' * 8193
            original = copy.deepcopy(available)
        a._interruptible_api_call = lambda *args, **kwargs: pytest.fail('extra main-model call')
        history = [{'role': 'user', 'content': task}]
        assembled = assemble(a, history)
        wire = a._build_api_kwargs(assembled.api_messages, tools_for_api=assembled.tools_for_api)
        ids = [tool_id(s) for s in wire['tools']]
        if fault:
            assert assembled.tools_for_api == original
        else:
            assert set(ids) == {*names[:useful_count + 1], 'tool_search', 'tool_describe', 'tool_call', 'read_file'}
            assert len(ids) == len(set(ids))
            assert len(native.calls) == 1
            body = native.calls[0][0]
            assert len(body['questions']) == len(available) == roster_size
            assert all(q['type'] == 'noul' for q in body['questions'].values())
            assert {r['id'] for r in body['state']['facts']['candidates']} == {tool_id(s) for s in available}
            assert foreign not in json.dumps(body)
            assert len(json.dumps(wire['tools'])) < len(json.dumps(original)) / 2
            assert len(assembled.api_messages) == 2  # system + user, not a new turn
            for schema in assembled.tools_for_api:
                assert schema == next(s for s in original if tool_id(s) == tool_id(schema))
        if fault in {'denied', 'unavailable', 'oversize'}:
            assert not native.calls
        assert a.tools == original
        assert history == [{'role': 'user', 'content': task}]
    finally:
        for name in [*names, foreign]:
            registry.deregister(name)
        model_tools._clear_tool_defs_cache()


def test_full_128_roster_can_keep_more_than_32_tools(native, monkeypatch):
    test_full_roster_request_schema_selection(native, monkeypatch, None, roster_size=128, useful_count=40)


@pytest.mark.parametrize('lines', [80, 400])
@pytest.mark.parametrize('fault', [None, 'stale', 'unavailable', 'malformed'])
def test_large_result_exact_windows_reduce_context(native, monkeypatch, lines, fault):
    native.accept('Report actual outcome and any limitations.')
    native.mode['selected_ids'] = []  # no winner needed to discard decorative rows
    text = ''.join(f'noise {i:04} ' + 'cache data ' * 70 + '\n' for i in range(lines))
    text += 'FAILED: one check not-run. receipt=abc; effect=none; cleanup=pending\n'
    raw = json.dumps({'output': text, 'exit_code': 1, 'effects': ['retained exact'],
                      'receipt': {'id': 'abc', 'partial': True}})
    submit = native.facade.submit
    def change(proposal):
        if fault == 'stale':
            proposal = {**proposal, 'expected': {**proposal['expected'], 'evidence': proposal['expected']['evidence'] + 1}}
        if fault == 'malformed':
            proposal = {**proposal, 'candidate_ids': ['invented'],
                'metadata': {**proposal['metadata'], 'selected_ids': ['invented']}}
        return submit(proposal)
    monkeypatch.setattr(native.facade, 'submit', change)
    if fault == 'unavailable':
        native.facade.unregister()
    native.agent._interruptible_api_call = lambda *args, **kwargs: pytest.fail('extra main turn')
    result = canonical_result_view(native.agent, raw, raw, tool_name='execute_code',
        call_id='large-output', env=None, budget=DEFAULT_BUDGET)
    if fault:
        assert result == raw
        return
    assert len(native.calls) == 1
    body = native.calls[0][0]
    assert 8 < len(body['questions']) <= 128
    assert len(json.dumps(body).encode()) < 256 * 1024
    assert all(q['type'] == 'noul' for q in body['questions'].values())
    view = json.loads(result)
    assert len(result) < len(raw)
    assert 'FAILED: one check not-run. receipt=abc; effect=none; cleanup=pending' in view['output']
    assert view['exit_code'] == 1 and view['effects'] == ['retained exact']
    assert view['receipt'] == {'id': 'abc', 'partial': True}
    spans = view['supervision_view']['spans']
    assert view['output'] == ''.join(text[s['start']:s['end']] for s in spans)
    assert Path(view['supervision_view']['full_output_ref']).read_text() == raw
    evaluated = {r['id'] for r in body['state']['facts']['candidates']}
    assert {b['id'] for b in _blocks(text)} - evaluated <= {s['id'] for s in spans}
    assert not native.agent._supervision_views._result_sources
    # Count with the host's real estimator (no remote tokenizer assets).
    from agent.model_metadata import estimate_tokens_rough
    assert estimate_tokens_rough(result) < estimate_tokens_rough(raw)
    print(f'F16 lines={lines}: rough tokens {estimate_tokens_rough(raw)} -> {estimate_tokens_rough(result)}; questions={len(body["questions"])}')


def test_result_does_not_expand_existing_compact_baseline(native):
    native.accept('Report the actual outcome.')
    native.mode['selected_ids'] = []
    raw = json.dumps({'output': ('decorative data ' * 60 + '\n') * 400 + 'FAILED receipt=abc\n'})
    baseline = 'Existing compact preview; full output already saved.'
    result = canonical_result_view(native.agent, raw, baseline, tool_name='execute_code',
        call_id='compact-output', env=None, budget=DEFAULT_BUDGET)
    assert native.calls and result == baseline



def test_large_result_reduces_real_post_dispatch_spill_preview(native, monkeypatch):
    import agent.supervision_views as views
    from agent.model_metadata import estimate_tokens_rough
    native.accept('Report the actual test outcome.')
    native.mode['selected_ids'] = []
    raw = json.dumps({'output': (('decorative cache data ' * 50).ljust(1199) + '\n') * 100 +
        'FAILED: one check not-run; receipt=abc; effect=none; cleanup=pending\n',
        'exit_code': 1, 'receipt': {'id': 'abc'}})
    original_consumer = views.canonical_result_view
    baselines = []
    def consume(agent, original, baseline, **kwargs):
        baselines.append(baseline)
        return original_consumer(agent, original, baseline, **kwargs)
    monkeypatch.setattr(views, 'canonical_result_view', consume)
    a = native.agent
    a.tools = _make_tool_defs('execute_code')
    a._interruptible_api_call = lambda *args, **kwargs: pytest.fail('extra main turn')
    assistant = SimpleNamespace(content='', tool_calls=[_mock_tool_call(name='execute_code', call_id='large-native')])
    messages = []
    with patch('model_tools.handle_function_call', return_value=raw):
        a._execute_tool_calls_sequential(assistant, messages, 'task')
    content = next(m['content'] for m in messages if m.get('role') == 'tool')
    view, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    assert baselines[0] != raw  # ordinary native persistence already compacted it
    assert 'supervision_view' in view
    assert estimate_tokens_rough(content) < estimate_tokens_rough(baselines[0])
    assert 'FAILED: one check not-run' in view['output']
    assert Path(view['supervision_view']['full_output_ref']).read_text() == raw
    assert len(native.calls) == 1 and len(messages) == 1
    print(f'F16 native spill preview: rough tokens {estimate_tokens_rough(baselines[0])} -> {estimate_tokens_rough(content)}')
