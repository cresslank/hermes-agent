"""Host producers keep complete candidate bytes and partial request coverage."""
from dataclasses import replace

import pytest

from agent.supervision_final_projection import coverage_windows
from agent.supervision_context import enumerate_requirements
from tests.agent.supervision_test_support import rig as rig, accept, proposal


def windows(source, candidate="An exact candidate answer."):
    requirements, _ = enumerate_requirements(source, "user-source")
    return list(coverage_windows(requirements, {"user-source": source}, candidate)), requirements


def test_preserves_exact_clauses_qualifiers_heading_and_full_candidate():
    source = "# Only public fixtures\n- Explain A\n  only for the supplied account.\n- Explain B"
    candidate = "α" * 1400
    projected, requirements = windows(source, candidate)
    rows = [row for window in projected for row in window["requirements"]]
    assert [row["text"] for row in rows] == [r.text for r in requirements]
    assert rows[0]["heading"] == "# Only public fixtures"
    assert "only for the supplied account" in rows[0]["text"]
    assert "".join(span["text"] for span in rows[0]["spans"]) == candidate
    assert all(window["global_coverage"] == "unknown" for window in projected)


def test_chunks_all_enumerated_items_without_claiming_unvisited_complete():
    projected, requirements = windows("\n".join(f"- Explain item {i}" for i in range(22)))
    all_ids = {r.id for r in requirements}
    seen = []
    for window in projected:
        ids = [r["id"] for r in window["requirements"]]
        assert 1 <= len(ids) <= 8
        assert set(window["omitted_requirement_ids"]) == all_ids - set(ids)
        seen.extend(ids)
    assert len(seen) == len(set(seen)) and set(seen) == all_ids


def test_oversized_clause_remains_pending_not_truncated():
    projected, requirements = windows("- Explain " + "x" * 1300 + "\n- Explain short")
    assert len(projected) == 1
    assert projected[0]["omitted_requirement_ids"] == [requirements[0].id]
    assert projected[0]["requirements"][0]["text"] == "- Explain short"


def test_candidate_prefix_cannot_prove_missing_content(rig):
    runtime = accept(rig.agent, "- Explain the final detail")
    assert runtime is not None
    rig.events.clear()
    assert runtime.prepare_final("Earlier content. " * 180 + "Final detail is here.") is None
    assert not rig.events


def test_damaged_or_evicted_source_never_gets_source_exact():
    source = "- Explain first\n- Explain second"
    requirements, _ = enumerate_requirements(source, "message")
    assert not list(coverage_windows(requirements, {}, "Answer"))
    assert not list(coverage_windows(requirements, {"message": source + "changed"}, "Answer"))
    forged = (replace(requirements[0], text="not the actual bytes"),)
    assert not list(coverage_windows(forged, {"message": source}, "Answer"))


def test_action_assertions_never_become_verified_receipts():
    projected, _ = windows("- Run `tests`\n- Explain the result", "I ran every test.")
    rows = [r for w in projected for r in w["requirements"]]
    assert [r["kind"] for r in rows] == ["action", "content"]
    assert not any(r["receipt_verified"] for r in rows)


def test_all_changed_subsets_share_original_deadline_and_later_gap_can_intervene(rig):
    runtime = accept(rig.agent, "\n".join(f"- Explain item {i}" for i in range(22)))
    assert runtime is not None
    rig.events.clear()
    observed = []
    def consume(event):
        if event["event"] != "pre_final_candidate":
            return
        observed.append(event)
        row = next((r for r in event["facts"]["requirements"] if r["text"] == "- Explain item 21"), None)
        if row:
            rig.facade.submit(proposal(rig, event, action="continue", template=None,
                refs=[row["id"]], metadata={"feature_action": "continue_once_for_named_gap",
                    "requirement_ids": [row["id"]], "grants_finality": False,
                    "global_coverage": "unknown"}))
    rig.facade._registration.consumer = consume
    advisory = runtime.prepare_final("Content covers the earlier items only.")
    assert advisory and "item 21" in advisory
    assert runtime.final_continuations == 1
    assert len(observed) > 1
    assert len({e["deadline"] for e in observed}) == 1
    assert len({e["deadline_issued_at"] for e in observed}) == 1
    assert all(e["facts"]["target_id"] in runtime.closed_targets for e in observed)
    assert runtime.prepare_final("Another candidate") is None


@pytest.mark.parametrize("change", [
    {"grants_finality": True}, {"global_coverage": "complete"},
    {"requirement_ids": ["foreign"]}, {"feature_action": "arbitrary_operation"},
])
def test_descriptor_cannot_grant_finality_or_foreign_evidence(rig, change):
    runtime = accept(rig.agent, "- Explain first")
    assert runtime is not None
    receipts = []
    def consume(event):
        ref = event["facts"]["requirements"][0]["id"]
        metadata = {"feature_action": "continue_once_for_named_gap", "requirement_ids": [ref],
                    "grants_finality": False, "global_coverage": "unknown", **change}
        receipts.append(rig.facade.submit(proposal(rig, event, action="continue", template=None,
                                                 refs=[ref], metadata=metadata)))
    rig.facade._registration.consumer = consume
    assert runtime.prepare_final("A candidate") is None
    assert receipts[0]["reason"] == "invalid_template"
