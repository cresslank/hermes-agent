"""Real CLI and quiet drains carry inbox IDs to normal turn staging."""
import queue
from types import SimpleNamespace

import pytest

from agent import completion_admission as admission
from agent.turn_context import _stage_turn_user_message
from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
from hermes_cli.quiet_single_query import continue_quiet_notify_completions
from tools import async_delegation as source
from tools.process_registry import process_registry
from tests.agent.test_supervision_store import owner, launch, optional, row


class CLI(CLIProcessNotificationsMixin):
    pass


def stage(db, message):
    agent = SimpleNamespace(_session_db=db, session_id='target')
    user, _ = _stage_turn_user_message(agent, message, None, None, None, None,
                                      getattr(message, 'supervision_metadata', None))
    return user


@pytest.mark.parametrize('surface', ['cli','quiet'])
def test_actual_drain_then_normal_turn_consumes_once(owner, monkeypatch, surface):
    event = launch()
    source._persist_completion(event, {'summary':'exact result'})
    monkeypatch.setattr(process_registry, 'drain_notifications', lambda **kw: [(event,'exact result')])
    if surface == 'cli':
        cli = CLI()
        cli.session_id, cli._session_db, cli._pending_input = 'target', owner, queue.Queue()
        cli._drain_process_notifications('cli')
        queued = cli._pending_input.get_nowait()
        unwrapped, _, _ = cli._tui_unwrap_input(queued)
    else:
        seen = []
        monkeypatch.setattr(process_registry, 'wait_for_pending_completions', lambda *a, **kw: {'timed_out':True})
        continue_quiet_notify_completions('target', lambda msg: seen.append(msg), max_rounds=1, linger_budget=0)
        assert len(seen) == 1
        unwrapped = seen[0]
    assert source.get_durable_delegation('job')['delivery_state'] == 'transferred'
    assert not owner.get_messages('target')
    user = stage(owner, unwrapped)
    assert user['_db_persisted'] is True
    assert stage(owner, unwrapped)['_row_id'] == user['_row_id']
    assert len(owner.get_messages('target')) == 1
    assert row(event)['state'] == 'consumed'


@pytest.mark.parametrize('surface', ['cli','quiet'])
def test_retained_drain_has_no_ui_or_agent_output(owner, monkeypatch, surface):
    event = launch()
    source._persist_completion(event, {'summary':'optional'})
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake',True))
    monkeypatch.setattr(process_registry, 'drain_notifications', lambda **kw: [(event,'optional')])
    seen = []
    if surface == 'cli':
        cli = CLI()
        cli.session_id, cli._session_db, cli._pending_input = 'target', owner, queue.Queue()
        cli._drain_process_notifications('cli')
        assert cli._pending_input.empty()
    else:
        monkeypatch.setattr(process_registry, 'wait_for_pending_completions', lambda *a, **kw: {'timed_out':True})
        continue_quiet_notify_completions('target', lambda msg: seen.append(msg), max_rounds=1, linger_budget=0)
    assert seen == []
    assert source.get_durable_delegation('job')['delivery_state'] == 'retained'
    assert not owner.get_messages('target')


def test_quiet_group_keeps_ordered_sibling_ids(owner, monkeypatch):
    events = [launch(f'job-{i}') for i in range(2)]
    for event in events:
        source._persist_completion(event, {'summary':event['delegation_id']})
    monkeypatch.setattr(process_registry, 'drain_notifications', lambda **kw: [(event,event['delegation_id']) for event in events])
    monkeypatch.setattr(process_registry, 'wait_for_pending_completions', lambda *a, **kw: {'timed_out':True})
    messages = []
    continue_quiet_notify_completions('target', lambda msg: messages.append(msg), max_rounds=1, linger_budget=0)
    metadata = messages[0].supervision_metadata
    assert [item['delegation_id'] for item in metadata['supervision_deliveries']] == ['job-0','job-1']
    stage(owner, messages[0])
    assert len(owner.get_messages('target')) == 1
    assert row(events[0])['message_row_id'] == row(events[1])['message_row_id']
