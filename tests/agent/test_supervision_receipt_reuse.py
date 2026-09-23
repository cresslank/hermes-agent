"""Native capability and ledger fencing; no optional plugin or network required."""
import json
import sqlite3

import pytest

from agent.supervision_types import Action
from agent.verification_evidence import record_verify_run, with_current_verification_receipt
from tests.agent.supervision_test_support import rig as rig


def test_receipt_capability_is_support_not_authority(rig, tmp_path):
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    assert rig.facade.negotiate()["receipt_reuse"] == "supervision.receipt-reuse.v1"
    assert "receipt_reuse" not in rig.facade.negotiate("supervision.unsupported")
    # A facade in another profile must not inherit even this same plugin's grants.
    other = tmp_path / "other-profile"
    other.mkdir()
    config = {"supervision": {"enabled": True, "plugins": {"fixture-supervisor": {
        "grants": ["observe", "advise", "continue", "retain_result"], "data_policy": []}}}}
    (other / "config.yaml").write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(other))
    facade = PluginContext(PluginManifest(name="fixture-supervisor"), manager).supervision
    assert facade.negotiate()["receipt_reuse"] == "supervision.receipt-reuse.v1"
    assert facade.negotiate()["grants"] == []
    try:
        registered = facade.register(consumer=lambda event: None,
            requested_grants=["observe", *(action.value for action in Action)])
        assert "reuse_receipt" not in registered["grants"]
        assert "reuse_receipt" not in facade.negotiate()["grants"]
        assert "reuse_receipt" in rig.facade.negotiate()["grants"]
    finally:
        facade.unregister()


@pytest.fixture
def receipt(rig, tmp_path, monkeypatch):
    monkeypatch.setattr("agent.verification_evidence._ledger_enabled", lambda: True)
    monkeypatch.setattr("agent.verification_evidence._project_facts", lambda root: {"root": str(tmp_path)})
    return record_verify_run(root=tmp_path, session_id=rig.agent.session_id, ok=True)


def test_receipt_admission_fences_other_native_ledger_writers(rig, receipt):
    # A deferred read transaction alone does NOT fence WAL writers.
    path = rig.home / "verification_evidence.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    consumed = []
    def consume():
        with sqlite3.connect(path, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("UPDATE verification_state SET last_edit_at='changed'")
        consumed.append(receipt["id"])
        return True
    assert with_current_verification_receipt(receipt, consume) is True
    assert consumed == [receipt["id"]]
    with sqlite3.connect(path, timeout=0) as conn:
        conn.execute("UPDATE verification_state SET last_edit_at='changed'")
    assert with_current_verification_receipt(receipt, lambda: pytest.fail("stale receipt")) is None


@pytest.mark.parametrize("case", ["missing", "busy", "forged", "failed", "nonzero_exit"])
def test_unavailable_or_invalid_receipt_preserves_baseline(rig, receipt, case):
    from agent import verification_evidence as ledger
    altered = dict(receipt)
    connection = None
    if case == "missing":
        altered["id"] += 1
    elif case == "busy":
        connection = sqlite3.connect(rig.home / "verification_evidence.db", timeout=0)
        connection.execute("BEGIN IMMEDIATE")
    elif case == "forged":
        altered["output_summary"] = "forged"
    else:
        # Real stored row, but never a reusable success, even if the mapping matches.
        altered["exit_code"] = 1
        if case == "failed":
            altered["status"] = "failed"
        with sqlite3.connect(rig.home / "verification_evidence.db") as conn:
            conn.execute("UPDATE verification_events SET exit_code=?, status=? WHERE id=?",
                         (altered["exit_code"], altered["status"], altered["id"]))
    try:
        assert ledger.with_current_verification_receipt(altered, lambda: pytest.fail("invalid reuse")) is None
    finally:
        if connection:
            connection.close()


def test_absent_ledger_is_not_created(rig, monkeypatch):
    monkeypatch.setattr("agent.verification_evidence._ledger_enabled", lambda: True)
    assert with_current_verification_receipt({"kind": "verify"}, lambda: True) is None
    assert not (rig.home / "verification_evidence.db").exists()
