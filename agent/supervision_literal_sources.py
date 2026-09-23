"""Native literal-source publication, not assertion truth or claim adoption.

Authority is the registered context-engine owner plus a one-shot native invocation,
not a callable attribute, a protocol string, or a copied tool response. Local only:
this API neither schedules inference nor grants provider egress. Python plugins
are trusted in-process code; this is not a sandbox against arbitrary Python access.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import threading
import uuid

from agent.supervision_types import Revision

VERSION = "supervision.literal-sources.v1"
SCHEMA = "lcm.literal-record.v1"
WORKING_PREMISE_VERSION = "supervision.working-premise.v1"
MAX_RECORDS = 64
MAX_PUBLICATION = 8


@dataclass(frozen=True)
class LiteralSourceRecordV1:
    schema_id: str
    exact_ref: str
    row_hash: str
    record_span: tuple[int, int]
    source_bytes: bytes
    coordinate_pins: tuple[tuple[str, str], ...]
    source_attribution: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SourcePropositionV1:
    ref: str
    owner_id: str
    owner_generation: str
    invocation_id: str
    revision: Revision
    deadline: float
    record: LiteralSourceRecordV1


class LiteralSourceProviderV1(ABC):
    """Explicit owner protocol implemented by the registered native LCM adapter."""
    @abstractmethod
    def owns_engine(self, engine) -> bool:
        pass

    def literal_source_binding(self, engine):
        """Opaque native lifecycle identity, or None when unavailable.

        Older adapters abstain until they implement the coordinated fence.
        Neither method may read source rows or acquire host graph locks.
        """
        return None

    @contextmanager
    def literal_source_binding_fence(self, engine, binding):
        """Hold lifecycle invalidation out through the host's final acceptance."""
        yield False

    def capture_final_source(self, engine, invocation_id, source_ref, scope, consumer):
        """Optional distinct native-local purpose. Old providers default to denial."""
        return None

    def authorize_final_source(self, engine, selection, scope, consumer, phase):
        return None

    @contextmanager
    def final_source_fence(self, permission):
        yield False

    def final_source_current(self, permission):
        return False

    def release_final_source(self, token):
        pass

    def release_correction_sources(self):
        """Withdraw transient correction custody; retained rows grant no replay."""
        pass

    @abstractmethod
    def resolve_literal_source(self, engine, invocation_id: str, source_ref: str) -> LiteralSourceRecordV1 | None:
        """Revalidate a retained immutable record against its exact native row only."""

    def working_literal_source(self, engine, exact_ref):
        """Read one already-selected whole record for a current retrieval use.

        Separate from v1 publication lookup: never renew or publish old handles.
        Older native owners abstain. No search, inference, or graph locks here.
        """
        return None

    @abstractmethod
    def release_literal_sources(self, invocation_id: str) -> None:
        pass


@dataclass(frozen=True, eq=False)
class LiteralSourceInvocationV1:
    id: str
    revision: Revision
    deadline: float
    engine: object
    binding: object


class LiteralSourceRegistration:
    def __init__(self, facade, engine, provider, recipients, final_policy=None, *, working_recipients=frozenset(), correction_policy=None, correction_recipients=frozenset()):
        self.facade, self.engine, self.provider = facade, engine, provider
        self.scope = facade._context._manager.scope_key
        self.generation = uuid.uuid4().hex
        self.recipients = recipients
        self.working_recipients = working_recipients
        self.correction_policy = correction_policy
        self.correction_recipients = correction_recipients
        self.active = True
        self.lock = threading.RLock()
        self.pending = {}
        self.records = {}
        from agent.supervision_final_use import NativeFinalUse
        self.final_use = NativeFinalUse(self, final_policy)

    def close(self):
        with self.lock:
            self.active = False
            self.final_use.release()
            self.provider.release_correction_sources()
            for invocation in {p.invocation_id for p, _ in self.records.values()} | set(self.pending):
                self.provider.release_literal_sources(invocation)
            self.pending.clear()
            self.records.clear()

    def current(self, runtime):
        context = self.facade._context
        return (self.active and getattr(self.facade, "_literal_source_registration", None) is self
                and context.plugin_id == "hermes-lcm"
                and context._manager._context_engine is self.engine
                and runtime is not None and not runtime.closed and runtime.revision.profile == self.scope
                and self.provider.owns_engine(getattr(runtime.agent(), "context_compressor", None)))

    def begin(self, engine):
        runtime = self.facade._active_runtime()
        if runtime is None:
            return None
        with runtime.lock, self.lock:
            if not self.current(runtime) or not self.provider.owns_engine(engine):
                return None
            # The real per-agent engine must be the caller, not another clone or
            # a prototype selected by a plugin-supplied payload.
            if getattr(runtime.agent(), "context_compressor", None) is not engine:
                return None
            runtime._assert_owner(tool_worker=True)
            self._prune(runtime)
            if len(self.pending) >= 8 or len(self.records) >= MAX_RECORDS:
                return None
            deadline = runtime.shared_deadline()
            if deadline <= runtime.clock():
                return None
            binding = self.provider.literal_source_binding(engine)
            if binding is None:
                return None
            invocation = LiteralSourceInvocationV1(uuid.uuid4().hex, runtime.revision, deadline, engine, binding)
            self.pending[invocation.id] = invocation
            return invocation

    def _prune(self, runtime):
        removed = set()
        for ref, (record, (engine, _, binding)) in tuple(self.records.items()):
            if (record.revision != runtime.revision or record.deadline <= runtime.clock()
                    or self.provider.literal_source_binding(engine) != binding):
                removed.add(record.invocation_id)
                del self.records[ref]
        for key, invocation in tuple(self.pending.items()):
            if (invocation.revision != runtime.revision or invocation.deadline <= runtime.clock()
                    or self.provider.literal_source_binding(invocation.engine) != invocation.binding):
                removed.add(key)
                del self.pending[key]
        for key in removed:
            self.provider.release_literal_sources(key)

    def publish(self, invocation, source_refs):
        runtime = self.facade._active_runtime()
        with self.lock:
            if (not isinstance(invocation, LiteralSourceInvocationV1)
                    or self.pending.get(invocation.id) is not invocation):
                return ()
            del self.pending[invocation.id]  # one attempt; failures cannot replay
        try:
            if (not self.current(runtime) or runtime.revision != invocation.revision
                    or runtime.clock() >= invocation.deadline
                    or getattr(runtime.agent(), "context_compressor", None) is not invocation.engine
                    or not self.provider.owns_engine(invocation.engine)
                    or type(source_refs) is not tuple or not 1 <= len(source_refs) <= MAX_PUBLICATION
                    or any(type(r) is not str or len(r) > 128 for r in source_refs)
                    or len(set(source_refs)) != len(source_refs)
                    or len(self.records) + len(source_refs) > MAX_RECORDS):
                return ()
            runtime._assert_owner(tool_worker=True)
            records = []
            for ref in source_refs:
                record = self.provider.resolve_literal_source(invocation.engine, invocation.id, ref)
                if not _valid_record(record):
                    return ()
                records.append(record)
            # Source reads happen before the final native revision/deadline fence.
            with runtime.lock, self.lock, self.provider.literal_source_binding_fence(
                    invocation.engine, invocation.binding) as binding_current:
                if (not binding_current or not self.current(runtime) or runtime.revision != invocation.revision
                        or runtime.clock() >= invocation.deadline
                        or getattr(runtime.agent(), "context_compressor", None) is not invocation.engine
                        or not self.provider.owns_engine(invocation.engine)
                        or len(self.records) + len(source_refs) > MAX_RECORDS):
                    return ()
                published = []
                for source_ref, record in zip(source_refs, records):
                    ref = "literal:" + uuid.uuid4().hex
                    proposition = SourcePropositionV1(ref, "hermes-lcm", self.generation,
                        invocation.id, invocation.revision, invocation.deadline, record)
                    self.records[ref] = (proposition, (invocation.engine, source_ref, invocation.binding))
                    published.append(ref)
                return tuple(published)
        finally:
            with self.lock:
                if not any(p.invocation_id == invocation.id for p, _ in self.records.values()):
                    self.provider.release_literal_sources(invocation.id)

    def lookup(self, runtime, ref, recipient):
        """Consumption gate. Every lookup rechecks *this* recipient and source owner."""
        from agent.supervision_facade import registrations_for_scope
        with self.lock:
            if (not self.current(runtime) or recipient is None
                    or not any(r is recipient for r in registrations_for_scope(self.scope))
                    or not recipient.active or recipient.scope != self.scope
                    or recipient.plugin_id not in self.recipients
                    or "observe" not in recipient.grants or "history_excerpt" not in recipient.data_policy):
                return None
            self._prune(runtime)
            item = self.records.get(ref) if type(ref) is str else None
            if item is None:
                return None
            proposition, (engine, source_ref, binding) = item
            if getattr(runtime.agent(), "context_compressor", None) is not engine or not self.provider.owns_engine(engine):
                return None
        record = self.provider.resolve_literal_source(engine, proposition.invocation_id, source_ref)
        with runtime.lock, self.lock, recipient.fence, self.provider.literal_source_binding_fence(
                engine, binding) as binding_current:
            if (not binding_current or self.records.get(ref) is not item or record != proposition.record or not self.current(runtime)
                    or runtime.revision != proposition.revision or runtime.clock() >= proposition.deadline
                    or getattr(runtime.agent(), "context_compressor", None) is not engine
                    or not self.provider.owns_engine(engine)
                    # Registry replacement irreversibly closes the old recipient
                    # under this fence. Do not acquire the registry lock here:
                    # registration replacement takes registry -> recipient.
                    or not recipient.active
                    or "observe" not in recipient.grants or "history_excerpt" not in recipient.data_policy
                    or recipient.plugin_id not in self.recipients):
                self.records.pop(ref, None)
                if not any(p.invocation_id == proposition.invocation_id for p, _ in self.records.values()):
                    self.provider.release_literal_sources(proposition.invocation_id)
                return None
            return proposition


def _valid_record(record):
    # Semantics come from the authenticated validator, not this shape check.
    return (type(record) is LiteralSourceRecordV1 and record.schema_id == SCHEMA
            and type(record.source_bytes) is bytes and 0 < len(record.source_bytes) <= 2400
            and type(record.exact_ref) is str and len(record.exact_ref) <= 128
            and type(record.row_hash) is str and len(record.row_hash) == 64
            and type(record.record_span) is tuple and len(record.record_span) == 2
            and record.record_span == (0, len(record.source_bytes.decode("utf-8")))
            and type(record.coordinate_pins) is tuple and len(record.coordinate_pins) == 8
            and type(record.source_attribution) is tuple and len(record.source_attribution) <= 16
            and all(type(pair) is tuple and len(pair) == 2 and all(type(x) is str for x in pair)
                    for pair in record.coordinate_pins + record.source_attribution))


def register(facade, *, version, engine, provider):
    from hermes_cli.plugins import PluginContext
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_loader import _plugin_home_scope
    context = facade._context
    if (version != VERSION or not isinstance(context, PluginContext)
            or context.plugin_id != "hermes-lcm" or context._manager._context_engine is not engine
            or not isinstance(provider, LiteralSourceProviderV1) or not provider.owns_engine(engine)):
        return None
    with _plugin_home_scope(Path(context._manager.scope_key)):
        config = (load_config_readonly() or {}).get("supervision", {})
    entries = config.get("plugins", {}) if isinstance(config, dict) and config.get("enabled") is True else {}
    policy = entries.get(context.plugin_id, {}) if isinstance(entries, dict) else {}
    grant = policy.get("literal_sources", {}) if isinstance(policy, dict) else {}
    recipients = grant.get("recipients") if isinstance(grant, dict) and grant.get("version") == VERSION else None
    if (not isinstance(recipients, list) or not 1 <= len(recipients) <= 16
            or any(type(r) is not str or not 0 < len(r) <= 128 for r in recipients)):
        return None
    working = policy.get("working_premises", {})
    working_recipients = working.get("recipients", []) if isinstance(working, dict) and working.get("version") == WORKING_PREMISE_VERSION else []
    if (not isinstance(working_recipients, list) or len(working_recipients) > 16
            or any(type(r) is not str or not 0 < len(r) <= 128 for r in working_recipients)):
        return None
    semantic = grant.get("correction_semantic", {})
    correction_recipients = semantic.get("recipients", []) if isinstance(semantic, dict) and semantic.get("version") == "supervision.literal-correction.v1" else []
    if (not isinstance(correction_recipients, list) or len(correction_recipients) > 16
            or any(type(r) is not str or not 0 < len(r) <= 128 for r in correction_recipients)):
        return None
    old = getattr(facade, "_literal_source_registration", None)
    if old is not None:
        old.close()
    registration = LiteralSourceRegistration(facade, engine, provider, frozenset(recipients),
        final_policy=grant.get("final_use"), working_recipients=frozenset(working_recipients),
        correction_policy=grant.get("correction_review"), correction_recipients=frozenset(correction_recipients))
    facade._literal_source_registration = registration
    context._manager._track_registration(context.manifest, "literal_sources", context.plugin_id, registration.close)
    return registration


def lookup_literal_source(runtime, ref, recipient):
    """Native join API; recipient is the actual active registration, never its name."""
    engine = getattr(runtime.agent(), "context_compressor", None)
    # The facade is only a route. Authority is rechecked against the actual
    # registered manager engine, registered provider, active engine and recipient.
    facade = getattr(engine, "supervision", None)
    registration = getattr(facade, "_literal_source_registration", None)
    if type(registration) is not LiteralSourceRegistration:
        return None
    return registration.lookup(runtime, ref, recipient)
