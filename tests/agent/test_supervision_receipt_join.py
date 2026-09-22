"""Combined delivery/retrieval receipt chain through the actual LCM adapter.

Synthetic source rows and strict inference transport are supplied by the existing
vertical fixture. Storage failures use an actual competing SQLite writer, not a
success-returning settlement double. These are owner-join tests, not clearance of
downstream visibility, cross-provider authorization, or exactly-once effects.
"""
import sqlite3

import pytest

from agent import supervision_receipts
from tests.agent.test_supervision_evidence_owners import vertical, seed, recall  # noqa: F401
from tests.agent.supervision_test_support import rig, accept, proposal  # noqa: F401


def receipt_rows(v):
    with sqlite3.connect(v.agent._session_db.db_path) as conn:
        return conn.execute(
            "SELECT status,reason,receipt_id FROM supervision_receipts"
        ).fetchall()


@pytest.mark.parametrize("failure", [None, "owner_selection", "owner_selected", "owner_consumed"])
def test_actual_lcm_ack_chain_and_storage_failures(vertical, monkeypatch, failure):
    v = vertical
    seed(v)
    v.engine.supervision = None
    baseline = recall(v)
    v.engine.supervision = v.facade("hermes-lcm")
    original = supervision_receipts.record
    trace = []
    proposal_ids = []
    selection_writes = 0
    faulted = []

    def record(runtime, proposal, receipt, **kwargs):
        nonlocal selection_writes
        if receipt.reason == "owner_selection":
            selection_writes += 1
        stage = receipt.reason.split(":", 1)[0]
        # The first selection is dequeue; exercise the additional exposure fence
        # inside _apply_owner_decision after dequeue has already persisted.
        inject = failure == stage and not faulted and (
            stage != "owner_selection" or selection_writes == 2
        )
        if inject:
            with sqlite3.connect(v.agent._session_db.db_path) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                ok = original(runtime, proposal, receipt, **kwargs)
            faulted.append(stage)
            assert not ok
        else:
            ok = original(runtime, proposal, receipt, **kwargs)
        proposal_ids.append(proposal.proposal_id)
        trace.append((receipt.status, receipt.reason, ok, receipt_rows(v)))
        return ok

    monkeypatch.setattr(supervision_receipts, "record", record)
    actual = recall(v)
    assert len(v.calls) == 1
    assert not v.runtime.owner_selections
    row, = receipt_rows(v)
    successful = [(status, reason.split(":", 1)[0]) for status, reason, ok, _ in trace if ok]
    if failure is None:
        assert not faulted
        assert successful == [
            ("accepted", "queued"), ("selected", "owner_selection"),
            ("selected", "owner_selection"), ("accepted", "owner_selected"),
            ("applied", "owner_consumed"),
        ]
        assert row[0] == "consumed"
        assert row[1].startswith("owner_consumed:") and len(row[1].split(":")[1]) == 64
        assert trace[-1][1] == row[1]
        assert actual["hits"][0]["store_id"] != baseline["hits"][0]["store_id"]
    else:
        assert faulted == [failure]
        assert actual["hits"] == baseline["hits"]
        assert row[:2] == (("accepted", "owner_selected") if failure == "owner_consumed"
                          else ("selected", "owner_selection"))
        assert not any(status == "applied" for status, _ in successful)
        if failure != "owner_consumed":
            assert not v.runtime.incidents
        # Clearing process-local receipts cannot turn the unresolved durable
        # disposition into consumed, nor does it recreate an owner selection.
        v.runtime.receipts.clear()
        assert len(set(proposal_ids)) == 1
        assert supervision_receipts.lookup(v.runtime, proposal_ids[0])[:2] == row[:2]
        assert receipt_rows(v) == [row]


def test_instruction_work_is_durable_before_child_observer_receipt(rig, monkeypatch):
    import json
    from types import SimpleNamespace
    from agent.supervision_types import Action, project

    runtime = accept(rig.agent, "- Initial requirement.")
    observed = []

    def changed(event):
        assert event == "task_revision"
        with sqlite3.connect(rig.agent._session_db.db_path) as conn:
            row = conn.execute(
                "SELECT revision_json FROM supervision_work WHERE work_id=?",
                (runtime.revision.work_id,),
            ).fetchone()
        # A callback may submit immediately; no later work-metadata writer may
        # race its zero-busy-timeout receipt connection for this instruction.
        assert row and json.loads(row[0]) == project(runtime.revision)
        runtime.observe("incident", {}, target_id="child-result",
                        evidence_refs=("exact:child",), actions=(Action.ADVISE,))
        receipt = rig.facade.submit(proposal(rig, rig.events[-1], refs=["exact:child"]))
        assert receipt["status"] == "accepted"
        observed.append(receipt)

    monkeypatch.setattr(runtime, "children", SimpleNamespace(changed=changed))
    accept(rig.agent, "- Updated requirement.", continuation=True)
    assert len(observed) == 1
