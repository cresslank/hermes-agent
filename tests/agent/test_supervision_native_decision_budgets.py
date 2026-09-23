"""Actual assembly/result consumers can decide after earlier unrelated work expires."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from tests.agent.test_supervision_native_views import native, result_archive_text
from tests.agent.test_supervision_cookbook_skills import catalog, response
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _make_tool_defs, _mock_tool_call


def expire_previous_boundary(native):
    native.runtime.round_deadline_issued_at = native.runtime.clock() - 31
    native.runtime.round_deadline = native.runtime.clock() - 30
    return native.runtime.round_deadline


def test_skill_assembly_is_not_disabled_by_an_expired_previous_decision(native):
    bodies = catalog(native, 9)
    native.mode['skill_answers'] = response('winner')
    old = expire_previous_boundary(native)
    result = assemble(native.agent, [{'role': 'user', 'content': 'Build a pitch deck.'}])
    native.drain()
    assert len(native.calls) == 2, native.bridge.supervisor.inspect()
    assert bodies['workflow-002'] in result.api_messages[-1]['content']
    assert native.runtime.round_deadline > old
    assert any(r.status == 'applied' for r in native.runtime.receipts.values())


def test_result_selection_gets_its_budget_after_slow_tool_execution(native):
    native.accept('Locate the answer in the report.')
    raw = result_archive_text()
    agent = native.agent
    agent.tools = _make_tool_defs('execute_code')
    assistant = SimpleNamespace(content='', tool_calls=[_mock_tool_call(name='execute_code', call_id='late-result')])
    messages = []

    def tool(*args, **kwargs):
        # Represents a previous pre-action decision long expired during tool I/O.
        expire_previous_boundary(native)
        return raw

    with patch('model_tools.handle_function_call', side_effect=tool):
        agent._execute_tool_calls_sequential(assistant, messages, 'task')
    content = next(m['content'] for m in messages if m.get('role') == 'tool')
    result, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    assert 'supervision_view' in result, native.bridge.supervisor.inspect()
    assert len(result['output']) < len(json.loads(raw)['output'])
    assert 'WARNING partial result; cleanup not run' in result['output']
    assert any(r.status == 'applied' for r in native.runtime.receipts.values())
