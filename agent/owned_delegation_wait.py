"""Event-driven child-control safe point in an already-owned synchronous join.

Wake only for submitted native proposals or future completion. The existing
interrupt timeout is unchanged; there is no semantic polling, extra inference,
new background supervisor or additional owner wait budget.
"""
from __future__ import annotations

import threading

from agent.owned_delegation import binding_of
from agent.owned_delegation_policy import ConfiguredDelegationOwner


def has_configured_children(batch):
    return any(binding and isinstance(binding[0], ConfiguredDelegationOwner)
               for _, _, child in batch.children if (binding := binding_of(child)))


class ChildControlWait:
    def __init__(self, batch):
        bindings = [binding_of(child) for _, _, child in batch.children]
        self.owner = next((b[0] for b in bindings if b and isinstance(b[0], ConfiguredDelegationOwner)), None)
        self.runtime = self.owner.runtime if self.owner is not None else None
        self.targets = {b[1].child_id for b in bindings if b and b[0] is self.owner}
        self.parent = batch.parent_agent
        self.thread = threading.get_ident()
        if self.runtime is not None:
            with self.runtime.lock:
                self.runtime.child_control_waiters[self.thread] = frozenset(self.targets)

    def notify(self, future):
        if self.runtime is not None:
            with self.runtime.ready:
                self.runtime.ready.notify_all()

    def wait(self, pending):
        rt = self.runtime
        assert rt is not None
        with rt.ready:
            rt.ready.wait_for(lambda: any(f.done() for f in pending)
                or getattr(self.parent, "_interrupt_requested", False) is True
                or any(p.owner == "owned_delegation" and p.target_id in self.targets for p, _ in rt.pending),
                timeout=.5)
            bridge = getattr(rt, "children", None)
            if bridge is not None and any(p.owner == "owned_delegation" and p.target_id in self.targets for p, _ in rt.pending):
                # No advisory/main-agent work is drained from a synchronous tool.
                if bridge.direct is not None:
                    bridge.direct.drain()
                for target in self.targets:
                    if target in bridge.targets:
                        from agent.supervision_types import Action
                        for action in (Action.REPRIORITIZE_CHILD, Action.CANCEL_CHILD):
                            rt.consume_owner_action(target, action, bridge.apply)
                rt.ready.notify_all()
        done = {f for f in pending if f.done()}
        return done, pending - done

    def close(self):
        if self.runtime is not None:
            with self.runtime.lock:
                self.runtime.child_control_waiters.pop(self.thread, None)
