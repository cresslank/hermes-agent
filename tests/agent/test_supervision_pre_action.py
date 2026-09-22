import json
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from tests.agent.supervision_test_support import rig as rig, accept, commit_plan, proposal


def dispatch(rig, monkeypatch, *, path="target.py", call_id="call", allowed=True):
    from agent.tool_executor import _dispatch_authorized_once, _ManagedToolResult, _ToolCallRef
    effects = []
    agent = rig.agent
    agent._tool_guardrails = SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True))
    monkeypatch.setattr("agent.tool_executor._pre_tool_block", lambda a, r: (None, r.args))
    monkeypatch.setattr("agent.tool_executor._begin_tool_execution", lambda *a: None)
    monkeypatch.setattr("agent.tool_executor._run_with_activity_heartbeat", lambda a, n, fn: fn())
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda *a: None)
    state = _ManagedToolResult(None, {"path": path}, [], False, False)
    result = _dispatch_authorized_once(agent, state, _ToolCallRef("read_file", state.args, "sandbox", call_id, []),
        execute=lambda args: effects.append(args) or "ordinary result", scope_block=None,
        display_index=None, begin_execution=None, authorization_gate=None)
    return result, effects


def test_real_pre_dispatch_consumer_prevents_call_with_bounded_advisory(rig, tmp_path, monkeypatch):
    rt, _, _ = commit_plan(rig, tmp_path / "plan.md", conflict=True)
    def consume(event):
        if event["event"] == "action_proposed_with_scope_conflict":
            assert rig.facade.submit(proposal(rig, event))["status"] == "accepted"
    rig.facade._registration.consumer = consume
    result, effects = dispatch(rig, monkeypatch)
    assert not effects
    assert json.loads(result)["executed"] is False
    assert rt.receipts["proposal-incident"].status == "applied"


def test_unlinked_action_and_no_plugin_preserve_baseline_without_event(rig, monkeypatch):
    accept(rig.agent, "Free prose without an exact action edge.")
    rig.events.clear()
    result, effects = dispatch(rig, monkeypatch)
    assert result == "ordinary result" and effects == [{"path": "target.py"}]
    assert rig.events == []
    del rig.agent._supervision_runtime
    result, effects = dispatch(rig, monkeypatch)
    assert result == "ordinary result" and effects == [{"path": "target.py"}]


def test_nonzero_delayed_decision_is_consumed_before_dispatch(rig, tmp_path, monkeypatch):
    rt, _, _ = commit_plan(rig, tmp_path / "plan.md", conflict=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def consume(event):
            if event["event"] == "action_proposed_with_scope_conflict":
                def delayed():
                    time.sleep(.015)
                    return rig.facade.submit(proposal(rig, event))
                pool.submit(delayed)
        rig.facade._registration.consumer = consume
        result, effects = dispatch(rig, monkeypatch)
    assert not effects and json.loads(result)["executed"] is False


def test_expired_decision_freezes_baseline_and_cannot_apply_later(rig, tmp_path, monkeypatch):
    rt, _, _ = commit_plan(rig, tmp_path / "plan.md", conflict=True)
    captured = []
    rig.facade._registration.consumer = captured.append
    rt.round_deadline = rt.clock() + .005
    result, effects = dispatch(rig, monkeypatch)
    assert result == "ordinary result" and effects
    assert rig.facade.submit(proposal(rig, captured[-1]))["status"] in {"expired", "stale"}
    assert not rt.drain_at_safe_point()


def test_nested_admissions_share_absolute_deadline(rig):
    rt = accept(rig.agent, "- Exact requirement")
    first = rt.shared_deadline()
    assert rt.shared_deadline(first + 100) == first
    assert rt.shared_deadline(first - .05) == first - .05
    assert rt.shared_deadline() == first


def test_aligned_linked_action_is_not_semantically_graded(rig, tmp_path, monkeypatch):
    commit_plan(rig, tmp_path / "plan.md")
    rig.events.clear()
    result, effects = dispatch(rig, monkeypatch)
    assert result == "ordinary result" and effects
    assert rig.events == []
