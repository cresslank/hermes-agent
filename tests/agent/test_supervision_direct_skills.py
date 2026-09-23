"""Real native selection settles full skill content, not a recommendation turn."""
import copy

import pytest

from agent.supervision_catalog import SkillCandidate
from tests.agent.test_supervision_native_views import native as native, skill_catalog
from tests.agent.test_supervision_views import assemble


def test_remote_selected_complete_body_reaches_main_request_without_skill_tool(native):
    skill_catalog(native)
    body = (native.home / 'skills' / 'alpha' / 'SKILL.md').read_text()
    history = [{'role': 'user', 'content': 'Analyze weather observations.'}]
    before = copy.deepcopy(history)
    request = assemble(native.agent, history)
    assert body in request.api_messages[-1]['content']
    assert 'Optional skill candidate:' not in request.api_messages[-1]['content']
    assert [call['state']['facts']['stage'] for call, _ in native.calls] == ['metadata', 'detail']
    assert history == before
    # Selecting/removing future eligibility cannot mutate previously sent bytes.
    native.facade.unregister()
    assert assemble(native.agent, history).api_messages == request.api_messages


@pytest.mark.parametrize('mandatory', [False, True])
def test_large_complete_skill_local_route_has_no_remote_vote(native, mandatory):
    body = '---\nname: alpha\ndescription: Analyze weather observations.\n---\n' + 'Keep the complete procedure.\n' * 160
    path = native.home / 'skills' / 'alpha' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(body)
    text = 'Analyze weather observations.' if mandatory else 'Use skill alpha.'
    native.accept(text)
    binding = native.agent._supervision_view_binding
    native.agent.tools = []
    native.agent.enabled_toolsets, native.agent.disabled_toolsets = [], []
    if mandatory:
        binding.views.skills.catalog((SkillCandidate('alpha', 'Analyze weather observations.', True),), 'mandatory')
    messages = [{'role': 'user', 'content': text}]
    result = assemble(native.agent, messages)
    assert body in result.api_messages[-1]['content'] and len(body) > 1200
    assert result.api_messages[0] == {'role': 'system', 'content': 'system'}
    assert messages == [{'role': 'user', 'content': text}]
    assert not native.calls


@pytest.mark.parametrize('text', ['Do not use skill alpha.', 'Use skill `alpha.', 'Use skill alpha`.',
                                 'Quoted example: Use skill alpha.'])
def test_mentions_negations_and_malformed_names_do_not_auto_load(native, text):
    skill_catalog(native)
    native.accept(text)
    result = assemble(native.agent, [{'role': 'user', 'content': text}])
    assert 'Native skill content:' not in str(result.api_messages)
    assert not native.calls


def test_unavailable_mandatory_body_cannot_be_replaced_by_optional_selection(native):
    skill_catalog(native)
    binding = native.agent._supervision_view_binding
    binding.views.skills.catalog((SkillCandidate('missing', 'Mandatory workflow.', True),
        SkillCandidate('alpha', 'Analyze weather observations.')), 'mandatory')
    result = assemble(native.agent, [{'role': 'user', 'content': 'Analyze weather observations.'}])
    assert 'Native skill content:' not in str(result.api_messages)
    assert any(c.id == 'missing' and c.required for c in binding.views.skills.candidates)
    assert not native.calls
