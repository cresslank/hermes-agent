"""Provider state lifetime, distinct from parent turn and native worker lifetime.

Capture retirement under the runtime fence; deliver only after every already
admitted observation has scheduled its callback. No provider callback belongs
under the runtime, native control, registration or SQLite locks.
"""
from dataclasses import dataclass, field
import uuid

from agent.supervision_types import project


@dataclass
class ProviderScope:
    deliveries: int = 0
    recipients: dict = field(default_factory=dict)
    terminal: dict | None = None
    notified: bool = False


def admit(runtime, registrations):
    """Caller holds runtime.lock. A post-turn completion owner may reopen state."""
    scope = runtime.provider_scope
    if scope.terminal is not None:
        scope = runtime.provider_scope = ProviderScope()
    scope.deliveries += 1
    scope.recipients.update((r.generation, r) for r in registrations)
    return scope


def retire(runtime):
    """Caller holds runtime.lock; capture the OLD revision before replacing it."""
    scope = runtime.provider_scope
    if scope.terminal is None:
        runtime.sequence += 1
        scope.terminal = dict(event="closed", sequence=runtime.sequence,
            revision=project(runtime.revision), owner="agent", data_policy={},
            facts={"target_id": "scope:" + uuid.uuid4().hex})
    return scope


def notify(runtime, scope, *, delivered=False):
    from agent.supervision_policy import _observer_callback
    with runtime.lock:
        if delivered:
            scope.deliveries -= 1
        if scope.terminal is None or scope.deliveries or scope.notified:
            return
        scope.notified = True
        recipients = tuple(scope.recipients.values())
        scope.recipients.clear()
    for registration in recipients:
        # Never look up a replacement registration on behalf of an old scope.
        if not registration.active:
            continue
        token = _observer_callback.set(True)
        try:
            registration.consumer(project(scope.terminal))
        except Exception:
            registration.note_failure()
        finally:
            _observer_callback.reset(token)


def has_live_children(runtime):
    """Caller holds runtime.lock. Unknown native settlement remains live."""
    children = getattr(runtime, "children", None)
    if children is None or children.direct is None:
        return False
    revision = runtime.revision
    scope = (revision.profile, revision.lineage, revision.work_id)
    with children.owner._lock:
        for handle, launched_scope in children.launches.values():
            if launched_scope[:3] != scope:
                continue
            live = children.owner._live.get(handle.child_id)
            if live is None or live.handle != handle or not live.snapshot["settled"]:
                return True
    return False


def retire_if_idle(runtime):
    with runtime.lock:
        if not runtime.closed or has_live_children(runtime):
            return
        scope = retire(runtime)
    notify(runtime, scope)
