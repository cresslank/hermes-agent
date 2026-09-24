"""Native lifetime -> real standalone provider retirement, with no live inference."""
from contextvars import copy_context
import threading

import pytest

from agent.supervision_policy import runtime_for_revision
from hermes_cli.plugins_loader import _plugin_home_scope
from tests.agent.supervision_test_support import accept
from tests.agent.test_owned_delegation_policy import factory


def enable(policy):
    from agent.owned_delegation_direct import POLICY
    policy.update(direct_control=dict(POLICY), allow_owned_child_stop=True, allow_optional_readonly=False)


def scope(revision):
    return (revision.profile, revision.lineage, revision.work_id)


def release(job):
    job.child.release.set()
    for child in job.siblings:
        child.release.set()
    job.thread.join(3)
    assert not job.thread.is_alive()


@pytest.mark.parametrize("ending", ["parent_first", "children_first", "same_work", "revoke", "new_work", "relaunch_at_exit"])
def test_native_scope_outlives_turn_but_not_its_last_owner(factory, ending, monkeypatch):
    from agent.supervision_children_direct import RecurringChildControl
    start = RecurringChildControl.start
    # Admit the finite sibling batch before starting periodic storage readers;
    # zero-wait authority admission deliberately refuses a contended launch.
    monkeypatch.setattr(RecurringChildControl, "start", lambda self: None)
    n = factory.make(text="- Inspect fixture.", policy_change=enable)
    n.mode["relation"] = "current"
    job = factory.launch(n, legacy=True, count=2)
    with _plugin_home_scope(n.home):
        start(n.runtime.children.direct)
    monkeypatch.setattr(RecurringChildControl, "start", start)
    retiring, resume_exit = threading.Event(), threading.Event()
    old_thread = n.runtime.children.direct.thread
    if ending == "relaunch_at_exit":
        from agent import supervision_scope_lifecycle as lifecycle
        retire_if_idle = lifecycle.retire_if_idle
        def held_exit(runtime):
            if threading.current_thread() is old_thread:
                retiring.set()
                assert resume_exit.wait(5)
            retire_if_idle(runtime)
        monkeypatch.setattr(lifecycle, "retire_if_idle", held_exit)
    factory.flush(n)
    revision = n.runtime.revision
    key = scope(revision)
    registration = n.runtime._registrations()[0]
    consumer = registration.consumer
    terminal = threading.Event()
    notifications, locks = [], []

    def observe(event):
        if event["event"] == "closed":
            from hermes_cli.sqlite_safe_read import _live_lock
            locks.append((n.runtime.lock._is_owned(), n.owner._lock._is_owned(),
                          registration.fence._is_owned(), _live_lock._is_owned()))
            notifications.append(event)
        result = consumer(event)
        if event["event"] == "closed":
            terminal.set()
        return result

    registration.consumer = observe
    with _plugin_home_scope(n.home):
        assert key in n.bridge.supervisor._revisions
        if ending in {"parent_first", "same_work"}:
            n.runtime.finish_turn()
            assert not notifications and n.owner.semantic_authorized(job.handle)
        if ending == "same_work":
            n.runtime.bind_turn()
            accept(n.agent, "- Keep working on the same fixture.", continuation=True)
            assert scope(n.runtime.revision) == key and n.owner.semantic_authorized(job.handle)
            assert not notifications
        if ending == "revoke":
            n.runtime.revoke()
            assert not n.owner.semantic_authorized(job.handle)
        elif ending == "new_work":
            accept(n.agent, "- A distinct task.", continuation=False)
            assert not n.owner.semantic_authorized(job.handle)
            assert runtime_for_revision(revision) is None
        else:
            # One child's durable completion is not the scope's completion.
            from hermes_cli.sqlite_safe_read import _live_lock
            with n.runtime.lock, n.owner._lock, _live_lock:
                n.owner.finish(job.handle)
                assert n.owner.status(job.handle)["settled"]
            assert not notifications
        release(job)
        if ending == "relaunch_at_exit":
            try:
                assert retiring.wait(3)
                replacement = factory.launch(n, legacy=True, goal="Inspect next fixture")
                assert n.runtime.children.direct.thread is not old_thread
                n.runtime.finish_turn()
                assert not notifications
            finally:
                resume_exit.set()
                old_thread.join(3)
            assert not old_thread.is_alive() and not notifications
            release(replacement)
        n.runtime.children.direct.thread.join(3)
        assert not n.runtime.children.direct.thread.is_alive()
        if ending in {"children_first", "same_work"}:
            assert not notifications  # parent still executing, no child authority needed
            n.runtime.finish_turn()
        assert terminal.wait(3)
        factory.flush(n)
        assert key not in n.bridge.supervisor._revisions
        assert len(notifications) == 1 and not any(any(row) for row in locks)
        assert tuple(notifications[0]["revision"][k] for k in ("profile", "lineage", "work_id")) == key
        if ending == "new_work":
            assert scope(n.runtime.revision) in n.bridge.supervisor._revisions
        state = n.owner.status(job.handle)
        assert state["obligation_state"] == "open" and not state["effects_reconciled"]


def test_retired_delivery_cannot_clear_replacement_and_completed_scopes_do_not_accumulate(factory):
    n = factory.make(text="- Inspect fixture.", policy_change=enable)
    factory.flush(n)
    registration = n.runtime._registrations()[0]
    consumer = registration.consumer
    entered, resume = threading.Event(), threading.Event()
    deliveries = []

    def delayed(event):
        if event["event"] == "held_observation":
            entered.set()
            assert resume.wait(5)
        deliveries.append(event)
        return consumer(event)

    registration.consumer = delayed
    with _plugin_home_scope(n.home):
        context = copy_context()
        worker = threading.Thread(target=lambda: context.run(n.runtime.observe, "held_observation", {}))
        worker.start()
        try:
            assert entered.wait(3)
            n.runtime.finish_turn()
            assert not any(e["event"] == "closed" for e in deliveries)
            n.runtime.bind_turn()
            n.runtime.observe("replacement_observation", {})
            current = n.runtime.revision
            factory.flush(n)
        finally:
            resume.set()
            worker.join(3)
        assert not worker.is_alive()
        factory.flush(n)
        assert n.bridge.supervisor._revisions[scope(current)].run_generation == current.run_generation
        assert any(e["event"] == "closed" for e in deliveries)
        n.runtime.finish_turn()
        factory.flush(n)
        assert not n.bridge.supervisor._revisions
        registration.consumer = consumer
        # Exceed the real provider's 512 live-scope bound through real native
        # admission, not direct Supervisor.invalidate or fabricated close events.
        for index in range(520):
            n.runtime.bind_turn()
            accept(n.agent, f"- Independent fixture {index}.", continuation=False)
            factory.flush(n)
            assert scope(n.runtime.revision) in n.bridge.supervisor._revisions
            n.runtime.finish_turn()
            factory.flush(n)
            assert not n.bridge.supervisor._revisions
        assert not n.bridge.supervisor._sequences
        assert not n.bridge._scope_fences
        assert not n.bridge._pending and not n.bridge._owner_by_target
        assert not n.calls  # lifecycle notifications never ask a semantic question
