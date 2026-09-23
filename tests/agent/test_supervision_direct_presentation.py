"""Direct content consumers through real request and tool-result assembly."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.supervision_skill_presentation import read_skill_content
from agent.supervision_views import AuthorizedDefault
from tests.agent.test_supervision_views import agent as agent, facade as facade, assemble, output
from tests.agent.test_tool_call_incremental_persistence import _mock_tool_call


def test_complete_skill_snapshot_at_new_tail_preserves_prior_request_prefix(agent, tmp_path):
    body = '---\nname: weather\ndescription: Weather analysis.\n---\n' + 'Keep this complete procedure.\n' * 140
    path = tmp_path / 'skills' / 'weather' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(body)
    snapshot = read_skill_content('weather')
    assert snapshot.content == body and len(body) > 1200
    contents = []
    agent._supervision_view_binding = SimpleNamespace(prepare_catalogs=lambda: None, skill_contents=lambda: tuple(contents))
    history = [{'role': 'user', 'content': 'Analyze observations.'}]
    first = assemble(agent, history).api_messages
    contents.append(snapshot)
    # A late selection cannot rewrite an already sent row, including a retry.
    assert assemble(agent, history).api_messages == first
    history += [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read', 'type': 'function',
        'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'New observations.'}]
    canonical = copy.deepcopy(history)
    selected = assemble(agent, history).api_messages
    assert selected[:len(first)] == first
    assert body in selected[-1]['content']
    assert history == canonical
    contents.clear()  # retiring the future selection cannot rewrite its old presentation
    assert assemble(agent, history).api_messages == selected
    history.append({'role': 'user', 'content': 'Next task.'})
    later = assemble(agent, history).api_messages
    assert later[:-1] == selected and body not in later[-1]['content']
    assert later[0] == first[0] == {'role': 'system', 'content': 'system'}


@pytest.mark.parametrize('suffix', ['[SKILL_PRUNED]', '!`uname`', '${HERMES_SESSION_ID}', 'x' * 65537])
def test_ineligible_skill_is_not_partially_admitted(tmp_path, monkeypatch, suffix):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'skills' / 'weather' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: weather\ndescription: Weather analysis.\n---\n' + suffix)
    assert read_skill_content('weather') is None


def test_owned_control_envelope_survives_actual_result_selection(agent, facade):
    control = {'actor': 'jev-supervisor', 'feature_id': 'F01', 'action': 'cancel',
        'state': 'stopped', 'reason_code': 'superseded', 'decision_id': 'owned:1',
        'original_input_ref': 'input:old', 'current_input_ref': 'input:new',
        'obligation_state': 'open', 'obligation_ref': 'review:current', 'partial_refs': ['part:1']}
    original = json.loads(output())
    original['supervisor_control'] = control
    original['output'] += 'supervisor_control: see the retained native envelope\n'
    raw = json.dumps(original)
    messages = []
    response = SimpleNamespace(content='', tool_calls=[_mock_tool_call(name='web_search', call_id='control')])
    with patch('model_tools.handle_function_call', return_value=raw):
        agent._execute_tool_calls_sequential(response, messages, 'task')
    content = next(row['content'] for row in messages if row.get('role') == 'tool')
    presented, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    assert presented['supervisor_control'] == control
    assert presented['receipts'] == original['receipts']
    assert presented['exit_code'] == original['exit_code']
    assert 'supervisor_control: see the retained native envelope' in presented['output']
    assert presented['output'] == ''.join(original['output'][span['start']:span['end']]
        for span in presented['supervision_view']['spans'])
    from pathlib import Path
    assert Path(presented['supervision_view']['full_output_ref']).read_text() == raw


def test_format_default_is_finite_and_deterministic_even_after_budget_expiry(agent, facade):
    owner = agent._supervision_views
    owner.defaults['Output format?'] = AuthorizedDefault('Output format?', 'json', owner.scope,
        'accepted:format', authorized=True, low_stakes=True)
    owner.deadline = owner.clock() - 1
    result = owner.clarify('Output format?', ['markdown', 'plain_text', 'json'])
    assert result['resolved_value'] == 'json' and result['user_response'] == ''
    assert result['evidence_ref'] == 'accepted:format' and not facade.calls
    assert owner.clarify('Output format?', ['markdown', 'json']) is None
    assert owner.clarify('Output format?', ['markdown', 'plain_text', 'json'], multi_select=True) is None
    owner.defaults['Approve?'] = AuthorizedDefault('Approve?', 'yes', owner.scope,
        'accepted:format', authorized=True, low_stakes=True)
    assert owner.clarify('Approve?', ['yes', 'no']) is None
    assert not facade.calls
