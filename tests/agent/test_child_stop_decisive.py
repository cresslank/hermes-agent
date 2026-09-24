"""One plugin decision -> native stop, with ordinary configured launch and scheduler."""
import dataclasses
import json
import time
from unittest.mock import Mock

import pytest

from agent.owned_delegation import ControlDenied, binding_of, dispatch_fence
from agent.owned_delegation_direct import POLICY
from hermes_cli.plugins_loader import _plugin_home_scope
from tests.agent.test_owned_delegation_policy import factory
from tests.agent.test_owned_delegation_direct import rig, direct_job, cancel
from tools.delegate_tool import _run_single_child as native_run_child


def enable(policy):
    policy.update(direct_control=dict(POLICY), allow_owned_child_stop=True, allow_optional_readonly=False)


def test_configured_ordinary_mutation_capable_child_stops_without_main_model_or_relaunch(factory, monkeypatch):
    from tools import delegate_tool as dt
    monkeypatch.setattr(dt, '_run_single_child', native_run_child)
    build = dt._build_child_preserving_parent_tools
    def build_running_child(**kwargs):
        child = build(**kwargs)
        def conversation(**kwargs):
            child.started.set()
            assert child.stopped.wait(5)
            assert child.release.wait(5)
            return dict(interrupted=True, completed=False, final_response='Retained partial finding')
        child.run_conversation = Mock(side_effect=conversation)
        return child
    monkeypatch.setattr(dt, '_build_child_preserving_parent_tools', build_running_child)
    n = factory.make(text='- Inspect the current fixture.', policy_change=enable, grants=['observe', 'cancel_child'])
    n.mode['relation'] = 'current'
    n.agent.run_conversation = Mock(side_effect=AssertionError('Main-model reconsideration'))
    assert n.owner is not None and n.owner.direct_enabled
    job = factory.launch(n, legacy=True)
    n.mode['relation'] = 'no_remaining_consumer'
    assert job.child.stopped.wait(4), json.dumps(dict(inspect=n.bridge.supervisor.inspect(), state=n.owner.status(job.handle), targets=list(n.runtime.children.direct.targets), alive=n.runtime.children.direct.thread.is_alive()))
    factory.flush(n)
    n.runtime.children.direct.stop.set()
    n.runtime.children.direct.thread.join(2)
    assert not n.runtime.children.direct.thread.is_alive()
    with _plugin_home_scope(n.home):
        state = n.owner.status(job.handle)
        assert state['cancel_requested'] and state['obligation_state'] == 'open'
        assert state['obligation'] == 'required' and state['effect_class'] == 'unknown'
        assert 'terminal' in job.child.valid_tool_names  # no silent read-only attenuation
        assert not state['worker_finished'] and not state['effects_reconciled']
        executed = Mock()
        with pytest.raises(ControlDenied, match='sealed'):
            with dispatch_fence(job.child, 'terminal', {'command': 'unreachable'}):
                executed()
        executed.assert_not_called()
        job.child.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        result = job.reply[0]['results'][0]
        assert result['status'] == 'interrupted'
        control = result['supervisor_control']
        assert control['state'] == 'stopped' and control['obligation_state'] == 'open'
        assert control['effects_reconciled'] is False  # no remote rollback claim
        assert len(n.owner.list_owned()) == 1 and n.calls
        assert job.child.run_conversation.call_count == 1
        assert n.owner._store.read(job.handle.child_id)['cancel_requested']
        from tests.agent.test_owned_delegation_bridge import Child
        replacement = Child('replacement')
        with pytest.raises(ControlDenied, match='explicit user instruction'):
            n.owner.launch(n.agent, replacement, goal=state['objective'])
        from agent.supervision_context import steer_from_user
        steer_from_user(n.agent, '- I explicitly authorize another attempt at the stopped fixture task.')
        later = n.owner.launch(n.agent, replacement, goal=state['objective'])
        assert later != job.handle and not n.owner.status(later)['cancel_requested']
        n.owner.finish(later)
    n.agent.run_conversation.assert_not_called()


def test_mutation_inflight_is_sealed_immediately_but_not_claimed_preempted(rig):
    child, handle = direct_job(rig)
    with rig.owner._lock:
        rig.owner._commit(rig.owner._get(handle), lambda s: s.update(effect_class='unknown', effect_policy_id=None))
    with dispatch_fence(child, 'write_file', {'path': 'fixture', 'content': 'issued'}):
        receipt = cancel(rig, handle)
        assert receipt.accepted and not receipt.settled and not child.stopped.is_set()
        state = rig.owner.status(handle)
        assert state['supervisor_control']['state'] == 'pending_stop' and state['inflight'] == 1
        with pytest.raises(ControlDenied, match='sealed'):
            with dispatch_fence(child, 'write_file', {}):
                pytest.fail('post-stop dispatch')
    assert child.stopped.is_set()
    rig.owner.finish(handle)
    state = rig.owner.status(handle)
    assert state['supervisor_control']['state'] == 'stopped'
    assert not state['processes_stopped'] and not state['effects_reconciled']


def test_native_owner_does_not_vote_again_on_plugin_decision(rig):
    child, handle = direct_job(rig)
    # Native API is trusted control authority. Relevance thresholds live only in
    # the plugin, not here; structural current-generation checks remain enforced.
    receipt = cancel(rig, handle, probability=.1, confidence=.1, relation='current')
    assert receipt.accepted and child.stopped.is_set()


def test_stale_generation_cannot_stop_new_child(rig):
    child, handle = direct_job(rig)
    stale = dataclasses.replace(handle, generation='obsolete-generation')
    with pytest.raises(ControlDenied, match='generation'):
        cancel(rig, stale)
    assert not child.stopped.is_set()


def test_schema_repair_never_restarts_a_sealed_child(rig):
    from tools.delegate_tool_child_run import _validate_child_output_schema
    child, handle = direct_job(rig)
    child._delegate_output_schema = {'type': 'object', 'required': ['value']}
    child.run_conversation = Mock(side_effect=AssertionError('Silent relaunch'))
    assert cancel(rig, handle).accepted
    result = _validate_child_output_schema(child, {'final_response': '{}'}, 0, 'fixture', None)
    assert not result.valid and result.retries == 0
    child.run_conversation.assert_not_called()


def test_finish_turn_keeps_live_control_scope_but_revoke_removes_authority(factory):
    n = factory.make(text='- Inspect the fixture.', policy_change=enable)
    n.mode['relation'] = 'current'
    job = factory.launch(n, legacy=True)
    with _plugin_home_scope(n.home):
        n.runtime.finish_turn()
        from hermes_cli.sqlite_safe_read import _live_lock
        with n.runtime.lock, n.owner._lock, _live_lock:
            assert n.owner.current() and n.owner.semantic_authorized(job.handle)
            n.runtime.bind_turn()
            assert n.owner.semantic_authorized(job.handle)
        n.runtime.revoke()
        assert not n.owner.current() and not n.owner.semantic_authorized(job.handle)
    assert not job.child.stopped.is_set()


def test_recurring_control_survives_a_transient_nonwaiting_authority_read(factory, monkeypatch):
    import threading
    n = factory.make(text='- Inspect the fixture.', policy_change=enable)
    current = n.owner.current
    misses = []
    def transient(**kwargs):
        if threading.current_thread().name == 'owned-child-control' and not misses:
            misses.append(True)
            return False
        return current(**kwargs)
    monkeypatch.setattr(n.owner, 'current', transient)
    job = factory.launch(n, legacy=True)
    assert job.child.stopped.wait(3)
    assert misses and n.owner.status(job.handle)['cancel_requested']


@pytest.mark.parametrize('grant', [False, None])
def test_direct_policy_requires_explicit_owned_stop_grant(factory, grant):
    def policy(p):
        enable(p)
        if grant is None:
            p.pop('allow_owned_child_stop')
        else:
            p['allow_owned_child_stop'] = grant
    n = factory.make(text='- Inspect the fixture.', policy_change=policy)
    assert n.owner is None
    job = factory.launch(n, legacy=True)
    assert job.handle is None and not job.child.stopped.is_set()
