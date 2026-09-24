"""Direct-control native store/dispatch fences and local input identities."""
import dataclasses
import subprocess
import time

import pytest

from agent.owned_delegation import ControlDenied, SemanticEvidence, supervisor_control_for_child
from agent.owned_delegation_direct import VERSION, input_identity
from tests.agent.test_owned_delegation_bridge import rig


def direct_job(rig):
    child, handle = rig.launch()
    rig.owner._grant = dataclasses.replace(rig.owner._grant, allow_owned_child_stop=True)
    with rig.owner._lock:
        live = rig.owner._get(handle)
        # This fixture exercises the lower owner contract; production launch
        # coverage belongs to the configured-policy integration tests.
        rig.owner._commit(live, lambda s: s.update(direct_control=VERSION, replaceable=True,
            input_mode="current", original_input_ref="input:old", current_input_ref="input:new",
            obligation="required", obligation_ref="obligation:fixture", obligation_state="open", partial_refs=[]))
    return child, handle


def cancel(rig, handle, **changes):
    state = rig.owner.status(handle)
    evidence = SemanticEvidence(tuple(rig.revision), tuple((c["ref"], 0.) for c in state["consumers"]), "superseded", .8, .8)
    evidence = dataclasses.replace(evidence, **changes)
    return rig.owner.request_semantic_cancel(handle, expected_revision=state["control_revision"], evidence=evidence,
        idempotency_key="direct-decision", deadline=time.monotonic() + 1)


def test_one_judgment_pending_stop_survives_inflight_and_expiry_with_open_obligation(rig):
    child, handle = direct_job(rig)
    path = rig.path / "input.txt"
    path.write_text("content")
    with rig.owner.dispatch(handle, "read_file", {"path": str(path)}):
        receipt = cancel(rig, handle)
        assert receipt.accepted and not child.stopped.is_set()
        control = supervisor_control_for_child(child)
        assert control["state"] == "pending_stop" and control["obligation_state"] == "open"
        with pytest.raises(ControlDenied, match="sealed"):
            with rig.owner.dispatch(handle, "read_file", {"path": str(path)}):
                pass
    assert child.stopped.is_set()
    assert supervisor_control_for_child(child)["state"] == "requested"
    rig.owner.finish(handle)
    control = supervisor_control_for_child(child)
    assert control["state"] == "stopped" and control["obligation_state"] == "open"
    assert rig.store.read(handle.child_id)["supervisor_control"]["decision_id"] == control["decision_id"]


@pytest.mark.parametrize("change", [dict(cleanup_pending=True), dict(consumer_set_closed=False), dict(handoffs=['pending'])])
def test_direct_native_owner_keeps_identity_capability_and_input_fences(rig, change):
    child, handle = direct_job(rig)
    with rig.owner._lock:
        rig.owner._commit(rig.owner._get(handle), lambda s: s.update(change))
    assert not cancel(rig, handle).accepted
    assert not child.stopped.is_set()


def test_finished_wins_and_signal_failure_is_durable(rig, monkeypatch):
    child, handle = direct_job(rig)
    def fail(*a, **kw):
        raise RuntimeError("signal failed")
    monkeypatch.setattr(child, "hard_interrupt", fail)
    assert cancel(rig, handle).accepted
    assert supervisor_control_for_child(child)["state"] == "signal_failed"
    rig.owner.finish(handle)
    assert supervisor_control_for_child(child)["state"] == "already_finished"
    assert rig.owner.status(handle)["processes_stopped"]


def test_input_identity_covers_same_head_dirty_untracked_delete_and_rename(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True).stdout
    git("init")
    path = tmp_path / "a.py"
    path.write_text("one")
    git("add", "a.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    head = git("rev-parse", "HEAD")
    identities = [input_identity([str(tmp_path)], budget=3)]
    path.write_text("two")
    identities.append(input_identity([str(tmp_path)], budget=3))
    other = tmp_path / "untracked.py"
    other.write_text("other")
    identities.append(input_identity([str(tmp_path)], budget=3))
    other.rename(tmp_path / "renamed.py")
    identities.append(input_identity([str(tmp_path)], budget=3))
    path.unlink()
    identities.append(input_identity([str(tmp_path)], budget=3))
    assert all(identities) and len(set(identities)) == len(identities)
    assert git("rev-parse", "HEAD") == head
    assert input_identity([str(tmp_path)], max_files=1) is None
