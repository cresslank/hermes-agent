"""Bounded F22 relation judgment; native contest is separate from notice delivery.

Historical source custody grants no inference permission. A new pair requires a
named source recipient and separately field-authorized egress, including at send
admission. Source reads stay outside host/recipient/canonical writer fences.
"""
from contextlib import contextmanager

from agent.supervision_types import Action, Completeness

VERSION = 'supervision.literal-correction.v1'
CLASSES = frozenset({'history_excerpt', 'project_excerpt', 'task_text'})


def _requests(rt):
    return getattr(rt, '_correction_semantics', {})


class SemanticPair:
    def __init__(self, review, pair, recipient):
        self.review, self.pair, self.recipient = review, pair, recipient
        self.rt = review.rt
        self.source = self.rt.agent().context_compressor.supervision._literal_source_registration
        self.deadline = self.rt.shared_deadline()
        self.revision = self.rt.revision

    def authorized(self):
        r, s = self.recipient, self.source
        return (s.active and s.provider is self.review.provider
                and r.active and r.plugin_id in s.correction_recipients
                and 'observe' in r.grants and Action.CONTEST_CLAIM.value in r.grants
                and CLASSES <= r.data_policy
                and all(r.egress_policy.get('sources', {}).get(k) == VERSION
                        and k in r.egress_policy.get('fields', {}) for k in ('a', 'b')))

    def current(self):
        return (self.authorized() and self.review.current()
                and self.rt.revision == self.revision and self.rt.clock() < self.deadline
                and self.review.provider.correction_current(self.review.custody, self.review))

    def read_current(self):
        return self.review.provider.correction_pair(
            self.review, self.review.original['id'], self.review.candidate) == self.pair

    @contextmanager
    def fence(self, recipient, read):
        with self.source.lock, self.review.provider.literal_source_binding_fence(
                self.review.custody.engine, self.review.custody.binding) as bound:
            yield bool(read and bound and recipient is self.recipient and self.current())

    def validate(self, p):
        m = p.metadata
        return (self.current() and p.feature_id == 'F22' and p.action == Action.CONTEST_CLAIM
                and p.plugin_generation == self.recipient.generation
                and p.evidence_refs == tuple(r.exact_ref for r in self.pair)
                and not p.candidate_ids and p.relation == 'incompatible'
                and p.template_id is None and not p.template_args
                and set(m) == {'feature_action', 'dependent_ids', 'relation', 'truth_winner', 'suspend_optional_reuse'}
                and m['feature_action'] == 'contest_dependent_claim'
                and tuple(m['dependent_ids']) == (self.review.claim['id'],)
                and m['relation'] == 'incompatible' and m['truth_winner'] is None
                and m['suspend_optional_reuse'] is True)


def valid_proposal(rt, proposal):
    request = _requests(rt).get(proposal.target_id)
    return request is not None and request.validate(proposal)


def current_read(rt, target):
    with rt.lock:
        request = _requests(rt).get(target)
    return request, request is not None and request.read_current()


@contextmanager
def dispatch_fence(rt, target, recipient, read):
    request = _requests(rt).get(target)
    if request is None or read[0] is not request:
        yield False
    else:
        with request.fence(recipient, read[1]) as current:
            yield current


def contest(review, pair):
    """Return true only after the native canonical contest actually committed."""
    rt = review.rt
    source = rt.agent().context_compressor.supervision._literal_source_registration
    recipients = [r for r in rt._registrations()
                  if r.plugin_id in source.correction_recipients]
    if len(recipients) != 1:
        return False
    request = SemanticPair(review, pair, recipients[0])
    if not request.current():
        return False
    def window(row):
        text = row.source_bytes.decode('utf-8')
        return dict(id=row.exact_ref, ref=row.exact_ref, text=text, qualifiers=text)
    facts = dict(a=window(pair[0]), b=window(pair[1]),
                 comparability=dict(entity=True, predicate=True, time=True, scope=True, units=True),
                 dependent_ids=[review.claim['id']], deterministic_invalidation=False,
                 delivered_consequential=True, relation_only=True)
    with rt.lock:
        if not hasattr(rt, '_correction_semantics'):
            rt._correction_semantics = {}
        if len(rt._correction_semantics) >= 4 or review.ident in rt.closed_targets:
            return False
        rt._correction_semantics[review.ident] = request
    try:
        snapshot = rt.observe('comparable_claim_pair', facts, target_id=review.ident,
            owner='corrections', actions=(Action.CONTEST_CLAIM,),
            evidence_refs=tuple(r.exact_ref for r in pair), relations=('incompatible',),
            deadline=request.deadline, expected_revision=request.revision,
            recipient=request.recipient, data_class='history_excerpt',
            required_data_classes=('project_excerpt', 'task_text'),
            completeness=Completeness(scope='linked_candidates', complete=True))
        if snapshot is None:
            return False
        rt._wait_for(review.ident, request.deadline, request.revision)
        current = request.read_current()
        with rt.lock:
            entry = rt._take(review.ident, {Action.CONTEST_CLAIM})
            if entry is None:
                return False
            proposal, recipient = entry
            with recipient.fence, request.fence(recipient, current) as valid:
                if not valid or rt._validate(proposal, recipient):
                    rt._settle(proposal, 'stale', 'owner_postvalidation')
                    return False
                applied = review.change('contested', guard=request.current,
                    relation='incompatible', classification='semantic_relation')
                if applied:
                    rt.incidents.add(proposal.incident_id)
                rt._settle(proposal, 'applied' if applied else 'unknown',
                           'owner_settlement' if applied else 'storage_unavailable')
                return applied
    finally:
        with rt.lock:
            rt.closed_targets.add(review.ident)
            _requests(rt).pop(review.ident, None)
