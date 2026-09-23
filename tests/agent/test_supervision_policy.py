import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.supervision_types import Action, OwnerRequestV1
from tests.agent.supervision_test_support import rig as rig, accept, proposal


def opportunity(rig):
    rt = accept(rig.agent, "- Requested item")
    assert rt is not None
    rt.observe("incident", {"requirements": [{"id": rt.requirements[0].id}]}, target_id="target",
               evidence_refs=(rt.requirements[0].id,), actions=(Action.ADVISE,))
    return rt, rig.events[-1]


def test_submit_queues_only_and_safe_point_applies_once(rig):
    rt, event = opportunity(rig)
    request = proposal(rig, event)
    receipt = rig.facade.submit(request)
    assert receipt["status"] == "accepted"
    assert not rt.incidents
    advisory = rt.drain_at_safe_point()
    assert len(advisory) == 1 and "lower-trust" in advisory[0]
    assert rig.facade.receipt(event["revision"], request["proposal_id"])["status"] == "applied"
    assert not rt.drain_at_safe_point()


def test_overlapping_detectors_settle_one_incident(rig):
    rt, event = opportunity(rig)
    for i in range(3):
        result = rig.facade.submit(proposal(rig, event, proposal_id=f"p-{i}", feature_id=f"F0{i+4}"))
        assert result["status"] == "accepted"
    assert len(rt.drain_at_safe_point()) == 1
    assert not rt.drain_at_safe_point()
    assert list(r.status for r in rt.receipts.values()).count("applied") == 1


def test_authenticated_steer_after_acceptance_fences_apply(rig):
    from agent.supervision_context import steer_from_user
    rt, event = opportunity(rig)
    req = proposal(rig, event)
    assert rig.facade.submit(req)["status"] == "accepted"
    steer_from_user(rig.agent, "- replacement")
    assert not rt.drain_at_safe_point()
    assert rig.facade.receipt(event["revision"], req["proposal_id"])["status"] == "stale"


@pytest.mark.parametrize("mutation,reason", [
    ({"evidence_refs": ["fabricated"]}, "unbound_evidence"),
    ({"owner": "other"}, "owner_action_mismatch"),
    ({"action": "cancel_child"}, "owner_action_mismatch"),
    ({"template_id": "shell_command"}, "invalid_template"),
    ({"template_args": [["command", "rm -rf /"]]}, "invalid_template"),
    ({"plugin_generation": "obsolete"}, "revoked"),
])
def test_invalid_proposals_cannot_gain_authority(rig, mutation, reason):
    rt, event = opportunity(rig)
    result = rig.facade.submit(proposal(rig, event, **mutation))
    assert result["status"] == "rejected" and result["reason"] == reason
    assert not rt.drain_at_safe_point()


def test_worker_can_submit_but_cannot_apply(rig):
    rt, event = opportunity(rig)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(rig.facade.submit, proposal(rig, event)).result()["status"] == "accepted"
        with pytest.raises(RuntimeError, match="not_execution_owner"):
            pool.submit(rt.drain_at_safe_point).result()
    assert len(rt.drain_at_safe_point()) == 1


def test_expiry_stop_and_unload_each_override_accepted_proposal(rig):
    rt, event = opportunity(rig)
    request = proposal(rig, event)
    assert rig.facade.submit(request)["status"] == "accepted"
    rt.clock = lambda: event["deadline"]
    assert not rt.drain_at_safe_point()
    assert rt.receipts[request["proposal_id"]].status == "expired"
    rt.clock = __import__("time").monotonic
    rt, event = opportunity(rig)
    assert rig.facade.submit(proposal(rig, event, incident="stop"))["status"] == "accepted"
    rig.agent._interrupt_requested = True
    assert not rt.drain_at_safe_point()
    rig.agent._interrupt_requested = False
    rt, event = opportunity(rig)
    assert rig.facade.submit(proposal(rig, event, incident="unload"))["status"] == "accepted"
    rig.facade.unregister()
    assert not rt.drain_at_safe_point()


def test_registration_intersects_owner_grants_and_profile(rig):
    rig.config["supervision"]["plugins"]["fixture-supervisor"]["grants"] = ["observe"]
    (rig.home / "config.yaml").write_text(json.dumps(rig.config))
    reg = rig.facade.register(consumer=rig.events.append, requested_grants=["observe", "cancel_child"])
    assert reg["grants"] == ["observe"]
    assert not rig.facade.negotiate("supervision.future")["supported"]
    with pytest.raises(ValueError, match="unsupported"):
        rig.facade.register(version="supervision.future", consumer=rig.events.append)


@pytest.mark.parametrize("boundary", ["advisory", "selection", "owner"])
def test_unload_between_selection_and_effect_rechecks_generation(rig, monkeypatch, boundary):
    rt, event = opportunity(rig)
    take = rt._take
    def revoke_after_take(*args):
        entry = take(*args)
        if entry:
            rig.facade.unregister()
        return entry
    monkeypatch.setattr(rt, "_take", revoke_after_take)
    effects = []
    if boundary == "advisory":
        assert rig.facade.submit(proposal(rig, event))["status"] == "accepted"
        assert not rt.drain_at_safe_point()
    elif boundary == "selection":
        def consume(event):
            rig.facade.submit(proposal(rig, event, action="rank_candidates", template=None,
                owner="retrieval", refs=["a"], candidate_ids=["a"]))
        rig.facade._registration.consumer = consume
        request = OwnerRequestV1("retrieval", "rank", rt.revision, ({"id": "a"},),
            rt.clock() + 1, data_policy=("public_source",))
        assert not rig.facade.rank_candidates(request).applied
    else:
        rt.observe("owned", {}, target_id="owned", actions=(Action.RETAIN_RESULT,), evidence_refs=("a",))
        assert rig.facade.submit(proposal(rig, rig.events[-1], action="retain_result", template=None,
            refs=["a"]))["status"] == "accepted"
        receipt = rt.consume_owner_action("owned", Action.RETAIN_RESULT, lambda p: effects.append(p) or "applied")
        assert receipt is not None
        assert receipt.status == "rejected"
    assert not effects and not rt.incidents
    assert rt.receipts["proposal-incident"].reason == "revoked"


def test_owner_ranking_keeps_required_ids_and_canonical_objects(rig):
    rt = accept(rig.agent, "- Rank source evidence")
    original = ({"id": "a", "excerpt": "first"}, {"id": "b", "excerpt": "second"})
    def consume(event):
        if event["event"] == "rank_candidates":
            rig.facade.submit(proposal(rig, event, action="rank_candidates", template=None,
                owner="retrieval", refs=["a"], candidate_ids=["b", "a"]))
    rig.facade._registration.consumer = consume
    request = OwnerRequestV1("retrieval", "rank", rt.revision, original, rt.clock()+1,
                             required_ids=("a",), data_policy=("public_source",))
    decision = rig.facade.rank_candidates(request)
    assert decision.applied and decision.candidate_ids == ("b", "a")
    assert original[0]["excerpt"] == "first"


def test_owner_selection_cannot_drop_mandatory_candidate(rig):
    rt = accept(rig.agent, "- Inspect source")
    def consume(event):
        rig.facade.submit(proposal(rig, event, action="select_windows", template=None,
            owner="retrieval", refs=["a"], candidate_ids=["b"]))
    rig.facade._registration.consumer = consume
    request = OwnerRequestV1("retrieval", "windows", rt.revision,
        ({"id": "a", "excerpt": "error"}, {"id": "b", "excerpt": "detail"}), rt.clock()+.001,
        required_ids=("a",), data_policy=("public_source",))
    result = rig.facade.select_windows(request)
    assert not result.applied and result.candidate_ids == ("a", "b")
