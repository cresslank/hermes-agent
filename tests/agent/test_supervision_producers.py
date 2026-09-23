import json

from agent.supervision_context import completed_batch_facts, parse_work_map
from tests.agent.supervision_test_support import rig as rig, accept, commit_plan


def test_real_write_receipt_accepts_work_map_and_binds_action(rig, tmp_path):
    rt, content, result = commit_plan(rig, tmp_path / "plan.md")
    assert rt.work_maps
    assert rt.action_requirements({"path": "target.py"})
    assert not rt.action_requirements({"path": "unrelated.py"})
    receipt = next(iter(rt.evidence.values()))
    assert receipt.outcome == "landed" and receipt.after_hash
    assert not any(e["event"] == "tool_batch_committed" for e in rig.events)


def test_todo_change_invalidates_previously_accepted_action_links(rig, tmp_path):
    rt, _, _ = commit_plan(rig, tmp_path / "plan.md")
    rig.agent._todo_store.write([{"id": "step", "content": "different", "status": "completed"}])
    assert not rt.action_requirements({"path": "target.py"})


def test_child_or_unverified_plan_cannot_install_authoritative_map(rig):
    rt = accept(rig.agent, "- Write `plan.md`.", message_id="request")
    block = '```hermes-work-map-v1\n{"version":1,"requirements":[],"steps":[],"claims":[]}\n```'
    rt.record_artifact("plan.md", block, verified=False, main_agent=True)
    assert not rt.work_maps
    rt.record_artifact("plan.md", block + "\n", verified=True, main_agent=False)
    assert not rt.work_maps


def test_work_map_unknown_keys_bad_span_and_unresolved_target_rejected():
    def parse(body):
        return parse_work_map("```hermes-work-map-v1\n" + json.dumps(body) + "\n```", artifact_ref="plan.md",
                              designated_refs={"plan.md"}, sources={"s": "- real"}, todos={}, artifacts={})
    body = {"version": 1, "requirements": [], "steps": [], "claims": []}
    assert parse(body) is not None
    assert parse({**body, "all_requirements_complete": True}) is None
    assert parse({**body, "requirements": [{"source_message_id": "s", "start": True, "end": 4}]}) is None
    assert parse({**body, "requirements": [{"source_message_id": "external", "start": 0, "end": 4}]}) is None


def batch(result, name="delegate_task"):
    return [{"role": "assistant", "tool_calls": [{"id": "call", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call", "content": json.dumps(result)}]


def test_real_batch_boundary_produces_derived_delegate_finding(rig, monkeypatch):
    rt = accept(rig.agent, "- Inspect `target.py`.")
    from agent.tool_executor import _finalize_tool_batch
    monkeypatch.setattr("agent.tool_executor.get_active_env", lambda _: None)
    monkeypatch.setattr("agent.tool_executor.enforce_turn_budget", lambda *a, **k: None)
    rig.agent._apply_pending_steer_to_tool_results = lambda *a: None
    messages = batch({"results": [{"agent_id": "child-1", "summary": "`target.py` contains the failing assertion."}]})
    _finalize_tool_batch(rig.agent, messages, "sandbox", 1, None)
    event = rig.events[-1]
    assert event["event"] == "tool_batch_committed"
    finding = event["facts"]["findings"][0]
    assert finding["status"] == "candidate" and finding["consumer_refs"]
    span = finding["span"]
    assert messages[-1]["content"][span["start"]:span["end"]] == span["text"]
    assert all(r.effect_class == "unknown" for r in rt.evidence.values())


def test_partial_batch_and_unlinked_shell_do_not_emit_verified_findings(rig):
    rt = accept(rig.agent, "- Inspect `target.py`.")
    messages = batch({"output": "target.py passed", "exit_code": 0}, "terminal")
    receipts, findings, coverage = completed_batch_facts(messages, revision=rt.revision,
        requirements=rt.requirements, target_refs=rt.designated_refs)
    assert receipts and not findings and receipts[0].outcome == "unknown"
    receipts, findings, coverage = completed_batch_facts(messages[:-1], revision=rt.revision,
        requirements=rt.requirements, target_refs=rt.designated_refs)
    assert not receipts and not findings and not coverage.complete
