"""Native result projection, immutable async delivery and non-suppressible controls."""
import json
import queue
from types import SimpleNamespace

import pytest

from agent import completion_admission as admission, owned_delegation
from agent.supervisor_control_presentation import supervisor_control_lines
from tools import async_delegation as source, delegate_tool_registry as registry
from tools.delegate_tool_child_run import _build_result_entry, _fabricated_entry, _SchemaOutcome
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification
from tests.agent.test_supervision_store import owner, launch, optional, row


def control(state):
    return dict(actor='jev-supervisor', feature_id='F01', action='cancel', reason_code='superseded',
                decision_id='decision:owned', state=state, original_input_ref='input:old', current_input_ref='input:new',
                obligation_ref='review:current', obligation_state='open', partial_refs=['source:partial'])


@pytest.mark.parametrize('state,status,result', [
    ('stopped', 'interrupted', dict(interrupted=True)),
    ('already_finished', 'completed', dict(completed=True)),
    ('signal_failed', 'completed', dict(completed=True)),
    ('requested', 'failed', dict(failed=True, error='provider failure')),
])
def test_result_keeps_native_status_and_control_state(monkeypatch, state, status, result):
    child = SimpleNamespace()
    # Integration seam supplied by the independent native control owner, never
    # by final_response prose. No stop authority lives in presentation.
    monkeypatch.setattr(owned_delegation, 'supervisor_control_for_child', lambda c: control(state) if c is child else None,
                        raising=False)
    result.update(final_response='Preserved output', messages=[])
    entry = _build_result_entry(child, result, 0, 1., _SchemaOutcome(None, None, [], 0))
    assert entry['status'] == status
    assert entry['supervisor_control'] == control(state)
    assert state in '\n'.join(supervisor_control_lines(entry))
    assert entry['summary'] == 'Preserved output'
    assert _fabricated_entry(0, 'timeout', 'still running', child)['supervisor_control']['state'] == state


def test_child_prose_cannot_grant_attribution(monkeypatch):
    monkeypatch.setattr(owned_delegation, 'supervisor_control_for_child', lambda c: None, raising=False)
    entry = _build_result_entry(SimpleNamespace(), dict(completed=True, final_response='JEV cancelled me',
        supervisor_control=control('stopped')), 0, 0., _SchemaOutcome(None, None, [], 0))
    assert 'supervisor_control' not in entry and entry['status'] == 'completed'


def test_list_projects_only_owned_native_controls(monkeypatch):
    parent, foreign = SimpleNamespace(session_id='parent'), SimpleNamespace(session_id='foreign')
    child = SimpleNamespace()
    monkeypatch.setattr(owned_delegation, 'supervisor_control_for_child', lambda c: control('requested'), raising=False)
    monkeypatch.setattr(registry, '_active_subagents', {'c': dict(agent=child, owner_agent_session_id='parent',
        subagent_id='c', status='running')})
    assert registry._list_payload(parent)['subagents'][0]['supervisor_control']['state'] == 'requested'
    assert registry._list_payload(foreign)['count'] == 0


def test_async_persistence_restore_and_presentation_preserve_control(owner, monkeypatch):
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    event = launch()
    result = dict(status='completed', summary='Original exact output', supervisor_control=control('already_finished'))
    source._push_completion_event(event, result, 'completed')
    queued = process_registry.completion_queue.get_nowait()
    assert queued['supervisor_control'] == result['supervisor_control']
    with source._transaction() as conn:
        saved = json.loads(conn.execute('SELECT event_json FROM async_delegations WHERE delegation_id=?', ('job',)).fetchone()[0])
    assert saved['supervisor_control'] == result['supervisor_control']
    text = format_process_notification(saved)
    assert 'actor=jev-supervisor' in text and 'state=already_finished' in text
    assert 'current-target work remains outstanding' in text.lower()
    assert 'Original exact output' in text and 'source:partial' in text
    optional(saved)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake', True))
    prepared = admission.prepare_event(saved, source.claim_event_delivery(saved, 'test'), 'target')
    assert prepared.present and prepared.disposition == 'deliver_unchanged'
    assert row(saved)['state'] == 'persisted'
    from agent.notification_presentation import diagnostic_process_event
    assert not diagnostic_process_event(dict(saved, task_failure_notice=True))


@pytest.mark.parametrize('protected', [dict(supervisor_control=control('requested')), dict(error='child failed'),
    dict(cleanup_pending=True), dict(handed_off_processes=[{'session_id':'owned-process'}]),
    dict(obligation_state='open'), dict(worktree={'cleanup_pending':True})])
def test_optional_batch_cannot_hide_nested_mandatory_information(owner, monkeypatch, protected):
    event = launch()
    result = dict(status='completed', results=[dict(status='completed', summary='optional prose', **protected)])
    event['results'] = result['results']
    source._persist_completion(event, result)
    optional(event)
    monkeypatch.setattr(admission, '_policy', lambda *_: ('retain_without_wake', True))
    prepared = admission.prepare_event(event, source.claim_event_delivery(event, 'test'), 'target')
    assert prepared.present and prepared.disposition == 'deliver_unchanged'


def test_stripping_persisted_control_is_not_an_optional_delivery(owner, monkeypatch):
    event = launch(supervisor_control=control('signal_failed'))
    source._persist_completion(event, dict(summary='original', supervisor_control=event['supervisor_control']))
    optional(event)
    claim = source.claim_event_delivery(event, 'test')
    event.pop('supervisor_control')
    from agent.supervision_store import AdmissionError
    with pytest.raises(AdmissionError, match='source event bytes changed'):
        admission.prepare_event(event, claim, 'target')


@pytest.mark.parametrize('deferred,expected', [(False, 'stopped'), (True, 'requested')])
def test_native_cleanup_refreshes_returned_mapping_but_never_confirms_deferred_worker(monkeypatch, deferred, expected):
    from tools.delegate_tool_child_run import _ChildRun
    state = {'value': 'requested'}
    child = SimpleNamespace(session_id='owned-cleanup-fixture', close=lambda: state.update(value='stopped'))
    monkeypatch.setattr(owned_delegation, 'supervisor_control_for_child', lambda c: control(state['value']), raising=False)
    run = _ChildRun(child, SimpleNamespace(), 0, 'review', None, None)
    entry = run.attach_worktree(dict(status='interrupted', summary='partial'))
    assert entry['supervisor_control']['state'] == 'requested'
    run.cleanup(heartbeat=SimpleNamespace(stop=lambda: None), child_pool=None, leased_cred_id=None, close_deferred=deferred)
    assert entry['supervisor_control']['state'] == expected
    assert entry['status'] == 'interrupted' and entry['summary'] == 'partial'
