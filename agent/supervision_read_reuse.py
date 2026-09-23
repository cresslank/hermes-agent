"""Selected main-read reuse at the existing authorized dispatch boundary.

No synthetic file/verification receipt: return the original recorded result with
an explicit provenance envelope. Source, consumer, control and registration
currentness are checked after inference; a failed proof executes the real read.
"""
from contextlib import ExitStack
from dataclasses import dataclass
import json
from typing import Any, Callable

from agent.supervision_types import Action, InterventionProposalV1


@dataclass(frozen=True)
class ReadReuse:
    proposal: InterventionProposalV1
    registration: Any
    arguments: dict
    intent: dict
    native_epoch: int
    current: Callable[[], bool]

    def consume(self, owner):
        try:
            value = self._consume(owner)
        except Exception:
            value = None  # optional proof failure preserves the real tool path
        if owner.runtime.lock.acquire(blocking=False):
            try:
                owner.runtime._settle(self.proposal, "applied" if value is not None else "stale",
                                      "existing_result_reused" if value is not None else "owner_postvalidation")
            finally:
                owner.runtime.lock.release()
        # A busy receipt owner leaves unknown/selected, never a replay job.
        return value

    def _consume(self, owner):
        from agent.owned_delegation import owner_of
        from agent.owned_delegation_policy import ConfiguredDelegationOwner
        from agent.owned_delegation_planning import native_idle_fence
        from hermes_cli import config
        from hermes_cli.config_cached import current_config_readonly

        rt, p = owner.runtime, self.proposal
        native = owner_of(rt.agent())
        if not isinstance(native, ConfiguredDelegationOwner):
            return None
        from agent.file_safety import get_read_block_error
        from agent.redact import _is_secret_file_arg
        path = self.arguments.get("path")
        if get_read_block_error(path) or _is_secret_file_arg(path):
            return None
        cached = current_config_readonly(deadline=p.expires_at_monotonic)
        if cached is None or self.current() is not True:
            return None
        value = None
        with ExitStack() as stack:
            for lock in (rt.lock, self.registration.fence, native.registration.fence,
                         native._lock, owner.lock, config._CONFIG_LOCK):
                if not lock.acquire(blocking=False):
                    return None
                stack.callback(lock.release)
            if (rt._validate(p, self.registration, acknowledging=True)
                    or p.feature_id != "F04" or p.action != Action.REUSE_CANDIDATE
                    or owner_of(rt.agent()) is not native
                    or current_config_readonly(deadline=p.expires_at_monotonic) is not cached
                    or not native.current(deadline=p.expires_at_monotonic)):
                return None
            m = p.metadata
            prior = owner.reads.get(m.get("candidate_id"))
            payload = owner.read_payloads.get(m.get("candidate_id"))
            if (prior is None or not isinstance(payload, bytes)
                    or m.get("feature_action") != "reuse_candidate"
                    or m.get("source_ref") != prior["source_ref"]
                    or m.get("requirement_id") != self.intent["requirement_id"]
                    or m.get("f01_eligibility_candidate") is not None
                    or m.get("cancellation_authorized") is not False
                    or prior["source_ref"] not in p.evidence_refs):
                return None
            with native_idle_fence(rt, rt.agent(), expected=self.native_epoch) as epoch:
                if epoch is None or rt.clock() >= p.expires_at_monotonic:
                    return None
                # The result belongs to the earlier call, not this skipped read.
                value = json.dumps({"executed": False, "reused_from": prior["source_ref"],
                    "source_snapshot": prior["snapshot"], "result": payload.decode("utf-8")})
                owner.read_pending.pop(p.target_id, None)
                rt.incidents.add(p.incident_id)
        # Receipt persistence is outside native/control fences. This certifies
        # reuse selection at dispatch, not persistence of a new tool execution.
        return value
