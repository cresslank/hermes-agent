"""Native planning consumption regressions: real ingress, file tools and plugin transport."""
import json
import threading
from contextvars import copy_context

import pytest

from tests.agent.test_owned_delegation_planning import (
    factory as factory, native as native, rig as rig, base_rig as base_rig,
)
from tests.agent.test_supervision_views import assemble
from agent.supervision_planning_records import load


def test_withdrawn_expansion_actual_dispatch_retains_history(factory):
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "withdrawing this optional expansion" in str(assemble(p.a, p.history).api_messages)
    node = p.inventory()[-1]
    p.commit([dict(type="withdraw_expansion", expansion_id=node["id"])])
    withdrawn = next(n for n in load(p.rt) if n["id"] == node["id"])
    assert withdrawn["status"] == "withdrawn_by_parent"
    call_id, result = p.call("read_file", p.expansion["operation"]["arguments"])
    assert "Synthetic source:" in result
    issued = next(n for n in load(p.rt) if n["id"] == node["id"])
    assert issued["status"] == "issued"
    assert issued["withdrawal"] == withdrawn["withdrawal"]
    assert issued["call_id"] == call_id
    graph = p.rt.dependencies.planning
    graph.clear()
    assert p.inventory()[-1]["status"] == "issued"
    graph.dispatch("read_file", p.expansion["operation"]["arguments"], call_id)
    assert next(n for n in load(p.rt) if n["id"] == node["id"]) == issued


@pytest.mark.parametrize("feature", ["F05", "F08"])
def test_planning_never_refreshes_config_under_instruction_fence(factory, monkeypatch, feature):
    from hermes_cli import config
    p = factory()
    if feature == "F08":
        p.pass_commit(1); p.pass_commit(2)
    calls = []
    original = config.load_config_readonly
    def observe():
        calls.append(p.rt.lock._is_owned())
        return original()
    monkeypatch.setattr(config, "load_config_readonly", observe)
    p.commit([p.expansion if feature == "F08" else p.delegation])
    assert "Task-bound planning advisory" in str(assemble(p.a, p.history).api_messages)
    assert not any(calls), calls


def test_planning_config_contention_abstains_without_lock_order_wait(factory, monkeypatch):
    from hermes_cli import config
    p = factory()
    native_lock = config._CONFIG_LOCK
    held, attempted = threading.Event(), threading.Event()
    observations, errors = [], []
    class Lock:
        def acquire(self, *args, **kwargs):
            result = native_lock.acquire(*args, **kwargs)
            observations.append((kwargs.get("blocking", True), p.rt.lock._is_owned()))
            attempted.set()
            return result
        def release(self):
            native_lock.release()
        def __enter__(self):
            observations.append((True, p.rt.lock._is_owned()))
            attempted.set()
            native_lock.acquire()
            return self
        def __exit__(self, *args):
            native_lock.release()
    def competitor():
        try:
            with native_lock:
                held.set()
                assert attempted.wait(3)
        except BaseException as exc:
            errors.append(repr(exc))
    monkeypatch.setattr(config, "_CONFIG_LOCK", Lock())
    thread = threading.Thread(target=competitor)
    thread.start()
    try:
        assert held.wait(3)
        with p.rt.lock:
            view = p.rt.dependencies.planning._preflight(p.expansion, optional=True)
        assert view is None
        assert observations == [(False, True)]
    finally:
        attempted.set(); thread.join(3)
        assert not thread.is_alive() and not errors, errors


@pytest.mark.parametrize("route", ["sync", "async", "batch", "owned", "lifecycle"])
def test_pending_native_constructor_vetoes_complete_inventory(factory, monkeypatch, route):
    from tools import delegate_tool as dt
    from tools.delegate_tool_child_run import _attach_child, _detach_child
    from tests.agent.test_owned_delegation_bridge import Child
    from agent import subagent_lifecycle as lifecycle
    from tools import async_delegation
    p = factory()
    background = route == "async"
    # Isolate process-local native publication owners, not planning authority.
    monkeypatch.setattr(async_delegation, "_records", {})
    monkeypatch.setattr(lifecycle._REGISTRY, "records", {})
    monkeypatch.setattr(lifecycle._REGISTRY, "correlations", {})
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    constructing, allow_attach, release = [threading.Event() for _ in range(3)]
    replies, errors, futures = [], [], []
    get_executor = async_delegation._get_executor
    class CaptureExecutor:
        def __init__(self, executor):
            self.executor = executor
        def submit(self, *args, **kwargs):
            future = self.executor.submit(*args, **kwargs)
            futures.append(future)
            return future
    monkeypatch.setattr(async_delegation, "_get_executor", lambda *a: CaptureExecutor(get_executor(*a)))
    child = Child("pending-native-worker")
    def build(**kwargs):
        constructing.set()
        assert allow_attach.wait(3)
        _attach_child(p.a, child)
        return child
    def run(*, child, task_index, **kwargs):
        try:
            assert release.wait(3)
            return dict(task_index=task_index, status="completed", result="legacy result", duration_seconds=0)
        finally:
            _detach_child(p.a, child)
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: dict(
        model="synthetic", provider="synthetic", base_url=None, api_key=None, api_mode=None))
    monkeypatch.setattr(dt, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: ("", "", None, None, False))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: (None, [], []))
    def launch():
        try:
            if route == "lifecycle":
                service = lifecycle.SubagentLifecycleService(lambda: p.a)
                replies.append(service.launch(lifecycle.SubagentLaunchRequest(goal="Unaccounted legacy use")))
            else:
                tasks = None
                if route == "batch":
                    tasks = [dict(goal="First unaccounted use"), dict(goal="Second unaccounted use")]
                elif route == "owned":
                    tasks = p.delegation["operation"]["arguments"]["tasks"]
                replies.append(json.loads(dt.delegate_task(parent_agent=p.a,
                    goal=None if tasks else "Unaccounted legacy use", tasks=tasks, background=background)))
        except BaseException as exc:
            errors.append(repr(exc))
    ctx = copy_context()
    thread = threading.Thread(target=lambda: ctx.run(launch))
    thread.start()
    try:
        assert constructing.wait(3), errors
        assert not p.a._active_children and not p.owner.list_owned()
        result = assemble(p.a, p.history)
        assert "withdrawing this optional expansion" not in str(result.api_messages)
        assert p.inventory()[-1]["status"] == "proposed"
    finally:
        allow_attach.set(); release.set(); thread.join(3)
        assert not thread.is_alive() and not errors, errors
        for future in futures:
            future.result(timeout=3)
        if route == "lifecycle" and replies:
            lifecycle._REGISTRY.records[replies[0].subagent_id].future.result(timeout=3)


def test_native_failed_admission_aba_after_semantic_observation_abstains(factory, monkeypatch):
    from tools import delegate_tool as dt
    from agent.owned_delegation_planning import _no_native_workers
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    def fail(**kwargs):
        raise ValueError("synthetic constructor rejection")
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", fail)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: dict(
        model="synthetic", provider="synthetic", base_url=None, api_key=None, api_mode=None))
    monkeypatch.setattr(dt, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: ("", "", None, None, False))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: (None, [], []))
    original = p.rt._wait_for
    calls = []
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(json.loads(dt.delegate_task(parent_agent=p.a, goal="Rejected native admission")))
        assert "synthetic constructor rejection" in str(calls[-1])
        assert _no_native_workers(p.owner, p.a)  # failed admission left no phantom worker
        return result
    monkeypatch.setattr(p.rt, "_wait_for", observed)
    assert "withdrawing this optional expansion" not in str(assemble(p.a, p.history).api_messages)
    assert calls and p.inventory()[-1]["status"] == "proposed"


def test_final_receipt_holds_native_inventory_fences(factory, monkeypatch):
    from tools import delegate_tool_registry as registry, async_delegation
    from agent.subagent_lifecycle import _REGISTRY
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    graph = p.rt.dependencies.planning
    original = graph._transition
    checked = []
    def transition(node, status, **updates):
        if status == "advised":
            def contender():
                for lock in (registry._active_subagents_lock, p.a._active_children_lock,
                             async_delegation._records_lock, _REGISTRY.lock):
                    acquired = lock.acquire(blocking=False)
                    checked.append(acquired)
                    if acquired:
                        lock.release()
            t = threading.Thread(target=contender)
            t.start(); t.join(3)
            assert not t.is_alive()
        return original(node, status, **updates)
    monkeypatch.setattr(graph, "_transition", transition)
    assert "withdrawing this optional expansion" in str(assemble(p.a, p.history).api_messages)
    assert checked == [False] * 4


@pytest.mark.parametrize("fault", ["changed_operation", "changed_work", "steering", "reset", "malformed_id", "busy"])
def test_withdrawal_does_not_rebind_unrelated_dispatch(factory, monkeypatch, fault):
    import sqlite3
    from tests.agent.supervision_test_support import accept
    p = factory()
    p.commit([p.expansion])
    node = p.inventory()[-1]
    p.commit([dict(type="withdraw_expansion", expansion_id=node["id"])])
    before = next(n for n in load(p.rt) if n["id"] == node["id"])
    args = dict(p.expansion["operation"]["arguments"])
    if fault == "changed_operation":
        args["limit"] = 1
    elif fault in {"changed_work", "steering"}:
        accept(p.a, "- A different current request.", continuation=fault == "steering")
    elif fault == "reset":
        with sqlite3.connect(p.a._session_db.db_path) as reset_conn:
            reset_conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id=?", (p.a.session_id,))
    conn = sqlite3.connect(p.a._session_db.db_path, timeout=0)
    try:
        if fault == "busy":
            conn.execute("BEGIN IMMEDIATE")
        if fault == "malformed_id":
            p.rt.dependencies.planning.dispatch("read_file", args, None)
        else:
            _, result = p.call("read_file", args)
            assert "Synthetic source:" in result
    finally:
        conn.rollback(); conn.close()
    rows = load(p.rt)
    current = next((n for n in rows if n["id"] == node["id"]), None)
    assert current is None or current["status"] != "issued"
    if fault in {"changed_operation", "malformed_id", "busy"}:
        assert current == before


def test_deferred_native_cleanup_remains_in_census(factory):
    from concurrent.futures import Future
    import weakref
    from tools.delegate_tool_child_run import _defer_close_after_timeout
    from tests.agent.test_owned_delegation_bridge import Child
    from agent.owned_delegation_planning import _no_native_workers
    p = factory()
    child = Child("deferred-native-cleanup")
    child._delegate_parent_ref = weakref.ref(p.a)
    future = Future()
    assert _no_native_workers(p.owner, p.a)
    _defer_close_after_timeout(child, future)
    try:
        assert not child.closed
        assert not _no_native_workers(p.owner, p.a)
    finally:
        future.set_result(None)
    assert child.closed and _no_native_workers(p.owner, p.a)


@pytest.mark.parametrize("fault", ["cold", "changed", "missing", "malformed", "foreign"])
def test_planning_cache_only_preflight_never_refreshes(factory, monkeypatch, tmp_path, fault):
    from hermes_cli import config
    p = factory()
    path = p.native.rig.home / "config.yaml"
    if fault == "cold":
        monkeypatch.delitem(config._LOAD_CONFIG_CACHE, str(path))
    elif fault == "changed":
        path.write_text(path.read_text() + "\n# changed signature\n")
    elif fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_text("supervision: [broken")
        config.load_config_readonly()  # outside the fence; last-known-good is not a grant
    else:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "foreign-home"))
    def forbidden():
        raise AssertionError("optional planning must not refresh configuration")
    monkeypatch.setattr(config, "load_config_readonly", forbidden)
    with p.rt.lock:
        assert p.rt.dependencies.planning._preflight(p.expansion, optional=True) is None
