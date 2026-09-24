"""An omitted accepted instruction must not be mistaken for permission to default."""
import json
from unittest.mock import MagicMock

import pytest

from tests.agent.test_supervision_native_views import native


def configure_clarification(native):
    # Only F19 is granted; this protection must not depend on F09 being enabled.
    from hermes_cli.config import load_config_readonly
    path = native.home / 'config.yaml'
    config = json.loads(path.read_text())
    section = config['supervision']['plugins']['fixture-views']
    section['grants'] = ['observe', 'clarify_default']
    section['egress_policy']['fields'].update({
        'accepted_history': 'synthetic', 'clarification_contract': 'synthetic'})
    path.write_text(json.dumps(config))
    load_config_readonly()
    registration = native.facade._registration
    registration.grants = frozenset(section['grants'])
    registration.egress_policy = section['egress_policy']
    native.mode['resolution'] = 'default:c0'


def invoke(native):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    return json.loads(INLINE_TOOL_EXECUTORS['clarify'](native.agent, {
        'question': 'How should the summary be arranged?',
        'choices': ['Bullets', 'Short paragraphs'],
    }, InlineToolContext('task')))


@pytest.mark.parametrize('instruction', [
    'Summarize the note. Ask me before choosing the format.',
    'Summarize the note using short paragraphs, not bullets.',
])
def test_evicted_accepted_preference_preserves_ui_without_authorization_grant(native, instruction):
    configure_clarification(native)
    native.accept(instruction, message_id='preference')
    for index in range(12):
        native.accept('Continue summarizing the same note.',
                      message_id=f'continuation-{index}', continuation=True)
    assert 'preference' not in native.runtime.sources
    assert not native.runtime.action_scope.complete
    assert not native.runtime.completeness.omitted  # latest message alone is complete
    assert 'authorize_action' not in native.facade._registration.grants
    native.agent.clarify_callback = MagicMock(return_value='Short paragraphs')
    native.agent.client.reset_mock()

    result = invoke(native)
    native.drain()

    native.agent.clarify_callback.assert_called_once()
    assert result['user_response'] == 'Short paragraphs'
    assert 'resolved_value' not in result
    assert not native.calls  # incomplete context does not even reach semantic defaulting
    assert not native.agent.client.mock_calls

    # A genuinely new accepted task restores completeness, not a short continuation.
    native.accept('Summarize this new note.', message_id='new-task')
    assert native.runtime.action_scope.complete
    native.agent.clarify_callback.reset_mock()
    result = invoke(native)
    native.drain()
    assert result['resolution'] == 'safe_default'
    assert result['resolved_value'] == 'Bullets'
    native.agent.clarify_callback.assert_not_called()
    assert len(native.calls) == 1
    assert set(native.calls[0][0]['questions']) == {'F19/decision'}
    assert not native.agent.client.mock_calls
