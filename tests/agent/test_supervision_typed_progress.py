"""Current typed progress is a local presentation decision, not remote inference."""
import pytest

from agent.notification_presentation import OptionalProgressText, OptionalUpdate
from tests.agent.test_supervision_status import owner, Surface


@pytest.mark.parametrize('phase', ['progress', 'local_load', 'first_chunk', 'post_chunk', 'first_event', 'reconnect', 'post_event'])
def test_native_phase_displays_changes_and_deduplicates_without_inference(owner, phase):
    queue, facade, coalescer = owner
    agent = Surface('tui')
    agent._status_coalescer = coalescer
    agent._emit_wait_notice(OptionalProgressText('current progress', revision=phase))
    agent._emit_wait_notice(OptionalProgressText('current progress', revision=phase))
    agent._emit_wait_notice(OptionalProgressText('updated progress', revision=phase))
    assert agent.thinking == ['current progress', 'updated progress']
    assert not facade.requests and not queue.timers
    assert len(agent.activity) == 3
    agent._emit_wait_notice('')
    agent._emit_wait_notice(OptionalProgressText('updated progress', revision=phase))
    assert agent.thinking[-2:] == ['', 'updated progress']


def test_control_marker_bypasses_dedupe_and_semantic_filter(owner):
    queue, facade, coalescer = owner
    agent = Surface('tui')
    agent._status_coalescer = coalescer
    marker = OptionalUpdate('control', 'r1', optional=True, replaceable=True, complete=True, supervisor_control=True)
    agent._emit_notice('stop requested; obligation open', optional_update=marker)
    agent._emit_notice('stop requested; obligation open', optional_update=marker)
    assert len(agent.notices) == 2
    assert not facade.requests and not queue.timers
