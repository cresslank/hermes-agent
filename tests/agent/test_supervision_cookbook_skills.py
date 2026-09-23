"""Whole native roster -> real provider/MockTransport -> applied full snapshot."""
import copy
import json

import pytest

from tests.agent.test_supervision_native_views import native
from tests.agent.test_supervision_views import assemble


def catalog(native, count):
    # Identical truncated descriptions, different full descriptions and openings.
    common = 'Presentation workflows for documents and communication artifacts. '
    bodies = {}
    for i in range(count):
        name = f'workflow-{i:03}'
        role = ('Edit an existing deck.', 'Extract text from slides.', 'Author a new pitch deck.')[i % 3]
        body = f'---\nname: {name}\ndescription: {common}{role}\n---\n# Workflow\n{role}\n' + 'Preserve complete procedure and safeguards.\n' * 250
        path = native.home / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(body)
        bodies[name] = body
    native.agent.tools = []
    native.agent.enabled_toolsets, native.agent.disabled_toolsets = [], []
    native.accept('Build a pitch deck.')  # No lexical overlap with the index descriptions.
    return bodies


def response(mode):
    def answer(body, answers):
        stage = body['state']['facts']['stage']
        choice = answers['F12/select']
        options = choice['probabilities']
        if stage == 'catalog':
            ids = [r['id'] for r in body['state']['facts']['candidates']]
            if len(ids) == 1:
                probabilities = {k: float(k == ids[0]) for k in options}
            else:
                top = {ids[0]: .35, ids[1]: .33, ids[2]: .30, 'none': .02}
                probabilities = {k: top.get(k, 0.) for k in options}
            choice.update(choice=ids[0], probabilities=probabilities, confidence=.1)
            if mode == 'no_need':
                for key in answers:
                    if key.startswith('F12/gate:'):
                        answers[key]['noul'] = float(key.endswith('prose_suffices'))
        else:
            ids = [r['id'] for r in body['state']['facts']['candidates']]
            winner = ids[-1] if mode not in ('none', 'insufficient') else mode
            choice.update(choice=winner, probabilities={k: float(k == winner) for k in options}, confidence=.2)
            if mode == 'no_fit':
                for key in answers:
                    if key.startswith('F12/fit:'):
                        answers[key]['noul'] = .29
        return answers
    return answer


@pytest.mark.parametrize('count', [1, 9, 182, 253])
def test_complete_roster_ambiguous_top_three_applies_full_winner(native, count):
    bodies = catalog(native, count)
    native.mode['skill_answers'] = response('winner')
    history = [{'role': 'user', 'content': 'Build a pitch deck.'}]
    before = copy.deepcopy(history)
    result = assemble(native.agent, history)
    native.drain()
    assert len(native.calls) == 2, native.bridge.supervisor.inspect()
    wide, detail = [call['state']['facts'] for call, _ in native.calls]
    assert len(wide['candidates']) == count
    assert {r['id'] for r in wide['candidates']} == set(bodies)
    assert set(native.calls[0][0]['questions']) == {
        'F12/select', 'F12/gate:acts_on_user_system',
        'F12/gate:would_follow_documented_procedure', 'F12/gate:prose_suffices'}
    shortlist = detail['candidates']
    assert [r['id'] for r in shortlist] == list(bodies)[:3]
    for row in shortlist:
        assert len(row['full_description']) > len(row['description'])
        assert len(row['content']) == 700 and row['excerpt_complete'] is False
        assert row['local_content']['chars'] == len(bodies[row['id']])
    winner = shortlist[-1]['id']
    assert bodies[winner] in result.api_messages[-1]['content']
    assert all(bodies[name] not in result.api_messages[-1]['content'] for name in bodies if name != winner)
    assert 'Optional skill candidate:' not in str(result.api_messages)
    assert result.api_messages[0] == {'role': 'system', 'content': 'system'}
    assert history == before
    assert any(r.status == 'applied' for r in native.runtime.receipts.values())
    assert assemble(native.agent, history).api_messages == result.api_messages
    assert len(native.calls) == 2  # no extra model selection or tool fetch


@pytest.mark.parametrize('mode,calls', [('no_need', 1), ('none', 2), ('insufficient', 2), ('no_fit', 2)])
def test_cookbook_no_match_does_not_load_a_skill(native, mode, calls):
    catalog(native, 182)
    native.mode['skill_answers'] = response(mode)
    history = [{'role': 'user', 'content': 'Build a pitch deck.'}]
    result = assemble(native.agent, history)
    native.drain()
    assert len(native.calls) == calls, native.bridge.supervisor.inspect()
    assert result.api_messages[-1] == history[-1]
    assert not native.agent._supervision_view_binding.skill_contents()
    assert not any(r.status == 'applied' for r in native.runtime.receipts.values())


def test_oversized_roster_is_rejected_whole_not_silently_prefiltered(native):
    bodies = catalog(native, 254)
    history = [{'role': 'user', 'content': 'Build a pitch deck.'}]
    result = assemble(native.agent, history)
    native.drain()
    assert {c.id for c in native.agent._supervision_views.skills.candidates} == set(bodies)
    assert not native.calls
    assert result.api_messages[-1] == history[-1]


def test_explicit_route_bypasses_whole_roster_and_remote_selection(native):
    bodies = catalog(native, 182)
    text = 'Use skill workflow-002.'
    native.accept(text)
    result = assemble(native.agent, [{'role': 'user', 'content': text}])
    assert bodies['workflow-002'] in result.api_messages[-1]['content']
    assert not native.calls
