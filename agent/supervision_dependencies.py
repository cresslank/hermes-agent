"""Bounded, source-linked optional support state. Never instruction or truth authority.

Work-map v1 supplies claim/requirement links, not evidence or qualifier declarations.
Only literal references in a linked claim resolve retained complete source bytes.
Missing source, large windows and unknown comparability remain unknown. No I/O,
new model turn, generic grading, or competing persistence lives in this owner.
"""
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from agent.supervision_context import digest, exact_span, refs_in_text, complete_requirement_context, visible_exact_occurrence
from agent.supervision_types import Action, Completeness

RELATION_ACTIONS = {
    "support_edge": "update_dependencies",
    "affected_dependency_invalidation": "update_dependencies",
    "targeted_gap": "continue",
}
RELATION_VERSION = "supervision.dependencies.v1"


@dataclass(frozen=True)
class LinkedOpportunity(Completeness):
    relevant: bool = True


@dataclass(frozen=True)
class Dependency:
    id: str
    claim: object
    requirement: object
    source: object
    state: str = "unknown"
    relation: str | None = None
    registration: str | None = None


class DependencyOwner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.edges = OrderedDict()
        self.bindings = {}
        self.requests = OrderedDict()
        self.instruction_relations = OrderedDict()
        self.revocations = OrderedDict()
        self.contested_claims = set()
        from agent.supervision_planning import PlanningGraph
        self.planning = PlanningGraph(self)

    def clear(self):
        self.edges.clear()
        self.bindings.clear()
        self.requests.clear()
        self.instruction_relations.clear()
        self.revocations.clear()
        self.contested_claims.clear()
        self.planning.clear()

    def invalidate_source(self, path, new_pin):
        # Immediate exact version invalidation, independent of semantic availability.
        # Keep both old spans and any replacement in the source owner. Never infer
        # comparability of time, entity, units or conditions from a shared file name.
        from dataclasses import replace
        for key, edge in tuple(self.edges.items()):
            if any(s.source_message_id == path and s.source_revision != new_pin
                   for s in (edge.claim, edge.source)):
                self.edges[key] = replace(edge, state="source_invalidated", registration=None)
                self.revocations[key] = {"dependent_ids": [key], "source_ref": edge.source.id,
                    "claim_ref": edge.claim.id, "replacement_pin": new_pin,
                    "deterministic_invalidation": True}
                self.runtime.last_final = None

    def committed(self):
        rt = self.runtime
        with rt.lock:
            rows = tuple(self.revocations.values())
            self.revocations.clear()
        for facts in rows:
            rt.observe("evidence_revoked", facts, owner="dependencies",
                evidence_refs=(facts["source_ref"], facts["claim_ref"]),
                data_class="project_excerpt", completeness=Completeness(scope="linked_candidates"))

    def optional_support(self, edge_id):
        """A reusable semantic relation only, NOT sufficient evidence or finality."""
        rt = self.runtime
        with rt.lock:
            edge = self.edges.get(edge_id)
            if rt.closed:
                return None
            if edge is None or edge.state != "supported" or edge.claim.id in self.contested_claims:
                return None
            if not any(r.generation == edge.registration and r.active for r in rt._registrations()):
                return None
            if not self._current(edge):
                return None
            return edge

    def _current(self, edge):
        rt = self.runtime
        binding = self.bindings.get(edge.id)
        if binding is None:
            return False
        path, pin, todo_revision = binding
        if (path not in rt.work_maps or rt.artifact_pins.get(path) != pin
                or rt.plan_steps()[1] != todo_revision):
            return False
        source = rt.sources.get(edge.requirement.source_message_id)
        return (source is not None and digest(source) == edge.requirement.source_revision
                and rt.artifact_pins.get(edge.claim.source_message_id) == edge.claim.source_revision
                and rt.artifact_pins.get(edge.source.source_message_id) == edge.source.source_revision)

    def _remember(self, target, kind, facts, ids):
        self.requests[target] = (kind, facts, tuple(ids))
        while len(self.requests) > 64:
            self.requests.popitem(last=False)

    def instruction_changed(self, origin, spans):
        """Called after epoch advance; only authenticated user-to-user exact-ref pairs.

        Existing proposal invalidation is unconditional. Semantic interpretation can
        only suspend known optional support. It cannot delete or replace requirements.
        """
        rt = self.runtime
        with rt.lock:
            expected = rt.revision
            candidates = self._instruction_candidates(origin, spans)
        # Never call a provider while holding the authenticated-input fence.
        # An intervening accepted input rejects this exact captured opportunity.
        for target, facts, refs in candidates:
            rt.observe("authenticated_instruction_admitted", facts, target_id=target,
                actions=(Action.UPDATE_DEPENDENCIES,), evidence_refs=refs,
                owner="dependencies", data_class="task_text", expected_revision=expected,
                completeness=Completeness(scope="linked_candidates", complete=False))

    def _instruction_candidates(self, origin, spans):
        rt = self.runtime
        if not origin.continuation:
            self.clear()
            return []
        candidates = []
        # The instruction epoch and removed plan bindings already fence all old
        # proposals/reuse immediately. Preserve source relations themselves until
        # a known affected edge is classified; unrelated evidence is not erased.
        for new in spans[:32]:
            refs = set(refs_in_text(new.text))
            prior = OrderedDict()
            for edge in self.edges.values():
                old = edge.requirement
                common = refs & set(refs_in_text(old.text))
                if not common or old.source_message_id == new.source_message_id:
                    continue
                if old.source_message_id not in rt.sources or len(old.text) > 2400 or len(new.text) > 2400:
                    continue
                row = prior.setdefault(old.id, {"id": old.id, "text": old.text, "ref": old.id,
                    "common_scope": sorted(common)[0], "precedence_allows": True, "affected_ids": []})
                if len(row["affected_ids"]) < 4:
                    row["affected_ids"].append(edge.id)
            rows = list(prior.values())
            for offset in range(0, len(rows), 4):
                subset = rows[offset:offset + 4]
                facts = {"new_instruction": {"id": new.id, "text": new.text, "ref": new.id},
                    "origin_authenticated": True, "origin_kind": "user", "quoted": False,
                    "epoch_advanced": True, "explicit_stop": False, "prior": subset}
                target = "instruction:" + digest(new.id + str(offset))
                ids = tuple(i for row in subset for i in row["affected_ids"])
                self._remember(target, "instruction", facts, ids)
                candidates.append((target, facts, (new.id, *(r["ref"] for r in subset))))
        return candidates

    def final_windows(self, text, deadline):
        rt = self.runtime
        with rt.lock:
            expected = rt.revision
            candidates = self._final_candidates(text)
        targets = []
        for target, facts, refs in candidates:
            snap = rt.observe("pre_final_candidate", facts, target_id=target,
                actions=(Action.UPDATE_DEPENDENCIES, Action.CONTINUE),
                evidence_refs=refs, owner="dependencies", expected_revision=expected,
                deadline=deadline, data_class="project_excerpt", required_data_classes=("task_text",),
                completeness=Completeness(scope="linked_candidates", complete=False))
            if snap is not None:
                targets.append(target)
        return targets

    def _final_candidates(self, text):
        """Only changed finals containing exact work-map-linked claims and source refs.

        Preserve the entire bounded source as its qualifier context. This does not
        assert that all qualifiers/evidence are known, nor an atomic decomposition.
        """
        rt = self.runtime
        from agent.supervision_context import MAX_TEXT_BYTES
        if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            return []
        targets = []
        todos, todo_revision = rt.plan_steps()
        for path, work_map in rt.work_maps.items():
            if rt.work_map_revisions.get(path) != todo_revision:
                continue
            for row, claim in zip(work_map["claims"], work_map["claim_spans"]):
                body = rt.artifacts.get(row["artifact_ref"])
                if body is None or len(body) > 2400 or not 0 <= row["start"] < row["end"] <= len(body):
                    continue
                if digest(body) != claim.source_revision:
                    continue
                # A unique complete literal span must be in this final, not a topical
                # match. Source text inside a fence/quote is not an asserted claim.
                if len(claim.text) > 2400 or not visible_exact_occurrence(text, claim.text):
                    continue
                refs = tuple(ref for ref in refs_in_text(claim.text)
                             if ref != row["artifact_ref"] and ref in rt.artifacts)
                if not refs or len(refs) > 4:
                    continue
                for index in row["requirement_indexes"]:
                    requirement = work_map["requirement_spans"][index]
                    if len(requirement.text) > 2400 or not complete_requirement_context(rt, requirement):
                        continue
                    edges = []
                    for ref in refs:
                        source_text = rt.artifacts[ref]
                        if not source_text or len(source_text) > 2400:
                            edges = []
                            break
                        source = exact_span(ref, digest(source_text), source_text, 0, len(source_text), kind="source_window")
                        key = digest(claim.id + requirement.id + source.id)
                        edge = self.edges.get(key) or Dependency(key, claim, requirement, source)
                        if key not in self.bindings and len(self.bindings) >= 64:
                            edges = []
                            break
                        self.bindings[key] = (path, rt.artifact_pins[path], todo_revision)
                        if not self._current(edge):
                            edges = []
                            break
                        edges.append(edge)
                    if not edges or all(self.optional_support(e.id) for e in edges):
                        continue
                    # Never evict unresolved source dependencies to claim coverage.
                    if len(self.edges.keys() | {e.id for e in edges}) + len(self.planning.nodes) > 64:
                        continue
                    self.edges.update((e.id, e) for e in edges)
                    candidates = [{"id": e.id, "ref": e.source.id, "excerpt": e.source.text,
                        "qualifiers": e.source.text, "source_available": True, "quote_located": True}
                        for e in edges]
                    facts = {"changed": True, "immutable_verified": False,
                        "claim": {"id": claim.id, "text": claim.text, "scope": body},
                        "linked_requirement_id": requirement.id, "evidence_contract": requirement.text,
                        "deterministic_contract": "unknown", "candidates": candidates}
                    target = "support:" + digest(digest(text) + claim.id + requirement.id)
                    self._remember(target, "support", facts, [e.id for e in edges])
                    targets.append((target, facts, tuple(e.source.id for e in edges)))
                    if len(targets) == 8:
                        return targets
        return targets

    def validate(self, proposal):
        request = self.requests.get(proposal.target_id)
        if request is None:
            return False
        kind, facts, ids = request
        meta = proposal.metadata
        if proposal.candidate_ids or proposal.relation is not None or proposal.template_args or proposal.template_id is not None:
            return False
        rows = meta.get("relations")
        if not isinstance(rows, (tuple, list)) or not rows or any(not isinstance(r, Mapping) for r in rows):
            return False
        if kind == "instruction":
            if set(meta) != {"feature_action", "affected_ids", "relations", "preserve_original_spans"}:
                return False
            affected_ids = meta.get("affected_ids")
            if not isinstance(affected_ids, (tuple, list)) or not affected_ids or not all(type(i) is str for i in affected_ids):
                return False
            if len(set(affected_ids)) != len(affected_ids):
                return False
            if (proposal.feature_id != "F21" or proposal.action != Action.UPDATE_DEPENDENCIES
                    or meta.get("feature_action") != "affected_dependency_invalidation"
                    or meta.get("preserve_original_spans") is not True):
                return False
            prior = {r["id"]: r for r in facts["prior"]}
            affected, refs, seen = set(), {facts["new_instruction"]["ref"]}, set()
            for row in rows:
                if any(type(v) is not str for v in row.values()):
                    return False
                old = prior.get(row.get("prior_id"))
                if (set(row) != {"prior_id", "relation", "scope"} or old is None
                        or row["prior_id"] in seen or row["scope"] != old["common_scope"]
                        or row["relation"] not in {"adds", "narrows", "replaces", "clarifies"}):
                    return False
                seen.add(row["prior_id"])
                refs.add(old["ref"])
                affected.update(old["affected_ids"])
            return (set(meta.get("affected_ids", ())) == affected and set(proposal.evidence_refs) == refs
                    and all(i in self.edges for i in affected))
        if set(meta) != {"feature_action", "claim_id", "relations", "semantic_only", "certifies_truth", "grants_finality", "deterministic_contract"}:
            return False
        if (proposal.feature_id != "F17" or meta.get("claim_id") != facts["claim"]["id"]
                or meta.get("semantic_only") is not True or meta.get("certifies_truth") is not False
                or meta.get("grants_finality") is not False or meta.get("deterministic_contract") != "unknown"):
            return False
        candidates = {r["id"]: r for r in facts["candidates"]}
        seen, refs, gaps = set(), set(), False
        for row in rows:
            if any(type(v) is not str for v in row.values()):
                return False
            candidate = candidates.get(row.get("candidate_id"))
            if (set(row) != {"candidate_id", "ref", "relation"} or candidate is None
                    or row["candidate_id"] in seen or row["ref"] != candidate["ref"]
                    or row["relation"] not in {"supports", "partial", "contradicts", "not_addressed"}):
                return False
            seen.add(row["candidate_id"])
            refs.add(row["ref"])
            gaps |= row["relation"] != "supports"
        expected_action = "targeted_gap" if gaps else "support_edge"
        expected_native = Action.CONTINUE if gaps else Action.UPDATE_DEPENDENCIES
        return (proposal.action == expected_native and meta.get("feature_action") == expected_action
                and set(proposal.evidence_refs) == refs and all(self._current(self.edges[i]) for i in ids))

    def consume(self, entry, *, allow_continuation=False):
        from dataclasses import replace
        rt = self.runtime
        proposal, registration = entry
        with registration.fence:
            failure = rt._validate(proposal, registration)
            if failure or not self.validate(proposal):
                rt._settle(proposal, *(failure or ("rejected", "invalid_dependency_relation")))
                return None
            kind, facts, ids = self.requests[proposal.target_id]
            if kind == "instruction":
                for key in proposal.metadata["affected_ids"]:
                    self.edges[key] = replace(self.edges[key], state="instruction_affected", registration=None)
                self.instruction_relations[proposal.target_id] = proposal.metadata["relations"]
                while len(self.instruction_relations) > 32:
                    self.instruction_relations.popitem(last=False)
            else:
                for row in proposal.metadata["relations"]:
                    key = row["candidate_id"]
                    state = {"supports": "supported", "partial": "gap", "not_addressed": "gap", "contradicts": "contested"}[row["relation"]]
                    self.edges[key] = replace(self.edges[key], state=state, relation=row["relation"], registration=registration.generation)
                    if state == "contested":
                        self.contested_claims.add(self.edges[key].claim.id)
            if proposal.action == Action.CONTINUE:
                if allow_continuation:
                    return rt._apply_advisory(entry)
                # Relation state was consumed even though another named gap used
                # the one continuation. Do not misreport that state change as no-op.
                rt.incidents.add(proposal.incident_id)
                rt._settle(proposal, "applied", "dependency_relation")
                return None
            rt.incidents.add(proposal.incident_id)
            rt._settle(proposal, "applied", "dependency_relation")
            return None

    def drain(self):
        rt = self.runtime
        for target in tuple(self.requests):
            entry = rt._take(target, {Action.UPDATE_DEPENDENCIES})
            if entry:
                self.consume(entry)
