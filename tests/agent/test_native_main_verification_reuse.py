"""Actual main native check executor + real Jev MockTransport, no optional owner."""
import json
import pytest
from agent.subagent_lifecycle import bind_subagent_parent
from tests.agent.supervision_test_support import rig as rig
from tests.agent.test_supervision_efficiency import native as native, feature_calls
from tests.agent.test_native_verification_supplier import prepare, track


@pytest.mark.parametrize("case", ["same", "required", "acceptance_test", "explicit_user_check",
    "fresh", "independent_review", "external_write_readback", "time_sensitive", "changed",
    "failed", "ineligible", "unavailable"])
def test_main_verification_decision_executes_zero_or_one(native, tmp_path, monkeypatch, case):
    from agent.verify.native_checks import run_native_checks
    root, path, specs = prepare(native, tmp_path, monkeypatch, second=False)
    raw = specs[0]
    if case == "failed":
        path.write_text("[]")
    evaluations, _ = track(monkeypatch)
    with bind_subagent_parent(native.rig.agent):
        first, = run_native_checks(root, [raw])
    assert len(evaluations) == 1
    evaluations.clear()
    pending = {**raw, "description": "Confirm metadata name is present"}
    if case in {"required", "acceptance_test", "explicit_user_check", "fresh", "independent_review", "external_write_readback", "time_sensitive"}:
        pending[case] = True
    if case == "changed":
        path.write_text('{"name":"changed"}')
    if case == "ineligible":
        pending.pop("independent_review")
    if case == "unavailable":
        native.answer_mutators.append(lambda answers: answers.clear())
    with bind_subagent_parent(native.rig.agent):
        result, = run_native_checks(root, [pending])
    reused = case in {"same", "required", "acceptance_test", "explicit_user_check"}
    assert len(evaluations) == (0 if reused else 1)
    assert result["reused"] is reused
    if reused:
        assert result["executed"] is False
        assert result["receipt"] == first["receipt"]
        assert len(feature_calls(native, "F07")) == 1
    else:
        assert result["receipt"]["id"] != first["receipt"]["id"]
    assert not native.runtime.drain_at_safe_point()
