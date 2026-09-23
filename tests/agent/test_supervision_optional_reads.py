"""Real public optional-read owner -> full Jev registry -> strict HTTP -> effect.

No admit_* call qualifies these reads. All source bytes come from actual reads;
consumer edges enter via authentic CLI input, todo and committed work-map files.
"""
import json
import os

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_optional_reads import VERSION
from tests.agent.supervision_test_support import rig as rig, accept
from tests.agent.test_supervision_efficiency import native as native, feature_calls, flush


def prepare(native, tmp_path, *, policy=True, link=True):
    source = tmp_path / "source.txt"
    source.write_text("Synthetic source.\nMore synthetic detail.\n", encoding="utf-8")
    plan = tmp_path / "plan.md"
    rt = accept(native.rig.agent, f"- Write `{plan}`.\n- Inspect `{source}`.")
    from tools.todo_tool import todo_tool
    from tools.file_tools import write_file_tool
    with bind_subagent_parent(native.rig.agent):
        todo = json.loads(todo_tool([{"id": "inspect", "content": f"Inspect `{source}`", "status": "in_progress"}],
                                   store=native.rig.agent._todo_store))
        original = todo["work_map_sources"]["requirements"][1]
        span = {k: original[k] for k in ("source_message_id", "start", "end")}
        body = {"version": 1, "requirements": [span], "steps": [{"todo_id": "inspect",
            "requirement_indexes": [0], "target_refs": [str(source)]}], "claims": []}
        if link:
            content = "```hermes-work-map-v1\n" + json.dumps(body) + "\n```\n"
            result = json.loads(write_file_tool(str(plan), content))
            assert result.get("verified") is True
    entry = native.rig.config["supervision"]["plugins"]["fixture-supervisor"]
    if policy:
        entry["optional_reads"] = {"version": VERSION, "purpose": "supplemental_context",
                                   "paths": [str(source)], "max_bytes": 4096}
    save_policy(native)
    if link:
        assert len(rt.action_requirements({"path": str(source)})) == 1
    return source, {"version": VERSION, "path": str(source), "objective": "Inspect supplemental source",
                    "acceptance": "Useful optional background", "offset": 1, "limit": 1}


def save_policy(native):
    (native.rig.home / "config.yaml").write_text(json.dumps(native.rig.config))


def read(native, request):
    with bind_subagent_parent(native.rig.agent):
        return native.bridge.native.read_optional_context(request)


def test_public_optional_read_returns_original_bytes_and_skips_second_open(native, tmp_path, monkeypatch):
    path, request = prepare(native, tmp_path)
    opened = []
    real_open = os.open
    def tracked(path_arg, *args, **kwargs):
        if path_arg == str(path):
            opened.append(path_arg)
        return real_open(path_arg, *args, **kwargs)
    monkeypatch.setattr(os, "open", tracked)
    original = read(native, request)
    assert original and original["content"] == "Synthetic source.\n"
    reused = read(native, {**request, "limit": 2})
    assert reused == original  # original identity/window/bytes, not a new result
    assert len(opened) == 1
    assert len(feature_calls(native, "F04")) == 1
    assert any(r.status == "applied" for r in native.runtime.receipts.values())
    assert not native.runtime.drain_at_safe_point()
    assert not native.rig.agent._interrupt_requested


def test_exact_optional_read_replay_has_no_semantic_call(native, tmp_path):
    _, request = prepare(native, tmp_path)
    original = read(native, request)
    assert original and read(native, request) == original
    flush(native)
    assert not feature_calls(native, "F04")


@pytest.mark.parametrize("case", ["no_policy", "no_link", "required", "independent", "no_budget", "unknown_key"])
def test_unknown_or_required_contract_does_not_read(native, tmp_path, monkeypatch, case):
    path, request = prepare(native, tmp_path, policy=case != "no_policy", link=case != "no_link")
    policy = native.rig.config["supervision"]["plugins"]["fixture-supervisor"].get("optional_reads")
    if case in {"required", "independent"}:
        policy["purpose"] = case
    elif case == "no_budget":
        policy.pop("max_bytes")
    elif case == "unknown_key":
        request["optional"] = True  # caller may not elevate authority
    save_policy(native)
    real_open = os.open
    def forbidden(path_arg, *args, **kwargs):
        assert path_arg != str(path)
        return real_open(path_arg, *args, **kwargs)
    monkeypatch.setattr(os, "open", forbidden)
    assert read(native, request) is None
    assert not feature_calls(native, "F04")


@pytest.mark.parametrize("case", ["permission", "source", "unload", "stop", "budget"])
def test_revocation_during_judgment_cannot_apply_or_read(native, tmp_path, case):
    path, request = prepare(native, tmp_path)
    original = read(native, request)
    assert original
    def revoke():
        if case in {"permission", "budget"}:
            policy = native.rig.config["supervision"]["plugins"]["fixture-supervisor"]["optional_reads"]
            if case == "permission":
                policy["paths"] = []
            else:
                policy["max_bytes"] = 1
            save_policy(native)
        elif case == "source":
            path.write_text("Changed source\n")
        elif case == "unload":
            native.bridge.native.unregister()
        else:
            native.rig.agent._interrupt_requested = True
    native.on_response.append(revoke)
    assert read(native, {**request, "limit": 2}) is None
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())


def test_absent_reuse_grant_executes_original_optional_read(native, tmp_path):
    _, request = prepare(native, tmp_path)
    reg = native.bridge.native._registration
    reg.grants = frozenset(g for g in reg.grants if g != "reuse_candidate")
    original = read(native, request)
    second = read(native, {**request, "limit": 2})
    assert original and second and second["source_ref"] != original["source_ref"]
    assert second["content"] == "Synthetic source.\nMore synthetic detail.\n"
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())


@pytest.mark.parametrize("method", ["finish_turn", "revoke", "unregister"])
def test_lifecycle_clears_retained_source_bytes(native, tmp_path, method):
    _, request = prepare(native, tmp_path)
    assert read(native, request)
    owner = native.runtime.optional_reads
    assert owner.receipts
    if method == "unregister":
        native.bridge.native.unregister()
    else:
        getattr(native.runtime, method)()
    assert not owner.receipts
    assert read(native, request) is None


def test_authenticated_steering_invalidates_existing_read_link(native, tmp_path):
    _, request = prepare(native, tmp_path)
    assert read(native, request)
    accept(native.rig.agent, "- Stop consulting the prior optional source.", continuation=True)
    assert read(native, {**request, "limit": 2}) is None
    assert not feature_calls(native, "F04")


def test_symlink_cannot_redirect_exact_path_policy(native, tmp_path):
    path, request = prepare(native, tmp_path)
    other = tmp_path / "other.txt"
    other.write_text("not authorized")
    path.unlink()
    path.symlink_to(other)
    assert read(native, request) is None
    assert not feature_calls(native, "F04")


def test_late_judgment_preserves_baseline_and_cannot_apply(native, tmp_path):
    import time
    _, request = prepare(native, tmp_path)
    original = read(native, request)
    native.on_response.append(lambda: time.sleep(1.1))
    second = read(native, {**request, "limit": 2})
    assert original and second and original["source_ref"] != second["source_ref"]
    assert "More synthetic detail" in second["content"]
    flush(native)
    assert not any(r.status == "applied" for r in native.runtime.receipts.values())


def test_singleton_requirement_is_not_a_main_read_link(native, tmp_path):
    path = tmp_path / "unlinked.txt"
    path.write_text("source")
    native.owner.admit_read_intent(str(path), independent_check=False)
    assert len(native.runtime.requirements) == 1
    assert native.owner.before_read({"path": str(path)}, "unlinked") is None
    assert not native.owner.read_pending
    assert not feature_calls(native, "F04")
