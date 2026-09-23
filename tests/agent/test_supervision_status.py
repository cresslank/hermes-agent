"""Exercise actual status/notice/wait producers before their first presentation."""
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from agent.notification_presentation import StatusCoalescer, OptionalUpdate, OptionalProgressText
from agent.status_output import StatusOutputMixin
from agent.supervision_views import SupervisionViews


class OwnerQueue:
    def __init__(self):
        self.now = 10.
        self.ready, self.timers = [], []
    def dispatch(self, callback):
        self.ready.append(callback)
    def later(self, seconds, callback):
        timer = [self.now + seconds, callback, False]
        self.timers.append(timer)
        return lambda: timer.__setitem__(2, True)
    def drain(self):
        while self.ready:
            self.ready.pop(0)()
    def expire(self):
        self.now += .2
        for deadline, callback, cancelled in self.timers:
            if not cancelled and deadline <= self.now:
                callback()
        self.drain()


class Facade:
    def __init__(self): self.requests, self.futures = [], []
    def evaluate_relation(self, request):
        self.requests.append(request)
        future = Future()
        self.futures.append(future)
        return future
    def resolve(self, label="progress_only", index=-1, revision=None):
        self.futures[index].set_result({"revision": revision or self.requests[index]["revision"], "relation": label})


class Surface(StatusOutputMixin):
    def __init__(self, platform):
        self.platform, self.log_prefix = platform, ""
        self._notification_config = {}
        self._print_fn = lambda *a, **kw: self.prints.append(a)
        self.prints, self.status, self.notices, self.thinking, self.activity, self.threads = [], [], [], [], [], []
        self.status_callback = lambda *a: self.status.append(a)
        self.notice_callback = lambda a: self.notices.append(a)
        self.notice_clear_callback = lambda a: self.notices.append(("clear", a))
        self.thinking_callback = self.think
    def think(self, text):
        self.thinking.append(text)
        self.threads.append(threading.get_ident())
    def _touch_activity(self, text): self.activity.append(text)
    def _has_stream_consumers(self): return False


@pytest.fixture
def owner():
    queue, facade = OwnerQueue(), Facade()
    views = SupervisionViews(facade, scope="profile:lineage:run", clock=lambda: queue.now)
    coalescer = StatusCoalescer(views, dispatch=queue.dispatch, call_later=queue.later, clock=lambda: queue.now)
    return queue, facade, coalescer


def update(revision="r1", **kwargs):
    return OptionalUpdate("subject", revision, optional=True, replaceable=True, complete=True, **kwargs)


@pytest.mark.parametrize("platform", ["cli", "telegram", "tui"])
@pytest.mark.parametrize("route", ["status", "notice", "wait"])
def test_real_presentation_paths_duplicate_then_changed_worker_cannot_render(owner, platform, route):
    queue, facade, coalescer = owner
    agent = Surface(platform)
    agent._status_coalescer = coalescer
    def emit(text, meta):
        if route == "status": agent._emit_status_kind("lifecycle", text, origin="test", optional_update=meta)
        if route == "notice": agent._emit_notice(SimpleNamespace(key="subject", level="info", text=text), optional_update=meta)
        if route == "wait": agent._emit_wait_notice(text, optional_update=meta)
    visible = {"status": agent.status, "notice": agent.notices, "wait": agent.thinking}[route]
    for _ in range(10): emit("same", update())
    assert len(visible) == 1 and not facade.requests
    emit("changed", update("r2"))
    assert len(visible) == 1 and len(facade.requests) == 1
    worker = threading.Thread(target=lambda: facade.resolve("material_outcome"))
    worker.start(); worker.join()
    assert len(visible) == 1  # inference completion may only POST
    queue.drain()
    assert len(visible) == 2
    if route == "wait":
        assert len(agent.activity) == 11
        assert set(agent.threads) == {threading.get_ident()}
    if route == "status": assert len(agent.prints) == 2


@pytest.mark.parametrize("label", ["progress_only", "duplicate", "insufficient", "material_outcome"])
def test_semantic_decision_normal_baseline_on_uncertainty(owner, label):
    queue, facade, coalescer = owner
    agent = Surface("tui"); agent._status_coalescer = coalescer
    agent._emit_wait_notice(OptionalProgressText("first", revision="1"))
    agent._emit_wait_notice(OptionalProgressText("second", revision="2"))
    facade.resolve(label); queue.drain()
    assert len(agent.thinking) == (1 if label in {"progress_only", "duplicate"} else 2)
    assert len(agent.activity) == 2


def test_expired_suppression_and_superseded_items_present_only_current(owner):
    queue, facade, coalescer = owner
    agent = Surface("cli"); agent._status_coalescer = coalescer
    agent._emit_wait_notice("first", optional_update=update())
    agent._emit_wait_notice("second", optional_update=update("r2"))
    agent._emit_wait_notice("third", optional_update=update("r3"))
    assert len(coalescer._pending) == 1
    assert facade.requests[0]["deadline"] == facade.requests[1]["deadline"]
    facade.resolve("material_outcome", index=0); queue.drain()
    assert agent.thinking == ["first"]
    queue.expire()
    assert agent.thinking == ["first", "third"]
    facade.resolve(); queue.drain()
    assert agent.thinking == ["first", "third"]


@pytest.mark.parametrize("flag", ["requested", "approval", "safety", "failure", "cleanup", "correction", "changed_commitment"])
def test_mandatory_never_delayed_even_with_optional_claim(owner, flag):
    queue, facade, coalescer = owner
    agent = Surface("telegram"); agent._status_coalescer = coalescer
    agent._emit_status_kind("lifecycle", "must show", origin="test", optional_update=update(**{flag: True}))
    assert agent.status == [("lifecycle", "must show")]
    assert not facade.requests and not queue.timers


@pytest.mark.parametrize("operation", ["clear", "reset", "unload"])
def test_clear_reset_unload_cancel_callbacks(owner, operation):
    queue, facade, coalescer = owner
    agent = Surface("tui"); agent._status_coalescer = coalescer
    agent._emit_notice("first", optional_update=update())
    agent._emit_notice("second", optional_update=update("r2"))
    if operation == "clear": agent._emit_notice_clear("subject")
    elif operation == "reset": coalescer.reset()
    else: coalescer.close()
    facade.resolve("material_outcome"); queue.expire()
    assert "second" not in agent.notices and not coalescer._pending
    assert all(t[2] for t in queue.timers)


def test_queue_exhaustion_and_missing_dispatcher_are_normal_baseline(owner):
    queue, facade, coalescer = owner
    shown = []
    for n in range(33):
        meta = replace(update(), subject=str(n))
        coalescer.present(meta, "first", lambda n=n: shown.append((n, "first")))
        coalescer.present(replace(meta, revision="r2"), "second", lambda n=n: shown.append((n, "second")))
    assert len(coalescer._pending) == 32 and len(facade.requests) == 32
    assert (32, "second") in shown
    other = StatusCoalescer(coalescer.views)
    other.present(update(), "first", lambda: shown.append("first"))
    other.present(update("r2"), "second", lambda: shown.append("second"))
    assert shown[-2:] == ["first", "second"]


def test_existing_warning_policy_retry_exhaustion_and_notice_clear(owner):
    queue, facade, coalescer = owner
    agent = Surface("cli"); agent._status_coalescer = coalescer
    agent._buffer_status("retry one")
    agent._buffer_retry_message("warn", "failed; cleanup needed")
    agent._flush_status_buffer()
    agent._emit_status_kind("warn", "warning", origin="test", optional_update=update())
    agent._emit_notice(SimpleNamespace(level="error", key="failure"), optional_update=update())
    assert len(agent.status) == 3 and len(agent.notices) == 1
    assert not facade.requests
    agent._emit_wait_notice(OptionalProgressText("first", revision="1"))
    agent._emit_wait_notice(OptionalProgressText("pending", revision="2"))
    agent._emit_wait_notice("")
    facade.resolve("material_outcome"); queue.expire()
    assert agent.thinking == ["first", ""]


def test_profile_capacity_shared_across_surface_owners():
    queues = [OwnerQueue(), OwnerQueue()]
    facades = [Facade(), Facade()]
    owners = [StatusCoalescer(SupervisionViews(facades[i], scope=f"run{i}", clock=lambda: 10.),
        profile_key="profile-capacity-test", dispatch=queues[i].dispatch,
        call_later=queues[i].later, clock=lambda: 10.) for i in range(2)]
    shown = []
    for i in range(33):
        owner = owners[i % 2]
        meta = replace(update(), subject=f"work:{i}")
        owner.present(meta, "first", lambda: None)
        owner.present(replace(meta, revision="r2"), "second", lambda: shown.append("overflow"))
    assert owners[0]._pending is owners[1]._pending
    assert len(owners[0]._pending) == 32
    assert sum(len(f.requests) for f in facades) == 32
    assert shown == ["overflow"]
    owners[0].close()


@pytest.mark.asyncio
async def test_real_asyncio_owner_dispatch_deadline_and_worker_completion():
    import asyncio
    from agent.notification_presentation import bind_status_owner, asyncio_owner_callbacks
    loop = asyncio.get_running_loop()
    facade = Facade()
    views = SupervisionViews(facade, scope="async-run")
    agent = Surface("tui")
    agent._supervision_views = views
    dispatch, later = asyncio_owner_callbacks(loop)
    cancel = bind_status_owner(agent, views, profile_key="async-profile", dispatch=dispatch, call_later=later)
    presented = asyncio.Event()
    original = agent.think
    def callback(text):
        original(text)
        if text == "second": presented.set()
    agent.thinking_callback = callback
    agent._emit_wait_notice(OptionalProgressText("first", revision="1"))
    agent._emit_wait_notice(OptionalProgressText("second", revision="2"))
    worker = threading.Thread(target=lambda: facade.resolve("material_outcome"))
    worker.start(); worker.join()
    assert not presented.is_set()
    await asyncio.wait_for(presented.wait(), timeout=.5)
    assert set(agent.threads) == {threading.get_ident()}
    # An unresolved update must present once on the original absolute deadline.
    agent._emit_wait_notice(OptionalProgressText("third", revision="3"))
    await asyncio.sleep(.18)
    assert agent.thinking == ["first", "second", "third"]
    cancel()


def test_diagnostic_wait_cancels_pending_optional_progress(owner):
    queue, facade, coalescer = owner
    agent = Surface("cli"); agent._status_coalescer = coalescer
    agent._emit_wait_notice(OptionalProgressText("first", revision="1"))
    agent._emit_wait_notice(OptionalProgressText("pending", revision="2"))
    agent._emit_diagnostic_wait("provider exhausted; cleanup needed")
    facade.resolve("material_outcome"); queue.expire()
    assert agent.thinking == ["first", "provider exhausted; cleanup needed"]
