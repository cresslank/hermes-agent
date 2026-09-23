"""Plugin-facing supervision.v1 negotiation and owner protocol.

The owner grants are read from profile configuration, not plugin settings or a
request payload. Registration gives no agent, DB handle or cancellation capability.
"""
from __future__ import annotations

from collections.abc import Mapping
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
    mcp_sources: tuple = ()
    mcp_adapter: object = None
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
        from agent.supervision_optional_reads import clear_optional_reads
        clear_optional_reads(self.scope, self.plugin_id)


def registrations_for_scope(scope, *, blocking=True):
    # Effect-edge readers already hold runtime/registration fences. Never wait
    # there for a registry replacement that may itself need a registration fence.
    if not _lock.acquire(blocking=blocking):
        return ()
    try:
        return tuple(r for (s, _), r in _registry.items() if s == scope and r.active)
    finally:
        _lock.release()


class SupervisionFacade:
    version = VERSION

    def __init__(self, context):
        self._context = context
        self._registration = None

    def negotiate(self, version=VERSION):
        capabilities = {"version": VERSION, "supported": version == VERSION,
                        "proposal_metadata": METADATA_VERSION,
                        "skill_details": "supervision.skill-details.v1",
                        "view_actions_version": "supervision.view-actions.v1",
                        "view_actions": {"present_material_once": "present_status",
                                         "retrieve": "clarify_retrieve", "ask_material": "clarify_ask"},
                        "owner_capabilities": self._owner_capabilities() if version == VERSION else [],
                        "dispatch_admission": "supervision.dispatch-admission.v1",
                        "owner_deadline": True,
                        "exact_expansion": "supervision.exact-expansion.v1",
                        "history_read_fence": "supervision.history-read.v1",
                        "owner_consumption": "supervision.owner-consumption.v1",
                        "retrieval_presentation": "supervision.retrieval-presentation.v1",
                        "mcp_results": "supervision.mcp-results.v1",
                        "grants": sorted(self._registration.grants) if self._registration and self._registration.active else [],
                        "data_policy": sorted(self._registration.data_policy) if self._registration and self._registration.active else []}
        if version == VERSION:
            # Support comes from the actual native owner, not an enum alias or a
            # plugin claim. It is NOT authority: register still intersects this
            # facade's profile policy with the separately requested action grant.
            from agent.supervision_efficiency import EfficiencyOwner
            capabilities["receipt_reuse"] = EfficiencyOwner.receipt_reuse_version
            from agent.supervision_children import VERSION as CHILD_RELEVANCE_VERSION
            capabilities['child_relevance'] = CHILD_RELEVANCE_VERSION
            capabilities['direct_control'] = 'supervision.direct-control.v1'
            capabilities["optional_read"] = "supervision.optional-read.v1"
            from agent.supervision_delivery import FINAL_ACTIONS_VERSION, FINAL_ACTIONS
            capabilities["final_actions_version"] = FINAL_ACTIONS_VERSION
            capabilities["final_actions"] = dict(FINAL_ACTIONS)
            from agent.supervision_dependencies import RELATION_VERSION, RELATION_ACTIONS
            capabilities["dependency_relations_version"] = RELATION_VERSION
            capabilities["dependency_relations"] = dict(RELATION_ACTIONS)
            from agent.supervision_claim_uses import VERSION as CLAIM_VERSION, ACTIONS as CLAIM_ACTIONS
            capabilities["claim_contest_version"] = CLAIM_VERSION
            capabilities["claim_contest_actions"] = dict(CLAIM_ACTIONS)
            capabilities["native_verification"] = "hermes.verify-check.v1"
            from agent.supervision_literal_sources import VERSION as LITERAL_SOURCES_VERSION
            capabilities["literal_sources"] = LITERAL_SOURCES_VERSION
            registration = getattr(self, "_literal_source_registration", None)
            capabilities["literal_source_owner"] = bool(registration and registration.current(self._active_runtime()))
        return capabilities

    def register_literal_source_owner(self, *, version, engine, provider):
        from agent.supervision_literal_sources import register
        return register(self, version=version, engine=engine, provider=provider)

    def begin_literal_sources(self, *, version, engine):
        from agent.supervision_literal_sources import VERSION
        registration = getattr(self, "_literal_source_registration", None)
        try:
            return registration.begin(engine) if version == VERSION and registration else None
        except RuntimeError:  # a non-owner thread has no publication authority
            return None

    def publish_literal_sources(self, *, version, invocation, source_refs):
        from agent.supervision_literal_sources import VERSION
        registration = getattr(self, "_literal_source_registration", None)
        refs = registration.publish(invocation, source_refs) if version == VERSION and registration else ()
        runtime = self._active_runtime()
        if refs and runtime is not None:
            try:
                registration.final_use.capture(runtime, refs)
            except Exception:
                # Optional local capture cannot fail an already completed source read.
                pass
            try:
                runtime.dependencies.claim_uses.published(registration, refs)
            except Exception:
                # An optional contest failure cannot invalidate this completed
                # source read. Do not retry or infer whether an effect committed.
                return refs
        return refs

    def cancel_literal_sources(self, *, version, invocation):
        from agent.supervision_literal_sources import VERSION, LiteralSourceInvocationV1
        registration = getattr(self, "_literal_source_registration", None)
        if version != VERSION or registration is None or not isinstance(invocation, LiteralSourceInvocationV1):
            return
        with registration.lock:
            if registration.pending.get(invocation.id) is invocation:
                del registration.pending[invocation.id]
                registration.provider.release_literal_sources(invocation.id)

    def literal_source(self, *, version, ref):
        from agent.supervision_literal_sources import VERSION, lookup_literal_source
        runtime = self._active_runtime()
        if version != VERSION or runtime is None:
            return None
        return lookup_literal_source(runtime, ref, self._registration)

    def _owner_capabilities(self):
        from agent.supervision_owner_protocol import OWNERS, LOCAL_CLASSES
        owner = OWNERS.get(self._context.plugin_id)
        if owner is None:
            return []
        scope = self._context._manager.scope_key
        runtime = self._active_runtime()
        if runtime is None or runtime.closed or runtime.revision.profile != scope:
            return []
        methods = {"rank_candidates", "expand_one_owned_ref" if owner == "lcm" else "select_windows"}
        return sorted({method for reg in registrations_for_scope(scope)
                       if "observe" in reg.grants and LOCAL_CLASSES[owner] in reg.data_policy
                       and reg.egress_policy
                       for method in methods if method in reg.grants})

    def register(self, *, version=VERSION, consumer, requested_grants=(), proposal_provider=None, mcp_adapter=None):
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
        from agent.supervision_mcp import source_grants
        reg.mcp_sources = source_grants(policy.get("mcp_sources") if isinstance(policy, dict) else None)
        reg.mcp_adapter = mcp_adapter if callable(mcp_adapter) else None
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
        source_registration = getattr(self, "_literal_source_registration", None)
        if source_registration is not None:
            source_registration.close()
        if self._registration:
            self._registration.close()

    def admit_dispatch(self, request):
        """Consume a live host-issued capability immediately before one send."""
        from agent.supervision_dispatch import consume
        return consume(self, request)

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
        from agent.supervision_dispatch import VERSION as DISPATCH_VERSION, issue_skill_details
        dispatch = request.get("dispatch_admission") == DISPATCH_VERSION
        source_request = {k: v for k, v in request.items() if k != "dispatch_admission"} if dispatch else request
        reply = await binding.acquire_skill_details(source_request, reg) if binding else None
        if reply is not None and dispatch:
            capability = issue_skill_details(runtime, reg, source_request, reply)
            return {**reply, "dispatch_admission": DISPATCH_VERSION, "dispatch_capability": capability}
        return reply

    def _active_runtime(self):
        from agent.subagent_lifecycle import get_active_subagent_parent
        from agent.supervision_policy import runtime_for_agent
        agent = get_active_subagent_parent()
        return runtime_for_agent(agent) if agent is not None else None

    def read_optional_context(self, request):
        """Read an explicitly profile-authorized supplemental local source.

        This is not main tool dispatch. Missing permission, budget or a current
        work-map link returns None without reading. No source can grant itself
        optionality; the profile policy and native file owner decide.
        """
        from agent.supervision_optional_reads import read_optional_context
        return read_optional_context(self, request)

    def request_verification_checks(self, *, session_id, changed_paths):
        """Queue a bounded native verification request; hook workers never execute it."""
        from agent.verify.native_checks import request_configured_checks
        return request_configured_checks(self, session_id=session_id, changed_paths=changed_paths)

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
        if isinstance(request, Mapping):
            from agent.supervision_owner_protocol import decode_request, encode_decision
            if action.value not in self._owner_capabilities():
                return None
            runtime = self._active_runtime()
            if runtime is None:
                return None
            try:
                runtime._assert_owner(tool_worker=True)
                with runtime.decision_boundary():
                    typed = decode_request(action, request, plugin_id=self._context.plugin_id, runtime=runtime)
                    decision = runtime.owner_decision(action, typed)
                encoded = encode_decision(action, request["request_id"], typed, decision)
                if encoded is None and decision.selected:
                    runtime.acknowledge_owner(typed.target_id, decision.receipt_id, decision.candidate_ids, None)
                return encoded
            except (ValueError, TypeError, KeyError, RuntimeError):
                return None
        if not isinstance(request, OwnerRequestV1):
            raise TypeError("owner_request_type")
        from agent.supervision_policy import runtime_for_revision
        runtime = runtime_for_revision(request.revision)
        # Owner scope must also match the calling facade; a copied candidate cannot cross profiles.
        if runtime is None or request.revision.profile != self._context._manager.scope_key:
            return OwnerDecisionV1(tuple(c["id"] for c in request.candidates))
        with runtime.decision_boundary():
            return runtime.owner_decision(action, request)

    def acknowledge_owner(self, acknowledgment):
        from agent.supervision_owner_protocol import OWNERS
        import re
        owner = OWNERS.get(self._context.plugin_id)
        runtime = self._active_runtime()
        if owner is None or runtime is None or runtime.revision.profile != self._context._manager.scope_key:
            return None
        if (not isinstance(acknowledgment, Mapping) or set(acknowledgment) !=
                {"request_id", "receipt_id", "candidate_ids", "effect_digest"}):
            return None
        digest = acknowledgment["effect_digest"]
        if digest is not None and (type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            return None
        if (type(acknowledgment["request_id"]) is not str or
                type(acknowledgment["receipt_id"]) is not str or
                not isinstance(acknowledgment["candidate_ids"], (list, tuple))):
            return None
        receipt = runtime.acknowledge_owner(owner + ":" + acknowledgment["request_id"],
            acknowledgment["receipt_id"], acknowledgment["candidate_ids"], digest)
        return project(receipt) if receipt else None

    def begin_history_expansion(self, selection):
        from agent.supervision_history_read import begin_history_expansion
        return begin_history_expansion(self, selection)

    def history_source_visibility(self, ref, excerpt):
        if self._context.plugin_id != "hermes-lcm":
            return None
        runtime = self._active_runtime()
        if runtime is None or runtime.revision.profile != self._context._manager.scope_key:
            return None
        runtime._assert_owner(tool_worker=True)
        from agent.supervision_history import source_visibility
        return source_visibility(runtime, ref, excerpt)

    def history_source_absent(self, ref, excerpt):
        if self._context.plugin_id != "hermes-lcm":
            return None
        runtime = self._active_runtime()
        if runtime is None or runtime.revision.profile != self._context._manager.scope_key:
            return None
        runtime._assert_owner(tool_worker=True)
        from agent.supervision_history import source_absent
        return source_absent(runtime, ref, excerpt)

    def expand_one_owned_ref(self, request):
        return self._owner(Action.EXPAND_ONE_OWNED_REF, request)

    def rank_candidates(self, request: OwnerRequestV1 | Mapping) -> OwnerDecisionV1 | dict | None:
        return self._owner(Action.RANK_CANDIDATES, request)

    def select_windows(self, request: OwnerRequestV1 | Mapping) -> OwnerDecisionV1 | dict | None:
        return self._owner(Action.SELECT_WINDOWS, request)

    def evaluate_relation(self, request: OwnerRequestV1 | Mapping) -> OwnerDecisionV1 | dict | None:
        return self._owner(Action.EVALUATE_RELATION, request)
