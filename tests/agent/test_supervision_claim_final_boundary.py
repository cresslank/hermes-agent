"""The ordinary tool-commit boundary retires an LCM publication before final use.

These are authority-lifetime controls, NOT adoption or correction-delivery tests.
They must not be turned green by preserving an expired/revision-stale egress grant.
"""
import json

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import lookup_literal_source
from agent.supervision_planning_records import load
from tests.agent.test_supervision_claim_uses import native, prepare  # noqa: F401


@pytest.mark.parametrize("mode", ["pack", "auto", "proposal"])
def test_ordinary_commit_retires_source_before_final_preparation(native, monkeypatch, record_property, mode):
    from agent.tool_executor import _finalize_tool_batch
    from tools.budget_config import BudgetConfig

    n = native
    sources = prepare(n)
    # Use just the original source: no conflict inference, clock substitution or
    # private-state clearing is involved in the loss of the publication grant.
    first = sources[0]
    arguments = dict(question="What is the mass of asset A?",
                     baseline_refs=[dict(exact_ref=first[2])],
                     budgets=dict(max_retrieval_calls=0))
    name = "lcm_evidence_pack"
    if mode != "pack":
        name = "lcm_compile_evidence"
        arguments["mode"] = mode
        if mode == "proposal":
            arguments["proposal"] = dict(version="evidence-selector-v1",
                requested_facets=[], missing_facets=[], selections=[dict(
                    claim_id="mass", facet="answer", exact_ref=first[2], quote=first[1])])

    # Only unrelated output-budget/steering I/O is substituted. The real tool
    # batch finalizer, runtime commit and source owner all execute unchanged.
    monkeypatch.setattr("agent.tool_executor.get_active_env", lambda _: None)
    monkeypatch.setattr("agent.tool_executor.enforce_turn_budget", lambda *a, **k: None)
    monkeypatch.setattr(n.agent, "_apply_pending_steer_to_tool_results", lambda *a: None, raising=False)
    recipient = n.facade._registration
    observed = []
    for attempt in range(2):
        with bind_subagent_parent(n.agent):
            result = n.engine.handle_tool_call(name, arguments)
        refs = json.loads(result)["source_propositions"]
        assert len(refs) == 1
        ref = refs[0]
        proposition = lookup_literal_source(n.rt, ref, recipient)
        assert proposition is not None
        assert n.rt.dependencies.claim_uses._eligible(load(n.rt)[0], proposition)
        before = n.rt.revision
        messages = [dict(role="assistant", content=None, tool_calls=[dict(
            id=f"source-{attempt}", type="function", function=dict(name=name, arguments=json.dumps(arguments)))]),
            dict(role="tool", tool_call_id=f"source-{attempt}", name=name, content=result)]
        _finalize_tool_batch(n.agent, messages, "claim-final-boundary", 1, BudgetConfig())
        after = n.rt.revision
        assert after.evidence == before.evidence + 1
        assert after != proposition.revision
        # The exact same current native record remains in LCM. Its availability
        # is not an authority to revive the consumed publication's revision.
        assert n.engine._store.get(first[0])["content"] == first[1]
        within_deadline = n.rt.clock() < proposition.deadline
        assert lookup_literal_source(n.rt, ref, recipient) is None
        observed.append(dict(mode=mode, attempt=attempt, source_ref=ref,
            source_revision_evidence=proposition.revision.evidence,
            before_evidence=before.evidence, after_evidence=after.evidence,
            within_original_deadline=within_deadline,
            source_row_unchanged=True, grant_after_commit="unavailable"))
    assert observed[0]["source_ref"] != observed[1]["source_ref"]
    assert not n.calls
    assert load(n.rt)[0]["status"] == "claim_declared"
    assert load(n.rt)[0]["emission"] == "unknown"
    assert not getattr(n.rt, "_native_emission_scope", None)
    record_property("ordinary_final_boundary", observed)
