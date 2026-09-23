"""Main-owned late literal corrections. No model/tool field grants an effect.

Whole adopted, request-linked original answers are a sufficient relevance class;
required-answer intent remains unknown. Admission is ordinary native completion,
not a second wake. Historical work identities are facts, never current authority.
"""
import copy
from dataclasses import dataclass
import hashlib
import json
import logging
import sqlite3
import uuid

from agent import supervision_planning_records as records
from agent.supervision_claim_uses import comparable
from agent.supervision_context import digest
from agent.supervision_original_output import _Origin, _OriginalText, _Codec, fingerprint
from agent.native_emission import EmittedPayload

logger = logging.getLogger(__name__)
POLICY = {'version': 'supervision.literal-correction.v1', 'consumer': 'native:main-correction', 'enabled': True}


def source_for(rt):
    engine = getattr(rt.agent(), 'context_compressor', None)
    return getattr(getattr(engine, 'supervision', None), '_literal_source_registration', None)


def enabled(rt):
    source = source_for(rt)
    return bool(source and source.active and source.correction_policy == POLICY)


def enroll_original(attempt):
    source = source_for(attempt.rt)
    if enabled(attempt.rt):
        source.provider.enroll_correction_source(attempt)


def candidate_received(rt, provider, original_id, candidate):
    source = source_for(rt)
    if not enabled(rt) or source.provider is not provider:
        return
    with rt.lock:
        pending = getattr(rt, '_correction_candidates', None)
        if pending is None:
            pending = rt._correction_candidates = {}
        if len(pending) < 64:
            pending.setdefault(candidate, (provider, original_id))


def source_event_received(rt, provider, original_id, candidate, kind):
    """Postcommit source-owner ingress into the existing routed completion queue."""
    candidate_received(rt, provider, original_id, candidate)
    custody = provider.correction_custody.get(original_id)
    if custody is None or not enabled(rt):
        return
    observation = custody.original.row['observation']
    target = dict(observation['target'])
    event = dict(type='literal_source_change', source_event_id=candidate[0],
        session_key=rt.session_id, parent_session_id=rt.session_id)
    if observation['surface'] == 'gateway.telegram':
        event.update(platform='telegram', chat_type='group', chat_id=target['chat_id'],
            thread_id=target['message_thread_id'])
    elif observation['surface'] == 'tui':
        event['origin_ui_session_id'] = target['session']
    from tools.process_registry import process_registry
    process_registry.completion_queue.put(event)


def prepare_source_event(event, target_session):
    from agent.supervision_delivery import runtime_for_session, _read
    from agent.supervision_store import generation
    rt = runtime_for_session(target_session)
    if rt is None or not enabled(rt) or event.get('_supervision_recovery'):
        return False
    provider = source_for(rt).provider
    ident = event.get('source_event_id')
    if not provider.correction_event_current(ident):
        return False
    candidate, _, kind = provider.correction_events[ident]
    custody = provider.correction_custody.get(candidate[3])
    if custody is None or custody.original.rt is not rt:
        return False
    with _read(rt) as conn:
        epoch = generation(conn, rt.session_id)
    original = custody.original
    incident = 'correction:' + digest(json.dumps((original.row['id'], original.row['observation']['id'],
        candidate[1:3], original.row['observation']['target']), sort_keys=True))
    with rt.lock:
        admitted = getattr(rt, '_correction_admitted', None)
        if admitted is None:
            admitted = rt._correction_admitted = {}
        review = admitted.get(incident)
        if review is None:
            if len(admitted) >= 4:
                return False
            review = Review(rt, provider, custody, candidate, incident, ident, epoch)
            review.source_event = True
            if not review.save_initial():
                return False
            admitted[incident] = review
        if review.started:
            return False
        event['_native_correction_review'] = review
        return True


def accept_source_event(event):
    review = event.get('_native_correction_review')
    if (type(review) is not Review or not review.source_event or not review.current()
            or not review.provider.correction_event_current(review.delivery_id)):
        return False
    review.source_admitted = True
    return True


def admit_completion(event, target_session):
    """After durable native admission. Prose/refs in a worker result are unused."""
    from agent.supervision_delivery import runtime_for_session, _read
    from agent import supervision_store as store
    rt = runtime_for_session(target_session)
    if rt is None or not enabled(rt) or event.get('_supervision_recovery'):
        return
    with _read(rt) as conn:
        launch = conn.execute('SELECT parent_session_id,task_json,control_child_ids,event_json FROM async_delegations WHERE delegation_id=?',
            (event.get('delegation_id'),)).fetchone()
        admission = store.get_admission(conn, event.get('supervision_delivery_id'))
        if not launch or launch[0] != rt.session_id or not admission or admission['state'] in {'parked', 'consumed'}:
            return
        children = json.loads(launch[2])
        binding = json.loads(launch[1]).get('supervision_revision', {})
        store.get_result_object(conn, admission['object_id'])
        generation = store.generation(conn, rt.session_id)
    with rt.lock:
        pending = getattr(rt, '_correction_candidates', {})
        admitted = getattr(rt, '_correction_admitted', None)
        if admitted is None:
            admitted = rt._correction_admitted = {}
        for candidate, (provider, original_id) in tuple(pending.items()):
            custody = provider.correction_custody.get(original_id)
            if custody is None or candidate[0] not in children:
                continue
            original = custody.original
            if (binding.get('work_id') != original.revision.work_id
                    or binding.get('profile') != rt.revision.profile
                    or binding.get('lineage') != rt.revision.lineage
                    or len(admitted) >= 4):
                continue
            ident = 'correction:' + digest(json.dumps((original_id, original.row['observation']['id'],
                candidate[1:3], original.row['observation']['target']), sort_keys=True))
            if ident in admitted:
                continue
            review = Review(rt, provider, custody, candidate, ident, admission['delivery_id'], generation)
            if review.save_initial():
                admitted[ident] = review


def drain(rt):
    """Existing main-agent safe point; no source I/O under the graph writer."""
    rt._assert_owner()
    if rt.closed or not enabled(rt):
        return
    for review in tuple(getattr(rt, '_correction_admitted', {}).values()):
        if review.started:
            continue
        try:
            review.run()
        except Exception:
            logger.debug("native correction remains unresolved", exc_info=True)
        # A native main turn reviews one pair. Other admitted obligations remain
        # pending, rather than being silently truncated or spawning another turn.
        break


@dataclass(eq=False)
class Review:
    rt: object
    provider: object
    custody: object
    candidate: tuple
    ident: str
    delivery_id: str
    generation: int

    def __post_init__(self):
        self.original = copy.deepcopy(self.custody.original.row)
        self.claim = copy.deepcopy(self.custody.original.claim)
        self.work_id = self.custody.original.revision.work_id
        self.database = self.custody.original.database
        self.started = False
        self.source_event = False
        self.source_admitted = False
        self.revision = None
        self.scope = None
        self.issued = {}
        self.attempted = False
        self.row = dict(id=self.ident, kind='correction', revision=1, status='evidence_received',
            original_id=self.original['id'], original_hash=fingerprint(self.original),
            claim_id=self.claim['id'], source_refs=[self.original['source']['exact_ref'], self.candidate[1]],
            source_hashes=[self.original['source']['row_hash'], self.candidate[2]],
            admission_id=self.delivery_id, truth_winner=None, suspend_optional_reuse=True,
            relevance='native_whole_answer_dependency.v1', required_answer_intent='unknown',
            observation=None)

    def current(self):
        rt = self.rt
        return (enabled(rt) and source_for(rt).provider is self.provider and rt.agent() is not None
            and getattr(rt.agent(), '_session_db', None) is self.database
            and getattr(rt.agent(), 'session_id', None) == rt.session_id
            and not getattr(rt.agent(), '_interrupt_requested', False)
            and (self.revision is None or rt.revision == self.revision))

    def save_initial(self):
        if (self.original['status'] != 'original_emitted' or self.claim['status'] != 'adopted'
                or self.claim['declaration']['use'] != 'answer_dependency'
                or not 1 <= len(self.claim['declaration']['requirement_refs']) <= 4
                or self.original['adoption']['body_hash'] != fingerprint(self.claim)
                or self.original['accepted_final']['hash'] != self.original['render_hash']):
            return False
        with self.rt.lock:
            return records.save_correction(self, self.row, initial=True)

    def change(self, status, *, guard=None, **metadata):
        with self.rt.lock:
            updated = {**self.row, **metadata, 'revision': self.row['revision'] + 1, 'status': status}
            if records.save_correction(self, updated, guard=guard):
                self.row = updated
                return True
        return False

    def run(self):
        from agent.supervision_delivery import _read
        from agent import supervision_store as store
        if self.source_event:
            if not self.source_admitted or not self.provider.correction_event_current(self.delivery_id):
                return
        with _read(self.rt) as conn:
            admission = None if self.source_event else store.get_admission(conn, self.delivery_id)
            if ((not self.source_event and (not admission or admission['state'] not in {'accepted', 'consumed'}))
                    or store.generation(conn, self.rt.session_id) != self.generation):
                return
        self.started = True
        self.revision = self.rt.revision
        if not self.current() or not self.change('review_admitted'):
            return
        pair = self.provider.correction_pair(self, self.original['id'], self.candidate)
        if pair is None:
            self.change('source_unavailable')
            return
        a, b = pair
        if not comparable(a, b):
            self.change('different_scope')
            return
        # Exact numeric/value invalidation is deterministic. No newer-is-true
        # rule: both references remain, and this does not declare either false.
        x, y = json.loads(a.source_bytes), json.loads(b.source_bytes)
        numeric = (x['quantity']['kind'] == 'quantity'
            and x['polarity'] == y['polarity'] == 'positive'
            and x['modality'] == y['modality'] == 'asserted')
        if not numeric:
            from agent.supervision_correction_semantics import contest
            if not contest(self, pair):
                self.change('no_contest')
                return
        elif x['value'] == y['value']:
            self.change('compatible')
            return
        elif not self.change('contested', relation='incompatible', classification='exact_numeric'):
            return
        self.rt.dependencies.contested_claims.add(self.claim['claim_id'])
        text = (f"Correction: my earlier answer relied on {a.exact_ref}. New evidence {b.exact_ref} "
            "conflicts with that claim under the same qualifiers; the claim is now contested. "
            "This does not establish which source is correct.")
        # Notices own a separate native lease: do not close the current answer's
        # original-output scope merely because a historical correction is ready.
        from agent.native_emission import NativeEmissionScope, EmissionIdentity
        revision = self.rt.revision
        turn = getattr(self.rt.agent(), '_current_turn_id', None)
        if not turn:
            return
        self.scope = NativeEmissionScope(EmissionIdentity(revision.profile, revision.lineage,
            revision.work_id, revision.run_generation, turn), self.observed,
            current=lambda: self.current() and getattr(self.rt.agent(), '_current_turn_id', None) == turn)
        self.scope._owner_fence = self.rt.lock
        self.rt._native_correction_scope = self.scope
        origin = _Origin(self, 'correction', text, uuid.uuid4().hex)
        self.issued[origin.id] = origin
        self.text = _OriginalText(origin)
        from agent.credits_tracker import AgentNotice
        callback = getattr(self.rt.agent(), 'notice_callback', None)
        if callable(callback):
            callback(AgentNotice(text=self.text, level='warn', kind='correction', key=self.ident))

    def reserve(self, surface, target, transport):
        """Native destination owner calls immediately before its ONE send."""
        if self.attempted or not self.current() or self.row['status'] != 'contested':
            return False
        old = self.original['observation']
        if (surface != old['surface'] or list(map(list, target)) != list(map(list, old['target']))
                or transport != old['transport_generation']):
            return False
        # Fresh presentation-purpose reread; review facts are not source permission.
        pair = self.provider.correction_pair(self, self.original['id'], self.candidate)
        if pair is None or not self.current():
            return False
        with self.rt.lock:
            if self.attempted or not self.current() or self.row['status'] != 'contested':
                return False
            self.attempted = True  # conservative even if intent persistence fails
            return self.change('selected', payload_hash=digest(self.text), target=list(map(list, target)),
                transport_generation=transport, surface=surface)

    def observed(self, receipt):
        if (type(receipt) is not EmittedPayload or type(receipt.origin) is not _Codec
                or receipt.origin.origin.owner is not self or not self.attempted
                or self.row['status'] != 'selected' or not self.current()
                or self.scope is not self.rt._native_correction_scope
                or receipt.identity != self.scope.identity or not receipt.current()
                or receipt.payload_sha256 != receipt.origin.wire_hash
                or hashlib.sha256(receipt.payload).hexdigest() != receipt.payload_sha256
                or receipt.surface != self.row['surface']
                or list(map(list, receipt.target)) != self.row['target']
                or receipt.transport_generation != self.row['transport_generation']):
            return
        if not self.rt.lock.acquire(blocking=False):
            return  # unknown; never retry external effects
        try:
            self.change('emitted', observation=dict(id=receipt.observation_id,
                payload_hash=receipt.payload_sha256, target=list(map(list, receipt.target)),
                transport_generation=receipt.transport_generation, surface=receipt.surface,
                operation=receipt.operation, frame_id=receipt.message_or_frame_id,
                codec=receipt.origin.name, original_id=self.original['id']))
        finally:
            self.rt.lock.release()


def notice_owner(text):
    from agent.supervision_original_output import origin_of
    origin = origin_of(text)
    return origin.owner if origin is not None and type(origin.owner) is Review and origin.stage == 'correction' else None
