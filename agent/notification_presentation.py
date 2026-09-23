"""Turn-local presentation of diagnostic-only wakes; execution and controls stay live."""
from contextlib import contextmanager
from contextvars import ContextVar


_muted_surface: ContextVar[str | None] = ContextVar("muted_notification_surface", default=None)
# These are text/media UI events, not approval/clarify/connection requests or outcomes.
_FREEFORM_EVENTS = frozenset({
    "message.start", "message.delta", "message.interim", "message.complete",
    "reasoning.delta", "thinking.delta", "status.update", "notification.show",
    "tool.start", "tool.complete", "tool.generating", "error", "reaction",
})
_PRESENTATION_CALLBACKS = (
    "stream_delta_callback", "interim_assistant_callback", "reasoning_callback",
    "tool_progress_callback",
    "tool_start_callback", "tool_complete_callback", "tool_gen_callback", "reaction_callback",
)


def event_presentation_muted(event: str, session_id: str) -> bool:
    return _muted_surface.get() == session_id and event in _FREEFORM_EVENTS


def diagnostic_process_event(event: dict) -> bool:
    """Early failure/monitor diagnostics, not the explicitly requested final result."""
    return bool(event.get("task_failure_notice")) or event.get("type") in {
        "watch_disabled", "watch_overflow_tripped", "watch_overflow_released",
    }


def notification_config_snapshot():
    """Read the owning effective config once per turn (the loader already returns a fresh copy)."""
    from gateway.warning_notifications import effective_user_config
    return effective_user_config()


@contextmanager
def notification_policy_snapshot(agent, platform, config):
    """Bind one foreground policy for callbacks, including worker threads. ``config`` is read-only."""
    missing = object()
    saved = {key: getattr(agent, key, missing)
             for key in ("_notification_config", "_notification_platform")}
    try:
        agent._notification_config = config
        agent._notification_platform = platform
        yield
    finally:
        for key, value in saved.items():
            if value is missing:
                delattr(agent, key)
            else:
                setattr(agent, key, value)


@contextmanager
def notification_turn(agent, *, muted: bool, session_id: str = ""):
    """Freeze the current turn's presentation without touching prompts or tool schemas."""
    if not muted:
        yield
        return
    missing = object()
    keys = (*_PRESENTATION_CALLBACKS, "suppress_status_output", "_mute_notification_reply")
    saved = {key: getattr(agent, key, missing) for key in keys}
    token = _muted_surface.set(session_id)
    try:
        for key in _PRESENTATION_CALLBACKS:
            setattr(agent, key, None)
        agent.suppress_status_output = True
        agent._mute_notification_reply = True
        yield
    finally:
        for key, value in saved.items():
            if value is missing:
                delattr(agent, key)
            else:
                setattr(agent, key, value)
        _muted_surface.reset(token)


# Optional status coalescing lives beside, not above, the existing presentation
# policy. Mandatory/legacy output never enters this queue.
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, replace
from weakref import WeakValueDictionary
import threading
import time

from agent.supervision_catalog import fingerprint


@dataclass(frozen=True)
class OptionalUpdate:
    subject: str
    revision: str
    facts: tuple[tuple[str, str], ...] = ()
    optional: bool = False
    replaceable: bool = False
    complete: bool = False
    requested: bool = False
    approval: bool = False
    safety: bool = False
    failure: bool = False
    cleanup: bool = False
    correction: bool = False
    changed_commitment: bool = False

    @property
    def eligible(self):
        return (self.optional and self.replaceable and self.subject and self.revision
                and not any((self.requested, self.approval, self.safety, self.failure,
                             self.cleanup, self.correction, self.changed_commitment)))


class OptionalProgressText(str):
    """Producer-owned marker; ordinary strings and warnings stay unclassified."""
    def __new__(cls, text, *, subject="provider_wait", revision="progress"):
        value = super().__new__(cls, text)
        value.optional_update = OptionalUpdate(subject, revision, optional=True,
                                              replaceable=True, complete=True)
        return value


@dataclass
class _PendingUpdate:
    metadata: OptionalUpdate
    fingerprint: str
    payload: object
    previous: object
    render: object
    generation: int
    deadline: float
    scope: str
    future: object = None
    cancel: object = None


class _ProfileQueue:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = {}
        self.last = OrderedDict()


_profile_queues = WeakValueDictionary()
_profile_queues_lock = threading.Lock()


class StatusCoalescer:
    """One instance PER PROFILE, shared by its presentation adapters; bounded32.

    ``dispatch(callback)`` posts to the UI/owner context (never calls inline on a
    worker). ``call_later(seconds, callback)`` schedules ONE shot and returns a
    cancellation callable. The callback itself only posts via dispatch. No network,
    callback invocation or cancellation occurs under the queue lock. Reset/unload
    and notice clearing must run on the same owner context as presentation.
    """
    CAPACITY = 32

    def __init__(self, views, *, profile_key=None, dispatch=None, call_later=None, clock=time.monotonic):
        self.views, self.dispatch, self.call_later, self.clock = views, dispatch, call_later, clock
        # Production adapters pass their immutable profile key. Missing key means
        # an isolated owner (useful to hosts without profile multiplexing).
        with _profile_queues_lock:
            state = _profile_queues.get(profile_key) if profile_key is not None else None
            if state is None:
                state = _ProfileQueue()
                if profile_key is not None:
                    _profile_queues[profile_key] = state
        self._queue_state = state
        self._lock, self._pending, self._last = state.lock, state.pending, state.last
        self._generation = 0
        self._owned_subjects = set()

    def clear(self, subject):
        with self._lock:
            item = self._pending.pop(subject, None)
            self._last.pop(subject, None)
        if item and callable(item.cancel):
            item.cancel()

    def detach(self):
        """Revoke only current closures now; return owner-dispatched timer cleanup.

        A later reset callback must not cancel notices created in the new scope.
        """
        with self._lock:
            self._generation += 1
            items = [self._pending.pop(s) for s in self._owned_subjects if s in self._pending]
            for subject in self._owned_subjects:
                self._last.pop(subject, None)
            self._owned_subjects.clear()
        def cleanup():
            for item in items:
                if callable(item.cancel):
                    item.cancel()
        return cleanup

    def reset(self):
        self.detach()()

    close = reset

    def _remember(self, subject, key, payload):
        self._last[subject] = (key, payload)
        self._last.move_to_end(subject)
        while len(self._last) > self.CAPACITY:
            self._last.popitem(last=False)

    def present(self, metadata, payload, render):
        if not isinstance(metadata, OptionalUpdate) or not metadata.eligible or len(str(payload)) > 4000:
            if isinstance(metadata, OptionalUpdate):
                self.clear(metadata.subject)
            render()
            return
        key = fingerprint((metadata.revision, metadata.facts, payload))
        old = None
        item = None
        with self._lock:
            self._owned_subjects.intersection_update(set(self._pending) | set(self._last))
            self._owned_subjects.add(metadata.subject)
            pending = self._pending.get(metadata.subject)
            if (self._last.get(metadata.subject) or (None,))[0] == key or (pending and pending.fingerprint == key):
                return  # exact optional duplicates never dispatch inference
            last = self._last.get(metadata.subject)
            can_queue = (last is not None and metadata.complete and callable(self.dispatch)
                         and callable(self.call_later)
                         and (pending is not None or len(self._pending) < self.CAPACITY))
            if can_queue:
                deadline = self.views.cycle_deadline()
                if self.clock() < deadline:
                    item = _PendingUpdate(metadata, key, payload, last, render, self._generation, deadline, self.views.scope)
                    old = self._pending.pop(metadata.subject, None)
                    self._pending[metadata.subject] = item
            if item is None:
                old = self._pending.pop(metadata.subject, None)
                self._remember(metadata.subject, key, payload)
        if old and callable(old.cancel):
            old.cancel()
        if item is None:
            render()
            return
        try:
            # Register timeout before worker admission, so exceptions cannot strand a notice.
            item.cancel = self.call_later(max(0, item.deadline - self.clock()),
                lambda: self.dispatch(lambda: self._finish(item, expired=True)))
            with self._lock:
                current = self._pending.get(metadata.subject) is item
            if not current:
                if callable(item.cancel):
                    item.cancel()
                return
            request = {"domain": "filterable_notification_proposed", "scope": item.scope,
                       "revision": metadata.revision, "deadline": item.deadline,
                       "facts": {"subject": metadata.subject, "content": payload,
                                 "changed_fields": metadata.facts, "previous_fingerprint": last[0],
                                 "previous_content": last[1],
                                 "optional": True, "complete": True}}
            result = self.views.facade.evaluate_relation(request)
            future = result if isinstance(result, Future) else Future()
            if not isinstance(result, Future):
                future.set_result(result)
            item.future = future
            # This callback may run on the inference worker: posting is its ONLY effect.
            future.add_done_callback(lambda _: self.dispatch(lambda: self._finish(item)))
        except Exception:
            self.dispatch(lambda: self._finish(item, expired=True))

    def _finish(self, item, *, expired=False):
        suppress = False
        if not expired and self.clock() < item.deadline and item.future is not None and item.future.done():
            try:
                answer = item.future.result()
                suppress = (isinstance(answer, dict) and answer.get("revision") == item.metadata.revision
                            and answer.get("relation") in {"progress_only", "duplicate"})
            except Exception:
                pass
        with self._lock:
            if item.generation != self._generation or self._pending.get(item.metadata.subject) is not item:
                return
            self._pending.pop(item.metadata.subject)
            if item.scope != self.views.scope:
                suppress = True  # replaced task/run: discard the old UI closure
            else:
                self._remember(item.metadata.subject, item.fingerprint,
                               item.previous[1] if suppress and item.previous else item.payload)
        if callable(item.cancel):
            item.cancel()
        release = getattr(self.views.facade, "release_status_future", None)
        if callable(release):
            release(item.future)
        if not suppress:
            item.render()


def asyncio_owner_callbacks(loop):
    """Thread-safe adapter for gateway/TUI's EXISTING owner event loop.

    The deadline remains absolute even if arming the timer is delayed by a busy
    loop. Cancellation is safe before or after the owner has armed its handle.
    No extra event loop or presentation/inference thread is created.
    """
    def dispatch(callback):
        loop.call_soon_threadsafe(callback)

    def call_later(delay, callback):
        deadline = time.monotonic() + max(0., delay)
        cancelled = threading.Event()
        handles = []
        def arm():
            if not cancelled.is_set():
                handles.append(loop.call_later(max(0., deadline - time.monotonic()), fire))
        def fire():
            if not cancelled.is_set():
                callback()
        def cancel():
            cancelled.set()
            def cancel_on_owner():
                for handle in handles:
                    handle.cancel()
            if not loop.is_closed():
                loop.call_soon_threadsafe(cancel_on_owner)
        dispatch(arm)
        return cancel
    return dispatch, call_later


def bind_status_owner(agent, views, *, profile_key, dispatch=None, call_later=None):
    """Bind after host supervision negotiation; return reset/unload cancellation.

    CLI adapters pass their owner/UI dispatcher (no dispatcher => immediate
    baseline); gateway/TUI can use ``asyncio_owner_callbacks(existing_loop)``.
    Host lifecycle must invoke the returned closure on reset and plugin unload.
    """
    previous = getattr(agent, "_status_coalescer", None)
    if isinstance(previous, StatusCoalescer):
        previous.close()
    agent._status_coalescer = StatusCoalescer(views, profile_key=profile_key,
        dispatch=dispatch, call_later=call_later, clock=views.clock)
    return agent._status_coalescer.close


def _optional_subject(agent, subject):
    from agent.supervision_views import SupervisionViews
    views = getattr(agent, "_supervision_views", None)
    return f"{views.scope}:{subject}" if isinstance(views, SupervisionViews) else subject


def clear_optional(agent, subject):
    coalescer = getattr(agent, "_status_coalescer", None)
    if isinstance(coalescer, StatusCoalescer):
        coalescer.clear(_optional_subject(agent, subject))


def present_optional(agent, metadata, payload, render):
    coalescer = getattr(agent, "_status_coalescer", None)
    if isinstance(coalescer, StatusCoalescer):
        if isinstance(metadata, OptionalUpdate):
            metadata = replace(metadata, subject=_optional_subject(agent, metadata.subject))
        coalescer.present(metadata, payload, render)
    else:
        render()
