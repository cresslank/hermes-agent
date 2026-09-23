import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_owner_protocol import decode_request, encode_decision
from agent.supervision_types import Action, OwnerDecisionV1, DECISION_BUDGET_SECONDS
from hermes_cli.plugins import PluginContext
from hermes_cli.plugins_manifest import PluginManifest
from tests.agent.supervision_test_support import rig as rig, accept


def envelope(**changes):
    return {"protocol": "supervision.v1", "owner": "lcm", "event": "retrieval_candidates",
            "request_id": "exact-id", "deadline": 200.0,
            "facts": {"candidates": [{"id": "a", "ref": "lcm:1:0-1", "excerpt": "a"},
                                      {"id": "b", "ref": "lcm:2:0-1", "excerpt": "b"}],
                      "query": "question", "baseline_ids": ["a", "b"]},
            "completeness": {"archive_complete": False}, **changes}


def request(rig):
    rt = accept(rig.agent, "- Check archive")
    rt.clock = lambda: 100.0
    return rt, decode_request(Action.RANK_CANDIDATES, envelope(), plugin_id="hermes-lcm", runtime=rt)


def test_codec_preserves_unknowns_immutable_facts_exact_refs_and_deadline(rig):
    rt, req = request(rig)
    assert req.event == "retrieval_candidates" and req.target_id == "lcm:exact-id"
    assert req.evidence_refs == ("lcm:1:0-1", "lcm:2:0-1")
    assert req.deadline == 100 + DECISION_BUDGET_SECONDS and rt.round_deadline_issued_at == 100
    assert not req.completeness.complete and req.completeness.global_coverage == "unknown"
    assert "qualifiers_complete" not in req.candidates[0]
    assert "constraints_match" not in req.candidates[0]
    with pytest.raises(TypeError):
        req.facts["query"] = "altered"
    rt.clock = lambda: 100.1
    second = decode_request(Action.RANK_CANDIDATES, envelope(request_id="second"), plugin_id="hermes-lcm", runtime=rt)
    assert second.deadline == req.deadline


@pytest.mark.parametrize("changes", [
    {"protocol": "other"}, {"owner": "muxyard"}, {"event": "missing_history_slot"},
    {"revision": {"profile": "foreign"}}, {"deadline": True}, {"deadline": float("nan")},
    {"request_id": ""}, {"facts": {"candidates": [{"id": "a"}] }},
    {"facts": {"candidates": [{"id": "a", "ref": "r"}] * 9}},
    {"facts": {"candidates": [{"id": "a", "ref": "r"}], "query": "x" * 2401}},
])
def test_mapping_never_supplies_authority_or_unbounded_input(rig, changes):
    rt, _ = request(rig)
    with pytest.raises((ValueError, TypeError)):
        decode_request(Action.RANK_CANDIDATES, envelope(**changes), plugin_id="hermes-lcm", runtime=rt)


def test_response_requires_exact_permutation_and_echoes_literal_id(rig):
    _, req = request(rig)
    assert encode_decision(Action.RANK_CANDIDATES, "exact-id", req, OwnerDecisionV1(("b", "a"), selected=True, receipt_id="receipt")) == {
        "request_id": "exact-id", "candidate_ids": ["b", "a"],
        "consumption": "supervision.owner-consumption.v1", "receipt_id": "receipt", "conflict_ids": [], "isolated_ids": []}
    for ids in [("a",), ("a", "a"), ("foreign", "a")]:
        assert encode_decision(Action.RANK_CANDIDATES, "exact-id", req, OwnerDecisionV1(ids, applied=True)) is None
    assert encode_decision(Action.RANK_CANDIDATES, "exact-id", req,
                           OwnerDecisionV1(("b", "a"), applied=True, metadata={"conflict_ids": ["foreign"]})) is None


def test_mapping_facade_authenticates_current_agent_profile_and_execution_thread(rig):
    rt = accept(rig.agent, "- Check archive")
    owner = PluginContext(PluginManifest(name="hermes-lcm"), rig.manager).supervision
    reg = rig.facade._registration
    reg.data_policy = frozenset({"history_excerpt"})
    reg.egress_policy = {"id": "local-test-only"}
    assert not owner.negotiate()["owner_capabilities"]  # no bound execution
    with bind_subagent_parent(rig.agent):
        assert "rank_candidates" in owner.negotiate()["owner_capabilities"]
        value = envelope(deadline=rt.clock() + .15)
        # copy_context transfers identity, not native execution-thread ownership.
        import contextvars
        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(context.run, owner.rank_candidates, value).result() is None
        assert "lcm:exact-id" not in rt.opportunities
        rig.manager.scope_key = "foreign-profile"
        assert owner.rank_candidates(value) is None
        rig.manager.scope_key = rt.revision.profile
        reg.close()
        assert not owner.negotiate()["owner_capabilities"]


def test_typed_optional_fields_remain_backward_compatible(rig):
    _, req = request(rig)
    legacy = replace(req, event="", facts={}, evidence_refs=())
    assert not legacy.facts and not legacy.event
    assert threading.get_ident() == rig.agent._execution_thread_id


def test_rank_output_contract_is_explicit_and_validates_both_blocks(rig):
    from agent.supervision_retrieval_presentation import VERSION
    rt, _ = request(rig)
    req = decode_request(Action.RANK_CANDIDATES, envelope(output_contract=VERSION),
                         plugin_id="hermes-lcm", runtime=rt)
    assert req.output_contract == VERSION and "output_contract" not in req.facts
    for metadata in ({"isolated_ids": ["a"]}, {"conflict_ids": ["b"]}):
        decision = OwnerDecisionV1(("a", "b"), selected=True, receipt_id="receipt", metadata=metadata)
        assert encode_decision(Action.RANK_CANDIDATES, "exact-id", req, decision)["output_contract"] == VERSION
    for key in ("conflict_ids", "isolated_ids"):
        for invalid in (["foreign"], ["a", "a"], [None], "a"):
            decision = OwnerDecisionV1(("a", "b"), selected=True, receipt_id="receipt", metadata={key: invalid})
            assert encode_decision(Action.RANK_CANDIDATES, "exact-id", req, decision) is None
    with pytest.raises(ValueError):
        decode_request(Action.RANK_CANDIDATES, envelope(output_contract="future"), plugin_id="hermes-lcm", runtime=rt)
