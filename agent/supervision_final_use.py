"""Native-local final use of selected LCM rows, never publication or egress.

The synchronous accepted-final phase owns one zero-wait point read and canonical
write. It does not renew the shared inference deadline. Selection is transient
provenance; only the provider's fresh one-consumption permission can authorize use.
"""
from dataclasses import dataclass, replace
import copy
import json
import logging
import weakref
from typing import Any

from agent import supervision_planning_records as records
from agent.supervision_context import digest
from agent.supervision_types import project
from agent.supervision_claim_uses import render_record

PURPOSE = "supervision.literal-final-use.v1"
CONSUMER = "native:accepted-final"
POLICY = {"version": PURPOSE, "consumer": CONSUMER, "enabled": True}
logger = logging.getLogger(__name__)


def valid_policy(value):
    return (type(value) is dict and set(value) == set(POLICY)
            and value.get("version") == PURPOSE and value.get("consumer") == CONSUMER
            and value.get("enabled") is True)


def selection_scope(runtime, node=None):
    # Only ordinary evidence bookkeeping is crossed. All other generations stay
    # exact, including catalog/control, even though they are not source bytes.
    scope = (replace(runtime.revision, evidence=0), runtime.session_id)
    if node is not None:
        scope += (digest(json.dumps(node, sort_keys=True, separators=(",", ":"), allow_nan=False)),)
    return scope


@dataclass(eq=False)
class _FinalPhase:
    owner: Any
    runtime: Any
    revision: Any
    engine: Any
    selection_scope: tuple
    text: str
    message: dict
    live: bool = True

    def current(self, consumer):
        rt = self.runtime
        agent = rt.agent()
        return (self.live and consumer is self.owner and consumer.current(rt)
                and rt.revision == self.revision and agent is not None
                and getattr(agent, "context_compressor", None) is self.engine
                and not getattr(agent, "_interrupt_requested", False)
                and getattr(agent, "session_id", None) == rt.session_id
                and self.message.get("role") == "assistant"
                and self.message.get("content") == self.text)


@dataclass(frozen=True)
class _SourceSelection:
    # Provenance only. Deliberately NOT SourcePropositionV1: no publication ref,
    # revision or deadline that could be mistaken for retained v1 authority.
    record: Any
    owner_id: str
    owner_generation: str
    invocation_id: str


@dataclass(eq=False)
class _Selection:
    runtime: object
    scope: tuple
    engine: object
    binding: object
    token: object
    node: dict
    source: _SourceSelection


class NativeFinalUse:
    """One fixed native consumer owned by the existing source registration."""
    def __init__(self, source, policy):
        self.source = source
        self.policy = copy.deepcopy(policy) if valid_policy(policy) else None
        self.selections = {}

    def current(self, rt):
        return (valid_policy(self.policy) and self.source.current(rt)
                and self.source.final_use is self
                and getattr(rt.agent(), "session_id", None) == rt.session_id
                and not getattr(rt.agent(), "_interrupt_requested", False))

    def _release(self, token):
        try:
            self.source.provider.release_final_source(token)
        except Exception:
            # Local authority is retired regardless of optional provider cleanup.
            logger.debug("optional final-use cleanup unavailable")

    def release(self, runtime=None):
        # Caller holds runtime/source locks in the established order.
        for key, item in tuple(self.selections.items()):
            if runtime is None or item.runtime() is runtime:
                self._release(item.token)
                del self.selections[key]

    def capture(self, rt, refs):
        """Called only after ordinary publication, while its old grant is current."""
        source = self.source
        with rt.lock, source.lock:
            if not self.current(rt):
                return
            for key, item in tuple(self.selections.items()):
                old = item.runtime()
                if (old is None or not self.current(old) or item.scope != selection_scope(old, item.node)
                        or source.provider.literal_source_binding(item.engine) != item.binding):
                    self._release(item.token)
                    del self.selections[key]
            uses = rt.dependencies.claim_uses
            uses.graph._load()
            for ref in refs:
                entry = source.records.get(ref)
                if entry is None:
                    continue
                proposition, (engine, source_ref, binding) = entry
                if proposition.revision != rt.revision or rt.clock() >= proposition.deadline:
                    continue
                eligible = [n for n in uses.graph.nodes.values() if uses._eligible(n, proposition)]
                # Ambiguous declarations are not a unique final adoption target.
                if len(eligible) != 1:
                    continue
                node = eligible[0]
                if (node["id"] in self.selections or len(self.selections) >= 64
                        or proposition.revision != rt.revision or rt.clock() >= proposition.deadline):
                    continue
                scope = selection_scope(rt, node)
                token = source.provider.capture_final_source(engine, proposition.invocation_id,
                    source_ref, scope, self)
                if token is not None:
                    self.selections[node["id"]] = _Selection(weakref.ref(rt), scope, engine,
                        binding, token, copy.deepcopy(node), _SourceSelection(proposition.record,
                            proposition.owner_id, proposition.owner_generation, proposition.invocation_id))
                    uses.final_sources.add(self)

    def accept(self, rt, text, message):
        source = self.source
        rt._assert_owner()
        with rt.lock, source.lock:
            if not self.current(rt):
                self.release(rt)
                return
            candidates = [s for s in self.selections.values() if s.runtime() is rt
                and s.scope == selection_scope(rt, s.node) and text == render_record(s.source.record)]
            if len(candidates) != 1:
                self.release(rt)
                return
            selected = candidates[0]
            uses = rt.dependencies.claim_uses
            if not uses._eligible(selected.node, selected.source):
                self.release(rt)
                return
            phase = _FinalPhase(self, rt, rt.revision, selected.engine, selected.scope, text, message)
        permission = None
        try:
            # This is a NEW purpose-bound permission, not resolve_literal_source
            # with a relaxed revision. The provider consumes selected provenance
            # and rereads exactly one original row outside graph/write locks.
            permission = source.provider.authorize_final_source(selected.engine,
                selected.token, selected.scope, self, phase)
            if permission is None:
                return
            with rt.lock, source.lock, source.provider.final_source_fence(permission) as current:
                def guard():
                    return (current and source.provider.final_source_current(permission)
                        and phase.current(self)
                        and self.selections.get(selected.node["id"]) is selected
                        and selection_scope(rt, selected.node) == selected.scope
                        and uses._eligible(selected.node, selected.source, stored=False))
                if not guard():
                    return
                row = {**selected.node, "revision": selected.node["revision"] + 1,
                    "status": "adopted", "final_use": dict(purpose=PURPOSE, consumer=CONSUMER,
                        final_hash=digest(text), final_span=[0, len(text)],
                        final_revision=project(phase.revision),
                        selected_invocation=selected.source.invocation_id,
                        claim_id=selected.node["claim_id"], declaration_revision=selected.node["revision"],
                        source=uses._identity(selected.source))}
                if records.save(rt, [row], validate=guard,
                        predecessors={row["id"]: (selected.node["revision"], selected.node["status"])},
                        predecessor_records={row["id"]: selected.node}):
                    uses.graph.nodes[row["id"]] = row
                    try:
                        from agent.supervision_original_output import adopted
                        adopted(rt.agent(), row)
                    except Exception:
                        logger.debug("optional original-output adoption join unavailable")
        finally:
            phase.live = False
            if permission is not None:
                self._release(permission)
            with rt.lock, source.lock:
                self.release(rt)  # accepted final consumes all selection attempts; no replay


def accepted_final(agent, text, message):
    """Only the native text-final owner calls this, AFTER all continuation gates."""
    from agent.supervision_policy import runtime_for_agent
    from agent.supervision_literal_sources import LiteralSourceRegistration
    rt = runtime_for_agent(agent)
    if rt is None or type(text) is not str:
        return
    engine = getattr(agent, "context_compressor", None)
    source = getattr(getattr(engine, "supervision", None), "_literal_source_registration", None)
    if type(source) is LiteralSourceRegistration:
        source.final_use.accept(rt, text, message)
