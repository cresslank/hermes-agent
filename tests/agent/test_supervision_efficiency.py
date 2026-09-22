"""Native owner paths through the actual optional Jev adapter and strict HTTP codec.

Run with the standalone plugin src on pythonpath. No live transport, credentials,
model generation or hand-built supervision event is used. The installed plugin
entry point is loaded with synthetic credentials and strict mock HTTP.
"""
import json
import importlib.metadata
import socket
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("jev_supervisor")
from jev_supervisor.config import ENDPOINT
from jev_supervisor.host_adapter import _ACTION as JEV_NATIVE_ACTIONS
import httpx

from agent.supervision_efficiency import (
    DelegationIntent, ResearchGap, ResearchPass, Route, VerificationCheck, for_agent,
)
from agent.subagent_lifecycle import bind_subagent_parent
from agent.tool_guardrails import ToolCallGuardrailController, ToolCallGuardrailConfig
from tests.agent.supervision_test_support import rig as rig, accept


FIELDS = "changed evidence_refs target_id proposed candidates exact_reusable incident_id error requirement deterministic_handler_available routes attempted_routes attempts window_start_sequence window_end_sequence registered_poll registered_retry pagination user_repetition healthy_progress already_intervened recovery_route operation progress signal phase_evidence_present already_diagnosed diagnostic_route subtask parent delegation_allowed budget_available overhead_favorable parallelism_favorable specialists pending prior passes gaps mandatory_gap corroboration_required next_pass completion_criteria budget_class".split()


@pytest.fixture
def native(rig, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    policy = {"id": "synthetic-owner-policy", "profile": str(rig.home), "fixture": True,
              "fields": {k: "synthetic" for k in FIELDS}, "sources": {}}
    rig.config["supervision"]["plugins"]["fixture-supervisor"]["egress_policy"] = policy
    settings = {"profile": str(rig.home), "enabled": True, "policy_id": policy["id"],
                "allowed_classes": ["synthetic"], "fixture_policy": True}
    rig.config["plugins"] = {"entries": {"fixture-supervisor": {"settings": {
        "policy": settings, "credential_environment": "JEV_EFFICIENCY_FIXTURE_KEY"}}}}
    monkeypatch.setenv("JEV_EFFICIENCY_FIXTURE_KEY", "synthetic-not-a-credential")
    (rig.home / "config.yaml").write_text(json.dumps(rig.config))
    calls = []
    on_response = []
    choice = {"F07": "none", "F10": "no_progress_cause_unknown", "F11": "diagnostic"}
    def respond(request):
        assert str(request.url) == ENDPOINT and request.method == "POST"
        body = json.loads(request.content)
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == "jev-1.13.0" and body["questions"]
        assert all(k in policy["fields"] for k in body["state"]["facts"])
        calls.append(body)
        answers = {}
        for key, q in body["questions"].items():
            assert q["instructions"]["targets"]
            feature = key.split("/")[0]
            if q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.0 if feature == "F08" else 1.0}
            elif q["type"] == "score":
                level = 0 if feature == "F08" else 2
                answers[key] = {"type": "score", "score": level, "confidence": 1.0,
                    "probabilities": {str(i): float(i == level) for i in range(len(q["criteria"]))},
                    "legend": {str(i): v for i, v in enumerate(q["criteria"])}}
            else:
                winner = choice[feature]
                assert winner in q["criteria"]
                answers[key] = {"type": "choice", "choice": winner, "confidence": 1.0,
                    "probabilities": {k: float(k == winner) for k in q["criteria"]}}
        for callback in on_response:
            callback()
        return httpx.Response(200, json={"model": body["model"], "answers": answers, "usage": {}})
    native_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return native_client(**{**kwargs, "transport": httpx.MockTransport(respond)})
    monkeypatch.setattr(httpx, "AsyncClient", client)
    from hermes_cli.plugins import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest
    context = PluginContext(PluginManifest(name="fixture-supervisor"), rig.manager)
    entry, = [e for e in importlib.metadata.entry_points(group="hermes.plugins") if e.name == "jev-supervisor"]
    bridge = entry.load().register(context)
    assert bridge is not None and bridge.inspect()["registered"]
    rig.facade = bridge.native
    rt = accept(rig.agent, "- Inspect the synthetic fixture.")
    owner = for_agent(rig.agent)
    permitted = [True]
    # A real host read-only diagnostic implementation; semantic code cannot call it.
    route = Route("diagnostic", "Read the retained native result receipt", "synthetic fixture",
                  "The result owner retains its receipt", "v1", "none", "readonly", lambda: permitted[0])
    owner.register_route(route)
    rig.agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "hard_stop_enabled": False, "warnings_enabled": False}))
    rig.agent._stall_guards_enabled = lambda: False
    rig.agent._tool_guardrail_halt_decision = None
    yield SimpleNamespace(rig=rig, bridge=bridge, runtime=rt, owner=owner, calls=calls, permitted=permitted, choice=choice, on_response=on_response)
    bridge.close()


def flush(native):
    with native.bridge._lock:
        futures = tuple(native.bridge._pending)
    for future in futures:
        future.result(timeout=3)


def observe_result(native, name, args, result, ident, failed):
    from run_agent import AIAgent
    return AIAgent._append_guardrail_observation(native.rig.agent, name, args, result,
                                                failed=failed, tool_call_id=ident)


def admit_failures(native, path, **overrides):
    policy = dict(registered_poll=False, registered_retry=False, pagination=False,
                  user_repetition=False, deterministic_handler_available=False)
    native.owner.admit_attempt_policy("read_file", path, **{**policy, **overrides})


def feature_calls(native, feature):
    return [b for b in native.calls if any(k.startswith(feature + "/") for k in b["questions"])]


def test_f04_native_dispatch_retains_main_read_and_reuses_only_advisory(native, monkeypatch, tmp_path):
    path = tmp_path / "fixture.txt"
    path.write_text("synthetic source")
    native.owner.admit_read_intent(str(path), independent_check=False)
    from agent.tool_executor import _dispatch_authorized_once, _ManagedToolResult, _ToolCallRef
    from tools.file_tools import read_file_tool
    monkeypatch.setattr("agent.tool_executor._pre_tool_block", lambda agent, ref: (None, ref.args))
    monkeypatch.setattr("agent.tool_executor._begin_tool_execution", lambda *args: None)
    monkeypatch.setattr("agent.tool_executor._run_with_activity_heartbeat", lambda agent, name, fn: fn())
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda *args: None)
    for index, ident in enumerate(("read-1", "read-2")):
        args = {"path": str(path), "limit": index + 1}
        state = _ManagedToolResult(None, args, [], False, False)
        result = _dispatch_authorized_once(native.rig.agent, state, _ToolCallRef("read_file", args, "default", ident, []),
            execute=lambda values: read_file_tool(**values), scope_block=None, display_index=None,
            begin_execution=None, authorization_gate=None)
        assert "synthetic source" in result and not state.blocked  # real file read still executes
        observe_result(native, "read_file", args, result, ident, False)
    assert len(feature_calls(native, "F04")) == 1
    assert any(r.status == "applied" for r in native.runtime.receipts.values())
    assert native.runtime.drain_at_safe_point()
    assert not native.runtime.drain_at_safe_point()


@pytest.mark.parametrize("independent", [True, None])
def test_unknown_or_independent_read_never_requests_dedupe(native, tmp_path, independent):
    path = str(tmp_path / "fixture.txt")
    with open(path, "w") as stream:
        stream.write("synthetic")
    if independent is not None:
        native.owner.admit_read_intent(path, independent_check=independent)
    for ident in ("a", "b"):
        assert native.owner.before_read({"path": path}, ident) is None
        native.owner.read_completed(ident, failed=False)
    flush(native)
    assert not native.calls


def test_f06_actual_guardrail_results_distinct_incident_and_single_hint(native):
    native.choice["F11"] = "none"  # isolate the later recurrence decision
    path = "synthetic.txt"
    admit_failures(native, path)
    hints, messages = [], []
    for i in range(4):
        ident = f"attempt-{i}"
        content = observe_result(native, "read_file", {"path": path}, '{"error":"missing fixture"}', ident, True)
        flush(native)
        hints.extend(native.runtime.drain_at_safe_point())
        messages.extend([{"role": "assistant", "tool_calls": [{"id": ident, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
            {"role": "tool", "tool_call_id": ident, "name": "read_file", "content": content}])
        native.runtime.committed_batch(messages)  # next native round, not a renewed wait inside one round
    assert len(feature_calls(native, "F06")) == 1
    assert len(hints) == 1 and "diagnostic" in hints[0]
    assert not native.runtime.drain_at_safe_point()
    assert not native.rig.agent._interrupt_requested
    assert any(r.status == "applied" for r in native.runtime.receipts.values())


@pytest.mark.parametrize("flag", ["registered_poll", "registered_retry", "pagination", "user_repetition", "deterministic_handler_available"])
def test_native_repetition_exemptions_produce_zero_questions(native, flag):
    admit_failures(native, "synthetic.txt", **{flag: True})
    for i in range(4):
        observe_result(native, "read_file", {"path": "synthetic.txt"}, '{"error":"missing"}', str(i), True)
    flush(native)
    assert not native.calls


def test_progress_or_changed_target_cannot_become_loop(native):
    admit_failures(native, "a")
    admit_failures(native, "b")
    for i, path in enumerate(("a", "b", "a")):
        observe_result(native, "read_file", {"path": path}, '{"error":"missing"}', str(i), True)
        flush(native)
    observe_result(native, "write_file", {"path": "a"}, '{"verified":true}', "write", False)
    observe_result(native, "read_file", {"path": "a"}, '{"error":"missing"}', "after", True)
    flush(native)
    assert not feature_calls(native, "F06")


def build_child(native, monkeypatch, tmp_path):
    from agent.owned_delegation import Consumer, OwnerGrant, install_owner, scoped_file_policy
    from tools.delegation_control_store import SQLiteControlStore
    from tests.agent.test_owned_delegation_bridge import Child
    from hermes_state import SessionDB
    from tools import delegate_tool as dt
    db = tmp_path / "control.db"
    parent = native.rig.agent
    session_db = SessionDB(db)
    try:
        session_db.create_session(parent.session_id, "cli")
    finally:
        session_db.close()
    store = SQLiteControlStore(lambda: sqlite3.connect(db))
    policy = scoped_file_policy((str(tmp_path),))
    consumers = {"parent:" + parent.session_id: Consumer("parent:" + parent.session_id, "optional"),
                 "research": Consumer("research", "optional")}
    owner = install_owner(parent, store=store, grant=OwnerGrant(str(native.rig.home), "fixture-supervisor", True, True),
        consumer_resolver=consumers.get, policy=policy, revision_provider=lambda: (1, 1, 1))
    child = Child("child-native")
    def launch_authorized():
        return any(h.child_id == child._subagent_id and owner.status(h)["effect_class"] == "read_only"
                   and not owner.status(h)["cancel_requested"] for h in owner.list_owned())
    native.owner.register_route(Route("delegate:leaf", "Run the constructed owned read-only leaf",
        parent.session_id, "Registered readonly child and current native launch policy", "v1",
        "none", "readonly", launch_authorized))
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kwargs: child)
    req = dict(obligation="optional", consumer_refs=["research"], consumer_set_closed=True, effect_policy_id=policy.policy_id)
    children, error = dt._build_children([{"goal": "Inspect fixture", "supervision": req}], [None],
        dict(model="synthetic", provider="synthetic", base_url=None, api_key=None, api_mode=None),
        top_role="leaf", max_iterations=2, parent_agent=parent, routing_cfg={}, live_deleg_id=None, live_writers=[])
    assert error is None and children
    return child, owner


def test_f05_normal_child_construction_emits_existing_route_only(native, monkeypatch, tmp_path):
    native.owner.admit_delegation(DelegationIntent("Inspect fixture", "Return source references", "Synthetic source",
        "parent-step", "Review another source", "read_file", True, False, False, True, True, True, "delegate:leaf"))
    child, owner = build_child(native, monkeypatch, tmp_path)
    flush(native)
    assert feature_calls(native, "F05")
    assert "delegate:leaf" in native.runtime.drain_at_safe_point()[0]
    assert not child.stopped.is_set()
    assert not owner.status(owner.list_owned()[0])["cancel_requested"]


def test_f10_native_heartbeat_not_elapsed_prose_and_no_cancel(native, monkeypatch, tmp_path):
    from tools.delegate_tool_child_run import _Heartbeat
    child, _ = build_child(native, monkeypatch, tmp_path)
    native.rig.agent._touch_activity = lambda text: None
    child.get_activity_summary = lambda: dict(current_tool="read_file", api_call_count=1,
        max_iterations=3, last_activity_ts=20.0)
    monkeypatch.setattr("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IN_TOOL", 2)
    native.owner.routes.clear()  # native heartbeat binds its real owned-status route
    heartbeat = _Heartbeat(child, native.rig.agent, 0)
    assert heartbeat.tick() is None
    assert heartbeat.tick() is None
    flush(native)
    assert not feature_calls(native, "F10")
    assert heartbeat.tick() is False
    flush(native)
    assert len(feature_calls(native, "F10")) == 1
    assert "owned-status:child-native" in native.runtime.drain_at_safe_point()[0]
    assert not child.stopped.is_set() and heartbeat.settled.is_set()  # baseline wait abandonment retained


def test_route_revocation_between_proposal_and_owner_consumption(native, monkeypatch, tmp_path):
    from tools.delegate_tool_child_run import _Heartbeat
    child, _ = build_child(native, monkeypatch, tmp_path)
    native.rig.agent._touch_activity = lambda text: None
    child.get_activity_summary = lambda: dict(current_tool="read_file", api_call_count=1, last_activity_ts=20.0)
    monkeypatch.setattr("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IN_TOOL", 1)
    heartbeat = _Heartbeat(child, native.rig.agent, 0)
    assert heartbeat.tick() is None
    assert heartbeat.tick() is False
    flush(native)
    native.permitted[0] = False
    assert not native.runtime.drain_at_safe_point()
    assert any(r.status == "stale" for r in native.runtime.receipts.values())


def test_f08_owner_yield_ledger_cannot_close_open_requirements(native):
    gap = ResearchGap("optional-detail", "Optional corroborating background", False)
    for i in range(2):
        assert native.owner.commit_research_pass(ResearchPass(str(i), "No additional matching sources", "same-source", (gap.id,), (), ()))
    before = native.runtime.requirements
    hint = native.owner.propose_expansion(pass_id="next", text="Repeat the same search", source_method="same-source",
        gaps=(gap,), completion_criteria="Corroborating detail is optional", budget_class="bounded",
        optional=True, corroboration_required=False)
    assert hint and feature_calls(native, "F08")
    assert native.runtime.requirements == before and not native.runtime.closed
    assert native.owner.propose_expansion(pass_id="mandatory", text="Check required evidence", source_method="same-source",
        gaps=(replace(gap, mandatory=True),), completion_criteria="Must verify", budget_class="bounded",
        optional=True, corroboration_required=False) is None
    assert len(feature_calls(native, "F08")) == 1


def check(ident="check", **overrides):
    return VerificationCheck(ident, "Verify synthetic recipe", ("claim",), "snapshot", "input",
        "environment-fingerprint", "dependencies", **{**dict(required=False, external_write_readback=False,
        acceptance_test=False, independent_review=False, time_sensitive=False, explicit_user_check=False), **overrides})


def record_native_check(native, tmp_path, monkeypatch):
    from agent.verification_evidence import record_verify_run
    monkeypatch.setattr("agent.verification_evidence._ledger_enabled", lambda: True)
    monkeypatch.setattr("agent.verification_evidence._project_facts", lambda root: {"root": str(tmp_path)})
    with bind_subagent_parent(native.rig.agent):
        receipt = record_verify_run(root=tmp_path, session_id=native.rig.agent.session_id, ok=True,
                                    output="recipe verified", supervision_check=check("prior"))
    assert receipt is not None and receipt["kind"] == "verify" and receipt["id"] > 0
    assert native.owner.checks["prior"][1] == receipt
    return receipt


@pytest.mark.parametrize("flag", VerificationCheck.protected_fields())
def test_f07_native_receipts_never_waive_mandatory_checks(native, tmp_path, monkeypatch, flag):
    from agent.verification_evidence import propose_verification_reuse
    record_native_check(native, tmp_path, monkeypatch)
    pending = check(**{flag: True})
    with bind_subagent_parent(native.rig.agent):
        assert propose_verification_reuse(pending, current=lambda: pending) is None
    flush(native)
    assert not feature_calls(native, "F07")


def test_unclassified_shell_success_cannot_bind_check_receipt(native):
    assert not native.owner.record_check(check(), {"id": 1, "kind": "test", "status": "passed", "root": "fixture"}, validated=True)
    assert not native.owner.checks


@pytest.mark.parametrize("case", ["reused", "fingerprint_changed", "mandatory_now", "forged_receipt",
    "advisory_is_not_reuse", "session_changed", "duplicate_claims", "malformed_claims",
    "wrong_source", "waived", "wrong_semantic_action", "new_failure", "workspace_edited",
    "current_unavailable", "grant_missing", "profile_changed", "new_success", "receipt_deleted",
    *("changed_" + name for name in (*VerificationCheck.fingerprint_fields(), *VerificationCheck.protected_fields()))])
def test_native_receipt_admission_contract_without_codec_substitution(native, tmp_path, monkeypatch, case):
    """Host effect proof separate from the real-plugin integration gap below."""
    from agent.verification_evidence import propose_verification_reuse
    receipt = record_native_check(native, tmp_path, monkeypatch)
    pending = check(plugin_owned=True)
    current = [pending]
    captured = []
    registration = {}
    def local_provider(event):
        if event["event"] != "verification_proposed":
            return
        captured.append(event)
        rid = "verification:" + str(receipt["id"])
        data = dict(proposal_id="receipt-proposal", plugin_generation=registration["generation"],
            feature_id="F07", incident_id="receipt-incident", expected=event["revision"],
            owner=event["owner"], target_id=pending.id, action="reuse_receipt", evidence_refs=[rid, *pending.claim_ids],
            expires_at_monotonic=event["deadline"], template_id="reuse_evidence",
            metadata={"feature_action": "reuse_exact_receipt", "receipt_id": rid, "source_ref": rid,
                      "snapshot": pending.snapshot, "claim_ids": list(pending.claim_ids), "verification_waived": False})
        if case == "forged_receipt":
            data["metadata"]["receipt_id"] = "verification:unknown"
        if case == "advisory_is_not_reuse":
            data["action"] = "advise"
        metadata_changes = {"duplicate_claims": {"claim_ids": ["claim", "claim"]},
            "malformed_claims": {"claim_ids": [["claim"]]}, "wrong_source": {"source_ref": "other"},
            "waived": {"verification_waived": True}, "wrong_semantic_action": {"feature_action": "useful_reuse_hint"}}
        data["metadata"].update(metadata_changes.get(case, {}))
        result = native.rig.facade.submit(data)
        assert result["status"] == ("rejected" if case in {"advisory_is_not_reuse", "grant_missing"} else "accepted")
        if case == "fingerprint_changed":
            current[0] = replace(pending, snapshot="changed")
        elif case == "mandatory_now":
            current[0] = replace(pending, required=True)
        elif case == "session_changed":
            native.rig.agent.session_id = "session-B"
        elif case in {"new_failure", "new_success"}:
            from agent.verification_evidence import record_verify_run
            record_verify_run(root=tmp_path, session_id=native.rig.agent.session_id, ok=case == "new_success")
        elif case == "profile_changed":
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
        elif case == "receipt_deleted":
            with sqlite3.connect(native.rig.home / "verification_evidence.db") as conn:
                conn.execute("DELETE FROM verification_events WHERE id=?", (receipt["id"],))
        elif case.startswith("changed_"):
            field = case.removeprefix("changed_")
            current[0] = replace(pending, **{field: True if field in pending.protected_fields() else "changed"})
        elif case == "workspace_edited":
            from agent.verification_evidence import mark_workspace_edited
            mark_workspace_edited(cwd=tmp_path, session_id=native.rig.agent.session_id, paths=["changed.py"])
    def current_check():
        if captured and case == "current_unavailable":
            raise OSError("fingerprint unavailable")
        return current[0]
    flush(native)
    grants = ["observe", "advise"]
    if case != "grant_missing":
        grants.append("reuse_receipt")
    registration.update(native.rig.facade.register(consumer=local_provider, requested_grants=grants))
    with bind_subagent_parent(native.rig.agent):
        actual = propose_verification_reuse(pending, current=current_check)
    assert len(captured) == 1
    assert actual == (receipt if case == "reused" else None)
    settlement = native.runtime.receipts["receipt-proposal"]
    stale = {"fingerprint_changed", "mandatory_now", "session_changed", "new_failure",
             "workspace_edited", "current_unavailable", "profile_changed", "new_success", "receipt_deleted"}
    assert settlement.status == ("applied" if case == "reused" else "stale" if case in stale or case.startswith("changed_") else "rejected")
    assert not native.calls  # no claim that this local provider proof is the Jev codec


def test_f07_main_check_receives_hint_not_a_replacement_receipt(native, tmp_path, monkeypatch):
    from agent.verification_evidence import propose_verification_reuse
    receipt = record_native_check(native, tmp_path, monkeypatch)
    pending = check(plugin_owned=False)
    with bind_subagent_parent(native.rig.agent):
        hint = propose_verification_reuse(pending, current=lambda: pending)
    assert isinstance(hint, str) and "verification:" + str(receipt["id"]) in hint
    assert feature_calls(native, "F07")
    with sqlite3.connect(native.rig.home / "verification_evidence.db") as conn:
        assert conn.execute("SELECT id FROM verification_events").fetchall() == [(receipt["id"],)]
    assert not native.owner.guards  # completed checks do not leak guard closures


def test_f07_optional_real_receipt_round_trip(native, tmp_path, monkeypatch):
    from agent.verification_evidence import propose_verification_reuse
    assert native.rig.facade.negotiate()["receipt_reuse"] == "supervision.receipt-reuse.v1"
    assert native.bridge.inspect()["optional_action_codecs"]["reuse_exact_receipt"] == "reuse_receipt"
    receipt = record_native_check(native, tmp_path, monkeypatch)
    pending = check(plugin_owned=True)
    with bind_subagent_parent(native.rig.agent):
        reused = propose_verification_reuse(pending, current=lambda: pending)
    assert reused == receipt
    assert feature_calls(native, "F07")


@pytest.mark.xfail("select_existing_authorized_route" not in JEV_NATIVE_ACTIONS, strict=True,
                   reason="Requires native authorized-route advisory codec")
def test_f11_real_failure_route_round_trip(native):
    admit_failures(native, "synthetic.txt")
    observe_result(native, "read_file", {"path": "synthetic.txt"}, '{"error":"missing"}', "failure", True)
    flush(native)
    assert feature_calls(native, "F11")
    assert "diagnostic" in native.runtime.drain_at_safe_point()[0]
