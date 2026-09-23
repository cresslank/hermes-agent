"""Native decision budgets exclude unrelated execution; dependencies keep one token."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent.supervision_types import DECISION_BUDGET_SECONDS
from tests.agent.supervision_test_support import rig as rig, accept


def test_boundaries_start_lazily_and_do_not_inherit_model_or_tool_latency(rig):
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    now = [100.0]
    rt.clock = lambda: now[0]
    with rt.decision_boundary():
        now[0] += 30  # No decision work exists yet.
        first = rt.shared_deadline()
        assert first == 130 + DECISION_BUDGET_SECONDS
        assert rt.decision_issued_at == 130
    now[0] += 30  # Main model or tool execution, outside the decision.
    with rt.decision_boundary():
        second = rt.shared_deadline()
        assert second == 160 + DECISION_BUDGET_SECONDS
        assert second > first
        assert rt.decision_issued_at == 160


def test_nested_decision_and_dependent_pass_never_renew_expired_budget(rig):
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    now = [100.0]
    rt.clock = lambda: now[0]
    with rt.decision_boundary():
        original = rt.shared_deadline()
        now[0] += 30
        with rt.decision_boundary():
            assert rt.shared_deadline() == original
            assert rt.shared_deadline(now[0] + 100) == original
            assert rt.decision_issued_at == 100
            assert rt.observe("test", {}, deadline=original) is None


def test_external_deadline_can_only_shorten_native_budget(rig):
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    rt.clock = lambda: 100.0
    with rt.decision_boundary():
        assert rt.shared_deadline(100.2) == 100.2
        assert rt.shared_deadline(99) == 99
        assert rt.shared_deadline(1000) == 100 + DECISION_BUDGET_SECONDS
        assert rt.decision_issued_at == 100


def test_concurrent_boundaries_keep_their_own_issuance_in_observations(rig):
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    barrier = threading.Barrier(2)
    local = threading.local()
    rt.clock = lambda: local.now

    def run(now):
        local.now = now
        with rt.decision_boundary():
            deadline = rt.shared_deadline()
            barrier.wait(timeout=3)
            snapshot = rt.observe("test", {}, deadline=deadline)
            assert snapshot is not None
            assert snapshot.deadline_issued_at == now
            assert snapshot.deadline == now + DECISION_BUDGET_SECONDS
            assert rt.shared_deadline() == deadline
            return snapshot

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [pool.submit(run, now) for now in (100.0, 110.0)]
        assert a.result(timeout=5).deadline_issued_at == 100
        assert b.result(timeout=5).deadline_issued_at == 110


def test_exception_closes_boundary_but_cannot_revive_its_old_opportunity(rig):
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    now = [100.0]
    rt.clock = lambda: now[0]
    with pytest.raises(ValueError), rt.decision_boundary():
        old = rt.shared_deadline()
        snapshot = rt.observe("test", {}, target_id="old", deadline=old)
        raise ValueError("owned failure")
    now[0] += 10
    with rt.decision_boundary():
        assert rt.shared_deadline() > old
        assert rt.opportunities["old"]["deadline"] == snapshot.deadline == old


def test_profile_runtimes_do_not_share_a_context_budget(rig):
    from agent.supervision_policy import SupervisionRuntime
    rt = accept(rig.agent, "Use the current task.")
    assert rt is not None
    other = SupervisionRuntime(rig.agent, "isolated-profile", "isolated-lineage", clock=lambda: 200.0)
    rt.clock = lambda: 100.0
    with rt.decision_boundary(), other.decision_boundary():
        assert rt.shared_deadline() == 100 + DECISION_BUDGET_SECONDS
        assert other.shared_deadline() == 200 + DECISION_BUDGET_SECONDS
        assert rt.decision_issued_at == 100
        assert other.decision_issued_at == 200
