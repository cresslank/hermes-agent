"""Native literal claim-use -> bounded contest, within DependencyOwner's graph.

A committed declaration is not adoption. Only a fresh ordinary source publication
can bind it to current native propositions. This slice never certifies delivery or
consequence and never requests a correction/continuation or additional retrieval.
"""
from contextlib import ExitStack, contextmanager
import copy
import json

from agent.supervision_context import digest, visible_exact_occurrence
from agent.supervision_types import Action, Completeness
from agent import supervision_planning_records as records
from agent.supervision_literal_sources import lookup_literal_source

VERSION = "supervision.claim-contest.v1"
ACTIONS = {"contest_dependent_claim": "contest_dependent_claim"}


def render_record(record):
    """Reversible whole-source rendering, including whitespace and every qualifier."""
    return "LCM record " + record.exact_ref + ": " + json.dumps(
        record.source_bytes.decode("utf-8"), ensure_ascii=True)


def comparable(a, b):
    left, right = dict(a.coordinate_pins), dict(b.coordinate_pins)
    # Values, polarity and modality remain in the complete semantic windows.
    # Predicate equality is mandatory, not inferred from entity/topic overlap.
    return (a.exact_ref != b.exact_ref and all(left.get(k) == right.get(k) and k in left
        for k in ("entity", "predicate", "time", "scope", "quantity")))


class ClaimUses:
    def __init__(self, dependencies):
        self.owner = dependencies
        self.rt = dependencies.runtime
        self.requests = {}
        self.final_sources = set()

    @property
    def graph(self):
        return self.owner.planning

    def clear_final_sources(self):
        for source in self.final_sources:
            with source.source.lock:
                source.release(self.rt)
        self.final_sources.clear()

    def clear(self):
        self.clear_final_sources()
        self.requests.clear()

    def declare(self, path, text, declaration):
        """Called only by the committed, designated main-agent annex owner."""
        d = declaration
        if (d["use"] != "answer_dependency" or type(d["source_ref"]) is not str
                or not 0 < len(d["source_ref"]) <= 128):
            return
        self.graph.resolve(d["claim_ref"])
        self.graph.refs(d["requirement_refs"], 4, accepted=True)
        linked = self._linked(path, d)
        if linked is None or len(self.graph.nodes) + len(self.owner.edges) >= 64:
            return
        ident = "claim-use:" + digest(json.dumps((self.graph.epoch(), path, digest(text), d["local_id"])))
        if ident in self.graph.nodes or any(n["declaration"].get("local_id") == d["local_id"]
                and n["epoch"] == self.graph.epoch() for n in self.graph.nodes.values()):
            return
        self.graph._save([dict(id=ident, kind="claim_delivery", revision=1, status="claim_declared",
            declaration=copy.deepcopy(d), path=path, pin=digest(text), epoch=self.graph.epoch(),
            todo_revision=self.rt.plan_steps()[1], claim_id=linked.id, members={},
            emission="unknown", correction_relevance="unknown")])

    def _linked(self, path, d):
        work_map = self.rt.work_maps.get(path)
        if not work_map or self.rt.work_map_revisions.get(path) != self.rt.plan_steps()[1]:
            return None
        ref = d["claim_ref"]
        for row, claim in zip(work_map["claims"], work_map["claim_spans"]):
            if (ref == dict(source_ref=claim.source_message_id, source_revision=claim.source_revision,
                    start=claim.start, end=claim.end)):
                requirements = [work_map["requirement_spans"][i] for i in row["requirement_indexes"]]
                expected = [dict(source_ref=r.source_message_id, source_revision=r.source_revision,
                    start=r.start, end=r.end) for r in requirements]
                if d["requirement_refs"] == expected:
                    return claim
        return None

    def _eligible(self, node, proposition, *, stored=True):
        if node["kind"] != "claim_delivery" or node["status"] != "claim_declared":
            return False
        d = node["declaration"]
        bindings_current = (not self.rt.closed and node["epoch"] == self.graph.epoch()
            and node["todo_revision"] == self.rt.plan_steps()[1]
            and self.rt.artifact_pins.get(node["path"]) == node["pin"])
        if (d["source_ref"] != proposition.record.exact_ref or not bindings_current
                or (stored and not records.current(self.rt, node))):
            return False
        try:
            claim = self._linked(node["path"], d)
            text = self.graph.resolve(d["claim_ref"])
            self.graph.refs(d["requirement_refs"], 4, accepted=True)
            body = self.rt.artifacts.get(d["claim_ref"]["source_ref"], "")
            rendered = render_record(proposition.record)
            return (claim is not None and claim.id == node["claim_id"] and text == rendered and body == rendered
                    and len(rendered) <= 2400 and visible_exact_occurrence(body, rendered))
        except (KeyError, ValueError, TypeError):
            return False

    def published(self, source, refs):
        """Admission inside the existing ordinary pack/compile window; no new wait budget."""
        rt = self.rt
        rt._assert_owner(tool_worker=True)
        with rt.lock:
            self.graph._load()
            nodes = tuple(n for n in self.graph.nodes.values() if n["kind"] == "claim_delivery"
                          and n["status"] == "claim_declared")
        if not nodes or len(refs) < 2:
            return
        # One authority, one exact pair per publication. Competing recipients do
        # not share source grants or become independent votes about truth.
        recipients = [r for r in rt._registrations() if r.active
            and Action.CONTEST_CLAIM.value in r.grants and "observe" in r.grants
            and {"history_excerpt", "project_excerpt", "task_text"} <= r.data_policy
            and r.plugin_id in source.recipients]
        if len(recipients) != 1:
            return
        recipient = recipients[0]
        values = [lookup_literal_source(rt, ref, recipient) for ref in refs]
        if any(p is None for p in values):
            return
        pairs = []
        with rt.lock:
            for i, a in enumerate(values):
                for b in values[i + 1:]:
                    if not comparable(a.record, b.record):
                        continue
                    affected = tuple(n for n in nodes if self._eligible(n, a) or self._eligible(n, b))
                    if len(affected) > 4:
                        return  # never truncate the affected scope
                    if affected:
                        pairs.append((a, b, affected))
        if len(pairs) != 1:
            return
        a, b, affected = pairs[0]
        deadline = min(a.deadline, b.deadline)
        target = "claim-pair:" + digest(json.dumps((
            sorted((p.record.exact_ref, p.record.row_hash) for p in (a, b)),
            sorted(n["id"] for n in affected))))
        def window(p):
            text = p.record.source_bytes.decode("utf-8")
            return dict(id=p.ref, ref=p.ref, text=text, qualifiers=text)
        facts = dict(a=window(a), b=window(b), comparability=dict(entity=True, predicate=True,
            time=True, scope=True, units=True), dependent_ids=[n["id"] for n in affected],
            deterministic_invalidation=False, delivered_consequential=False)
        with rt.lock:
            if target in rt.closed_targets or len(self.requests) >= 8:
                return
            self.requests[target] = (recipient, a, b, affected)
        try:
            snap = rt.observe("comparable_claim_pair", facts, target_id=target, owner="claim_uses",
                actions=(Action.CONTEST_CLAIM,), evidence_refs=(a.ref, b.ref), relations=("incompatible",),
                deadline=deadline, expected_revision=a.revision, recipient=recipient, data_class="history_excerpt",
                required_data_classes=("project_excerpt", "task_text"),
                completeness=Completeness(scope="linked_candidates", complete=True))
            if snap is None:
                return
            rt._wait_for(target, deadline, a.revision)
            # All point reads are outside the graph and canonical SQLite fences.
            current = tuple(lookup_literal_source(rt, p.ref, recipient) for p in (a, b))
            with rt.lock:
                entry = rt._take(target, {Action.CONTEST_CLAIM})
                if entry is None:
                    return
                proposal, registration = entry
                with self._source_fence(source, recipient, (a, b)) as valid:
                    if (not valid or current != (a, b) or registration is not recipient
                            or not all(self._eligible(n, a if n["declaration"]["source_ref"] == a.record.exact_ref else b)
                                       for n in affected) or rt._validate(proposal, registration)):
                        rt._settle(proposal, "stale", "owner_postvalidation")
                        return
                    rows = [{**n, "revision": n["revision"] + 1, "status": "contested",
                        "source_refs": [a.ref, b.ref], "source_identities": [self._identity(a), self._identity(b)],
                        "truth_winner": None, "suspend_optional_reuse": True} for n in affected]
                    # Recheck freshness after obtaining the zero-wait canonical writer.
                    def guard():
                        return (self._fenced_current(source, recipient, (a, b))
                            and rt._validate(proposal, registration) is None
                            and all(self._eligible(n, a if n["declaration"]["source_ref"] == a.record.exact_ref else b,
                                                   stored=False) for n in affected))
                    if not records.save(rt, rows, validate=guard,
                            predecessors={n["id"]: (n["revision"], n["status"]) for n in affected}):
                        rt._settle(proposal, "unknown", "storage_unavailable")
                        return
                    self.graph.nodes.update((n["id"], n) for n in rows)
                    self.owner.contested_claims.update(n["claim_id"] for n in rows)
                    rt.incidents.add(proposal.incident_id)
                    rt._settle(proposal, "applied", "owner_settlement")
        finally:
            with rt.lock:
                rt.closed_targets.add(target)
                self.requests.pop(target, None)

    @staticmethod
    def _identity(p):
        r = p.record
        return dict(exact_ref=r.exact_ref, row_hash=r.row_hash, record_span=r.record_span,
            attribution=r.source_attribution, owner=p.owner_id, generation=p.owner_generation)

    def _fenced_current(self, source, recipient, propositions):
        rt = self.rt
        return (source.current(rt) and recipient.active and "observe" in recipient.grants
            and Action.CONTEST_CLAIM.value in recipient.grants
            and {"history_excerpt", "project_excerpt", "task_text"} <= recipient.data_policy
            and recipient.plugin_id in source.recipients
            and all(p.revision == rt.revision and rt.clock() < p.deadline
                and source.records.get(p.ref, (None,))[0] is p for p in propositions))

    @contextmanager
    def _source_fence(self, source, recipient, propositions):
        with source.lock, recipient.fence, ExitStack() as stack:
            valid = self._fenced_current(source, recipient, propositions)
            if valid:
                for p in propositions:
                    _, (engine, _, binding) = source.records[p.ref]
                    valid &= stack.enter_context(source.provider.literal_source_binding_fence(engine, binding))
            yield valid

    @contextmanager
    def dispatch_fence(self, target, recipient):
        request = self.requests.get(target)
        if request is None or request[0] is not recipient:
            yield False
            return
        engine = getattr(self.rt.agent(), "context_compressor", None)
        source = getattr(getattr(engine, "supervision", None), "_literal_source_registration", None)
        if source is None:
            yield False
            return
        with self._source_fence(source, recipient, request[1:3]) as current:
            yield current

    def validate(self, proposal):
        request = self.requests.get(proposal.target_id)
        if request is None:
            return False
        recipient, a, b, nodes = request
        meta = proposal.metadata
        return (proposal.feature_id == "F22" and proposal.action == Action.CONTEST_CLAIM
            and proposal.plugin_generation == recipient.generation
            and proposal.evidence_refs == (a.ref, b.ref) and not proposal.candidate_ids
            and proposal.relation == "incompatible" and proposal.template_id is None and not proposal.template_args
            and set(meta) == {"feature_action", "dependent_ids", "relation", "truth_winner", "suspend_optional_reuse"}
            and meta["feature_action"] == "contest_dependent_claim" and meta["relation"] == "incompatible"
            and meta["truth_winner"] is None and meta["suspend_optional_reuse"] is True
            and isinstance(meta["dependent_ids"], (tuple, list))
            and tuple(meta["dependent_ids"]) == tuple(n["id"] for n in nodes))
