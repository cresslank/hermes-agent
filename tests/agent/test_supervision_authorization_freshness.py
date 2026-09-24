"""F09 authority is consumed at real dispatch, not before later owner waits.

The native provider uses strict HTTP MockTransport; only I/O and adversarial
interleavings are substituted. These tests do not qualify live classification.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.agent.test_supervision_native_authorization import native, no_network
from tests.agent.supervision_test_support import accept
from agent.supervision_context import accepted_input_origin


@pytest.mark.parametrize("boundary", ["terminal", "start_order", "heartbeat", "attempt"])
@pytest.mark.parametrize("change", [
    "none", "instruction", "arguments", "unregister", "generation", "action_grant",
    "source_grant", "egress", "runtime", "session", "unavailable", "unavailable_instruction",
])
def test_authorization_survives_only_unchanged_dispatch(native, monkeypatch, boundary, change):
    from agent import tool_executor as executor
    from agent import supervision_tool_attempts as attempts

    rt = accept(native.agent, "Run only the local schema tests.")
    assert rt is not None
    native.mode["F09"] = "within_scope"
    if change.startswith("unavailable"):
        native.mode["malformed"] = True
    agent = native.agent
    registration = native.facade._registration
    agent._tool_guardrails = SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True))
    args = {"command": "pytest tests/schema", "options": {"scope": ["local"]}}
    state = executor._ManagedToolResult(None, args, [], False, False)
    ref = executor._ToolCallRef("terminal", args, "sandbox", "freshness-call", [])
    effects, waits, starts, advances = [], [], [], []
    gate = executor._StartOrderGate(timeout=3)
    start = executor._WorkerStartOnce(gate, 1, ref.name)

    def change_authority():
        if change in {"instruction", "unavailable_instruction"}:
            rt.accept_instruction(accepted_input_origin(
                "Stop running tests; prepare a local explanation instead.", kind="cli", continuation=True))
        elif change == "arguments":
            args["options"]["scope"][0] = "production"
        elif change == "unregister":
            native.facade.unregister()
        elif change == "generation":
            registration.generation += "-replacement"
        elif change == "action_grant":
            registration.grants -= {"authorize_action"}
        elif change == "source_grant":
            registration.data_policy -= {"project_excerpt"}
        elif change == "egress":
            registration.egress_policy = {}
        elif change == "runtime":
            del agent._supervision_runtime
        elif change == "session":
            agent.session_id += "-replacement"

    def interleave(at):
        if at != boundary:
            return
        # Every interleaving is strictly after the real prepare returned and its
        # decision was settled (or explicitly unavailable), never during inference.
        assert any("F09/" in key for body in native.calls for key in body["questions"])
        if not change.startswith("unavailable"):
            assert any(r.reason == "action_authorized" for r in rt.receipts.values())
        waits.append(at)
        with rt.lock:
            change_authority()

    monkeypatch.setattr(executor, "_pre_tool_block", lambda a, r: (None, r.args))
    monkeypatch.setattr(executor, "_begin_tool_execution", lambda *a: starts.append(True))
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda ref: interleave("terminal"))
    monkeypatch.setattr(executor, "_run_with_activity_heartbeat", lambda a, n, run: (interleave("heartbeat"), run())[1])
    real_attempt = attempts.run_attempt
    def attempt(a, name, arguments, call, task, execute):
        # The native attempt wrapper may acquire owner locks before calling I/O.
        return real_attempt(a, name, arguments, call, task,
                            lambda: (interleave("attempt"), execute())[1])
    monkeypatch.setattr(attempts, "run_attempt", attempt)

    # Park the actual start-order gate until a predecessor has changed authority.
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

    def execute(actual):
        # Admission releases the runtime and registration fences before a tool
        # runs, and detaches nested arguments from the caller's mutable aliases.
        acquired = []
        def lock_probe():
            with rt.lock, registration.fence:
                acquired.append(True)
        probe = threading.Thread(target=lock_probe)
        probe.start(); probe.join(3)
        assert acquired and not probe.is_alive()
        effects.append(actual)
        if change in {"none", "unavailable"}:
            assert actual == args and actual is not args
            assert actual["options"] is not args["options"]
        return "ordinary result"

    try:
        result = executor._dispatch_authorized_once(agent, state, ref, execute=execute,
            scope_block=None, display_index=None, begin_execution=begin, authorization_gate=None)
    finally:
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert waits == [boundary] and advances == [True] and starts == [True]
    assert len([key for body in native.calls for key in body["questions"] if key.startswith("F09/")]) == 1
    if change in {"none", "unavailable"}:
        assert result == "ordinary result" and len(effects) == 1 and not state.blocked
    else:
        assert not effects and state.blocked
        assert json.loads(result)["executed"] is False
        assert json.loads(result)["type"] == "supervision_advisory"
