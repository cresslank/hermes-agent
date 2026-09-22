"""Host-owned launch/control authority, independent of supervisor providers.

The host installs an owner with an explicit profile grant, trusted consumer resolver,
immutable capability policy, and authoritative meaningful-revision provider. Model
``supervision`` dictionaries are requests, never attestations. Plugin views contain
only immutable scoped handles and bounded facts. Persistence is mandatory.

ControlStore protocol: create(snapshot), read(child_id), compare_and_swap(snapshot,
expected_revision). All transitions share one owner RLock and durable CAS; no lock
spans tool execution or inference. Restart does NOT restore control authority: a
new owner must not adopt an unsandboxed live worker from stored prose.
"""
from __future__ import annotations

import copy
import dataclasses
import math
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from agent.interrupt_compat import request_hard_interrupt


class ControlDenied(ValueError):
    pass


@contextmanager
def control_fence(lock, deadline):
    """One original action budget for every lock, with no fresh timeout."""
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ControlDenied('A finite propagated monotonic deadline is required')
    remaining = max(0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise ControlDenied('Delegation action deadline expired')
    try:
        # The consumer still checks expiry before effects; an uncontended expired
        # request may return its historical receipt without acquiring a new budget.
        yield
    finally:
        lock.release()


@dataclasses.dataclass(frozen=True)
class OwnerGrant:
    profile: str
    plugin_id: str
    allow_optional_readonly: bool = False
    controls_all_consumers: bool = False

    def __post_init__(self):
        if (not self.profile or not self.plugin_id or type(self.allow_optional_readonly) is not bool or
                type(self.controls_all_consumers) is not bool):
            raise ControlDenied('Owner grants require explicit profile policy booleans')


@dataclasses.dataclass(frozen=True)
class Consumer:
    ref: str
    obligation: str = 'unknown'
    requires_result: bool = False
    requires_effects: bool = False
    requires_cleanup: bool = False
    requirement_ids: tuple[str, ...] = ()
    requires_corroboration: bool = False

    def __post_init__(self):
        if not isinstance(self.ref, str) or not 1 <= len(self.ref) <= 256 or self.obligation not in {'required', 'optional', 'unknown'}:
            raise ControlDenied('Invalid consumer')
        if any(type(v) is not bool for v in (self.requires_result, self.requires_effects, self.requires_cleanup, self.requires_corroboration)):
            raise ControlDenied('Invalid consumer obligations')
        if (not isinstance(self.requirement_ids, tuple) or len(self.requirement_ids) > 4
                or len(set(self.requirement_ids)) != len(self.requirement_ids)
                or any(not isinstance(r, str) or not 0 < len(r) <= 256 for r in self.requirement_ids)):
            raise ControlDenied('Invalid consumer requirement links')


@dataclasses.dataclass(frozen=True)
class CapabilityContract:
    tool_name: str
    version: str
    validate: Callable[[dict], bool] = dataclasses.field(repr=False, compare=False)


# A generic command/program/browser script cannot be attested by supplying a
# validator. Nested delegation has a separate owner-controlled transition below.
_FORBIDDEN = frozenset({'terminal', 'execute_code', 'browser_console', 'computer_use',
                        'tool_call', 'tool_describe', 'hermes_tool_search', 'delegate_task'})


@dataclasses.dataclass(frozen=True)
class ReadOnlyPolicy:
    policy_id: str
    contracts: tuple[CapabilityContract, ...]

    def __post_init__(self):
        if not isinstance(self.contracts, tuple) or any(not isinstance(c, CapabilityContract) for c in self.contracts):
            raise ControlDenied('Capability contracts must be an immutable tuple')
        names = [c.tool_name for c in self.contracts]
        if (not self.policy_id or len(names) != len(set(names)) or
                any(not c.version or not callable(c.validate) or c.tool_name in _FORBIDDEN
                    for c in self.contracts)):
            raise ControlDenied('Read-only policy requires versioned, non-opaque contracts')

    def permits(self, name, args):
        return any(c.tool_name == name and c.validate(copy.deepcopy(args)) is True for c in self.contracts)


def scoped_file_policy(roots: tuple[str, ...]) -> ReadOnlyPolicy:
    """Local read_file only: strict parameters and resolved resource roots.

    The owner must supply trusted immutable/read-only resource roots; this is not
    an OS sandbox against another process concurrently replacing filesystem paths.
    """
    paths = tuple(Path(p).resolve(strict=True) for p in roots)
    if not paths:
        raise ControlDenied('At least one resource root is required')

    def valid(args):
        if not isinstance(args, dict) or set(args) - {'path', 'offset', 'limit'}:
            return False
        if not isinstance(args.get('path'), str):
            return False
        if any(type(args.get(k, 1)) is not int or args.get(k, 1) < 1 for k in ('offset', 'limit')):
            return False
        if args.get('limit', 2000) > 2000:
            return False
        try:
            path = Path(args['path']).resolve(strict=True)
            return path.is_file() and any(path == root or root in path.parents for root in paths)
        except (OSError, ValueError):
            return False
    return ReadOnlyPolicy('host.scoped-file-read.v1', (CapabilityContract('read_file', '1', valid),))


SUPERVISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': 'Proposed launch controls, validated by the host. Missing metadata stays unknown.',
    'properties': {
        'obligation': {'type': 'string', 'enum': ['required', 'optional', 'unknown']},
        'consumer_refs': {'type': 'array', 'maxItems': 64, 'uniqueItems': True, 'items': {'type': 'string'}},
        'consumer_set_closed': {'type': 'boolean'},
        'effect_policy_id': {'type': 'string'},
    },
    'required': ['obligation', 'consumer_refs', 'consumer_set_closed', 'effect_policy_id'],
}


def validate_request(request):
    if request is None:
        return None
    if not isinstance(request, dict) or set(request) != set(SUPERVISION_SCHEMA['required']):
        raise ControlDenied('Malformed supervision request')
    refs = request['consumer_refs']
    if (request['obligation'] not in ('required', 'optional', 'unknown') or
            type(request['consumer_set_closed']) is not bool or
            not isinstance(request['effect_policy_id'], str) or
            not isinstance(refs, list) or len(refs) > 64 or
            any(not isinstance(r, str) or not r or len(r) > 256 for r in refs) or
            len(set(refs)) != len(refs)):
        raise ControlDenied('Malformed supervision request')
    return copy.deepcopy(request)


@dataclasses.dataclass(frozen=True)
class OwnedHandle:
    child_id: str
    generation: str
    parent_session_id: str
    profile: str
    plugin_id: str


@dataclasses.dataclass(frozen=True)
class CancelReceipt:
    accepted: bool
    reason: str
    control_revision: int
    cancel_requested: bool
    settled: bool
    processes_stopped: bool
    effects_reconciled: bool


@dataclasses.dataclass(frozen=True)
class SemanticEvidence:
    """Host-validated F01 evidence. F04 must supply this exact same gate input.

    revision is (instruction_event, requirements_revision, evidence_revision),
    compared to the host revision provider, not arbitrary plugin call counters.
    contribution probabilities must cover ALL active consumer refs exactly.
    """
    revision: tuple[int, int, int]
    contributions: tuple[tuple[str, float], ...]
    relation: str
    probability: float
    confidence: float


@dataclasses.dataclass
class _Live:
    handle: OwnedHandle
    child: object
    snapshot: dict
    parent_child_id: str | None = None
    milestone_pending: bool = False
    completed_dispatches: set[str] = dataclasses.field(default_factory=set)
    completed_handoffs: set[str] = dataclasses.field(default_factory=set)
    finish_pending: bool = False
    finalization_failed: bool = False


class OwnedDelegationOwner:
    def __init__(self, *, parent_session_id: str, store, grant: OwnerGrant,
                 consumer_resolver: Callable[[str], Consumer | None], policy: ReadOnlyPolicy,
                 revision_provider: Callable[[], tuple[int, int, int]], consumer_inventory=None):
        if not parent_session_id or not isinstance(grant, OwnerGrant) or not isinstance(policy, ReadOnlyPolicy):
            raise ControlDenied('Invalid owner configuration')
        self.parent_session_id = parent_session_id
        self._store, self._grant, self._resolve = store, grant, consumer_resolver
        self._policy, self._revision = policy, revision_provider
        self._consumer_inventory = consumer_inventory
        self._lock = threading.RLock()
        self._live: dict[str, _Live] = {}

    def _commit(self, live, change, *, deadline=None, finalizer=False):
        if live.finalization_failed:
            raise ControlDenied('Owned lifecycle outcome is unknown')
        old = live.snapshot
        new = copy.deepcopy(old)
        change(new)
        if new.get('candidate') is None:
            new['semantic_observation'] = None
        new['control_revision'] = old['control_revision'] + 1
        if finalizer:
            commit = getattr(self._store, 'compare_and_swap_finalizer', self._store.compare_and_swap)
            commit(new, old['control_revision'])
        elif deadline is None:
            self._store.compare_and_swap(new, old['control_revision'])
        else:
            self._store.compare_and_swap(new, old['control_revision'], deadline=deadline)
        live.snapshot = new
        return new

    def _get(self, handle):
        if not isinstance(handle, OwnedHandle):
            raise ControlDenied('Invalid owned handle')
        live = self._live.get(handle.child_id)
        if live is None or live.handle != handle:
            raise ControlDenied('Handle is not owned by this profile, plugin and generation')
        return live

    @staticmethod
    def _open(snapshot):
        if snapshot['cancel_requested'] or snapshot['settled']:
            raise ControlDenied('Child dispatch is sealed')

    def _launch_controls(self, parent, request, ancestor=None, *, resolver=None):
        refs = list(request['consumer_refs']) if request else []
        parent_ref = 'parent:' + str(getattr(parent, 'session_id', self.parent_session_id))
        if parent_ref not in refs:
            refs.append(parent_ref)
        consumers = []
        for ref in refs:
            consumer = (self._resolve if resolver is None else resolver)(ref)
            if not isinstance(consumer, Consumer) or consumer.ref != ref:
                consumer = Consumer(ref)
            consumers.append({**dataclasses.asdict(consumer), 'requirement_ids': list(consumer.requirement_ids)})
        closed = bool(request and request['consumer_refs'] and request['consumer_set_closed'] and
                      self._grant.controls_all_consumers and all(c['obligation'] != 'unknown' for c in consumers))
        obligation = request['obligation'] if request else 'unknown'
        if any(c['obligation'] == 'required' or any(c[k] for k in ('requires_result', 'requires_effects', 'requires_cleanup', 'requires_corroboration'))
               for c in consumers):
            obligation = 'required'
        elif not closed or not self._grant.allow_optional_readonly:
            obligation = 'unknown'
        restricted = bool(request and request['effect_policy_id'] == self._policy.policy_id and
                          self._grant.allow_optional_readonly)
        if request and request['effect_policy_id'] != self._policy.policy_id:
            raise ControlDenied('Unknown effect policy')
        if ancestor and ancestor.snapshot['effect_class'] == 'read_only' and not restricted:
            raise ControlDenied('Nested work must inherit the read-only policy')
        return consumers, closed, obligation, restricted

    def planning_preflight(self, parent, request, *, require_inventory=False, goal="", operation=None):
        """Observe the same launch restrictions, without constructing or reserving work."""
        request = validate_request(request)
        with self._lock:
            if binding_of(parent) or len(self._live) >= 1024:
                return None
            consumers, closed, obligation, restricted = self._launch_controls(parent, request)
            if not restricted:
                return None
            if require_inventory:
                # Unlike launch's caller-declared closure, stopping expansion must
                # account a positively enumerated CURRENT native consumer set.
                if not callable(self._consumer_inventory):
                    return None
                inventory = self._consumer_inventory()
                if (not isinstance(inventory, tuple) or not 1 <= len(inventory) <= 64
                        or any(not isinstance(c, Consumer) for c in inventory)
                        or len({c.ref for c in inventory}) != len(inventory)):
                    return None
                actual = [{**dataclasses.asdict(c), 'requirement_ids': list(c.requirement_ids)} for c in inventory]
                if sorted(actual, key=lambda c: c['ref']) != sorted(consumers, key=lambda c: c['ref']):
                    return None
            return dict(consumers=consumers, closed=closed, obligation=obligation,
                        policy_id=self._policy.policy_id,
                        contracts={c.tool_name: c.version for c in self._policy.contracts},
                        revision=self._revision())

    def launch(self, parent, child, request=None, *, goal=""):
        """Called BEFORE scheduling by BOTH ordinary delegate and public launch."""
        request = validate_request(request)
        parent_binding = binding_of(parent)
        child_id = getattr(child, '_subagent_id', None)
        if not isinstance(child_id, str) or not child_id:
            raise ControlDenied('Child lacks canonical identity')
        with self._lock:
            if child_id in self._live:
                raise ControlDenied('Child identity already registered')
            if len(self._live) >= 1024:
                raise ControlDenied('Owned child registry capacity exhausted')
            ancestor = None
            if parent_binding:
                if parent_binding[0] is not self:
                    raise ControlDenied('Nested delegation cannot change control owners')
                ancestor = self._get(parent_binding[1])
                self._open(ancestor.snapshot)
                # Even an omitted nested request cannot escape inherited restrictions.
                if ancestor.snapshot['effect_class'] == 'read_only' and request is None:
                    request = dict(obligation='unknown', consumer_refs=[], consumer_set_closed=False,
                                   effect_policy_id=self._policy.policy_id)
            consumers, closed, obligation, restricted = self._launch_controls(parent, request, ancestor)
            handle = OwnedHandle(child_id, secrets.token_hex(16), self.parent_session_id,
                                 self._grant.profile, self._grant.plugin_id)
            snap = dict(child_id=child_id, generation=handle.generation, parent_session_id=self.parent_session_id,
                        profile=handle.profile, plugin_id=handle.plugin_id, control_revision=1,
                        obligation=obligation, consumers=consumers, consumer_set_closed=closed,
                        effect_policy_id=self._policy.policy_id if restricted else None,
                        effect_contracts={c.tool_name: c.version for c in self._policy.contracts} if restricted else {},
                        effect_class='read_only' if restricted else 'unknown', cancel_requested=False,
                        settled=False, worker_finished=False, processes_stopped=False, effects_reconciled=False,
                        inflight=0, dispatches={}, handoffs=[], cleanup_pending=False, candidate=None, receipts={},
                        objective=str(goal)[:16000], latest_milestone='',
                        semantic_observation=None, priority=0)
            if ancestor:
                self._commit(ancestor, lambda s: (s['handoffs'].append(child_id), s.update(candidate=None)))
            try:
                self._store.create(snap)
            except BaseException:
                if ancestor:
                    ancestor.completed_handoffs.add(child_id)
                    self._recover_lifecycle(ancestor)
                raise
            live = _Live(handle, child, snap, ancestor.handle.child_id if ancestor else None)
            self._live[child_id] = live
            child._owned_delegation_binding = (self, handle)
            child._owned_delegation_owner = self
            if restricted:
                allowed = set(snap['effect_contracts']) | {'delegate_task'}
                # Exposure is narrowed, and dispatch revalidates every call after hooks.
                tools = getattr(child, 'tools', None)
                if isinstance(tools, list):
                    child.tools = [t for t in tools if isinstance(t, dict) and
                                   t.get('function', t).get('name') in allowed]
                names = getattr(child, 'valid_tool_names', None)
                if isinstance(names, (set, list, tuple)):
                    child.valid_tool_names = set(names) & allowed
            return handle

    def list_owned(self, *, limit=64):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ControlDenied('Invalid bounded list limit')
        with self._lock:
            return tuple(l.handle for l in list(self._live.values())[:limit])

    def status(self, handle):
        with self._lock:
            live = self._get(handle)
            self._recover_lifecycle(live)
            return copy.deepcopy(live.snapshot)

    def reconcile_lifecycle(self, handle):
        """Retry only observed native exits/finish; never replay work or adopt it.

        Status and finish also drive this bounded, nonwaiting recovery path.
        Process loss loses these facts: persisted inflight work stays unknown.
        """
        with self._lock:
            return self._recover_lifecycle(self._get(handle))

    def _recover_lifecycle(self, live):
        from agent.owned_delegation_finalizers import recover_lifecycle
        return recover_lifecycle(self, live)

    def record_progress(self, handle, milestone: str):
        """Bounded owner-produced observation; never grants contribution authority."""
        if not isinstance(milestone, str):
            raise ControlDenied('Invalid progress')
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            bounded = milestone[:1000]
            if bounded == live.snapshot['latest_milestone']:
                return copy.deepcopy(live.snapshot)
            state = self._commit(live, lambda s: s.update(latest_milestone=bounded))
            if state['inflight']:
                live.milestone_pending = True
                return state
        bridge = getattr(self, '_relevance_bridge', None)
        if bridge is not None:
            bridge.changed('child_milestone', handle)
        return state

    def enroll_consumer(self, handle, ref, *, expected_revision):
        """Owner calls before binding any result/finding handle to another consumer."""
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            if live.snapshot['control_revision'] != expected_revision:
                raise ControlDenied('Stale control revision')
            if (not isinstance(ref, str) or not 1 <= len(ref) <= 256 or
                    (len(live.snapshot['consumers']) >= 65 and ref not in {c['ref'] for c in live.snapshot['consumers']})):
                raise ControlDenied('Invalid or over-capacity consumer enrollment')
            consumer = self._resolve(ref)
            if not isinstance(consumer, Consumer) or consumer.ref != ref:
                consumer = Consumer(ref)
            def change(s):
                s['consumers'] = [c for c in s['consumers'] if c['ref'] != ref] + [{**dataclasses.asdict(consumer), 'requirement_ids': list(consumer.requirement_ids)}]
                s['candidate'] = None
                if consumer.obligation == 'unknown':
                    s.update(consumer_set_closed=False, obligation='unknown')
                elif consumer.obligation == 'required' or any((consumer.requires_result, consumer.requires_effects, consumer.requires_cleanup, consumer.requires_corroboration)):
                    s['obligation'] = 'required'
            return self._commit(live, change)['control_revision']

    def mark_handoff(self, handle, handoff_id, *, expected_revision):
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            if (live.snapshot['control_revision'] != expected_revision or not isinstance(handoff_id, str) or
                    not 1 <= len(handoff_id) <= 256 or len(live.snapshot['handoffs']) >= 256):
                raise ControlDenied('Invalid handoff revision')
            return self._commit(live, lambda s: (s['handoffs'].append(handoff_id), s.update(candidate=None)))['control_revision']

    def invalidate_coverage(self, handle, *, expected_revision):
        """Untracked sharing must invalidate coverage BEFORE the handle escapes."""
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            if live.snapshot['control_revision'] != expected_revision:
                raise ControlDenied('Stale control revision')
            return self._commit(live, lambda s: s.update(consumer_set_closed=False, obligation='unknown', candidate=None))

    def propose_capability_change(self, handle, *, expected_revision):
        """No live widening: record the proposal, invalidate attestation, keep fence."""
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            if live.snapshot['control_revision'] != expected_revision:
                raise ControlDenied('Stale control revision')
            return self._commit(live, lambda s: s.update(effect_class='unknown', cleanup_pending=True, candidate=None))

    @contextmanager
    def dispatch(self, handle, name, args):
        with self._lock:
            live = self._get(handle)
            self._open(live.snapshot)
            # Retain the installed fence even after an invalidating change.
            if live.snapshot['effect_policy_id']:
                if name == 'delegate_task':
                    if not isinstance(args, dict) or args.get('action', 'spawn') not in ('spawn', ''):
                        raise ControlDenied('Read-only children may only use owned nested spawn')
                elif not self._policy.permits(name, args):
                    raise ControlDenied('Tool/parameters are outside the versioned read-only policy')
            self._recover_lifecycle(live)
            # Do not accumulate an unbounded recovery queue behind a writer.
            if (live.finish_pending or live.snapshot['worker_finished'] or live.completed_dispatches
                    or len(live.snapshot['dispatches']) >= 256):
                raise ControlDenied('Owned dispatch accounting is pending')
            dispatch_id = secrets.token_hex(16)
            def begin(s):
                s['dispatches'][dispatch_id] = name == 'delegate_task'
                s['inflight'] += 1
                s['latest_dispatch'] = name[:1000]
                if name == 'delegate_task':
                    s['handoffs'].append('dispatch:nested')
                    s['candidate'] = None
            self._commit(live, begin)
        try:
            yield
        finally:
            with self._lock:
                # The body really exited. Retain this exact fact until CAS
                # commits, so cleanup contention cannot overwrite its result.
                live.completed_dispatches.add(dispatch_id)
                self._recover_lifecycle(live)
                notify = live.milestone_pending and live.snapshot['inflight'] == 0
                if notify:
                    live.milestone_pending = False
            bridge = getattr(self, '_relevance_bridge', None)
            if notify and bridge is not None:
                bridge.changed('child_milestone', handle)

    @staticmethod
    def _settle(s):
        s['settled'] = bool(s['worker_finished'] and s['inflight'] == 0 and not s['handoffs'])
        # An attested policy cannot create external processes or task mutations.
        # Unknown/legacy work requires an explicit owner reconciliation receipt.
        if s['settled'] and s['effect_class'] == 'read_only' and not s['cleanup_pending']:
            s['processes_stopped'] = s['effects_reconciled'] = True

    def finish(self, handle, *, worker_finished=True):
        with self._lock:
            live = self._get(handle)
            live.finish_pending = live.finish_pending or worker_finished
            self._recover_lifecycle(live)

    def reconcile(self, handle, *, expected_revision, processes_stopped, effects_reconciled, completed_handoff=None):
        """Host cleanup owner only; never exposed as a supervisor proposal action."""
        with self._lock:
            live = self._get(handle)
            if live.snapshot['control_revision'] != expected_revision:
                raise ControlDenied('Stale reconciliation')
            if type(processes_stopped) is not bool or type(effects_reconciled) is not bool:
                raise ControlDenied('Reconciliation requires explicit booleans')
            def change(s):
                if completed_handoff is not None:
                    s['handoffs'].remove(completed_handoff)
                s.update(processes_stopped=processes_stopped, effects_reconciled=effects_reconciled,
                         cleanup_pending=not effects_reconciled)
                self._settle(s)
            return self._commit(live, change)

    @staticmethod
    def _receipt(s, accepted, reason):
        return CancelReceipt(accepted, reason, s['control_revision'], s['cancel_requested'], s['settled'],
                             s['processes_stopped'], s['effects_reconciled'])

    def clear_semantic_candidate(self, handle, *, expected_revision):
        """Owner calls when fresh contribution evidence invalidates an earlier vote.

        This safe observation is needed even if the feature produces no action on
        its positive/ambiguous observation. It never stops or instructs a child.
        """
        with self._lock:
            live = self._get(handle)
            if live.snapshot['control_revision'] != expected_revision:
                raise ControlDenied('Stale contribution observation')
            if live.snapshot['cancel_requested'] or live.snapshot['settled']:
                return False
            self._commit(live, lambda s: s.update(candidate=None))
            return True

    def request_explicit_stop(self, handle):
        """Separate deterministic owner/user stop; no semantic evidence required."""
        with self._lock:
            live = self._get(handle)
            if not live.snapshot['cancel_requested'] and not live.snapshot['settled']:
                self._commit(live, lambda s: s.update(cancel_requested=True, candidate=None))
            return self._receipt(live.snapshot, not live.snapshot['settled'], 'explicit_stop')

    def semantic_authorized(self, handle, *, deadline=None) -> bool:
        """Configured owners recheck live profile and authenticated consumer authority."""
        return True

    def request_semantic_cancel(self, handle, *, expected_revision, evidence: SemanticEvidence,
                                idempotency_key: str, deadline: float, feature_id='F01', _defer_signal=False):
        with control_fence(self._lock, deadline):
            live = self._get(handle)
            s = live.snapshot
            if (feature_id not in ('F01', 'F04') or type(expected_revision) is not int or expected_revision < 1 or
                    not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
                raise ControlDenied('Invalid semantic cancellation request')
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ControlDenied('A finite propagated monotonic deadline is required')
            if not isinstance(evidence, SemanticEvidence):
                raise ControlDenied('Semantic evidence must be host-validated')
            # Bind replay to all semantic operands, not merely a caller-supplied key.
            import hashlib
            import json
            fingerprint = hashlib.sha256(json.dumps([dataclasses.asdict(evidence), expected_revision, feature_id, deadline], sort_keys=True).encode()).hexdigest()
            old = s['receipts'].get(idempotency_key)
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ControlDenied('Idempotency key reused with different request')
                return CancelReceipt(**old['receipt'])
            if time.monotonic() >= deadline:
                return self._receipt(s, False, 'deadline_expired')
            if s['control_revision'] != expected_revision:
                return self._receipt(s, False, 'stale_control_revision')
            if (not self.semantic_authorized(handle, deadline=deadline) or s['cancel_requested'] or s['settled'] or s['obligation'] != 'optional' or
                    not s['consumer_set_closed'] or not s['consumers'] or s['effect_class'] != 'read_only' or
                    s['handoffs'] or s['cleanup_pending'] or not self._grant.allow_optional_readonly or
                    any(c['obligation'] != 'optional' or c['requires_result'] or c['requires_effects'] or c['requires_cleanup'] or c.get('requires_corroboration', True) for c in s['consumers'])):
                return self._receipt(s, False, 'ineligible')
            revision = self._revision()
            if (not isinstance(evidence, SemanticEvidence) or evidence.revision != revision or
                    not isinstance(revision, tuple) or len(revision) != 3 or
                    any(type(v) is not int or v < 0 for v in revision)):
                return self._receipt(s, False, 'stale_meaningful_revision')
            values = dict(evidence.contributions)
            def prob(v):
                return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
            strong = (len(values) == len(evidence.contributions) and set(values) == {c['ref'] for c in s['consumers']} and
                      all(prob(v) and v <= .05 for v in values.values()) and
                      evidence.relation in ('no_remaining_consumer', 'superseded') and
                      prob(evidence.probability) and evidence.probability >= .97 and
                      prob(evidence.confidence) and evidence.confidence >= .90)
            previous = s['candidate']
            accepted = bool(strong and previous is not None and tuple(previous) != revision and
                            all(a <= b for a, b in zip(previous, revision)))
            reason = 'cancel_requested' if accepted else 'await_distinct_revision' if strong else 'contribution_or_uncertainty'
            def change(new):
                new['candidate'] = list(revision) if strong else None
                if accepted:
                    new['cancel_requested'] = True
                receipt = dataclasses.asdict(self._receipt({**new, 'control_revision': new['control_revision'] + 1}, accepted, reason))
                # Bound receipt growth without evicting an uncertain accepted command.
                if len(new['receipts']) >= 128:
                    raise ControlDenied('Cancellation receipt capacity exhausted')
                new['receipts'][idempotency_key] = dict(fingerprint=fingerprint, receipt=receipt)
            if time.monotonic() >= deadline:
                return self._receipt(live.snapshot, False, 'deadline_expired')
            from tools.delegation_control_store import ControlDeadlineExpired
            try:
                s = self._commit(live, change, deadline=deadline)
            except ControlDeadlineExpired:
                return self._receipt(live.snapshot, False, 'deadline_expired')
            child = live.child if accepted else None
            receipt = self._receipt(s, accepted, reason)
        if child is not None and not _defer_signal:
            self.signal_semantic_cancel(handle)
        return receipt

    def signal_semantic_cancel(self, handle):
        # Only signal already-sealed work, outside the control lock. Native joins
        # can defer this until their enclosing compare/commit critical section ends.
        with self._lock:
            live = self._get(handle)
            child = live.child if live.snapshot['cancel_requested'] else None
        if child is not None:
            try:
                request_hard_interrupt(child, 'Optional work no longer contributes', tool_reason='owned semantic cancellation')
            except Exception:
                pass


def install_owner(parent, *, store, grant, consumer_resolver, policy, revision_provider, consumer_inventory=None):
    owner = OwnedDelegationOwner(parent_session_id=str(parent.session_id), store=store, grant=grant,
                                 consumer_resolver=consumer_resolver, policy=policy, revision_provider=revision_provider,
                                 consumer_inventory=consumer_inventory)
    parent._owned_delegation_owner = owner
    return owner


def owner_of(parent):
    owner = getattr(parent, '_owned_delegation_owner', None)
    if not isinstance(owner, OwnedDelegationOwner):
        from agent.owned_delegation_policy import configured_owner
        owner = configured_owner(parent)
        if owner is None:
            return None
    if str(getattr(parent, 'session_id', '')) != owner.parent_session_id and not binding_of(parent):
        from agent.owned_delegation_policy import ConfiguredDelegationOwner
        if isinstance(owner, ConfiguredDelegationOwner):
            return None  # reset preserves legacy launch and the old children's owner
        raise ControlDenied('Parent session generation changed; reinstall the owner')
    return owner


def binding_of(child):
    binding = getattr(child, '_owned_delegation_binding', None)
    return binding if isinstance(binding, tuple) and len(binding) == 2 and isinstance(binding[0], OwnedDelegationOwner) else None


def register_launch(parent, child, request=None, *, goal=""):
    owner = owner_of(parent)
    if owner is None:
        if request is not None:
            raise ControlDenied('Supervised launch requires a configured host owner grant')
        return None
    handle = owner.launch(parent, child, request, goal=goal)
    from agent.supervision_children import attach
    if handle is not None:
        attach(parent, owner, handle)
    return handle


@contextmanager
def dispatch_fence(child, name, args):
    binding = binding_of(child)
    if binding:
        with binding[0].dispatch(binding[1], name, args):
            yield
    else:
        yield


def seal_explicit_stop(child):
    binding = binding_of(child)
    if binding:
        return binding[0].request_explicit_stop(binding[1])


def finish_child(child, *, worker_finished=True):
    binding = binding_of(child)
    if binding:
        binding[0].finish(binding[1], worker_finished=worker_finished)
