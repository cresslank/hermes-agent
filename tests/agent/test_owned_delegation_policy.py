"""Offline public launches: configuration + authenticated input, never install_owner.

Full standalone registration/registry/adapter and native scheduler/SQLite are real.
Only model execution, credential routing and HTTP transport are synthetic.
"""
from contextvars import copy_context
import json
import os
import queue
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest

source = os.environ.get("JEV_SUPERVISOR_SOURCE")
pointer = Path.home() / ".hermes" / "jev-supervisor-test-source"
if not source and pointer.is_file():
    source = pointer.read_text().strip()
if not source:
    pytest.skip("standalone source required", allow_module_level=True)
sys.path.insert(0, str(Path(source) / "src"))

from agent.owned_delegation import ControlDenied, binding_of, finish_child
from agent.owned_delegation_policy import CONSUMERS, VERSION
from agent.supervision_context import accept_pending_input
from agent.subagent_lifecycle import bind_subagent_parent
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_loader import _plugin_home_scope
from hermes_cli.plugins_manifest import PluginManifest
from hermes_state import SessionDB
from tests.agent.supervision_test_support import Agent, accept
from tests.agent.test_owned_delegation_bridge import Child
from tools.todo_tool import TODO_SCHEMA, TodoStore
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext

GOAL = "Inspect supplemental fixture appendix"
CLAUSE = "- Compare the public methods; an appendix is discretionary."


def instruction(**changes):
    row = dict(ref="job:appendix", goal=GOAL, requirements=[dict(start=0, end=len(CLAUSE))],
        consumer_scope="parent_only", obligation="optional", requires_result=False,
        requires_effects=False, requires_cleanup=False, requires_handoff=False)
    row.update(changes)
    return CLAUSE + "\n```hermes-owned-delegation-v1\n" + json.dumps(dict(version=1, jobs=[row])) + "\n```\n"


@pytest.fixture
def factory(tmp_path, monkeypatch):
    from tools import delegate_tool as dt
    import jev_supervisor
    import jev_supervisor.runtime as plugin_runtime
    from jev_supervisor.transport import Transport

    instances, children, threads = [], [], []
    mode = {"relation": "no_remaining_consumer", "value": .01}
    calls = []

    class Credential:
        def __init__(self, profile):
            self.profile = profile

        def bearer(self, profile, endpoint):
            assert profile == self.profile and endpoint == "https://api.typesafe.ai/v1/systemone"
            return "offline-synthetic-not-a-credential"

    def transport(config, credentials, **kwargs):
        async def handle(request):
            assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
            body = json.loads(request.content)
            assert set(body) == {"model", "state", "questions"} and body["model"] == "jev-1.13.0"
            assert body["questions"] and all(k.startswith("F01/") for k in body["questions"])
            calls.append((config.profile, body))
            answers = {}
            for key, question in body["questions"].items():
                if question["type"] == "noul":
                    answers[key] = dict(type="noul", noul=mode["value"])
                else:
                    choice = mode["relation"]
                    answers[key] = dict(type="choice", choice=choice, confidence=.99,
                        probabilities={k: .99 if k == choice else .01 / (len(question["criteria"]) - 1)
                                       for k in question["criteria"]})
            return httpx.Response(200, json=dict(model=body["model"], usage={}, answers=answers))
        return Transport(config, Credential(config.profile), http_transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(plugin_runtime, "Transport", transport)

    def build(**kwargs):
        child = Child("child-" + uuid.uuid4().hex)
        child._todo_store = TodoStore()
        child.tools.append({"type": "function", "function": TODO_SCHEMA})
        child.valid_tool_names.add("todo_list")
        child.commands = queue.Queue()
        child.release, child.started = threading.Event(), threading.Event()
        children.append(child)
        return child

    def run_child(*, task_index, child, **kwargs):
        child.started.set()
        while not child.release.is_set():
            try:
                args, errors, done = child.commands.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                # Synthetic model output enters the real inline todo_list executor
                # on the running child, behind its actual dispatch fence.
                with bind_subagent_parent(child):
                    binding = binding_of(child)
                    with binding[0].dispatch(binding[1], "todo_list", args):
                        value = INLINE_TOOL_EXECUTORS["todo_list"](child, args, InlineToolContext(child.session_id))
                        assert json.loads(value)["todos"]
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        finish_child(child)
        return dict(task_index=task_index, status="interrupted" if child.stopped.is_set() else "completed",
                    result="Synthetic result preserved", duration_seconds=0)

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", run_child)
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a: dict(model="synthetic", provider="synthetic", base_url=None, api_key=None, api_mode=None))
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 8)
    monkeypatch.setattr(dt, "_get_max_concurrent_children", lambda: 3)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(dt, "_oneshot_spawn_budget", lambda *a: None)
    monkeypatch.setattr(dt, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: ("", "", None, None, False))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **kw: (None, [], []))

    def make(name="A", *, text=None, policy_change=None, db_kind="canonical"):
        home = tmp_path / name
        home.mkdir()
        policy = dict(version=VERSION, allow_optional_readonly=True, consumer_contract=CONSUMERS, read_roots=[str(home)])
        if policy_change:
            policy_change(policy)
        fields = "target_id changed meaningful_change evidence_refs changed_requirement_ids explicit_owner_stop explicit_supersession current_instructions child_relevance_contract native_observation requirements job".split()
        config = {"supervision": {"enabled": True, "plugins": {"fixture-owned": {
            "grants": ["observe", "reprioritize_child", "cancel_child"], "data_policy": ["task_text"],
            "owned_delegation": policy,
            "egress_policy": dict(id="offline-fixture", profile=str(home), fields={k: "synthetic" for k in fields}, sources={}, fixture=True)}}},
            "plugins": {"entries": {"fixture-owned": {"settings": {
                "policy": dict(profile=str(home), enabled=True, policy_id="offline-fixture", allowed_classes=["synthetic"], fixture_policy=True),
                "credential_environment": "UNUSED_OFFLINE_SYNTHETIC"}}}}}
        (home / "config.yaml").write_text(json.dumps(config))
        with _plugin_home_scope(home):
            ctx = PluginContext(PluginManifest(name="fixture-owned"), PluginManager(scope_key=str(home)))
            bridge = jev_supervisor.register(ctx)
            assert bridge and len(bridge.supervisor.registry) == 22
            agent = Agent()
            agent._delegate_depth = 0
            db = None if db_kind == "missing" else SessionDB(home / ("state.db" if db_kind in {"canonical", "readonly"} else "foreign.db"))
            if db:
                db.create_session(agent.session_id, "cli")
                if db_kind == "readonly":
                    db.close()
                    db = SessionDB(home / "state.db", read_only=True)
            agent._session_db = db
            runtime = accept(agent, instruction() if text is None else text)
            owner = getattr(agent, "_owned_delegation_owner", None)
        n = SimpleNamespace(home=home, config=config, ctx=ctx, bridge=bridge, agent=agent,
                            db=db, runtime=runtime, owner=owner, calls=calls, mode=mode)
        instances.append(n)
        return n

    def launch(n, *, legacy=False, refs=None, goal=GOAL, count=1):
        replies = []
        task = dict(goal=goal)
        if not legacy:
            task["supervision"] = dict(obligation="optional", consumer_refs=refs or ["job:appendix"],
                consumer_set_closed=True, effect_policy_id="host.owned-parent-read.v1")
        index = len(children)
        with _plugin_home_scope(n.home):
            context = copy_context()
            t = threading.Thread(target=lambda: context.run(lambda: replies.append(json.loads(dt.delegate_task(tasks=[dict(task) for _ in range(count)], parent_agent=n.agent)))))
            threads.append(t)
            t.start()
        # A deterministic constructor notification would also work; completion is
        # awaited through the existing scheduler's worker-start event below.
        import time
        deadline = time.monotonic() + 3
        while len(children) == index and t.is_alive() and time.monotonic() < deadline:
            t.join(.01)
        if len(children) == index:
            t.join(3)
            return SimpleNamespace(reply=replies, child=None, thread=t)
        child = children[index]
        assert child.started.wait(3), replies
        siblings = children[index + 1:index + count]
        assert all(c.started.wait(3) for c in siblings)
        binding = binding_of(child)
        return SimpleNamespace(reply=replies, child=child, siblings=siblings, thread=t, handle=binding[1] if binding else None)

    def flush(n):
        with n.bridge._lock:
            pending = tuple(n.bridge._pending)
        for future in pending:
            future.result(timeout=3)

    def progress(n, job, text, *, row_id="remaining"):
        assert job.child.started.is_set()
        args = {"todos": [dict(id=row_id, content=text, status="in_progress")]}
        errors, done = [], threading.Event()
        job.child.commands.put((args, errors, done))
        assert done.wait(3)
        assert not errors, errors
        flush(n)
        # Observe completion of the native synchronous join's safe point; never
        # invoke a parent drain or manufacture progress before scheduling.
        with n.runtime.ready:
            assert n.runtime.ready.wait_for(lambda: not any(
                p.target_id == job.handle.child_id for p, _ in n.runtime.pending), timeout=3)

    yield SimpleNamespace(make=make, launch=launch, progress=progress, flush=flush)
    for child in children:
        child.release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    for n in instances:
        with _plugin_home_scope(n.home):
            n.runtime.revoke()
            n.bridge.close()
            assert not n.bridge._thread.is_alive()
            if n.db:
                n.db.close()


@pytest.mark.parametrize("count", [1, 2])
def test_ordinary_registration_installation_and_native_first_cancel(factory, count, capsys):
    n = factory.make()
    assert n.owner is not None  # installed by accepted normal ingress, no test setter
    job = factory.launch(n, count=count)
    first_launch = n.owner.status(job.handle)
    assert first_launch["consumer_set_closed"] and first_launch["obligation"] == "optional"
    assert {c["ref"] for c in first_launch["consumers"]} == {"job:appendix", "parent:" + n.agent.session_id}
    assert all(c["requirement_ids"] == [n.runtime.requirements[0].id] for c in first_launch["consumers"])
    assert not n.calls
    factory.progress(n, job, "Optional appendix section one")
    first = n.owner.status(job.handle)
    assert first["semantic_observation"] and first["candidate"] and not first["cancel_requested"]
    assert first["priority"] == 0 and first == n.owner._store.read(job.handle.child_id)
    assert list(n.runtime.receipts.values())[-1].status == "no_op"  # no applied priority fiction
    evidence = n.runtime.revision.evidence
    call_count = len(n.calls)
    factory.progress(n, job, "Optional appendix section one", row_id="new-bookkeeping-id")
    renamed = n.owner.status(job.handle)
    assert {k: v for k, v in renamed.items() if k != "control_revision"} == {
        k: v for k, v in first.items() if k != "control_revision"}
    assert renamed["control_revision"] > first["control_revision"]  # dispatch bookkeeping only
    assert n.runtime.revision.evidence == evidence
    assert len(n.calls) == call_count
    factory.progress(n, job, "Optional appendix section two")
    state = n.owner.status(job.handle)
    assert state["cancel_requested"] and job.child.stopped.is_set() and not state["settled"]
    assert not state["processes_stopped"] and not state["effects_reconciled"]
    with pytest.raises(ControlDenied), n.owner.dispatch(job.handle, "terminal", {"command": "true"}):
        pass
    job.child.release.set()
    for sibling in job.siblings:
        assert not sibling.stopped.is_set()
        sibling.release.set()
    job.thread.join(3)
    assert not job.thread.is_alive() and not n.runtime.child_control_waiters
    final = n.owner._store.read(job.handle.child_id)
    assert final["settled"] and final["processes_stopped"] and final["effects_reconciled"]
    assert job.reply[0]["results"][0]["result"] == "Synthetic result preserved"
    if count == 1:
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("changes", [{"obligation": "required"}, {"obligation": "unknown"},
    {"requires_result": True}, {"requires_effects": True}, {"requires_cleanup": True}, {"requires_handoff": True},
    {"requirements": [{"start": 1, "end": len(CLAUSE)}]}, {"consumer_scope": "foreign"},
    {"requires_result": "false"}, {"unrecognized": True}, {"ref": "parent:session-A"},
    {"requirements": [{"start": 0, "end": len(CLAUSE)}] * 2}])
def test_required_unknown_and_unvalidated_records_cannot_be_optional(factory, changes):
    n = factory.make(text=instruction(**changes))
    job = factory.launch(n)
    factory.progress(n, job, "Optional label cannot erase obligations")
    factory.progress(n, job, "Still labelled optional by child")
    state = n.owner.status(job.handle)
    assert state["obligation"] != "optional" and not state["cancel_requested"] and state["priority"] == 0


@pytest.mark.parametrize("kind", ["missing", "foreign", "readonly", "malformed", "disabled", "absent"])
def test_unavailable_owner_preserves_legacy_and_refuses_control(factory, kind):
    mutate = {"malformed": lambda p: p.update(unrecognized=True),
              "disabled": lambda p: p.update(allow_optional_readonly=False), "absent": lambda p: p.clear()}.get(kind)
    n = factory.make(policy_change=mutate, db_kind=kind if kind in {"missing", "foreign", "readonly"} else "canonical")
    assert n.owner is None
    denied = factory.launch(n)
    assert denied.child is None and denied.reply[0].get("error")
    legacy = factory.launch(n, legacy=True)
    assert legacy.handle is None and not n.calls


@pytest.mark.parametrize("kind", ["legacy", "foreign_ref", "wrong_goal", "untrusted", "unlinked"])
def test_model_requests_cannot_create_consumer_authority(factory, kind):
    n = factory.make(text=CLAUSE if kind in {"untrusted", "unlinked"} else None)
    if kind == "untrusted":
        with _plugin_home_scope(n.home):
            assert accept_pending_input(n.agent, {"text": instruction(), "kind": "cli"}) is None
    job = factory.launch(n, legacy=kind == "legacy", refs=["job:foreign"] if kind == "foreign_ref" else None,
                         goal="Unrequested alternate job" if kind == "wrong_goal" else GOAL)
    if kind == "legacy":
        assert job.handle is None
    else:
        assert n.owner.status(job.handle)["obligation"] == "unknown"
        factory.progress(n, job, "Optional milestone cannot supply missing authority")
    assert not n.calls


def test_launch_installs_when_canonical_db_arrives_after_ingress(factory):
    n = factory.make(db_kind="missing")
    assert n.owner is None
    with _plugin_home_scope(n.home):
        n.db = SessionDB(n.home / "state.db")
        n.db.create_session(n.agent.session_id, "cli")
        n.agent._session_db = n.db
    job = factory.launch(n)
    n.owner = n.agent._owned_delegation_owner
    assert job.handle and n.owner.status(job.handle)["obligation"] == "optional"
    factory.progress(n, job, "First lazy-installed milestone")
    factory.progress(n, job, "Second lazy-installed milestone")
    assert n.owner.status(job.handle)["cancel_requested"]


def test_no_registered_supervisor_has_no_installation(factory):
    n = factory.make()
    n.bridge.close()
    with _plugin_home_scope(n.home):
        from agent.owned_delegation import owner_of
        fresh = Agent()
        fresh._session_db = n.db
        assert accept(fresh, instruction()) is None
        assert owner_of(fresh) is None


def test_wait_thread_authority_never_authorizes_observer_callbacks(factory):
    from agent.supervision_policy import _observer_callback
    n = factory.make()
    job = factory.launch(n)
    # Negative authority probe: even a registered wait thread must preserve the
    # existing observer-callback seal, before it can take a queued control.
    tid = threading.get_ident()
    with n.runtime.lock:
        n.runtime.child_control_waiters[tid] = frozenset({job.handle.child_id})
    token = _observer_callback.set(True)
    try:
        with pytest.raises(RuntimeError, match="not_execution_owner"):
            n.runtime.consume_owner_action(job.handle.child_id, "cancel_child", lambda p: "applied")
    finally:
        _observer_callback.reset(token)
        with n.runtime.lock:
            n.runtime.child_control_waiters.pop(tid)
    assert not n.owner.status(job.handle)["cancel_requested"] and not n.calls


def test_noop_and_profile_a_b_a(factory):
    a, b = factory.make("A"), factory.make("B")
    for n in (a, b, a):
        job = factory.launch(n)
        n.mode.update(relation="current", value=.99)
        factory.progress(n, job, "Useful appendix " + job.child.session_id)
        state = n.owner.status(job.handle)
        assert not state["cancel_requested"] and state["priority"] == 0
        assert n.calls[-1][0] == str(n.home)
        other = b if n is a else a
        with pytest.raises(ControlDenied):
            other.owner.status(job.handle)
        with _plugin_home_scope(other.home):
            assert not n.owner.semantic_authorized(job.handle)
        with _plugin_home_scope(n.home):
            assert n.owner.semantic_authorized(job.handle)
        n.mode.update(relation="no_remaining_consumer", value=.01)
        factory.progress(n, job, "Optional appendix revised " + job.child.session_id)
        assert n.owner.status(job.handle)["semantic_observation"]
        factory.progress(n, job, "Optional appendix revised again " + job.child.session_id)
        assert n.owner.status(job.handle)["cancel_requested"] and job.child.stopped.is_set()
        job.child.release.set()
        job.thread.join(3)


@pytest.mark.parametrize("kind", ["disabled", "unload", "steer", "reset", "new_work", "handoff", "enroll"])
def test_revocation_does_not_drop_lifetime_or_reuse_confirmation(factory, kind):
    n = factory.make()
    job = factory.launch(n)
    factory.progress(n, job, "First optional milestone")
    assert n.owner.status(job.handle)["semantic_observation"]
    with _plugin_home_scope(n.home):
        if kind == "disabled":
            n.config["supervision"]["enabled"] = False
            (n.home / "config.yaml").write_text(json.dumps(n.config))
        elif kind == "unload":
            n.bridge.close()
        elif kind == "steer":
            accept(n.agent, "- The appendix result is now required.", continuation=True)
        elif kind == "new_work":
            accept(n.agent, instruction())
        elif kind == "reset":
            n.agent.session_id = "reset-session"
        elif kind == "handoff":
            n.owner.mark_handoff(job.handle, "required-handoff", expected_revision=n.owner.status(job.handle)["control_revision"])
        else:
            n.owner.enroll_consumer(job.handle, "foreign-result-consumer", expected_revision=n.owner.status(job.handle)["control_revision"])
    factory.progress(n, job, "Second optional milestone after authority change")
    assert not n.owner.status(job.handle)["cancel_requested"]
    assert binding_of(job.child)[0] is n.owner
    # Revocation never removes the already-installed child effect fence.
    with pytest.raises(ControlDenied), n.owner.dispatch(job.handle, "terminal", {"command": "true"}):
        pass
    job.child.release.set()
    job.thread.join(3)
    assert n.owner._store.read(job.handle.child_id)["worker_finished"]
