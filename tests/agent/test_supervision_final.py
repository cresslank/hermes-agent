from tests.agent.supervision_test_support import rig as rig, accept, proposal


def final_gate(rig, monkeypatch, text="Incomplete answer", streamed=False):
    from agent.turn_stop_gates import apply_stop_gates
    monkeypatch.setattr("agent.turn_stop_gates._verify_on_stop_nudge", lambda a: None)
    monkeypatch.setattr("agent.turn_stop_gates._pre_verify_nudge", lambda *a: None)
    monkeypatch.setattr("agent.turn_stop_gates._kanban_stop_nudge", lambda *a: None)
    rig.agent._interim_content_was_streamed = lambda _: streamed
    emissions = []
    rig.agent._emit_interim_assistant_message = emissions.append
    messages = [{"role": "user", "content": "- First\n- Second"}]
    verdict = apply_stop_gates(rig.agent, {"role": "assistant", "content": text}, final_response=text,
        messages=messages, conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False)
    return verdict, messages, emissions


def test_targeted_continuation_not_interim_display_and_only_once(rig, monkeypatch):
    accept(rig.agent, "- First\n- Second")
    def consume(event):
        if event["event"] == "pre_final_candidate":
            rig.facade.submit(proposal(rig, event, action="continue", template="cover_requirement"))
    rig.facade._registration.consumer = consume
    verdict, messages, emissions = final_gate(rig, monkeypatch)
    assert verdict.continue_turn and verdict.final_response is None
    assert verdict.pending_verification_response == "Incomplete answer"
    assert not verdict.pending_verification_response_previewed and not emissions
    assert messages[-1]["_pre_verify_synthetic"]
    assert len(messages[-1]["content"]) <= 600
    verdict, messages, emissions = final_gate(rig, monkeypatch, "Changed answer")
    assert not verdict.continue_turn and not emissions


def test_streamed_candidate_honestly_records_nonretractable_preview(rig, monkeypatch):
    accept(rig.agent, "- First\n- Second")
    rig.facade._registration.consumer = lambda event: rig.facade.submit(
        proposal(rig, event, action="continue", template="review_claim"))
    verdict, _, emissions = final_gate(rig, monkeypatch, streamed=True)
    assert verdict.continue_turn and verdict.pending_verification_response_previewed
    assert not emissions


def test_existing_verifier_budget_prevents_extra_optional_continuation(rig, monkeypatch):
    accept(rig.agent, "- First")
    rig.agent._pre_verify_nudges = 1
    rig.events.clear()
    verdict, _, _ = final_gate(rig, monkeypatch)
    assert not verdict.continue_turn and not rig.events


def test_unlinked_prose_unknown_coverage_does_not_grade_final(rig, monkeypatch):
    rt = accept(rig.agent, "General prose request with no enumerable clauses.")
    rig.events.clear()
    verdict, messages, emissions = final_gate(rig, monkeypatch)
    assert not verdict.continue_turn and not rig.events and not emissions
    assert messages == [{"role": "user", "content": "- First\n- Second"}]
    assert rt.completeness.global_coverage == "unknown"


def test_late_final_proposal_cannot_reopen_turn(rig, monkeypatch):
    rt = accept(rig.agent, "- First")
    rt.round_deadline = rt.clock() + .001
    verdict, _, _ = final_gate(rig, monkeypatch)
    event = rig.events[-1]
    rt.finish_turn()
    assert not verdict.continue_turn
    assert rig.facade.submit(proposal(rig, event, action="continue", template="cover_requirement"))["status"] == "rejected"


from tests.agent.test_verification_continuation_budget import agent as _real_agent
real_agent = _real_agent


def test_actual_conversation_corrects_once_without_publishing_incomplete_candidate(rig, real_agent, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from agent.supervision_context import accepted_input_origin, accepted_origin_scope
    monkeypatch.setenv("HERMES_HOME", str(rig.home))
    real_agent.max_iterations = 3
    real_agent.iteration_budget.max_total = 3
    answers = iter(["incomplete confidential draft", "First and second are addressed."])
    def response(_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(answers), tool_calls=None),
                              finish_reason="stop")], model="test/model", usage=None)
    real_agent._interruptible_api_call = response
    real_agent._emit_interim_assistant_message = MagicMock()
    def consume(event):
        if event["event"] == "pre_final_candidate":
            rig.facade.submit(proposal(rig, event, action="continue", template="cover_requirement"))
    rig.facade._registration.consumer = consume
    monkeypatch.setattr("agent.turn_stop_gates._verify_on_stop_nudge", lambda a: None)
    monkeypatch.setattr("agent.turn_stop_gates._pre_verify_nudge", lambda *a: None)
    monkeypatch.setattr("agent.turn_stop_gates._kanban_stop_nudge", lambda *a: None)
    text = "- First\n- Second"
    with accepted_origin_scope(accepted_input_origin(text, kind="cli")):
        result = real_agent.run_conversation(text)
    assert result["final_response"] == "First and second are addressed."
    assert result["completed"] is True
    assert not real_agent._emit_interim_assistant_message.called
    assert not any(m.get("content") == "incomplete confidential draft" for m in result["messages"])
    assert real_agent._supervision_runtime.final_continuations == 1
