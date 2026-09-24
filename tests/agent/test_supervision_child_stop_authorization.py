"""F09 authorization and owned cancellation share the final execution boundary.

Real provider codec, owner/store, executor and start-order gate; no live inference.
"""
import json
import threading
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from tests.agent.test_supervision_native_authorization import native, no_network
from tests.agent.test_owned_delegation_bridge import rig
from tests.agent.test_owned_delegation_direct import cancel
from tests.agent.supervision_test_support import accept
from agent.supervision_context import accepted_input_origin


@pytest.mark.parametrize("boundary", ["approval", "start_order", "attempt"])
@pytest.mark.parametrize("change", ["none", "stop", "instruction"])
def test_prepared_owned_action_is_rechecked_after_waits(native, rig, monkeypatch, boundary, change):
    from agent import tool_executor as executor
    from agent import supervision_tool_attempts as attempts

    rt = accept(native.agent, "Read the local fixture.")
    assert rt is not None
    (rig.path / "fixture.txt").write_text("fixture")
    native.mode["F09"] = "within_scope"
    agent = native.agent
    agent._subagent_id = "authorized-child"
    agent.hard_interrupt = Mock()
    handle = rig.owner.launch(rig.parent, agent, rig.request, goal="Read the local fixture")
    agent._tool_guardrails = SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True))
    args = {"path": str(rig.path / "fixture.txt")}
    state = executor._ManagedToolResult(None, args, [], False, False)
    ref = executor._ToolCallRef("read_file", args, "sandbox", "owned-authorized-call", [])
    effects, advances, stops = [], [], []

    def interleave(at):
        if at != boundary:
            return
        assert any(r.reason == "action_authorized" for r in rt.receipts.values())
        if change == "stop":
            receipt = cancel(rig, handle, relation="no_remaining_consumer")
            assert receipt.accepted
            stops.append(receipt)
        elif change == "instruction":
            rt.accept_instruction(accepted_input_origin(
                "Do not read; explain the plan instead.", kind="cli", continuation=True))

    monkeypatch.setattr(executor, "_pre_tool_block", lambda a, r: (None, r.args))
    monkeypatch.setattr(executor, "_begin_tool_execution", lambda *a: None)
    monkeypatch.setattr(executor, "_run_with_activity_heartbeat", lambda a, n, run: run())
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda ref: interleave("approval"))
    real_attempt = attempts.run_attempt
    def attempt(a, name, arguments, call, task, execute):
        return real_attempt(a, name, arguments, call, task,
                            lambda: (interleave("attempt"), execute())[1])
    monkeypatch.setattr(attempts, "run_attempt", attempt)

    gate = executor._StartOrderGate(timeout=3)
    start = executor._WorkerStartOnce(gate, 1, ref.name)
    entered = threading.Event()
    real_wait = gate._condition.wait_for
    def wait(predicate, timeout=None):
        entered.set()
        return real_wait(predicate, timeout)
    monkeypatch.setattr(gate._condition, "wait_for", wait)
    errors = []
    def predecessor():
        try:
            assert entered.wait(3)
            interleave("start_order")
            assert gate.begin_in_order(0)
        except BaseException as exc:
            errors.append(exc)
            gate.abandon()
    thread = threading.Thread(target=predecessor)
    thread.start()
    def begin(callback):
        advances.append(True)
        start.advance(callback)

    try:
        result = executor._dispatch_authorized_once(agent, state, ref,
            execute=lambda values: effects.append(values) or "ordinary result",
            scope_block=None, display_index=None, begin_execution=begin, authorization_gate=None)
    finally:
        thread.join(3)
        rig.owner.finish(handle)
    assert not thread.is_alive() and not errors
    assert advances == [True]
    assert len([key for body in native.calls for key in body["questions"] if key.startswith("F09/")]) == 1
    assert rig.owner.status(handle)["inflight"] == 0
    if change == "none":
        assert result == "ordinary result" and len(effects) == 1 and not state.blocked
    else:
        assert not effects and state.blocked
        assert json.loads(result)["error"]
    if change == "stop":
        assert len(stops) == 1
        agent.hard_interrupt.assert_called_once()
        assert rig.owner.status(handle)["cancel_requested"]
