"""Ordinary file dispatch -> native receipt -> full Jev registry/strict HTTP.

No attempt setter, manufactured failure facts or fixture recovery route qualifies
these tests. Profile declarations bind accepted input; todo + real file commit
supply the work edge. Every main call still executes the native registry handler.
"""
import hashlib
import json

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_efficiency import _pin
from agent.supervision_tool_attempts import FLAGS, VERSION
from tests.agent.supervision_test_support import rig as rig, accept
from tests.agent.test_supervision_efficiency import native as native, feature_calls, flush, observe_result


def save(native):
    (native.rig.home / "config.yaml").write_text(json.dumps(native.rig.config))


def prepare(native, tmp_path, *, missing=False, link=True):
    path = tmp_path / "source"
    if not missing:
        path.mkdir()
        (path / "evidence.txt").write_text("synthetic evidence\n")
    plan = tmp_path / "plan.md"
    text = f"- Write `{plan}`.\n- Inspect `{path}`."
    rt = accept(native.rig.agent, text)
    from tools.todo_tool import todo_tool
    from tools.file_tools import write_file_tool
    with bind_subagent_parent(native.rig.agent):
        result = json.loads(todo_tool([{"id": "inspect", "content": f"Inspect `{path}`", "status": "in_progress"}],
                                      store=native.rig.agent._todo_store))
        original = result["work_map_sources"]["requirements"][1]
        span = {k: original[k] for k in ("source_message_id", "start", "end")}
        body = {"version": 1, "requirements": [span], "steps": [{"todo_id": "inspect",
                "requirement_indexes": [0], "target_refs": [str(path)]}], "claims": []}
        if link:
            committed = json.loads(write_file_tool(str(plan), "```hermes-work-map-v1\n" + json.dumps(body) + "\n```\n"))
            assert committed.get("verified") is True
    from tools.registry import registry
    assert registry.get_entry("read_file") and registry.get_entry("search_files")
    native.rig.agent.valid_tool_names = {"read_file", "search_files", "write_file"}
    alternative = {"tool": "search_files", "arguments": {
        "path": str(tmp_path if missing else path), "pattern": "*.txt", "target": "files",
        "limit": 10, "offset": 0, "order": "discovery"}, "authorized": True, "privacy_allowed": True}
    policy = {"version": VERSION, "path": str(path), "instructions_sha256": [hashlib.sha256(text.encode()).hexdigest()],
              **dict.fromkeys(FLAGS, False), "alternative": alternative}
    native.rig.config["supervision"]["file_attempts"] = [policy]
    save(native)
    native.owner.routes.clear()  # no fixture diagnostic route can qualify a positive
    native.choice["F11"] = "file-search:" + _pin((str(path), alternative))
    assert not native.owner.attempt_policies
    return path, policy


@pytest.fixture
def dispatch(native, monkeypatch):
    from agent.tool_executor import _dispatch_authorized_once, _ManagedToolResult, _ToolCallRef
    from tools.registry import registry
    # UI/activity/approval machinery only; policy admission, registry, file owner,
    # read metadata, failure classification, settlement and Jev stay real.
    monkeypatch.setattr("agent.tool_executor._pre_tool_block", lambda agent, ref: (None, ref.args))
    monkeypatch.setattr("agent.tool_executor._begin_tool_execution", lambda *args: None)
    monkeypatch.setattr("agent.tool_executor._run_with_activity_heartbeat", lambda agent, name, fn: fn())
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda *args: None)
    calls = []
    def run(name, args, ident, *, settle=True, execute=None):
        state = _ManagedToolResult(None, args, [], False, False)
        def actual(values):
            calls.append((name, values))
            return registry.dispatch(name, values, task_id="native-attempt-fixture")
        with bind_subagent_parent(native.rig.agent):
            result = _dispatch_authorized_once(native.rig.agent, state,
                _ToolCallRef(name, args, "native-attempt-fixture", ident, []), execute=execute or actual,
                scope_block=None, display_index=None, begin_execution=None, authorization_gate=None)
        assert not state.blocked
        if settle:
            observe_result(native, name, args, result, ident, bool(json.loads(result).get("error")))
            flush(native)
        return result
    run.calls = calls
    return run


def repeat(native, dispatch, path, count=4, **arguments):
    hints = []
    for i in range(count):
        raw = dispatch("read_file", {"path": str(path), **arguments}, f"attempt-{i}")
        assert "not a regular file" in raw
        hints.extend(native.runtime.drain_at_safe_point())
        native.runtime.committed_batch([])  # real round boundary, not a renewed wait
    return hints


@pytest.mark.parametrize("loop", [False, True])
def test_ordinary_read_failure_and_recurrence_advisory(native, dispatch, tmp_path, loop):
    path, policy = prepare(native, tmp_path)
    if loop:
        native.choice["F11"] = "none"
    hints = repeat(native, dispatch, path)
    assert len(dispatch.calls) == 4  # required/main work never suppressed
    assert len(hints) == 1 and 'search_files' in hints[0] and '*.txt' in hints[0]
    assert len(feature_calls(native, "F11")) == 1
    assert len(feature_calls(native, "F06")) == int(loop)
    facts = feature_calls(native, "F11")[0]["state"]["facts"]
    assert facts["error"]["category"] == "not_regular"
    assert facts["error"]["mechanism"] == "regular_file_guard"
    assert facts["deterministic_handler_available"] is False
    assert json.loads(facts["routes"][0]["text"]) == policy["alternative"]
    assert len(native.owner.handled_incidents) == 1
    assert not native.owner.attempt_policies and not native.owner.native_attempts
    assert not native.rig.agent._interrupt_requested
    assert any(r.status == "applied" for r in native.runtime.receipts.values())
    # Consuming a recommendation does not execute the route. Normal dispatch does.
    result = json.loads(dispatch("search_files", policy["alternative"]["arguments"], "alternative"))
    assert str(path / "evidence.txt") in result["files"]
    assert native.owner.attempted_routes
    assert not native.runtime.drain_at_safe_point()


@pytest.mark.parametrize("case", [*FLAGS, "page", "absent", "unknown", "missing_flag", "wrong_input", "no_link",
    "permission", "privacy", "missing_permission", "unavailable", "mutating_route", "symlink", "disabled"])
def test_ordinary_unknown_and_exempt_attempts_are_silent(native, dispatch, tmp_path, case):
    path, policy = prepare(native, tmp_path, link=case != "no_link")
    if case in FLAGS:
        policy[case] = True
    elif case == "absent":
        native.rig.config["supervision"].pop("file_attempts")
    elif case == "disabled":
        native.rig.config["supervision"]["enabled"] = False
    elif case == "unknown":
        policy["registered_retry"] = None
    elif case == "missing_flag":
        policy.pop("user_repetition")
    elif case == "wrong_input":
        policy["instructions_sha256"] = ["0" * 64]
    elif case in {"permission", "privacy"}:
        policy["alternative"]["authorized" if case == "permission" else "privacy_allowed"] = False
    elif case == "missing_permission":
        policy["alternative"].pop("authorized")
    elif case == "unavailable":
        native.rig.agent.valid_tool_names.remove("search_files")
    elif case == "mutating_route":
        policy["alternative"]["tool"] = "write_file"
    elif case == "symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other, target_is_directory=True)
    save(native)
    assert not repeat(native, dispatch, path, offset=2 if case == "page" else 1)
    assert len(dispatch.calls) == 4
    assert not feature_calls(native, "F06") and not feature_calls(native, "F11")


def test_real_not_found_handler_and_cache_keep_their_policy(native, dispatch, tmp_path):
    path, _ = prepare(native, tmp_path, missing=True)
    for i in range(4):
        result = dispatch("read_file", {"path": str(path)}, str(i), settle=False)
        receipt = native.owner.native_attempts[str(i)]
        assert receipt.policy["deterministic_handler_available"] is True
        assert receipt.classification == ("not_found", "unicode_recovery_and_similar_files", True)
        assert "File not found" in result
        observe_result(native, "read_file", {"path": str(path)}, result, str(i), True)
    flush(native)
    assert len(dispatch.calls) == 4 and not native.calls


@pytest.mark.parametrize("case", ["copied_json", "terminal_stderr", "changed_result", "changed_arguments", "stale_call"])
def test_only_exact_native_dispatch_receipts_qualify(native, dispatch, tmp_path, case):
    path, _ = prepare(native, tmp_path)
    args = {"path": str(path)}
    raw = dispatch("read_file", args, "original", settle=False)
    if case == "copied_json":
        dispatch("read_file", args, "copy", execute=lambda values: raw)
    elif case == "terminal_stderr":
        observe_result(native, "terminal", args, raw, "original", True)
    else:
        observe_result(native, "read_file", {**args, **({"offset": 2} if case == "changed_arguments" else {})},
            raw + " " if case == "changed_result" else raw, "unknown" if case == "stale_call" else "original", True)
    flush(native)
    assert not native.calls


@pytest.mark.parametrize("case", ["permission", "source", "steering", "stop", "unload", "expiry", "profile"])
def test_live_authority_revocation_cannot_claim_an_effect(native, dispatch, tmp_path, monkeypatch, case):
    path, policy = prepare(native, tmp_path)
    def revoke():
        if case == "permission":
            policy["alternative"]["authorized"] = False
            save(native)
        elif case == "source":
            (path / "new-evidence.txt").write_text("progress")
        elif case == "steering":
            accept(native.rig.agent, "- Do something else.", continuation=True)
        elif case == "stop":
            native.rig.agent._interrupt_requested = True
        elif case == "unload":
            native.bridge.native.unregister()
        elif case == "profile":
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
        else:
            native.runtime.clock = lambda: native.runtime.round_deadline + 1
    native.on_response.append(revoke)
    dispatch("read_file", {"path": str(path)}, "read")
    assert len(dispatch.calls) == 1 and len(feature_calls(native, "F11")) == 1
    assert not native.runtime.drain_at_safe_point()
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())


def test_real_alternative_dispatch_excludes_repeat_recommendation(native, dispatch, tmp_path):
    path, policy = prepare(native, tmp_path)
    native.choice["F11"] = "none"
    dispatch("read_file", {"path": str(path)}, "read")
    dispatch("search_files", policy["alternative"]["arguments"], "search")
    native.runtime.committed_batch([])
    assert not repeat(native, dispatch, path)
    assert len(feature_calls(native, "F11")) == 1 and not feature_calls(native, "F06")


def test_new_directory_evidence_breaks_no_progress(native, dispatch, tmp_path):
    path, _ = prepare(native, tmp_path)
    native.choice["F11"] = "none"
    repeat(native, dispatch, path, count=2)
    (path / "new.txt").write_text("new evidence")
    repeat(native, dispatch, path, count=2)
    assert not feature_calls(native, "F06")


@pytest.mark.parametrize("interleave", ["parameter", "page", "success", "unknown_failure"])
def test_no_recurrence_across_changed_or_unknown_work(native, dispatch, tmp_path, interleave):
    path, _ = prepare(native, tmp_path)
    native.choice["F11"] = "none"
    for i in range(3):
        dispatch("read_file", {"path": str(path)}, f"read-{i}")
        if interleave == "parameter":
            dispatch("read_file", {"path": str(path), "limit": 2}, f"other-{i}")
        elif interleave == "page":
            dispatch("read_file", {"path": str(path), "offset": 2}, f"other-{i}")
        elif interleave == "success":
            dispatch("read_file", {"path": str(path / "evidence.txt")}, f"other-{i}")
        else:
            dispatch("read_file", {"path": str(path / "absent.txt")}, f"other-{i}")
        native.runtime.committed_batch([])
    assert len(dispatch.calls) == 6 and not feature_calls(native, "F06")


def test_required_file_mutation_continues_after_recovery_hint(native, dispatch, tmp_path):
    path, _ = prepare(native, tmp_path)
    dispatch("read_file", {"path": str(path)}, "failure")
    assert native.runtime.drain_at_safe_point()
    target = path / "required-output.txt"
    result = json.loads(dispatch("write_file", {"path": str(target), "content": "required artifact"}, "required"))
    assert result["verified"] is True and target.read_text() == "required artifact"
    assert len(dispatch.calls) == 2 and not native.rig.agent._interrupt_requested


def test_unknown_file_backend_never_inherits_host_authority(native, dispatch, tmp_path, monkeypatch):
    path, _ = prepare(native, tmp_path)
    from tools import file_tools
    original = file_tools._get_file_ops("native-attempt-fixture")
    class UnknownBackend:
        env = None  # legacy host-path helper treats this as local; supervision must not
        def __getattr__(self, name):
            return getattr(original, name)
    backend = UnknownBackend()
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: backend)
    assert not repeat(native, dispatch, path)
    assert len(dispatch.calls) == 4 and not native.calls


def test_no_grant_preserves_native_dispatch_without_effect(native, dispatch, tmp_path):
    path, _ = prepare(native, tmp_path)
    grants = native.rig.config["supervision"]["plugins"]["fixture-supervisor"]["grants"]
    grants.remove("advise")
    save(native)
    assert not repeat(native, dispatch, path)
    assert len(dispatch.calls) == 4
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())
