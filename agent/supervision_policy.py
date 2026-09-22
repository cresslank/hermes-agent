"""Host-owned revision fences and silent settlement. No provider imports or network I/O."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import replace
import json
import threading
import time
import uuid
import weakref
from contextvars import ContextVar

from agent.supervision_types import (
    Action, Completeness, DecisionSnapshotV1, EffectReceiptV1,
    InterventionProposalV1, OwnerDecisionV1, OwnerRequestV1, Revision, SettlementV1,
    project,
)
from agent.supervision_context import (
    MAX_TEXT_BYTES, bounded_text, claim_candidates, digest, enumerate_requirements,
    is_accepted_origin, parse_work_map, refs_in_text,
)

# All rendered words belong to the host, not the semantic provider. Arguments are exact
# registered evidence IDs; source excerpts never become instructions or destinations.
TEMPLATES = {
    "review_action": "Reconsider the proposed operation against the linked requirement before retrying it.",
    "review_claim": "Check the linked claim against its exact evidence before finalizing; preserve uncertainty when support is missing.",
    "cover_requirement": "Address the linked requested item before finalizing, or state the concrete blocker.",
    "reuse_evidence": "Consider the linked existing evidence before repeating work; its permissions and freshness still apply.",
}
_ACTION_GRANTS = {a: a.value for a in Action}
_runtimes = weakref.WeakValueDictionary()
_registry_lock = threading.RLock()
_observer_callback = ContextVar("supervision_observer_callback", default=False)


def _advisory_template(proposal):
    """Decode a finite owner operation, never provider-authored instructions."""
    if proposal.template_args:
        return None
    if proposal.template_id in TEMPLATES:
        return proposal.template_id
    if (proposal.template_id is None and proposal.action == Action.CONTINUE
            and proposal.metadata.get("feature_action") == "continue_once_for_named_gap"
            and proposal.metadata.get("grants_finality") is False
            and proposal.metadata.get("global_coverage") == "unknown"):
        ids = proposal.metadata.get("requirement_ids")
        if (isinstance(ids, (tuple, list)) and ids
                and all(type(item) is str for item in ids)
                and set(ids) <= set(proposal.evidence_refs)):
            return "cover_requirement"
    return None


def runtime_for_agent(agent, *, create=False):
    from hermes_constants import hermes_home_key
    runtime = getattr(agent, "_supervision_runtime", None)
    if isinstance(runtime, SupervisionRuntime):
        return runtime if runtime.revision.profile == hermes_home_key() else None
    if not create:
        return None
    from hermes_constants import hermes_home_key
    from agent.supervision_facade import registrations_for_scope
    profile = hermes_home_key()
    if not any("observe" in r.grants for r in registrations_for_scope(profile)):
        return None
    lineage_fn = getattr(agent, "_conversation_root_id", None)
    lineage = lineage_fn() if callable(lineage_fn) else getattr(agent, "session_id", None)
    if not isinstance(lineage, str) or not lineage:
        return None
    runtime = SupervisionRuntime(agent, profile, lineage)
    agent._supervision_runtime = runtime
    from agent.supervision_view_binding import attach_views
    attach_views(runtime)
    return runtime


def runtime_for_revision(revision):
    with _registry_lock:
        return _runtimes.get((revision.profile, revision.lineage, revision.work_id))


class SupervisionRuntime:
    """One agent's semantic work. Only host execution owners may call effect methods.

    submit() is the sole worker-safe mutation; it only enqueues. Authenticated ingress
    invalidates revisions immediately under the same fence used by effect settlement.
    """
    def __init__(self, agent, profile, lineage, *, clock=time.monotonic):
        self.agent = weakref.ref(agent)
        self.clock = clock
        self.lock = threading.RLock()
        self.ready = threading.Condition(self.lock)
        self.revision = Revision(profile, lineage, uuid.uuid4().hex)
        self.sequence = 0
        self.sources = OrderedDict()
        self.requirements = ()
        self.completeness = Completeness()
        self.designated_refs = set()
        self.artifacts = OrderedDict()
        self.artifact_pins = {}
        self.work_maps = {}
        self.work_map_revisions = {}
        self.evidence = OrderedDict()
        self.pending_artifacts = []
        self.pending_artifact_bytes = 0
        self.pending = deque(maxlen=32)
        self.receipts = OrderedDict()
        self.incidents = set()
        self.opportunities = OrderedDict()
        self.action_edges = {}
        self.invalidated_action_edges = {}
        self.closed_targets = set()
        self.last_final = None
        self.last_final_text = ""
        self.round_deadline = None
        self.round_deadline_issued_at = None
        self.closed = False
        self.final_continuations = 0
        self._remember()

    def _assert_owner(self, *, tool_worker=False):
        agent = self.agent()
        tid = threading.get_ident()
        owner = getattr(agent, "_execution_thread_id", None)
        workers = getattr(agent, "_tool_worker_threads", ()) if tool_worker else ()
        if _observer_callback.get() or agent is None or (owner is not None and tid != owner and tid not in workers):
            raise RuntimeError("not_execution_owner")

    def _remember(self):
        with _registry_lock:
            _runtimes[(self.revision.profile, self.revision.lineage, self.revision.work_id)] = self

    def bind_turn(self):
        with self.lock:
            self.revision = replace(self.revision, run_generation=self.revision.run_generation + 1)
            self.round_deadline = None
            self.round_deadline_issued_at = None
            self.final_continuations = 0
            self.last_final = None
            self.last_final_text = ""
            self.closed_targets.clear()
            self.closed = False
        from agent.supervision_view_binding import reset_views
        reset_views(self, preserve_defaults=True)

    def shared_deadline(self, owner_deadline=None):
        with self.lock:
            if self.round_deadline is None:
                self.round_deadline_issued_at = self.clock()
                self.round_deadline = self.round_deadline_issued_at + .150
            return min(self.round_deadline, owner_deadline) if owner_deadline is not None else self.round_deadline

    def accept_instruction(self, origin):
        if not is_accepted_origin(origin):
            return False
        with self.lock:
            if origin.message_id in self.sources:
                return False
            self.closed = False
            if not origin.continuation:
                self.revision = replace(self.revision, work_id=uuid.uuid4().hex)
                self.sources.clear()
                self.requirements = ()
                self.designated_refs.clear()
                self.work_maps.clear()
                self.work_map_revisions.clear()
                self.artifacts.clear()
                self.artifact_pins.clear()
                self.evidence.clear()
                self.opportunities.clear()
                self.incidents.clear()
                self.action_edges.clear()
                self.pending_artifacts.clear()
                self.pending_artifact_bytes = 0
            self.work_maps.clear()  # accepted steering invalidates old action links immediately
            self.invalidated_action_edges.clear()
            self.revision = replace(self.revision, instruction_event=self.revision.instruction_event + 1,
                                    requirements=self.revision.requirements + 1)
            bounded, omitted = bounded_text(origin.text)
            self.sources[origin.message_id] = bounded
            while len(self.sources) > 12:
                self.sources.popitem(last=False)
            spans, complete = enumerate_requirements(origin.text, origin.message_id)
            self.requirements = (*self.requirements, *spans)[-32:]
            self.completeness = replace(complete, omitted=complete.omitted or omitted)
            self.designated_refs.update(refs_in_text(bounded))
            self._remember()
            self.ready.notify_all()  # stale a waiting action without granting a fresh budget
        from agent.supervision_view_binding import reset_views
        reset_views(self)
        binding = getattr(self.agent(), "_supervision_view_binding", None)
        if binding is not None:
            binding.accept_defaults(origin)
        self.observe("authenticated_instruction_admitted", {"requirements": project(spans), "source_message_id": origin.message_id,
                     "text": bounded, "continuation": origin.continuation},
                     completeness=self.completeness, origin_kind=origin.kind, data_class="task_text",
                     required_obligations=tuple(r.id for r in self.requirements))
        return True

    def _registrations(self):
        from agent.supervision_facade import registrations_for_scope
        return registrations_for_scope(self.revision.profile)

    def observe(self, event, facts, *, target_id=None, actions=(), evidence_refs=(),
                deadline=None, completeness=None, origin_kind="owner", data_class="task_text",
                owner="agent", candidates=(), required_ids=(), relations=(), required_data_classes=(),
                required_obligations=(), deadline_issued_at=None):
        regs = [r for r in self._registrations() if "observe" in r.grants and data_class in r.data_policy
                and set(required_data_classes) <= r.data_policy]
        if not regs or self.closed or (deadline is not None and deadline <= self.clock()):
            return None
        with self.lock:
            self.sequence += 1
            if deadline is None:
                issued = self.clock()
                expiry = issued + 1
            else:
                issued = deadline_issued_at if deadline_issued_at is not None else self.round_deadline_issued_at
                # Unknown issuance remains unknown; never mint a renewed remote wait.
                expiry = deadline
            event_id = uuid.uuid4().hex
            target_id = target_id or event_id
            self.opportunities[target_id] = {
                "revision": self.revision, "deadline": expiry, "actions": frozenset(actions),
                "refs": frozenset(evidence_refs), "owner": owner, "candidates": tuple(candidates),
                "required_ids": tuple(required_ids), "relations": tuple(relations),
            }
            while len(self.opportunities) > 64:
                self.opportunities.popitem(last=False)
            base = DecisionSnapshotV1(event, event_id, self.sequence, self.revision,
                {**facts, "target_id": target_id}, completeness or self.completeness, expiry,
                origin_kind=origin_kind, deadline_class="owner" if deadline is not None else "background",
                owner=owner, required_obligations=required_obligations, deadline_issued_at=issued,
                turn_id=getattr(self.agent(), "_current_turn_id", "") or "",
                tool_call_id=target_id if event == "action_proposed_with_scope_conflict" else "")
        for reg in regs:
            # Consumers only schedule their bounded worker. Never call arbitrary provider code
            # under the instruction/control fence, tool authorization lock or database lock.
            policy = reg.egress_policy
            if policy and set(base.facts) <= policy["fields"].keys():
                policy = {**policy, "fields": {key: policy["fields"][key] for key in base.facts},
                          "sources": {key: policy["sources"][key] for key in base.facts if key in policy["sources"]}}
            else:
                policy = {}  # legacy local class grants are not a remote egress grant
            snapshot = replace(base, data_policy=policy)
            token = _observer_callback.set(True)
            try:
                reg.consumer(snapshot.to_mapping())
            except Exception:
                # Third-party observers cannot break execution or leak raw exception/source text.
                reg.note_failure()
            finally:
                _observer_callback.reset(token)
        return base

    def _settle(self, proposal, status, reason):
        receipt = SettlementV1(proposal.proposal_id, status, reason, self.sequence, uuid.uuid4().hex)
        self.receipts[proposal.proposal_id] = receipt
        while len(self.receipts) > 256:
            self.receipts.popitem(last=False)
        return receipt

    def _validate(self, proposal, registration):
        if self.closed or not registration.active or proposal.plugin_generation != registration.generation:
            return "rejected", "revoked"
        if proposal.expected != self.revision:
            return "stale", "revision_changed"
        if self.clock() >= proposal.expires_at_monotonic:
            return "expired", "deadline"
        if _ACTION_GRANTS[proposal.action] not in registration.grants:
            return "rejected", "grant_missing"
        if proposal.target_id in self.closed_targets:
            return "stale", "target_closed"
        opportunity = self.opportunities.get(proposal.target_id)
        if opportunity is None or opportunity["revision"] != self.revision:
            return "stale", "opportunity_missing"
        if self.clock() >= opportunity["deadline"] or proposal.expires_at_monotonic > opportunity["deadline"]:
            return "expired", "opportunity_deadline"
        if proposal.owner != opportunity["owner"] or proposal.action not in opportunity["actions"]:
            return "rejected", "owner_action_mismatch"
        if not proposal.evidence_refs or not set(proposal.evidence_refs) <= opportunity["refs"]:
            return "rejected", "unbound_evidence"
        if proposal.incident_id in self.incidents:
            return "no_op", "incident_settled"
        if proposal.action in {Action.ADVISE, Action.CONTINUE}:
            if _advisory_template(proposal) is None:
                return "rejected", "invalid_template"
        if proposal.candidate_ids:
            if (len(set(proposal.candidate_ids)) != len(proposal.candidate_ids) or
                not set(proposal.candidate_ids) <= set(opportunity["candidates"]) or
                not set(opportunity["required_ids"]) <= set(proposal.candidate_ids)):
                return "rejected", "invalid_candidates"
        if proposal.relation is not None and proposal.relation not in opportunity["relations"]:
            return "rejected", "invalid_relation"
        agent = self.agent()
        if agent is None or getattr(agent, "_interrupt_requested", False) is True:
            return "rejected", "user_stop"
        return None

    def submit(self, proposal, registration):
        if not isinstance(proposal, InterventionProposalV1):
            raise TypeError("proposal_type")
        with self.ready:
            previous = self.receipts.get(proposal.proposal_id)
            if previous is not None:
                return previous
            failure = self._validate(proposal, registration)
            if failure:
                return self._settle(proposal, *failure)
            if len(self.pending) >= 32:
                return self._settle(proposal, "rejected", "queue_full")
            self.pending.append((proposal, registration))
            receipt = self._settle(proposal, "accepted", "queued")
            self.ready.notify_all()
        from agent.supervision_view_binding import proposal_queued
        proposal_queued(self, proposal.target_id)
        return receipt

    def _take(self, target_id, actions):
        """Effect-edge compare under the instruction fence. Called by execution owners only."""
        chosen = None
        pending = deque(maxlen=32)
        while self.pending:
            proposal, registration = self.pending.popleft()
            failure = self._validate(proposal, registration)
            if failure:
                self._settle(proposal, *failure)
            elif chosen is None and proposal.target_id == target_id and proposal.action in actions:
                chosen = (proposal, registration)
            else:
                pending.append((proposal, registration))
        self.pending = pending
        return chosen

    def _wait_for(self, target_id, deadline, revision):
        # One propagated absolute budget; no retry, sleep loop or renewed child allowance.
        with self.ready:
            self.ready.wait_for(lambda: self.revision != revision or self.closed or
                any(p.target_id == target_id for p, _ in self.pending),
                timeout=max(0, deadline - self.clock()))

    def _apply_advisory(self, entry):
        proposal, registration = entry
        with registration.fence:
            failure = self._validate(proposal, registration)
            if failure:
                self._settle(proposal, *failure)
                return None
            template = _advisory_template(proposal)
            if template is None:
                self._settle(proposal, "rejected", "invalid_template")
                return None
            self.incidents.add(proposal.incident_id)
            self._settle(proposal, "applied", "owner_advisory")
            refs = ", ".join(proposal.evidence_refs[:3])
            spans = {r.id: r.text for r in self.requirements}
            for work_map in self.work_maps.values():
                spans.update((r.id, r.text) for r in work_map["requirement_spans"])
            excerpt = next((spans[r] for r in proposal.evidence_refs if r in spans), "")
            return (f"[Task-bound advisory; lower-trust evidence, not an instruction]\n"
                    f"{TEMPLATES[template]}\nSource clause (quoted data): {json.dumps(excerpt[:160])}\nEvidence: {refs}")[:600]

    def drain_at_safe_point(self, *, allow_advisory=True):
        self._assert_owner()
        with self.lock:
            out = []
            if not allow_advisory:
                self._take("", set())  # only settle invalidated/expired proposals
                return ()
            efficiency = getattr(self, "efficiency", None)
            if efficiency is not None:
                advisory = efficiency.drain()
                if advisory:
                    return (advisory,)
            for target in tuple(self.opportunities):
                if self.opportunities[target]["owner"] in {"efficiency", "efficiency.check"}:
                    continue
                proposal = self._take(target, {Action.ADVISE})
                if proposal:
                    advisory = self._apply_advisory(proposal)
                    if advisory:
                        out.append(advisory)
                        break
            return tuple(out)

    def plan_steps(self):
        agent = self.agent()
        store = getattr(agent, "_todo_store", None)
        snapshot = store.snapshot() if store is not None and callable(getattr(store, "snapshot", None)) else {}
        if not isinstance(snapshot, dict):
            return {}, 0
        return {str(t["id"]): t for t in snapshot.get("todos", []) if isinstance(t, dict) and "id" in t}, snapshot.get("revision", 0)

    def designated_artifact_refs(self):
        todos, _ = self.plan_steps()
        return self.designated_refs | {r for t in todos.values() for r in refs_in_text(str(t.get("content", "")))}

    def record_artifact(self, path, text, *, verified, main_agent):
        with self.lock:
            pin = digest(text)
            if self.artifact_pins.get(path) == pin:
                return
            text_bytes = len(text.encode("utf-8"))
            if self.pending_artifact_bytes + text_bytes > MAX_TEXT_BYTES or len(self.pending_artifacts) >= 8:
                self.work_maps.pop(path, None)
                self.work_map_revisions.pop(path, None)
                self.invalidated_action_edges.clear()
                self.completeness = replace(self.completeness, complete=False, omitted=True)
                self.revision = replace(self.revision, evidence=self.revision.evidence + 1)
                return
            self.pending_artifact_bytes += text_bytes
            previous_text = self.artifacts.get(path)
            prior_map = self.work_maps.pop(path, None)
            self.work_map_revisions.pop(path, None)
            self.invalidated_action_edges.clear()
            self.artifact_pins[path] = pin
            self.artifacts[path] = text
            while len(self.artifacts) > 8:
                old, _ = self.artifacts.popitem(last=False)
                self.artifact_pins.pop(old, None)
                self.work_maps.pop(old, None)
            self.revision = replace(self.revision, evidence=self.revision.evidence + 1)
            receipt = EffectReceiptV1(uuid.uuid4().hex, "", path, pin,
                effect_class="mutation" if verified else "unknown", outcome="landed" if verified else "unknown",
                after_hash=pin if verified else None, status="verified" if verified else "candidate")
            self.evidence[receipt.receipt_id] = receipt
            todos, todo_revision = self.plan_steps()
            designated = self.designated_refs | {r for t in todos.values() for r in refs_in_text(str(t.get("content", "")))}
            if verified and main_agent:
                work_map = parse_work_map(text, artifact_ref=path, designated_refs=designated,
                                          sources=self.sources, todos=todos, artifacts=self.artifacts)
                if work_map:
                    if prior_map:
                        new_targets = {step["todo_id"]: set(step["target_refs"]) for step in work_map["steps"]}
                        for old_step in prior_map["steps"]:
                            if old_step["todo_id"] not in new_targets:
                                continue  # missing linkage is unknown, not a scope conflict
                            for old_target in set(old_step["target_refs"]) - new_targets[old_step["todo_id"]]:
                                self.invalidated_action_edges[old_target] = tuple(
                                    prior_map["requirement_spans"][i].id for i in old_step["requirement_indexes"])
                    self.work_maps[path] = work_map
                    self.work_map_revisions[path] = todo_revision
            claims, coverage = claim_candidates(text, path, required_refs=self.designated_refs, previous_text=previous_text)
            self.pending_artifacts.append({"receipt": project(receipt), "claims": project(claims),
                "work_map": project(self.work_maps.get(path)), "plan_revision": todo_revision,
                "completeness": project(coverage)})
            self.pending_artifacts = self.pending_artifacts[-8:]

    def committed_batch(self, messages):
        from agent.turn_iteration_prep import _previous_tool_round
        from agent.supervision_context import completed_batch_facts
        batch = _previous_tool_round(messages)
        with self.lock:
            artifacts, self.pending_artifacts = self.pending_artifacts, []
            self.pending_artifact_bytes = 0
            self.revision = replace(self.revision, evidence=self.revision.evidence + 1)
            self.round_deadline = None
            self.round_deadline_issued_at = None
            # Only committed exact artifact facts already bound to pending requirements qualify.
            receipts, source_findings, batch_coverage = completed_batch_facts(
                messages, revision=self.revision, requirements=self.requirements, target_refs=self.designated_refs)
            self.evidence.update((r.receipt_id, r) for r in receipts)
            findings = project(source_findings)
            for item in artifacts:
                receipt = item["receipt"]
                for claim in item["claims"][:8]:
                    finding = {"finding_id": claim["id"], "source_receipt": receipt["receipt_id"],
                        "span": claim, "status": "candidate", "consumer_refs": [r.id for r in self.requirements]}
                    if finding["consumer_refs"]:
                        findings.append(finding)
            while len(self.evidence) > 64:
                self.evidence.popitem(last=False)
        if artifacts or findings:
            self.observe("tool_batch_committed", {"artifacts": artifacts, "findings": findings,
                         "batch_size": len(batch), "requirements": project(self.requirements)},
                         evidence_refs=tuple(self.evidence), actions=(Action.ADVISE,),
                         completeness=batch_coverage, data_class="project_excerpt")

    def action_requirements(self, arguments):
        targets = {v for v in arguments.values() if type(v) is str}
        links = []
        todos, todo_revision = self.plan_steps()
        for path, work_map in self.work_maps.items():
            if self.work_map_revisions.get(path) != todo_revision:
                continue
            for step in work_map["steps"]:
                if step["todo_id"] not in todos or todos[step["todo_id"]].get("status") == "completed":
                    continue
                if targets.intersection(step["target_refs"]):
                    links.extend(work_map["requirement_spans"][i].id for i in step["requirement_indexes"])
        return tuple(dict.fromkeys(links))

    def prepare_action(self, tool_name, arguments, tool_call_id):
        self._assert_owner(tool_worker=True)
        # These owners expose a literal resource field. Shell/code/opaque connector
        # argument strings are not evidence of the target or effect class.
        target_fields = {"read_file": ("path",), "write_file": ("path",), "patch": ("path",)}.get(tool_name, ())
        targets = [arguments[k] for k in target_fields if type(arguments.get(k)) is str]
        links = tuple(dict.fromkeys(ref for target in targets
                      for ref in self.invalidated_action_edges.get(target, ())))
        if not links or tool_call_id in self.closed_targets:
            return None
        deadline = self.shared_deadline()
        revision = self.revision
        self.observe("action_proposed_with_scope_conflict", {"tool_name": tool_name, "arguments": arguments,
                     "requirement_refs": links, "linkage": "invalidated_known_edge",
                     "scope_conflict": True},
                     target_id=tool_call_id, actions=(Action.ADVISE,), evidence_refs=links,
                     deadline=deadline, data_class="task_text", required_obligations=links)
        self._wait_for(tool_call_id, deadline, revision)
        with self.lock:
            proposal = self._take(tool_call_id, {Action.ADVISE})
            advisory = self._apply_advisory(proposal) if proposal else None
            self.closed_targets.add(tool_call_id)
            return advisory

    def mark_dispatched(self, tool_call_id):
        with self.lock:
            self.closed_targets.add(tool_call_id)

    def prepare_final(self, text):
        self._assert_owner(tool_worker=True)
        if not isinstance(text, str) or not text or self.final_continuations or self.last_final == digest(text):
            return None
        self.last_final = digest(text)
        claims, coverage = claim_candidates(text, "final:" + self.last_final, required_refs=self.designated_refs,
                                            previous_text=self.last_final_text)
        self.last_final_text = bounded_text(text)[0]
        # Explicit lists qualify even when whole-request coverage is unknown. Free prose without
        # a link does not solicit generic semantic grading.
        if not self.requirements and not claims:
            return None
        agent = self.agent()
        if (getattr(agent, "_verification_stop_nudges", 0) or getattr(agent, "_pre_verify_nudges", 0)
                or getattr(getattr(agent, "iteration_budget", None), "remaining", 0) <= 0):
            return None
        from agent.supervision_final_projection import coverage_windows
        windows = list(coverage_windows(self.requirements, self.sources, text))
        if not windows:
            # Linked claims remain available to their evidence owner, but a clipped
            # answer or unauthenticated clause is not a negative-coverage window.
            return None
        target_root = "final:" + self.last_final
        deadline, revision = self.shared_deadline(), self.revision
        targets = []
        # Submit the bounded changed subsets under ONE original deadline before
        # waiting. An abstaining first subset must not hide a later required item.
        for index, facts in enumerate(windows):
            refs = tuple(row["source_ref"] for row in facts["requirements"])
            target = f"{target_root}:coverage:{index}"
            snapshot = self.observe("pre_final_candidate", facts, target_id=target,
                         actions=(Action.CONTINUE,), evidence_refs=refs, deadline=deadline,
                         completeness=replace(self.completeness,
                             omitted=self.completeness.omitted or bool(facts["omitted_requirement_ids"])),
                         data_class="task_text", required_obligations=refs)
            if snapshot is not None:
                targets.append(target)
        if not targets:
            return None
        with self.ready:
            self.ready.wait_for(lambda: self.revision != revision or self.closed or
                any(p.target_id in targets for p, _ in self.pending),
                timeout=max(0, deadline - self.clock()))
            advisory = None
            for target in targets:
                proposal = self._take(target, {Action.CONTINUE})
                if proposal and advisory is None:
                    advisory = self._apply_advisory(proposal)
                elif proposal:
                    self._settle(proposal[0], "no_op", "continuation_budget")
                self.closed_targets.add(target)
            if advisory is not None:
                self.final_continuations += 1
                return advisory
        return None

    def owner_decision(self, action, request):
        self._assert_owner(tool_worker=True)
        if not isinstance(request, OwnerRequestV1):
            raise TypeError("owner_request_type")
        baseline = OwnerDecisionV1(tuple(c["id"] for c in request.candidates))
        if (request.revision != self.revision or request.deadline <= self.clock() or not request.candidates
                or not request.data_policy):
            return baseline
        deadline = self.shared_deadline(request.deadline)
        ids = tuple(c["id"] for c in request.candidates)
        refs = request.evidence_refs or ids
        facts = project(request.facts) if request.event else {"candidates": project(request.candidates),
            "required_ids": request.required_ids, "critical_spans": request.critical_spans,
            "relations": request.relations}
        snapshot = self.observe(request.event or action.value, facts,
            target_id=request.target_id, actions=(action,),
            evidence_refs=refs, deadline=deadline, completeness=request.completeness,
            owner=request.owner, candidates=ids, required_ids=request.required_ids,
            relations=request.relations, data_class=request.data_policy[0],
            required_data_classes=request.data_policy, required_obligations=request.required_ids)
        if snapshot is None:
            return baseline
        self._wait_for(request.target_id, deadline, request.revision)
        with self.lock:
            entry = self._take(request.target_id, {action})
            decision = self._apply_owner_decision(entry, request, action, baseline) if entry else baseline
            self.closed_targets.add(request.target_id)
            return decision

    def _apply_owner_decision(self, entry, request, action, baseline):
        proposal, registration = entry
        with registration.fence:
            failure = self._validate(proposal, registration)
            if failure:
                self._settle(proposal, *failure)
                return baseline
            selected_ids = proposal.candidate_ids or (baseline.candidate_ids if action == Action.EVALUATE_RELATION else ())
            selected = [c for c in request.candidates if c["id"] in selected_ids]
            if any(not any(span in c.get("excerpt", "") for c in selected) for span in request.critical_spans):
                self._settle(proposal, "rejected", "critical_span_omitted")
                return baseline
            if action != Action.EVALUATE_RELATION and not proposal.candidate_ids:
                self._settle(proposal, "rejected", "empty_selection")
                return baseline
            if action == Action.EVALUATE_RELATION and proposal.relation not in request.relations:
                self._settle(proposal, "rejected", "missing_relation")
                return baseline
            self.incidents.add(proposal.incident_id)
            receipt = self._settle(proposal, "applied", "owner_selection")
            ids = proposal.candidate_ids or baseline.candidate_ids
            if action == Action.RANK_CANDIDATES:
                ids = (*ids, *(i for i in baseline.candidate_ids if i not in ids))
            return OwnerDecisionV1(ids, proposal.relation, True, receipt.receipt_id, proposal.metadata)

    def consume_owner_action(self, target_id, action, apply):
        """HOST OWNER ONLY, at its existing execution/safe-point boundary.

        `apply(proposal)` is synchronous and must compare the owner's own control
        revision under its control lock before returning applied/no_op/stale/rejected.
        No provider I/O, arbitrary commands, waiting or new agent turns belong here.
        """
        self._assert_owner(tool_worker=True)
        action = Action(action)
        with self.lock:
            entry = self._take(target_id, {action})
            if entry is None:
                return None
            proposal, registration = entry
            with registration.fence:
                failure = self._validate(proposal, registration)
                if failure:
                    return self._settle(proposal, *failure)
                status = apply(proposal)
                if status not in {"applied", "no_op", "stale", "rejected"}:
                    raise ValueError("invalid_owner_settlement")
                if status == "applied":
                    self.incidents.add(proposal.incident_id)
                return self._settle(proposal, status, "owner_settlement")

    def finish_turn(self):
        from agent.supervision_view_binding import reset_views
        reset_views(self)
        with self.ready:
            self.closed = True
            self.ready.notify_all()

    def revoke(self):
        from agent.supervision_view_binding import close_views
        close_views(self)
        with self.ready:
            self.closed = True
            self.revision = replace(self.revision, run_generation=self.revision.run_generation + 1)
            for proposal, _ in self.pending:
                self._settle(proposal, "rejected", "revoked")
            self.pending.clear()
            self.sources.clear()
            self.requirements = ()
            self.artifacts.clear()
            self.work_maps.clear()
            self.evidence.clear()
            self.ready.notify_all()
