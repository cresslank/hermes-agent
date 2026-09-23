"""A multi-feature policy catalog must not inherit one snapshot's field budget."""
from dataclasses import replace

import pytest

from agent.supervision_types import Completeness, DecisionSnapshotV1, Revision
from tests.agent.supervision_test_support import rig as rig, accept
from tests.agent.test_supervision_wire import configure_egress, explicit_policy


def test_large_catalog_registers_but_observations_project_only_exact_fields(rig):
    required = ["requirements", "source_message_id", "text", "continuation", "target_id"]
    keys = [*required, *(f"other_feature_{i}" for i in range(100))]
    policy = explicit_policy(str(rig.home), keys)
    configure_egress(rig, policy)
    assert set(rig.facade._registration.egress_policy["fields"]) == set(keys)
    rt = accept(rig.agent, "- Complete this requested item")
    assert rt is not None
    event = rig.events[-1]
    assert set(event["data_policy"]["fields"]) == set(event["facts"]) == set(required)
    assert event["data_policy"]["sources"] == {k: "accepted-task-source" for k in required}
    rt.observe("other", {"unapproved": "not authorized"})
    assert rig.events[-1]["data_policy"] == {}
    configure_egress(rig, explicit_policy("other-profile", keys))
    assert rig.facade._registration.egress_policy == {}
    configure_egress(rig, explicit_policy(str(rig.home), [f"field_{i}" for i in range(257)]))
    assert rig.facade._registration.egress_policy == {}


def test_expanding_catalog_does_not_expand_snapshot_budget_or_source_authority():
    keys = [f"field_{i}" for i in range(64)]
    policy = explicit_policy("profile", keys)
    snapshot = DecisionSnapshotV1("event", "event-id", 1, Revision("profile", "lineage", "work"),
        {k: "bounded" for k in keys}, Completeness(), 1., data_policy=policy)
    larger = explicit_policy("profile", [*keys, "extra"])
    with pytest.raises(ValueError, match="invalid_data_policy"):
        replace(snapshot, facts={**snapshot.facts, "extra": "bounded"}, data_policy=larger)
    with pytest.raises(ValueError, match="missing_source_grant"):
        replace(snapshot, data_policy={**policy, "sources": {}})
    with pytest.raises(ValueError, match="unclassified_fact"):
        replace(snapshot, facts={**snapshot.facts, "unapproved": "not authorized"})
