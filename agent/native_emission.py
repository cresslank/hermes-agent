"""Private, opt-in native sink observations, NOT claim adoption or durable receipts.

The host installs a bounded scope around ordinary rendering/delivery. Only native
sinks call prepare/succeeded; no plugin grant, model field, returned message id or
transcript can invoke a successful transition. The later canonical claim owner
must join these exact bytes to adoption and persist its own transition. Absence,
capacity, failure, cancellation and unsupported transports all remain unknown.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import threading
import uuid
import weakref

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_OBSERVATIONS = 256


@dataclass(frozen=True)
class EmissionIdentity:
    profile: str
    lineage: str
    work_id: str
    run_generation: int
    turn_id: str

    def __post_init__(self):
        if (type(self.run_generation) is not int or not 0 <= self.run_generation < 2**63
                or any(not isinstance(v, str) or not 0 < len(v) <= 256
                       for v in (self.profile, self.lineage, self.work_id, self.turn_id))):
            raise ValueError("invalid native emission identity")


@dataclass(frozen=True)
class EmittedPayload:
    identity: EmissionIdentity
    observation_id: str
    surface: str
    target: tuple[tuple[str, str], ...]
    transport_generation: str
    operation: str
    message_or_frame_id: str
    payload: bytes
    payload_sha256: str
    # Byte offsets into payload, never offsets into pre-render/model text.
    start: int
    end: int
    ack_kind: str = "native_sink_completed"
    batch_id: str = ""
    segment_index: int = 0
    segment_count: int = 1
    source_sha256: str = ""


class NativeEmissionScope:
    """Host-local callback lease. Bounded metadata; no queue, replay or database.

    `current` is the installing native owner's revision/lifecycle predicate. Close
    on reset, deletion or cancellation. Callbacks must be synchronous/nonblocking;
    their exceptions never change delivery or cause an external send retry.
    """
    def __init__(self, identity: EmissionIdentity, callback, *, current):
        if type(identity) is not EmissionIdentity or not callable(callback) or not callable(current):
            raise TypeError("native owner identity, callback and current predicate required")
        self.identity = identity
        self._callback = callback
        self._current = current
        self._lock = threading.RLock()
        self._remaining = MAX_OBSERVATIONS
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True

    @contextmanager
    def activate(self):
        token = _scope.set(self)
        try:
            yield self
        finally:
            _scope.reset(token)

    def _reserve(self):
        with self._lock:
            if self._closed or self._remaining <= 0 or not self._current():
                return False
            self._remaining -= 1
            return True

    def _publish(self, receipt):
        with self._lock:
            if not self._closed and self._current():
                self._callback(receipt)


_scope: ContextVar[NativeEmissionScope | None] = ContextVar("native_emission_scope", default=None)
_generations = {}
_generation_lock = threading.RLock()
_segment: ContextVar[tuple | None] = ContextVar("native_emission_segment", default=None)
_guards: ContextVar[tuple] = ContextVar("native_emission_guards", default=())


@contextmanager
def guard_emissions(current):
    guards = _guards.get()
    # Excess nesting vetoes observations, not the native operation.
    token = _guards.set((*guards, current) if len(guards) < 4 else (lambda: False,))
    try:
        yield
    finally:
        _guards.reset(token)


@contextmanager
def segment_scope(batch_id, index, count, text):
    # Only splitting owners may supply this manifest; transformed native bytes
    # retain their own hash, so the source hash never certifies equivalence.
    value = None
    if (_scope.get() is not None and 0 <= index < count <= MAX_OBSERVATIONS
            and isinstance(text, str) and len(text) <= MAX_PAYLOAD_BYTES):
        value = (batch_id, index, count, hashlib.sha256(text.encode("utf-8")).hexdigest())
    token = _segment.set(value)
    try:
        yield
    finally:
        _segment.reset(token)


def transport_generation(transport):
    """Object lifetime identity, never a destination label or reusable Python id."""
    if _scope.get() is None:
        return None
    try:
        # WeakKeyDictionary follows __eq__/__hash__; two native SDK clients for
        # one remote account can compare equal while owning distinct transports.
        key = id(transport)
        def retired(ref):
            with _generation_lock:
                entry = _generations.get(key)
                if entry is not None and entry[0] is ref:
                    _generations.pop(key)
        with _generation_lock:
            found = _generations.get(key)
            if found is not None and found[0]() is transport:
                return found[1]
            if len(_generations) >= MAX_OBSERVATIONS:
                return None
            generation = uuid.uuid4().hex
            _generations[key] = (weakref.ref(transport, retired), generation)
            return generation
    except TypeError:  # non-weakrefable embedding sinks have unknown identity
        return None


class _PendingEmission:
    def __init__(self, scope, receipt):
        self.scope, self.receipt = scope, receipt
        self._guards = _guards.get()
        self._settled = False
        self._lock = threading.Lock()

    def succeeded(self, *, message_id=None):
        with self._lock:
            if self._settled:
                return
            self._settled = True
        try:
            if not all(current() for current in self._guards):
                return
            receipt = self.receipt
            if message_id is not None:
                if not isinstance(message_id, str) or not 0 < len(message_id) <= 256:
                    return
                receipt = replace(receipt, message_or_frame_id=message_id)
            self.scope._publish(receipt)
        except Exception:
            # Observing output must neither interrupt nor repeat output.
            return


def prepare(payload, *, surface, target, generation, operation, frame_id=""):
    scope = _scope.get()
    if scope is None or not generation or not isinstance(payload, str):
        return None
    if len(payload) > MAX_PAYLOAD_BYTES:
        return None
    try:
        raw = payload.encode("utf-8")
        target = tuple(target)
        if (len(raw) > MAX_PAYLOAD_BYTES or not 0 < len(target) <= 8
                or any(not isinstance(k, str) or not isinstance(v, str)
                       or len(k) > 64 or len(v) > 512 for k, v in target)
                or any(not isinstance(v, str) or len(v) > 256
                       for v in (surface, generation, operation, frame_id))
                or not scope._reserve()):
            return None
        receipt = EmittedPayload(scope.identity, uuid.uuid4().hex, surface, target,
                                 generation, operation, frame_id or uuid.uuid4().hex,
                                 raw, hashlib.sha256(raw).hexdigest(), 0, len(raw))
        segment = _segment.get()
        if segment is not None:
            receipt = replace(receipt, batch_id=segment[0], segment_index=segment[1],
                              segment_count=segment[2], source_sha256=segment[3])
        return _PendingEmission(scope, receipt)
    except Exception:
        return None


class ObservedTextIO:
    """Forward writes unchanged; publish only fully written AND flushed bytes.

    A local renderer wrapper, not a global stdout replacement. At most one bounded
    payload is held until flush; oversize/multiple failed writes invalidate it.
    """
    def __init__(self, stream, *, surface, operation):
        self._stream = stream
        self._surface, self._operation = surface, operation
        self._generation = transport_generation(stream)
        self._parts = []
        self._size = 0
        self._valid = True

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def write(self, text):
        try:
            written = self._stream.write(text)
        except BaseException:
            self._valid = False
            self._parts.clear()
            raise
        if self._generation and self._valid:
            self._size += len(text)
            if written != len(text) or self._size > MAX_PAYLOAD_BYTES or len(self._parts) >= MAX_OBSERVATIONS:
                self._valid = False
                self._parts.clear()
            elif text:
                self._parts.append(text)
        return written

    def flush(self):
        try:
            result = self._stream.flush()
        except BaseException:
            self._valid = False
            self._parts.clear()
            raise
        if self._valid and self._parts:
            pending = prepare("".join(self._parts), surface=self._surface,
                              target=(("stream", "stdout"),), generation=self._generation,
                              operation=self._operation)
            if pending:
                pending.succeeded()
        self._parts.clear()
        self._size = 0
        return result


def observed_stream(stream, *, surface, operation):
    import io
    # A queue-like stdout proxy can acknowledge enqueue, not write/flush.
    if _scope.get() is None or not isinstance(stream, io.TextIOBase):
        return stream
    return ObservedTextIO(stream, surface=surface, operation=operation)


def exact_emitted_span(receipt, literal: bytes, *, identity, target, generation):
    """Pure exact-byte join for the later canonical owner; no authority/adoption.

    A transformed, repeated or cross-segment literal abstains. Callers must pass
    their independently current native destination/generation and adopted bytes.
    """
    if (type(receipt) is not EmittedPayload or receipt.identity != identity
            or receipt.target != tuple(target) or receipt.transport_generation != generation
            or not isinstance(literal, bytes) or not literal
            or hashlib.sha256(receipt.payload).hexdigest() != receipt.payload_sha256):
        return None
    start = receipt.payload.find(literal)
    # bytes.count ignores overlapping matches (b"aa" occurs twice in b"aaa").
    if start < 0 or receipt.payload.find(literal, start + 1) >= 0:
        return None
    return (start, start + len(literal))


def complete_exact_batch(receipts):
    """Only a complete unchanged split manifest, never IDs/partial_overflow alone."""
    if type(receipts) not in (tuple, list) or not receipts or len(receipts) > MAX_OBSERVATIONS:
        return False
    if any(type(r) is not EmittedPayload for r in receipts):
        return False
    first = receipts[0]
    if type(first) is not EmittedPayload or not first.batch_id:
        return False
    return (len(receipts) == first.segment_count
            and {r.segment_index for r in receipts} == set(range(first.segment_count))
            and all(type(r) is EmittedPayload and r.batch_id == first.batch_id
                    and r.identity == first.identity and r.target == first.target
                    and r.transport_generation == first.transport_generation
                    and r.segment_count == first.segment_count
                    and r.payload_sha256 == r.source_sha256
                    and hashlib.sha256(r.payload).hexdigest() == r.payload_sha256
                    for r in receipts))


def install_for_runtime(runtime, callback):
    """Native owner installation only; deliberately absent from PluginContext.

    The parent claim/delivery owner calls this after selecting a real turn. No
    observe/proposal grant implicitly enables output capture. Revision, turn and
    runtime replacement invalidate the lease without resetting normal output.
    """
    from agent.supervision_policy import SupervisionRuntime
    if type(runtime) is not SupervisionRuntime:
        raise TypeError("native supervision runtime required")
    runtime._assert_owner()
    agent = runtime.agent()
    turn = getattr(agent, "_current_turn_id", None)
    revision = runtime.revision
    if runtime.closed or not turn or getattr(agent, "_supervision_runtime", None) is not runtime:
        raise ValueError("no current native turn")
    agent_ref, runtime_ref = weakref.ref(agent), weakref.ref(runtime)
    def current():
        a, r = agent_ref(), runtime_ref()
        # finish_turn seals reasoning before the caller renders the answer.
        # revoke/reset advance the revision; sealing alone is not sink closure.
        return bool(a is not None and r is not None
                    and r.revision == revision and getattr(a, "_supervision_runtime", None) is r
                    and getattr(a, "_current_turn_id", None) == turn)
    scope = NativeEmissionScope(EmissionIdentity(revision.profile, revision.lineage,
        revision.work_id, revision.run_generation, turn), callback, current=current)
    prior = getattr(runtime, "_native_emission_scope", None)
    if type(prior) is NativeEmissionScope:
        prior.close()
    setattr(runtime, "_native_emission_scope", scope)
    return scope


@contextmanager
def for_agent(agent):
    runtime = getattr(agent, "_supervision_runtime", None)
    scope = getattr(runtime, "_native_emission_scope", None)
    if type(scope) is NativeEmissionScope:
        with scope.activate():
            yield
    elif runtime is not None:
        with restore_context(None):
            yield
    else:
        yield  # preserve an explicit embedding-owner scope, if present


def observe_agent_output(function):
    """Install the native CLI owner's lease at its ordinary rendering boundary."""
    from functools import wraps
    @wraps(function)
    def wrapped(owner, *args, **kwargs):
        with for_agent(getattr(owner, "agent", owner)):
            return function(owner, *args, **kwargs)
    return wrapped


def capture_context():
    """Carry the private lease through an existing native queue, never into JSON."""
    return _scope.get()


def capture_guards():
    return _guards.get()


@contextmanager
def restore_context(scope, *, guards=()):
    token = _scope.set(scope)
    guard_token = _guards.set(guards)
    segment_token = _segment.set(None)
    try:
        yield
    finally:
        _segment.reset(segment_token)
        _guards.reset(guard_token)
        _scope.reset(token)
