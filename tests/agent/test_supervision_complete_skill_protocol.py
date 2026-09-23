"""Bound remote metadata selection, full local bodies, and nested result controls."""
import copy
import json
from pathlib import Path

import pytest

from tests.agent.test_supervision_native_views import native, skill_catalog, commit_result
from tests.agent.test_supervision_views import assemble


@pytest.mark.parametrize('size', [1201, 8193, 32000, 65536, 65537])
def test_remote_skill_selection_keeps_complete_large_body_local(native, size):
    skill_catalog(native)
    path = native.home / 'skills/alpha/SKILL.md'
    prefix = path.read_text()
    body = prefix + ('\nComplete local-only procedure: preserve all safeguards. ' * size)[:size - len(prefix)]
    path.write_text(body)
    history = [{'role': 'user', 'content': 'Analyze weather observations.'}]
    original = copy.deepcopy(history)
    result = assemble(native.agent, history)
    native.drain()
    assert history == original
    assert body not in json.dumps(native.calls)
    stages = [call['state']['facts']['stage'] for call, _ in native.calls]
    if size > 65536:
        assert stages == ['metadata']
        assert result.api_messages[-1] == history[-1]
        assert not native.agent._supervision_view_binding.skill_contents()
    else:
        assert stages == ['metadata', 'detail'], native.bridge.supervisor.inspect()
        assert body in result.api_messages[-1]['content']
        row, = native.calls[-1][0]['state']['facts']['candidates']
        assert row['local_content']['chars'] == len(body)
        assert 'content' not in row and 'excerpt_complete' not in row
        assert 'local-only procedure' not in json.dumps(native.calls)
        assert any(r.status == 'applied' for r in native.runtime.receipts.values())
        assert assemble(native.agent, history).api_messages == result.api_messages


@pytest.mark.parametrize('fault', ['changed', 'removed', 'dynamic', 'pruned', 'grant', 'revoked', 'expired', 'reply_pin'])
def test_local_body_hydration_rechecks_exact_snapshot_and_fences(native, monkeypatch, fault):
    skill_catalog(native)
    path = native.home / 'skills/alpha/SKILL.md'
    body = path.read_text() + '\nNever disclose setup or execute shell templates.\n' * 100
    if fault == 'dynamic':
        body += '!`touch forbidden`'
    if fault == 'pruned':
        body += '[SKILL_PRUNED]'
    path.write_text(body)
    acquire = native.facade.acquire_skill_details
    async def change(request):
        reply = await acquire(request)
        if fault == 'changed':
            path.write_text(body + '\nChanged exact body.')
        elif fault == 'removed':
            path.unlink()
        elif fault == 'grant':
            native.facade._registration.grants -= {'select_skills'}
        elif fault == 'revoked':
            native.facade.unregister()
        elif fault == 'expired':
            monkeypatch.setattr(native.runtime, 'clock', lambda: native.runtime.round_deadline + 1)
        elif fault == 'reply_pin':
            reply['candidates'][0]['local_content']['sha256'] = '0' * 64
        return reply
    monkeypatch.setattr(native.facade, 'acquire_skill_details', change)
    history = [{'role': 'user', 'content': 'Analyze weather observations.'}]
    result = assemble(native.agent, history)
    native.drain()
    assert result.api_messages[-1] == history[-1]
    assert not native.agent._supervision_view_binding.skill_contents()
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())
    assert 'Never disclose setup' not in json.dumps(native.calls)


@pytest.mark.parametrize('key', ['supervisor_control', 'obligation', 'source', 'provenance', 'supervis\\u006fr_control'])
@pytest.mark.parametrize('oversize', [False, True])
def test_nested_control_value_survives_real_result_selection(native, key, oversize, monkeypatch):
    import jev_supervisor.runtime as provider_runtime
    from jev_supervisor.validation import MODEL, thaw
    from jev_supervisor.wire import MAX_REQUEST
    build_request = provider_runtime.make_request
    request_sizes = []
    def measured_request(state, questions):
        request_sizes.append(len(json.dumps({'model': MODEL, 'state': thaw(state), 'questions': thaw(questions)},
            ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')))
        return build_request(state, questions)
    monkeypatch.setattr(provider_runtime, 'make_request', measured_request)
    native.accept('Find the requested report.')
    # The middle of this value is several blocks away from its identifying key.
    control = {'actor': 'jev-supervisor', 'original_input_ref': 'original:1', 'current_input_ref': 'current:2',
               'details': ['opaque-' + str(i) + '-' + 'x' * 120 for i in range(23)], 'state': 'open'}
    encoded = json.dumps(control, indent=2)
    text = ('discardable prelude\n' * 30 + '"' + key + '": ' + encoded + '\n' +
            'discardable epilogue\n' * (140 if oversize else 100))
    raw = json.dumps({'output': text, 'exit_code': 0, 'supervisor_control': control})
    from agent.supervision_views import _blocks, _CRITICAL
    blocks = _blocks(text)
    assert blocks is not None
    hits = {i for i, b in enumerate(blocks) if _CRITICAL.search(b['excerpt'])}
    old_required = hits | {j for i in hits for j in (i - 1, i + 1) if 0 <= j < len(blocks)}
    assert any(b['critical'] and i not in old_required for i, b in enumerate(blocks))
    result = commit_result(native, raw, 'nested-control')
    native.drain()
    assert result['supervisor_control'] == control
    assert encoded in result['output']
    assert len(request_sizes) == 1
    if oversize:
        assert request_sizes[0] > MAX_REQUEST
        assert not native.calls
        assert result == json.loads(raw)
        assert not any(r.status == 'applied' for r in native.runtime.receipts.values())
    else:
        assert request_sizes[0] <= MAX_REQUEST
        assert len(native.calls) == 1
        view = result['supervision_view']  # baseline-only preservation is not success
        assert Path(view['full_output_ref']).read_text() == raw
        assert len(result['output']) < len(text)
        assert result['output'] == ''.join(text[s['start']:s['end']] for s in view['spans'])
        assert any(r.status == 'applied' for r in native.runtime.receipts.values())


@pytest.mark.parametrize('value', ['{"details": [', '{"label": "bad\\q"}'])
def test_malformed_nested_control_keeps_original_result_without_dispatch(native, value):
    native.accept('Find the requested report.')
    text = 'preamble\n' * 150 + '"supervisor_control": ' + value + '\n' + 'epilogue\n' * 150
    raw = json.dumps({'output': text, 'exit_code': 0})
    assert commit_result(native, raw, 'malformed-control') == json.loads(raw)
    native.drain()
    assert not native.calls
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())
