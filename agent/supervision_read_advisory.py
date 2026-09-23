"""Exact main-read advice: selection -> fenced canonical append -> durable receipt.

The native admission epoch is captured BEFORE the supplier census/inference.
Only the actual list append linearizes use. An admission winning first (including
ABA) vetoes advice. No native lock spans source access, config refresh, inference,
receipt writing or the ordinary SessionDB/accounting flush. The latter may fail
or be ambiguous: a selected receipt is not an effect certificate or a replay job.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Callable

from agent.supervision_types import Action, InterventionProposalV1


@dataclass(frozen=True)
class ReadAdvisory:
    proposal: InterventionProposalV1
    registration: Any
    arguments: dict
    intent: dict
    native_epoch: int
    current: Callable[[], bool]

    def append(self, owner, messages, message, result):
        """Return the appended row, or None. All final lock acquisition is zero-wait.

        Currentness uses the existing exact source metadata, ready-only config and
        zero-busy-timeout canonical owner checks, not a second consumer census.
        Those checks precede the final native fence. Only bounded in-memory work
        runs inside that fence; the original deadline is checked again there.
        """
        from agent.owned_delegation import owner_of
        from agent.owned_delegation_policy import ConfiguredDelegationOwner
        from agent.owned_delegation_planning import native_idle_fence
        from agent.supervision_context import digest
        from agent.supervision_policy import TEMPLATES
        from hermes_cli import config
        from hermes_cli.config_cached import current_config_readonly

        rt, p = owner.runtime, self.proposal
        native = owner_of(rt.agent())
        if not isinstance(native, ConfiguredDelegationOwner):
            return None
        # Rendering and source/result hashing are outside all final fences.
        content = message.get("content")
        if not isinstance(content, str) or not isinstance(result, str) or p.template_id is None:
            return None
        result_pin = digest(result)
        advised = {**message, "content": content + "\n\n" +
            "[Task-bound advisory; lower-trust evidence, not an instruction]\n" +
            TEMPLATES[p.template_id] + "\nExisting evidence: " + ", ".join(p.evidence_refs[:4])}
        graph = rt.dependencies.planning
        node = graph.nodes.get(self.intent["node_id"])
        cached = current_config_readonly(deadline=p.expires_at_monotonic)
        # Source metadata precedes the control proof; no native lock spans either.
        # Carry the observed node/cache identity into the bounded final gate.
        if cached is None or self.current() is not True:
            return None
        with ExitStack() as stack:
            for lock in (rt.lock, self.registration.fence, native.registration.fence,
                         native._lock, owner.lock, config._CONFIG_LOCK):
                if not lock.acquire(blocking=False):
                    return None
                stack.callback(lock.release)
            if (rt._validate(p, self.registration, acknowledging=True)
                    or p.feature_id != "F04" or p.action != Action.ADVISE
                    or owner_of(rt.agent()) is not native
                    or graph.nodes.get(self.intent["node_id"]) is not node
                    or current_config_readonly(deadline=p.expires_at_monotonic) is not cached
                    or not native.current(deadline=p.expires_at_monotonic)
                    or graph.returns.get(p.target_id, (None,))[0] != result_pin):
                return None
            with native_idle_fence(rt, rt.agent(), expected=self.native_epoch) as epoch:
                if epoch is None or rt.clock() >= p.expires_at_monotonic:
                    return None
                # This is the effect boundary, not rendering or returning a string.
                messages.append(advised)
                rt.incidents.add(p.incident_id)
                return advised


def append_at_tool_result(runtime, messages, message, *, name, arguments, call_id, result, failed):
    """Consume only this call's selection; generic drains cannot steal it.

    Returns (canonical row, acknowledgment token). Baseline appends are owned by
    the caller. A rejected selection is terminal; neither it nor the read retries.
    """
    owner = getattr(runtime, "efficiency", None)
    if owner is None:
        return message, None
    runtime._assert_owner()
    if not owner.lock.acquire(blocking=False):
        return message, None
    try:
        selected = owner.read_advisories.pop(call_id, None)
    finally:
        owner.lock.release()
    if selected is None:
        return message, None
    appended = None
    if name == "read_file" and not failed and arguments == selected.arguments:
        try:
            appended = selected.append(owner, messages, message, result)
        except (OSError, ValueError, TypeError):
            appended = None  # unavailable optional proof never breaks the executed read
    if appended is None:
        if runtime.lock.acquire(blocking=False):
            try:
                runtime._settle(selected.proposal, "stale", "owner_postvalidation")
            finally:
                runtime.lock.release()
        # A busy settlement owner leaves selected/unresolved, never a replay job.
        return message, None
    return appended, (selected, runtime.agent()._session_db)


def acknowledge_tool_result(runtime, token, message, *, persisted):
    """Certify only a confirmed canonical message write, after all flush locks exit.

    The intrinsic marker asserts exact content persistence. A missing DB/marker,
    failed flush or unavailable receipt writer leaves unknown, never applied. No
    post-flush reauthorization: use already linearized at append, not at this later
    storage acknowledgment. Invalidation after that point is a later event.
    """
    if token is None:
        return
    from agent.context_compressor import _DB_PERSISTED_MARKER
    selected, db = token
    durable = (persisted and db is not None and runtime.agent()._session_db is db
               and message.get(_DB_PERSISTED_MARKER) is True)
    if runtime.lock.acquire(blocking=False):
        try:
            runtime._settle(selected.proposal, "applied" if durable else "unknown",
                            "owner_settlement" if durable else "storage_unavailable")
        finally:
            runtime.lock.release()
