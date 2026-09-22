"""Plugin-facing supervision.v1 negotiation and owner protocol.

The owner grants are read from profile configuration, not plugin settings or a
request payload. Registration gives no agent, DB handle or cancellation capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import uuid

from agent.supervision_types import (
    VERSION, Action, InterventionProposalV1, OwnerDecisionV1, OwnerRequestV1,
    Revision, METADATA_VERSION, field_data_policy, project,
)

_registry = {}
_lock = threading.RLock()


@dataclass
class _Registration:
    plugin_id: str
    scope: str
    generation: str
    consumer: object
    grants: frozenset
    data_policy: frozenset
    egress_policy: dict = field(default_factory=dict)
    active: bool = True
    failures: int = 0
    fence: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def note_failure(self):
        self.failures += 1

    def close(self):
        # Revoke generation before any plugin worker is asked to stop.
        with self.fence:
            self.active = False
        with _lock:
            if _registry.get((self.scope, self.plugin_id)) is self:
                _registry.pop((self.scope, self.plugin_id), None)
        from agent.supervision_view_binding import registration_closed
        registration_closed(self.scope)


def registrations_for_scope(scope):
    with _lock:
        return tuple(r for (s, _), r in _registry.items() if s == scope and r.active)


class SupervisionFacade:
    version = VERSION

    def __init__(self, context):
        self._context = context
        self._registration = None

    def negotiate(self, version=VERSION):
        return {"version": VERSION, "supported": version == VERSION,
                "proposal_metadata": METADATA_VERSION,
                "skill_details": "supervision.skill-details.v1",
                "view_actions_version": "supervision.view-actions.v1",
                "view_actions": {"present_material_once": "present_status",
                                 "retrieve": "clarify_retrieve", "ask_material": "clarify_ask"},
                "grants": sorted(self._registration.grants) if self._registration else [],
                "data_policy": sorted(self._registration.data_policy) if self._registration else []}

    def register(self, *, version=VERSION, consumer, requested_grants=(), proposal_provider=None):
        """Register one scheduling-only callback. Worker completion calls submit().

        proposal_provider is reserved for registration parity: proposals are never polled
        on ordinary safe points and no hidden model turn is created. It must be omitted.
        """
        if version != VERSION:
            raise ValueError("unsupported_supervision_version")
        if proposal_provider is not None:
            raise ValueError("use_submit_not_polling_provider")
        if not callable(consumer):
            raise TypeError("consumer_required")
        context = self._context
        scope = context._manager.scope_key
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_loader import _plugin_home_scope
        with _plugin_home_scope(Path(scope)):
            config = load_config_readonly() or {}
        config = config.get("supervision", {})
        entries = config.get("plugins", {}) if isinstance(config, dict) and config.get("enabled") is True else {}
        policy = entries.get(context.plugin_id, {}) if isinstance(entries, dict) else {}
        allowed = policy.get("grants", []) if isinstance(policy, dict) else []
        data_policy = policy.get("data_policy", []) if isinstance(policy, dict) else []
        if not isinstance(allowed, list) or not all(type(x) is str for x in allowed):
            allowed = []
        if not isinstance(data_policy, list) or not all(type(x) is str for x in data_policy):
            data_policy = []
        known = {"observe", *(a.value for a in Action)}
        if isinstance(requested_grants, str) or not set(requested_grants) <= known:
            raise ValueError("unknown_grant")
        grants = frozenset(allowed).intersection(requested_grants)
        # This is host profile policy, NOT ctx.get_config (plugin-owned settings).
        # Invalid/foreign policy denies egress; local observation grants remain independent.
        try:
            egress = field_data_policy(policy.get("egress_policy", {}), profile=scope)
        except (AttributeError, ValueError):
            egress = field_data_policy({}, profile=scope)
        reg = _Registration(context.plugin_id, scope, uuid.uuid4().hex, consumer,
                            grants, frozenset(data_policy), egress_policy=egress)
        with _lock:
            previous = _registry.get((scope, context.plugin_id))
            if previous:
                previous.close()
            _registry[(scope, context.plugin_id)] = reg
            self._registration = reg
        context._manager._track_registration(context.manifest, "supervision", context.plugin_id, reg.close)
        return {"version": VERSION, "generation": reg.generation, "grants": sorted(grants),
                "data_policy": sorted(reg.data_policy), "active": bool(grants)}

    def unregister(self):
        if self._registration:
            self._registration.close()

    def submit(self, proposal):
        """Return queued/terminal receipt. Mapping input is strictly converted, never authority."""
        reg = self._registration
        if reg is None or not reg.active:
            return {"status": "rejected", "reason": "unregistered"}
        try:
            if isinstance(proposal, dict):
                data = dict(proposal)
                data["expected"] = Revision(**data["expected"])
                proposal = InterventionProposalV1(**data)
            if not isinstance(proposal, InterventionProposalV1):
                raise TypeError("invalid_proposal")
        except (ValueError, KeyError, TypeError):
            return {"status": "rejected", "reason": "invalid_proposal"}
        if proposal.expected.profile != reg.scope:
            return {"status": "rejected", "reason": "scope_mismatch"}
        from agent.supervision_policy import runtime_for_revision
        runtime = runtime_for_revision(proposal.expected)
        if runtime is None:
            return {"status": "stale", "reason": "work_missing"}
        return project(runtime.submit(proposal, reg))

    def receipt(self, revision, proposal_id):
        from agent.supervision_policy import runtime_for_revision
        if isinstance(revision, dict):
            revision = Revision(**revision)
        reg = self._registration
        if reg is None or revision.profile != reg.scope:
            return None
        runtime = runtime_for_revision(revision)
        if runtime is None:
            return None
        with runtime.lock:
            value = runtime.receipts.get(proposal_id)
            return project(value) if value else None

    async def acquire_skill_details(self, request):
        """Bound, selected local skill reads serviced on the execution owner."""
        reg = self._registration
        if reg is None or not reg.active or not isinstance(request, dict):
            return None
        try:
            expected = Revision(**request['expected'])
        except (KeyError, TypeError, ValueError):
            return None
        if expected.profile != reg.scope:
            return None
        from agent.supervision_policy import runtime_for_revision
        runtime = runtime_for_revision(expected)
        binding = getattr(runtime.agent(), '_supervision_view_binding', None) if runtime else None
        return await binding.acquire_skill_details(request, reg) if binding else None

    def _active_runtime(self):
        from agent.subagent_lifecycle import get_active_subagent_parent
        from agent.supervision_policy import runtime_for_agent
        agent = get_active_subagent_parent()
        return runtime_for_agent(agent) if agent is not None else None

    def current_revision(self):
        runtime = self._active_runtime()
        return project(runtime.revision) if runtime else None

    def drain_at_safe_point(self):
        runtime = self._active_runtime()
        return runtime.drain_at_safe_point() if runtime else ()

    def prepare_action(self, *, tool_name, arguments, tool_call_id):
        runtime = self._active_runtime()
        return runtime.prepare_action(tool_name, arguments, tool_call_id) if runtime else None

    def prepare_final(self, candidate):
        runtime = self._active_runtime()
        return runtime.prepare_final(candidate) if runtime else None

    def _owner(self, action, request):
        if not isinstance(request, OwnerRequestV1):
            raise TypeError("owner_request_type")
        from agent.supervision_policy import runtime_for_revision
        runtime = runtime_for_revision(request.revision)
        # Owner scope must also match the calling facade; a copied candidate cannot cross profiles.
        if runtime is None or request.revision.profile != self._context._manager.scope_key:
            return OwnerDecisionV1(tuple(c["id"] for c in request.candidates))
        return runtime.owner_decision(action, request)

    def rank_candidates(self, request: OwnerRequestV1) -> OwnerDecisionV1:
        return self._owner(Action.RANK_CANDIDATES, request)

    def select_windows(self, request: OwnerRequestV1) -> OwnerDecisionV1:
        return self._owner(Action.SELECT_WINDOWS, request)

    def evaluate_relation(self, request: OwnerRequestV1) -> OwnerDecisionV1:
        return self._owner(Action.EVALUATE_RELATION, request)
