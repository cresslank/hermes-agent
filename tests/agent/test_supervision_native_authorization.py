"""Full registry -> strict HTTP wire -> native grant -> real pre-dispatch consumer.

Synthetic answers test effect closure, not live Jev classification accuracy.
"""
import json
import socket

import pytest

from tests.agent.test_supervision_native_relations import native
from tests.agent.supervision_test_support import accept
from tests.agent.test_supervision_pre_action import dispatch
from agent.supervision_context import accepted_input_origin


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import os
    from pathlib import Path
    import jev_supervisor
    source = Path(os.environ["JEV_SUPERVISOR_SOURCE"]).resolve()
    assert Path(jev_supervisor.__file__).resolve().is_relative_to(source)
    def denied(*a, **kw):
        raise AssertionError("live_socket_denied")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.mark.parametrize("relation", ["needs_approval", "insufficient"])
def test_explicit_permission_uncertainty_suppresses_and_survives_timeout(native, monkeypatch, relation):
    accept(native.agent, "Draft a release announcement; publishing still needs my approval.")
    native.mode['F09'] = relation
    arguments = {"channel": "public", "text": "release announcement"}
    result, effects = dispatch(native, monkeypatch, name="publish", arguments=arguments)
    assert not effects and json.loads(result)['executed'] is False
    native.mode['delay'] = .1
    monkeypatch.setattr("agent.supervision_policy.DECISION_BUDGET_SECONDS", .025)
    result, effects = dispatch(native, monkeypatch, name="publish", arguments=arguments, call_id="retry")
    assert not effects and json.loads(result)['executed'] is False


def test_unavailable_assessment_is_baseline_not_a_new_permission(native, monkeypatch):
    rt = accept(native.agent, "Read the local schema.")
    assert rt is not None
    native.mode['delay'] = .1
    monkeypatch.setattr("agent.supervision_policy.DECISION_BUDGET_SECONDS", .025)
    result, effects = dispatch(native, monkeypatch)
    assert result == 'ordinary result' and len(effects) == 1
    native.drain()
    assert not any(r.reason == 'action_authorized' for r in rt.receipts.values())


def test_explicit_changed_instruction_invalidates_inflight_allow(native, monkeypatch):
    rt = accept(native.agent, "Publish the announcement.")
    assert rt is not None
    native.mode['F09'] = 'within_scope'
    native.mode['on_response'] = lambda: rt.accept_instruction(accepted_input_origin(
        "Do not publish; save a local draft only.", kind="cli", continuation=True))
    result, effects = dispatch(native, monkeypatch, name="publish", arguments={"text": "announcement"})
    native.drain()
    assert not effects and json.loads(result)['executed'] is False
    assert 'changed' in json.loads(result)['error']
    assert not any(r.reason == 'action_authorized' for r in rt.receipts.values())


def test_new_explicit_permission_and_native_decision_resolve_prior_uncertainty(native, monkeypatch):
    rt = accept(native.agent, "Draft the announcement; ask before publishing.")
    assert rt is not None
    native.mode['F09'] = 'needs_approval'
    args = {"text": "announcement"}
    result, effects = dispatch(native, monkeypatch, name="publish", arguments=args)
    assert not effects
    accept(native.agent, "Publish that announcement now.", continuation=True)
    native.mode['F09'] = 'within_scope'
    result, effects = dispatch(native, monkeypatch, name="publish", arguments=args, call_id="approved")
    assert result == 'ordinary result' and len(effects) == 1
    assert not rt.action_scope.blocked


def test_observe_and_advise_do_not_imply_authorization_grant(native, monkeypatch):
    accept(native.agent, "Fix the schema locally.")
    registration = native.facade._registration
    registration.grants = frozenset({"observe", "advise"})
    native.drain()
    native.calls.clear()
    result, effects = dispatch(native, monkeypatch)
    assert result == 'ordinary result' and effects and not native.calls


def test_task_text_does_not_grant_private_action_egress(native, monkeypatch):
    accept(native.agent, "Use my private project to fix the schema.")
    registration = native.facade._registration
    from dataclasses import replace
    from agent.supervision_types import project
    policy = project(registration.egress_policy)
    policy['fields']['action'] = 'private_project'
    policy['sources']['action'] = 'private-project-without-egress-consent'
    registration.egress_policy = policy
    native.bridge.supervisor.config = replace(native.bridge.supervisor.config,
        allowed_classes=frozenset({'synthetic', 'private_project'}))
    # A declared source ID is not disclosure consent; task text cannot create a source_grant.
    result, effects = dispatch(native, monkeypatch)
    assert result == 'ordinary result' and effects
    assert not native.calls


def test_scope_permission_never_bypasses_required_native_approval(native, monkeypatch):
    from agent.owned_delegation import ControlDenied
    accept(native.agent, "Run the local schema tests.")
    native.mode['F09'] = 'within_scope'
    approvals = []
    def deny(ref):
        approvals.append(ref.name)
        raise ControlDenied("approval_denied")
    result, effects = dispatch(native, monkeypatch, name="terminal", arguments={"command": "pytest"}, approval=deny)
    assert not effects and approvals == ["terminal"]
    assert json.loads(result)['error']
    assert len(native.calls) == 1  # no second model vote


def test_long_prose_and_actual_arguments_are_not_silently_abbreviated(native, monkeypatch):
    request = "Fix the adapter locally. " + "Keep existing public compatibility. " * 50
    args = {"path": "adapter.py", "content": "# Preserve existing compatibility.\n" * 50}
    accept(native.agent, request)
    native.mode['F09'] = 'within_scope'
    result, effects = dispatch(native, monkeypatch, name="write_file", arguments=args)
    assert result == 'ordinary result' and len(effects) == 1
    facts = native.calls[0]['state']['facts']
    assert facts['accepted_scope'][0]['text'] == request
    assert facts['action']['arguments'] == args


def test_actual_arguments_are_fenced_after_inflight_decision(native, monkeypatch):
    accept(native.agent, "Run the local schema tests.")
    native.mode['F09'] = 'within_scope'
    args = {"command": "pytest tests/schema"}
    native.mode['on_response'] = lambda: args.update(command="deploy production")
    result, effects = dispatch(native, monkeypatch, name="terminal", arguments=args)
    assert not effects and json.loads(result)['executed'] is False
    assert 'changed' in json.loads(result)['error']
