"""Ordinary pre_verify hook + CLI -> native runner -> installed F07 -> original receipt.

Only transport is synthetic; no admit_*, fake VerificationCheck or record_check
qualifies these positives. Inputs and work-map edges enter the real file owner.
"""
import hashlib
import json
import sqlite3

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.verify.native_checks import VERSION, FLAGS
from agent.verification_evidence import verification_status, mark_workspace_edited
from tests.agent.supervision_test_support import rig as rig, accept
from tests.agent.test_supervision_efficiency import native as native, feature_calls, flush
from tests.verify.test_ledger_and_nudge_integration import make_args


def prepare(native, tmp_path, monkeypatch, *, second=True, **flags):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", native.rig.manager)
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text('{}')
    path, plan = root / "input.json", root / "plan.md"
    accept(native.rig.agent, f"- Write `{plan}`.\n- Inspect `{path}`.")
    from tools.todo_tool import todo_tool
    from tools.file_tools import write_file_tool
    with bind_subagent_parent(native.rig.agent):
        todo = json.loads(todo_tool([{"id": "inspect", "content": f"Inspect `{path}`", "status": "in_progress"}],
                                    store=native.rig.agent._todo_store))
        req = todo["work_map_sources"]["requirements"][1]
        span = {k: req[k] for k in ("source_message_id", "start", "end")}
        body = {"version": 1, "requirements": [span], "steps": [{"todo_id": "inspect",
            "requirement_indexes": [0], "target_refs": [str(path)]}], "claims": []}
        assert json.loads(write_file_tool(str(plan), "```hermes-work-map-v1\n" + json.dumps(body) + "\n```"))["verified"]
        content = '{"name":"fixture","value":3}'
        written = write_file_tool(str(path), content)
        assert json.loads(written)["verified"]
        native.rig.agent._record_file_mutation_result(
            "write_file", {"path": str(path), "content": content}, written, False)
    assert str(path) in native.rig.agent._turn_file_mutation_paths
    raw = {"version": VERSION, "kind": "json_object_keys", "path": "input.json",
           "keys": ["name"], "description": "Check metadata name", **dict.fromkeys(FLAGS, False), **flags}
    specs = [raw, {**raw, "description": "Confirm the same metadata name"}] if second else [raw]
    manifest = root / ".hermes/environment.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"version": 1, "recipe": {"name": "native", "nativeChecks": specs}}))
    entry = native.rig.config["supervision"]["plugins"]["fixture-supervisor"]
    entry["optional_verification"] = {"version": VERSION, "purpose": "supplemental_confirmation",
        "root": str(root), "recipe_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    save_policy(native)
    return root, path, specs


def save_policy(native):
    (native.rig.home / "config.yaml").write_text(json.dumps(native.rig.config))


def hook(native):
    from agent.turn_stop_gates import _pre_verify_nudge
    with bind_subagent_parent(native.rig.agent):
        assert _pre_verify_nudge(native.rig.agent, "candidate answer", 0) is None


def track(monkeypatch):
    import agent.verify.native_checks as checks
    import agent.verify.runner as runner
    evaluations, runs = [], []
    evaluate, run = checks._evaluate, runner.run_verify
    def counted(data, keys):
        evaluations.append((data, keys))
        return evaluate(data, keys)
    def recorded(*args, **kwargs):
        result = run(*args, **kwargs)
        runs.append(result)
        return result
    monkeypatch.setattr(checks, "_evaluate", counted)
    monkeypatch.setattr(runner, "run_verify", recorded)
    return evaluations, runs


def test_ordinary_hook_semantic_reuse_returns_original_receipt(native, tmp_path, monkeypatch):
    root, _, _ = prepare(native, tmp_path, monkeypatch)
    evaluations, runs = track(monkeypatch)
    before = verification_status(session_id=native.rig.agent.session_id, cwd=root)
    hook(native)
    assert len(runs) == 1 and runs[0].ok
    original, reused = runs[0].checks
    from agent.verify.native_checks import _dependencies
    assert _dependencies()
    assert native.owner.checks, runs[0].checks
    assert original["reused"] is False and reused["reused"] is True
    assert reused["receipt"] == original["receipt"]
    assert len(evaluations) == 1 and len(feature_calls(native, "F07")) == 1
    assert any(r.status == "applied" for r in native.runtime.receipts.values())
    assert verification_status(session_id=native.rig.agent.session_id, cwd=root) == before
    assert not native.runtime.drain_at_safe_point()  # no forced model turn or chatter


def test_exact_repeat_skips_evaluator_without_semantics(native, tmp_path, monkeypatch):
    prepare(native, tmp_path, monkeypatch, second=False)
    evaluations, runs = track(monkeypatch)
    hook(native)
    hook(native)
    assert len(evaluations) == 1
    assert runs[1].checks[0]["receipt"] == runs[0].checks[0]["receipt"]
    assert runs[1].checks[0]["reused"]
    flush(native)
    assert not feature_calls(native, "F07")


@pytest.mark.parametrize("flag", FLAGS)
def test_protected_checks_execute_even_if_recipe_calls_them_optional(native, tmp_path, monkeypatch, flag):
    prepare(native, tmp_path, monkeypatch, **{flag: True})
    evaluations, runs = track(monkeypatch)
    hook(native)
    assert len(evaluations) == 2 and runs[0].ok
    assert not any(c["reused"] for c in runs[0].checks)
    assert not feature_calls(native, "F07")


def test_normal_cli_consumes_advisory_but_always_executes_main_checks(native, tmp_path, monkeypatch, capsys):
    root, _, _ = prepare(native, tmp_path, monkeypatch, second=False)
    evaluations, runs = track(monkeypatch)
    hook(native)
    from hermes_cli.verify_cmd import run_verify_command
    with bind_subagent_parent(native.rig.agent):
        assert run_verify_command(make_args(root, phase=["test"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(evaluations) == 2
    assert not payload["checks"][0]["reused"]
    assert "Task-bound advisory" in payload["checks"][0]["advisory"]
    assert len(feature_calls(native, "F07")) == 1
    assert payload["checks"][0]["receipt"]["id"] != runs[0].checks[0]["receipt"]["id"]


@pytest.mark.parametrize("case", ["source", "dependency", "environment", "unknown_dependency", "ledger_edit", "ledger_failed"])
def test_changed_or_unknown_state_executes_baseline(native, tmp_path, monkeypatch, case):
    root, path, _ = prepare(native, tmp_path, monkeypatch, second=False)
    evaluations, runs = track(monkeypatch)
    hook(native)
    import agent.verify.native_checks as checks
    if case == "source":
        path.write_text('{"name":"changed"}')
    elif case == "dependency":
        old = checks._dependencies
        monkeypatch.setattr(checks, "_dependencies", lambda: old() + ":changed")
    elif case == "environment":
        old = checks._environment
        monkeypatch.setattr(checks, "_environment", lambda: old() + ":changed")
    elif case == "unknown_dependency":
        def unknown():
            raise OSError("unavailable")
        monkeypatch.setattr(checks, "_dependencies", unknown)
    elif case == "ledger_edit":
        mark_workspace_edited(session_id=native.rig.agent.session_id, cwd=root, paths=[str(path)])
    else:
        with sqlite3.connect(native.rig.home / "verification_evidence.db") as conn:
            conn.execute("UPDATE verification_events SET status='failed'")
    hook(native)
    assert len(evaluations) == 2 and not runs[1].checks[0]["reused"]
    assert not feature_calls(native, "F07")


@pytest.mark.parametrize("case", ["policy", "unload", "stop", "steer", "file", "grant"])
def test_revalidate_at_semantic_consumption(native, tmp_path, monkeypatch, case):
    root, path, _ = prepare(native, tmp_path, monkeypatch)
    evaluations, runs = track(monkeypatch)
    def invalidate():
        if case == "policy":
            native.rig.config["supervision"]["plugins"]["fixture-supervisor"].pop("optional_verification")
            save_policy(native)
        elif case == "unload":
            native.bridge.native.unregister()
        elif case == "stop":
            native.rig.agent._interrupt_requested = True
        elif case == "steer":
            accept(native.rig.agent, "- Stop optional validation.", continuation=True)
        elif case == "file":
            path.write_text('{"name":"new"}')
        else:
            reg = native.bridge.native._registration
            reg.grants = frozenset(g for g in reg.grants if g != "reuse_receipt")
    native.on_response.append(invalidate)
    hook(native)
    assert not any(c.get("reused") for c in runs[0].checks)
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())
    assert len(evaluations) == (2 if case in {"file", "grant"} else 1)


@pytest.mark.parametrize("case", ["unknown_flag", "unknown_key", "failed", "no_grant", "no_policy", "no_link", "bad_version"])
def test_absence_and_errors_never_reuse(native, tmp_path, monkeypatch, case):
    root, path, specs = prepare(native, tmp_path, monkeypatch)
    evaluations, runs = track(monkeypatch)
    entry = native.rig.config["supervision"]["plugins"]["fixture-supervisor"]
    if case in {"unknown_flag", "unknown_key", "bad_version"}:
        for spec in specs:
            if case == "unknown_flag":
                spec.pop("independent_review")
            elif case == "unknown_key":
                spec["complete"] = True
            else:
                spec["version"] = "unsupported"
        manifest = root / ".hermes/environment.json"
        manifest.write_text(json.dumps({"recipe": {"name": "native", "nativeChecks": specs}}))
        entry["optional_verification"]["recipe_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    elif case == "failed":
        path.write_text('[]')
    elif case == "no_grant":
        reg = native.bridge.native._registration
        reg.grants = frozenset(g for g in reg.grants if g != "reuse_receipt")
    elif case == "no_policy":
        entry.pop("optional_verification")
    elif case == "no_link":
        accept(native.rig.agent, "- Inspect unrelated scope.", continuation=True)
    save_policy(native)
    hook(native)
    assert not feature_calls(native, "F07")
    assert all(not c.get("reused") for r in runs for c in r.checks)
    assert len(evaluations) == (2 if case in {"unknown_flag", "failed", "no_grant"} else 0)

@pytest.mark.parametrize("case", ["expired", "error", "insufficient", "new_scope"])
def test_unusable_semantic_result_executes_original(native, tmp_path, monkeypatch, case):
    prepare(native, tmp_path, monkeypatch)
    evaluations, runs = track(monkeypatch)
    if case == "expired":
        native.runtime.round_deadline = native.runtime.clock() - 1
    elif case == "error":
        def fail():
            raise RuntimeError("synthetic transport failure")
        native.on_response.append(fail)
    else:
        native.choice["F07"] = case
    hook(native)
    assert len(evaluations) == 2 and runs[0].ok
    assert not any(c["reused"] for c in runs[0].checks)
    flush(native)
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())


def test_cli_retains_every_shell_command_and_native_check(native, tmp_path, monkeypatch, capsys):
    root, _, _ = prepare(native, tmp_path, monkeypatch)
    manifest = root / ".hermes/environment.json"
    doc = json.loads(manifest.read_text())
    doc["recipe"]["test"] = ["printf x >> mandatory.log", "printf x >> mandatory.log"]
    manifest.write_text(json.dumps(doc))
    from hermes_cli.verify_cmd import run_verify_command
    evaluations, _ = track(monkeypatch)
    # The actual CLI also works without any active in-process supervision binding.
    assert run_verify_command(make_args(root, phase=["test"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(evaluations) == 2 and (root / "mandatory.log").read_text() == "xx"
    assert len(payload["phases"]) == 2
    assert not any(c["reused"] for c in payload["checks"])
    assert not feature_calls(native, "F07")


@pytest.mark.parametrize("bad", [None, {}, "unknown", [None]])
def test_malformed_native_checks_fail_cli_not_silent_green(tmp_path, capsys, bad):
    root = tmp_path / "project"
    (root / ".hermes").mkdir(parents=True)
    (root / ".hermes/environment.json").write_text(json.dumps({"name": "bad", "nativeChecks": bad}))
    from hermes_cli.verify_cmd import run_verify_command
    from agent.verify.recipes import Recipe
    assert Recipe.from_dict({"name": "bad", "nativeChecks": bad}).to_dict()["nativeChecks"] == bad
    assert run_verify_command(make_args(root, phase=["test"])) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_supplemental_retention_cannot_erase_required_workspace_receipt(native, tmp_path, monkeypatch):
    root, _, _ = prepare(native, tmp_path, monkeypatch, second=False)
    from agent import verification_evidence as ledger
    original = ledger.record_verify_run(root=root, session_id=native.rig.agent.session_id, ok=False)
    before = verification_status(session_id=native.rig.agent.session_id, cwd=root)
    monkeypatch.setattr(ledger, "_MAX_EVENTS_PER_SESSION_ROOT", 2)
    for _ in range(4):
        ledger.record_verify_run(root=root, session_id=native.rig.agent.session_id, ok=True, supplemental=True)
    assert verification_status(session_id=native.rig.agent.session_id, cwd=root) == before
    assert before["evidence"] == original and before["status"] == "failed"


def test_stale_main_advisory_cannot_suppress_native_execution(native, tmp_path, monkeypatch, capsys):
    root, _, _ = prepare(native, tmp_path, monkeypatch, second=False)
    evaluations, _ = track(monkeypatch)
    hook(native)
    native.on_response.append(lambda: accept(native.rig.agent, "- New scope.", continuation=True))
    from hermes_cli.verify_cmd import run_verify_command
    with bind_subagent_parent(native.rig.agent):
        assert run_verify_command(make_args(root, phase=["test"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(evaluations) == 2 and not payload["checks"][0]["reused"]
    assert "advisory" not in payload["checks"][0]


def test_source_identity_excludes_access_time_but_includes_content_metadata():
    from types import SimpleNamespace
    from agent.verify.native_checks import _file_state
    original = SimpleNamespace(st_dev=1, st_ino=2, st_mode=3, st_size=4,
                               st_mtime_ns=5, st_ctime_ns=6, st_atime_ns=7)
    accessed = SimpleNamespace(**{**vars(original), "st_atime_ns": 8})
    changed = SimpleNamespace(**{**vars(original), "st_mtime_ns": 9})
    assert _file_state(original) == _file_state(accessed)
    assert _file_state(original) != _file_state(changed)


def test_input_changed_during_native_evaluation_is_not_recorded_as_success(native, tmp_path, monkeypatch):
    _, path, _ = prepare(native, tmp_path, monkeypatch, second=False)
    import agent.verify.native_checks as checks
    evaluate = checks._evaluate
    def mutate(data, keys):
        ok = evaluate(data, keys)
        path.write_text('{}')
        return ok
    monkeypatch.setattr(checks, "_evaluate", mutate)
    _, runs = track(monkeypatch)
    hook(native)
    assert not runs[0].ok
    assert runs[0].checks[0]["receipt"]["status"] == "failed", runs[0].checks
    assert not native.owner.checks


def test_profile_switch_does_not_borrow_optional_authority(native, tmp_path, monkeypatch):
    prepare(native, tmp_path, monkeypatch, second=False)
    evaluations, runs = track(monkeypatch)
    hook(native)
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    (home_b / "config.yaml").write_text('{}')
    with monkeypatch.context() as other:
        other.setenv("HERMES_HOME", str(home_b))
        hook(native)
        assert len(evaluations) == 1
    hook(native)
    assert len(evaluations) == 1 and runs[-1].checks[0]["reused"]

