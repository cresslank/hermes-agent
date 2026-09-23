"""F04: accepted consumer census + committed annex -> ordinary main read.

No test intent setter, installed owner, fabricated event, source read or inference
is used to manufacture eligibility. Real file dispatch/results remain mandatory.
"""
import copy
import json

import pytest

from tests.agent.test_owned_delegation_planning import (
    base_rig as base_rig, rig as rig, native as native, factory as factory,
)
from tests.agent.test_supervision_efficiency import feature_calls, flush
from tests.agent.test_supervision_views import assemble
from tests.agent.supervision_test_support import accept


def requests(p, *, exact=False):
    rows = []
    for i in range(2):
        row = copy.deepcopy(p.expansion)
        row["local_id"] = "read-" + str(i)
        row["operation"]["arguments"]["limit"] = 1 if exact else i + 1
        rows.append(row)
    return rows


def read(p, row, *, unchanged=False):
    from agent.tool_executor import _commit_tool_result, _ToolCallRef
    from tools.budget_config import DEFAULT_BUDGET
    args = row["operation"]["arguments"]
    ident, result = p.call("read_file", args, settle=False)
    # Replace the helper's uncommitted fixture row with the real canonical
    # observe/append/persistence owner, which consumes ready F04 advice.
    p.history.pop()
    committed = _commit_tool_result(p.a, p.history,
        _ToolCallRef("read_file", args, "default", ident, []), result,
        budget=DEFAULT_BUDGET, tool_duration=0, is_error=False, blocked=False,
        effect_disposition=None, observed=True)
    assert committed is not None
    if unchanged:
        assert json.loads(result)["content_returned"] is False
        assert json.loads(result)["dedup"] is True
    else:
        assert "Synthetic source:" in result
    return ident, result


def test_configured_ordinary_read_supplier_reuses_exact_recorded_bytes(factory):
    p = factory()
    rows = requests(p)
    p.commit(rows)
    first, first_bytes = read(p, rows[0])
    assert not feature_calls(p.native, "F04")
    assert p.native.owner.read_payloads[first] == first_bytes.encode()
    second, result = read(p, rows[1])
    reused = json.loads(result)
    assert reused["executed"] is False and reused["reused_from"] == "tool:" + first
    assert reused["result"] == first_bytes
    assert len(feature_calls(p.native, "F04")) == 1
    facts = feature_calls(p.native, "F04")[0]["state"]["facts"]
    assert facts["proposed"]["id"] == second
    assert facts["proposed"]["owner"] == "main" and facts["proposed"]["optional"] is False
    assert facts["proposed"]["independent_check"] is False
    assert facts["candidates"][0]["source_ref"] == "tool:" + first
    receipts = list(p.rt.receipts.values())
    assert len(receipts) == 1 and receipts[0].status == "applied"
    durable = p.a._session_db._conn.execute(
        "SELECT status,target_id,evidence_json FROM supervision_receipts WHERE proposal_id=?",
        (receipts[0].proposal_id,)).fetchone()
    assert durable[0] == "applied" and durable[1] == second
    assert "tool:" + first in json.loads(durable[2])
    original = copy.deepcopy(p.history)
    view = assemble(p.a, p.history)
    assert "reused_from" in str(view.api_messages)
    assert "tool:" + first in str(view.api_messages)
    assert p.history == original
    assert not p.rt.drain_at_safe_point()  # no second delivery; history remains intact
    stored = p.a._session_db.get_messages(p.a.session_id)
    assert "reused_from" in str(stored)
    assert not p.owner.list_owned()  # F04 never creates or cancels a child


@pytest.mark.parametrize("fault", [
    "missing", "v1", "independent", "required", "unknown", "unlinked", "no_permission",
    "wrong_operation", "stale_plan", "steering", "profile", "session", "unload", "changed_source",
    "no_native_capture", "partial_source", "other_independent_consumer",
])
def test_supplier_unknown_stale_required_and_independent_are_baseline(factory, fault, monkeypatch, tmp_path):
    def jobs(rows, _):
        if fault == "missing": rows[0].pop("requires_corroboration")
        if fault == "independent": rows[0]["requires_corroboration"] = True
        if fault in {"required", "unknown"}: rows[0]["obligation"] = fault
        if fault == "unlinked": rows[0]["requirements"][0]["start"] += 1
        if fault == "other_independent_consumer":
            rows.append({**rows[0], "ref": "job:independent", "requires_corroboration": True})
    def policy(section):
        if fault == "no_permission":
            section["plugins"]["fixture-supervisor"]["owned_delegation"]["allow_optional_readonly"] = False
    p = factory(jobs=jobs, version=1 if fault == "v1" else 2, policy_change=policy)
    if fault == "other_independent_consumer": p.expansion["consumer_refs"].append("job:independent")
    if fault == "no_native_capture":
        monkeypatch.setattr("agent.supervision_planning.publish_read", lambda *a: None)
    if fault == "partial_source": p.source.write_text("Synthetic source: partial\nsecond\nthird\n")
    rows = requests(p)
    p.commit(rows)
    read(p, rows[0])
    if fault == "wrong_operation": rows[1]["operation"]["arguments"]["limit"] = 3
    if fault == "stale_plan": p.commit([])
    if fault == "steering": accept(p.a, "- Require an independent check now.", continuation=True)
    if fault == "profile": monkeypatch.setenv("HERMES_HOME", str(tmp_path / "foreign"))
    if fault == "session":
        p.a._session_db.create_session("foreign-session", "cli")
        p.a.session_id = "foreign-session"
    if fault == "unload": p.native.bridge.close()
    if fault == "changed_source": p.source.write_text("Synthetic source: changed snapshot.\n")
    read(p, rows[1])
    flush(p.native)
    assert not feature_calls(p.native, "F04")
    assert not [r for r in p.rt.receipts.values() if r.status == "applied"]
    assert not p.owner or not p.owner.list_owned()


@pytest.mark.parametrize("fault", ["source", "instruction", "permission"])
def test_supplier_rechecks_after_semantic_response(factory, fault):
    p = factory()
    rows = requests(p)
    p.commit(rows)
    read(p, rows[0])
    def revoke():
        if fault == "source": p.source.write_text("Synthetic source: changed during response.\n")
        if fault == "instruction": accept(p.a, "- Independently check again.", continuation=True)
        if fault == "permission": p.native.bridge.native.unregister()
    p.native.on_response.append(revoke)
    read(p, rows[1])
    assert len(feature_calls(p.native, "F04")) == 1
    assert not [r for r in p.rt.receipts.values() if r.status == "applied"]
    assert not p.rt.drain_at_safe_point()


def test_semantic_non_substitution_retains_baseline(factory):
    p = factory()
    rows = requests(p)
    p.commit(rows)
    read(p, rows[0])
    def distinct(answers):
        for value in answers.values():
            value["score"] = 0
            value["probabilities"] = {key: float(key == "0") for key in value["probabilities"]}
    p.native.answer_mutators.append(distinct)
    read(p, rows[1])
    assert len(feature_calls(p.native, "F04")) == 1
    assert "Task-bound advisory" not in str(p.history)
    assert not [r for r in p.rt.receipts.values() if r.status == "applied"]


def test_exact_requested_windows_bypass_semantics(factory):
    p = factory()
    # One request is issued, then an independently committed ordinary request
    # for the identical window; do not introduce ambiguous duplicate declarations.
    rows = requests(p, exact=True)
    p.commit([rows[0]])
    read(p, rows[0])
    p.commit([rows[1]])
    read(p, rows[1], unchanged=True)
    assert not feature_calls(p.native, "F04")
