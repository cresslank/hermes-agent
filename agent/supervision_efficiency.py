"""Bounded native efficiency producers; no provider, model calls or effect grants.

Owners register concrete routes/check contracts, not model-supplied fact dictionaries.
Absent contracts deliberately leave the original execution path unchanged. Semantic
advice cannot cancel children, waive checks, close requirements or execute a route.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
from typing import Callable

from agent.supervision_types import Action, Completeness


@dataclass(frozen=True)
class OpportunityCompleteness(Completeness):
    # Relevant to a qualified owner incident; completeness stays unknown.
    relevant: bool = True


def _pin(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 2400


def _ids(values, maximum=4):
    return isinstance(values, tuple) and 0 < len(values) <= maximum and all(_text(v) for v in values) and len(set(values)) == len(values)


@dataclass(frozen=True)
class Route:
    """A route the native owner already implements, with a LIVE authorization predicate.

    The predicate must be cheap and synchronous and certify CURRENT permission,
    privacy policy, prerequisite availability and any retry-after restriction.
    No execution callback is exposed to the supervisor. A recommendation must
    still traverse normal dispatch policy; registration itself grants nothing.
    """
    id: str
    text: str
    scope: str
    prerequisites: str
    prerequisite_revision: str
    side_effects: str
    effect_class: str
    permitted: Callable[[], bool]

    def __post_init__(self):
        if (not all(_text(getattr(self, k)) for k in ("id", "text", "scope", "prerequisites", "prerequisite_revision", "side_effects"))
                or self.id in {"none", "insufficient"} or self.effect_class != "readonly" or not callable(self.permitted)):
            raise ValueError("invalid_readonly_route")

    def available(self):
        try:
            return self.permitted() is True
        except Exception:
            return False

    def facts(self):
        return {"id": self.id, "text": self.text, "scope": self.scope,
                "prerequisites": self.prerequisites, "prerequisite_revision": self.prerequisite_revision,
                "side_effects": self.side_effects, "effect_class": self.effect_class,
                "authorized": True, "available": True, "privacy_allowed": True,
                "prerequisites_available": True, "retry_after_satisfied": True, "mutation_retry_safe": False}


@dataclass(frozen=True)
class VerificationCheck:
    """Explicit owner scope. Shell exit status is never a verification contract.

    All protection flags are required. Fingerprints come from the check owner,
    which must supply a fresh current() comparison before result reuse.
    """
    id: str
    text: str
    claim_ids: tuple[str, ...]
    snapshot: str
    input_fingerprint: str
    environment: str
    dependency_fingerprint: str
    required: bool
    external_write_readback: bool
    acceptance_test: bool
    independent_review: bool
    time_sensitive: bool
    explicit_user_check: bool
    plugin_owned: bool = False

    def __post_init__(self):
        if (not all(_text(getattr(self, k)) for k in ("id", "text", *self.fingerprint_fields()))
                or not _ids(self.claim_ids) or any(type(getattr(self, k)) is not bool for k in (*self.protected_fields(), "plugin_owned"))):
            raise ValueError("invalid_verification_contract")

    @staticmethod
    def fingerprint_fields():
        return ("snapshot", "input_fingerprint", "environment", "dependency_fingerprint")

    @staticmethod
    def protected_fields():
        return ("required", "external_write_readback", "acceptance_test", "independent_review", "time_sensitive", "explicit_user_check")

    @property
    def protected(self):
        return any(getattr(self, k) for k in self.protected_fields())

    def facts(self):
        return {**self.__dict__, "claim_ids": list(self.claim_ids),
                "owner": "plugin" if self.plugin_owned else "main", "optional": not self.protected, "issued": False}


@dataclass(frozen=True)
class DelegationIntent:
    """Existing planner declaration; unknown independence is not inferred from goal prose."""
    goal: str
    acceptance: str
    inputs: str
    parent_step_id: str
    parent_next_step: str
    parent_capability: str
    separable: bool
    trivial_lookup: bool
    shared_mutation: bool
    budget_available: bool
    overhead_favorable: bool
    parallelism_favorable: bool
    route_id: str

    def __post_init__(self):
        for name, value in self.__dict__.items():
            if name in {"separable", "trivial_lookup", "shared_mutation", "budget_available", "overhead_favorable", "parallelism_favorable"}:
                if type(value) is not bool:
                    raise ValueError("delegation_intent")
            elif not _text(value):
                raise ValueError("delegation_intent")


@dataclass(frozen=True)
class ResearchGap:
    id: str
    text: str
    mandatory: bool

    def __post_init__(self):
        if not _text(self.id) or not _text(self.text) or type(self.mandatory) is not bool:
            raise ValueError("research_gap")


@dataclass(frozen=True)
class ResearchPass:
    id: str
    text: str
    source_method: str
    gap_ids: tuple[str, ...]
    accepted_evidence_ids: tuple[str, ...]
    rejected_evidence_ids: tuple[str, ...]

    def __post_init__(self):
        if not all(_text(getattr(self, k)) for k in ("id", "text", "source_method")) or not _ids(self.gap_ids):
            raise ValueError("research_pass")
        for refs in (self.accepted_evidence_ids, self.rejected_evidence_ids):
            if refs != () and not _ids(refs, 16):
                raise ValueError("research_evidence")
        if set(self.accepted_evidence_ids) & set(self.rejected_evidence_ids):
            raise ValueError("research_evidence_conflict")


class EfficiencyOwner:
    """Turn-local owner ledger. Bounded IDs, receipts and recurrence, no history patrol."""
    receipt_reuse_version = "supervision.receipt-reuse.v1"

    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.routes = OrderedDict()
        self.attempt_policies = {}
        self.native_attempts = {}
        self.requirement_links = {}
        self.read_intents = {}
        self.delegations = {}
        self.hints = deque(maxlen=4)
        self.attempts = deque(maxlen=12)
        self.reads = OrderedDict()
        self.read_pending = {}
        self.read_payloads = {}
        self.checks = OrderedDict()
        self.passes = deque(maxlen=3)
        self.emitted = OrderedDict()
        self.bound_routes = {}
        self.incident_by_target = {}
        self.handled_incidents = set()
        self.guards = {}
        self.attempted_routes = deque(maxlen=12)
        self.sequence = 0
        self.epoch = None

    def _sync(self):
        r = self.runtime.revision
        epoch = (r.work_id, r.run_generation, r.instruction_event, r.requirements)
        if epoch != self.epoch:
            self.attempts.clear()
            self.attempt_policies.clear()
            self.native_attempts.clear()
            self.requirement_links.clear()
            self.read_intents.clear()
            self.delegations.clear()
            self.hints.clear()
            self.reads.clear()
            self.read_pending.clear()
            self.read_payloads.clear()
            self.checks.clear()
            self.passes.clear()
            self.emitted.clear()
            self.bound_routes.clear()
            self.incident_by_target.clear()
            self.handled_incidents.clear()
            self.guards.clear()
            self.attempted_routes.clear()
            self.epoch = epoch

    def admit_read_intent(self, path, *, independent_check, requirement_id=None):
        """Native planner declares independence; missing intent never means false."""
        self.runtime._assert_owner(tool_worker=True)
        if (type(independent_check) is not bool or not _text(path)
                or requirement_id is not None and not _text(requirement_id)):
            raise ValueError("read_intent")
        with self.lock:
            self._sync()
            if len(self.read_intents) >= 32 and path not in self.read_intents:
                raise ValueError("read_intent_capacity")
            self.read_intents[path] = independent_check
            self.requirement_links["read_file", path] = requirement_id

    def admit_attempt_policy(self, name, path, *, registered_poll, registered_retry,
                             pagination, user_repetition, deterministic_handler_available, requirement_id=None):
        """Native retry owner explicitly qualifies one route; no prose inference."""
        self.runtime._assert_owner(tool_worker=True)
        flags = dict(registered_poll=registered_poll, registered_retry=registered_retry,
                     pagination=pagination, user_repetition=user_repetition,
                     deterministic_handler_available=deterministic_handler_available)
        if (not _text(name) or not _text(path) or any(type(v) is not bool for v in flags.values())
                or requirement_id is not None and not _text(requirement_id)):
            raise ValueError("attempt_policy")
        with self.lock:
            self._sync()
            if len(self.attempt_policies) >= 32 and (name, path) not in self.attempt_policies:
                raise ValueError("attempt_policy_capacity")
            self.attempt_policies[name, path] = flags
            self.requirement_links[name, path] = requirement_id

    def drain(self):
        self.runtime._assert_owner()
        if self.hints:
            return self.hints.popleft()
        for target, opportunity in tuple(self.runtime.opportunities.items()):
            if opportunity["owner"] == "efficiency":
                advisory = self.consume(target)
                if advisory:
                    return advisory
        return None

    def register_route(self, route):
        self.runtime._assert_owner(tool_worker=True)
        if not isinstance(route, Route):
            raise TypeError("route_contract_required")
        with self.lock:
            if route.id not in self.routes and len(self.routes) >= 4:
                raise ValueError("route_capacity")
            self.routes[route.id] = route

    def revoke_route(self, route_id):
        with self.lock:
            self.routes.pop(route_id, None)

    def route_attempted(self, route_id):
        """Called by the route's existing dispatch owner, not the semantic provider."""
        self.runtime._assert_owner(tool_worker=True)
        with self.lock:
            self._sync()
            route = self.routes.get(route_id)
            if route is not None:
                self.attempted_routes.append({"id": route.id, "prerequisite_revision": route.prerequisite_revision})

    def _available(self):
        return [r for r in self.routes.values() if r.available() and not any(
            a["id"] == r.id and a["prerequisite_revision"] == r.prerequisite_revision for a in self.attempted_routes)]

    def _requirement(self, name, arguments):
        # Even a singleton ledger is not an operation-to-requirement edge.
        if not isinstance(arguments, dict):
            return None
        refs = self.runtime.action_requirements(arguments)
        declared = self.requirement_links.get((name, arguments.get("path")))
        if declared is not None:
            refs = (declared,)
        if len(refs) != 1:
            return None
        spans = {r.id: r.text for r in self.runtime.requirements}
        for work_map in self.runtime.work_maps.values():
            spans.update((r.id, r.text) for r in work_map["requirement_spans"])
        return {"id": refs[0], "text": spans[refs[0]]} if refs[0] in spans else None

    def _emit(self, event, facts, target, refs, *, routes=(), wait=False, relations=(), valid=None):
        key = (event, target)
        with self.lock:
            if key in self.emitted or len(self.emitted) >= 64:
                return None
            self.emitted[key] = True
            self.bound_routes[target] = {r.id: r for r in routes}
            if _text(facts.get("incident_id")):
                self.incident_by_target[target] = facts["incident_id"]
            if valid is not None:
                self.guards[target] = valid
        facts = {**facts, "changed": True, "evidence_refs": list(refs)}
        rt = self.runtime
        deadline = rt.shared_deadline() if wait else None
        snapshot = rt.observe(event, facts, target_id=target, evidence_refs=refs,
                              actions=(Action.ADVISE,), candidates=tuple(r.id for r in routes),
                              owner="efficiency", completeness=OpportunityCompleteness(),
                              relations=relations, deadline=deadline, data_class="project_excerpt")
        if snapshot is None:
            return None
        if wait:
            rt._wait_for(target, snapshot.deadline, snapshot.revision)
            try:
                return self.consume(target, routes=routes)
            finally:
                rt.mark_dispatched(target)
        return target

    def consume(self, target, *, routes=()):
        """One ID-bound advisory, at the normal owner boundary. No route execution.

        Revalidate registered route identity and live permission under the owner's
        lock. Plain ADVISE rendering does not establish any verification or completion.
        """
        result = []
        allowed = self.bound_routes.get(target, {})
        def apply(proposal):
            with self.lock:
                guard = self.guards.get(target)
                try:
                    if guard is not None and guard() is not True:
                        return "stale"
                except Exception:
                    return "stale"
                incident = self.incident_by_target.get(target)
                if incident in self.handled_incidents:
                    return "no_op"
                m = proposal.metadata
                route_id = m.get("route_id")
                if route_id is not None:
                    route = allowed.get(route_id)
                    if route is None or not route.available() or self.routes.get(route_id) != route:
                        return "stale"
                    if any(a["id"] == route_id and a["prerequisite_revision"] == route.prerequisite_revision for a in self.attempted_routes):
                        return "no_op"
                    detail = "Existing route (quoted data): " + json.dumps({"id": route.id, "description": route.text})
                else:
                    detail = "Existing evidence: " + ", ".join(proposal.evidence_refs[:4])
                from agent.supervision_policy import TEMPLATES
                result.append("[Task-bound advisory; lower-trust evidence, not an instruction]\n" +
                              TEMPLATES[proposal.template_id] + "\n" + detail)
                if incident is not None:
                    self.handled_incidents.add(incident)
                return "applied"
        self.runtime.consume_owner_action(target, Action.ADVISE, apply)
        return result[0] if result else None

    def failed_tool(self, name, arguments, result, *, call_id, failed):
        """Native post-call observation. Poll/retry tools and changing targets excluded.

        A failure with no literal resource target cannot establish locality; opaque
        shell/code commands therefore never acquire semantic-loop metadata here.
        """
        from agent.tool_guardrails import is_stall_guard_repeatable
        self.runtime._assert_owner(tool_worker=True)
        with self.lock:
            self._sync()
            self.sequence += 1
            receipt = self.native_attempts.pop(call_id, None)
            if not failed:
                self.attempts.clear()  # conservative progress: never equate success with no progress
                return None
            requirement = self._requirement(name, arguments)
            target = arguments.get("path") if isinstance(arguments, dict) else None
            if (not requirement or not _text(target) or not isinstance(result, str)
                    or not _text(call_id) or is_stall_guard_repeatable(name)):
                self.attempts.clear()
                return None
            policy = self.attempt_policies.get((name, target))
            valid = None
            category, mechanism = name, "tool_reported_failure"
            if receipt is not None:
                if (name != "read_file" or receipt.arguments_pin != _pin(arguments) or receipt.result != result
                        or not receipt.current()):
                    return None
                policy = receipt.policy
                valid = receipt.current
                category, mechanism, _ = receipt.classification
            if policy is None or any(policy.values()):
                self.attempts.clear()  # unknown/exempt work cannot certify uninterrupted no progress
                return None
            routes = self._available()
            if receipt is not None:
                routes = [r for r in routes if r == receipt.route]
            if not routes:
                self.attempts.clear()
                return None
            r = self.runtime.revision
            pin = _pin((r.instruction_event, r.requirements, tuple(sorted(self.runtime.artifact_pins.items())),
                        receipt.route.prerequisite_revision if receipt is not None else None))
            attempt = {"id": call_id, "text": name + " " + target, "outcome": "failed",
                       "failure_excerpt": result[:600], "target": target, "route_family": name,
                       "artifact_revision": pin, "evidence_revision": _pin(result),
                       "progress_revision": pin, "input_revision": _pin(arguments), "sequence": self.sequence}
            self.attempts.append(attempt)
            keys = ("target", "route_family", "artifact_revision", "evidence_revision", "progress_revision", "input_revision")
            # A multi-target/parameter loop is not one repeatedly failed approach.
            # Count only the trailing unchanged streak, never A/B/A/B/A as A/A/A.
            matching = []
            for prior in reversed(self.attempts):
                if any(prior[k] != attempt[k] for k in keys):
                    break
                matching.append(prior)
                if len(matching) == 6:
                    break
            matching.reverse()
            incident = "failure:" + _pin((self.epoch, tuple(attempt[k] for k in keys)))
            if incident in self.handled_incidents:
                return None
        # Emit the concrete failure once; exact error handlers still own their paths.
        hint = self._emit("failure_incident", {"incident_id": incident,
            "error": {"id": call_id, "text": result[:600], "mechanism": mechanism, "category": category},
            "requirement": requirement, "deterministic_handler_available": False,
            "routes": [r.facts() for r in routes], "attempted_routes": list(self.attempted_routes)},
            incident, (call_id, requirement["id"], *(r.id for r in routes)), routes=routes, wait=True, valid=valid)
        if (incident not in self.handled_incidents and len(matching) >= 3
                and matching[-1]["sequence"] - matching[0]["sequence"] < 12):
            loop = "loop:" + incident.removeprefix("failure:")
            return self._emit("guardrail_candidate", {"incident_id": incident, "requirement": requirement,
                "attempts": matching, "window_start_sequence": matching[0]["sequence"],
                "window_end_sequence": matching[-1]["sequence"], "registered_poll": False,
                "registered_retry": False, "pagination": False, "user_repetition": False,
                "healthy_progress": False, "already_intervened": False, "recovery_route": routes[0].facts()},
                loop, tuple(a["id"] for a in matching) + (requirement["id"], routes[0].id),
                routes=routes, wait=True, valid=valid) or hint
        return hint

    def heartbeat_stopped(self, child, last_seen, *, threshold):
        """Called only by the native heartbeat's frozen-activity threshold branch.

        Requires an owned child and real activity clock; no elapsed-prose parsing,
        generic quiet-log inference, new timer, cancellation or status replacement.
        """
        from agent.owned_delegation import binding_of
        binding = binding_of(child)
        if binding is None or last_seen.get("ts") is None:
            return None
        owner, handle = binding
        state = owner.status(handle)
        if state["settled"] or state["cancel_requested"]:
            return None
        with self.lock:
            self._sync()
            routes = self._available()
            if not routes:
                # This route is the existing local lifecycle owner's status API,
                # not a model-invented command. Authority is bound to its handle.
                route_id = "owned-status:" + handle.child_id
                if route_id not in self.routes and len(self.routes) >= 4:
                    return None
                def status_available():
                    current = owner.status(handle)
                    return (current["control_revision"] == state["control_revision"] and
                            not current["settled"] and not current["cancel_requested"])
                route = Route(route_id, "Read the owned child's native lifecycle status; do not stop it",
                    handle.parent_session_id, "Current owned lifecycle handle", str(state["control_revision"]),
                    "none", "readonly", status_available)
                self.routes[route_id] = route
                routes = [route]
            incident = "heartbeat:" + _pin((handle.child_id, handle.generation, last_seen["ts"], last_seen["iter"], last_seen["tool"]))
        progress_id = "activity:" + _pin(last_seen)
        def still_stalled():
            current = owner.status(handle)
            summary = child.get_activity_summary()
            return (current["control_revision"] == state["control_revision"] and not current["settled"] and
                    (summary.get("last_activity_ts"), summary.get("api_call_count"), summary.get("current_tool")) ==
                    (last_seen["ts"], last_seen["iter"], last_seen["tool"]))
        return self._emit("liveness_candidate", {"incident_id": incident,
            "operation": {"id": handle.child_id, "owned": True,
                "phase_contract": "Child heartbeat advances on API count, tool transition or activity timestamp; frozen threshold " + str(threshold),
                "expected_quiet": False, "healthy_progress": False, "settled": False,
                "effect_class": "readonly" if state["effect_class"] == "read_only" else "unknown"},
            "progress": [{"id": progress_id, "text": json.dumps(last_seen, sort_keys=True)}],
            "signal": "heartbeat_stopped", "phase_evidence_present": True, "already_diagnosed": False,
            "diagnostic_route": routes[0].facts(), "recovery_route": routes[0].facts()},
            handle.child_id, (progress_id, routes[0].id), routes=routes,
            relations=("no_progress_cause_unknown", "blocked_known_cause"),
            valid=still_stalled)

    def before_read(self, arguments, call_id):
        """Recommend prior exact local read receipts, but NEVER suppress main calls."""
        self.runtime._assert_owner(tool_worker=True)
        requirement = self._requirement("read_file", arguments)
        path = arguments.get("path")
        with self.lock:
            self._sync()
            independent = self.read_intents.get(path) if isinstance(path, str) else None
        if not requirement or not _text(path) or not _text(call_id) or independent is not False:
            return None
        try:
            resolved = Path(path).resolve(strict=True)
            stat = resolved.stat()
            if not resolved.is_file():
                return None
        except (OSError, ValueError):
            return None
        snapshot = _pin((str(resolved), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        operation = {"id": call_id, "objective": "Read " + path,
            "acceptance": "Requested line window " + json.dumps({k: arguments.get(k) for k in ("offset", "limit")}),
            "requirement_id": requirement["id"], "task_id": self.runtime.revision.work_id,
            "target": str(resolved), "scope": "file_read", "snapshot": snapshot,
            "cwd": str(resolved.parent), "consumer_ids": [requirement["id"]], "independent_check": False,
            "effect_class": "readonly", "owner": "main", "optional": False, "issued": False}
        with self.lock:
            self._sync()
            # No claim that required/independent reads are waste: all main reads
            # still execute. An explicit independence owner flag is not inferable.
            candidates = [v for v in self.reads.values() if v["target"] == str(resolved) and v["snapshot"] == snapshot][-4:]
            if len(self.read_pending) < 32:
                self.read_pending[call_id] = operation
        # Identical native read requests already have a deterministic file-owner
        # reuse path. Semantic work is only for differing requested windows.
        if not candidates or any(c["acceptance"] == operation["acceptance"] for c in candidates):
            return None
        return self._emit("operation_proposed", {"proposed": operation, "candidates": candidates,
            "exact_reusable": False}, call_id, (requirement["id"], *(c["source_ref"] for c in candidates)), wait=True)

    def read_completed(self, call_id, *, failed, result=None):
        with self.lock:
            operation = self.read_pending.pop(call_id, None)
            if operation is None or failed or not isinstance(result, str) or len(result.encode("utf-8")) > 131072:
                return
            try:
                readback = json.loads(result)
            except (ValueError, TypeError):
                return
            if (not isinstance(readback, dict) or not isinstance(readback.get("content"), str)
                    or readback.get("content_returned") is False or readback.get("error")):
                return
            # The original pre-read pin must still identify the file after execution.
            try:
                stat = Path(operation["target"]).stat()
                current = _pin((operation["target"], stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except OSError:
                return
            if current != operation["snapshot"]:
                return
            self.reads[call_id] = {**operation, "status": "succeeded", "source_ref": "tool:" + call_id,
                                  "result_valid": True, "authorized_reuse": True}
            self.read_payloads[call_id] = result.encode("utf-8")
            while len(self.reads) > 16:
                oldest, _ = self.reads.popitem(last=False)
                self.read_payloads.pop(oldest, None)

    def admit_delegation(self, intent):
        self.runtime._assert_owner(tool_worker=True)
        if not isinstance(intent, DelegationIntent):
            raise TypeError("delegation_intent_required")
        with self.lock:
            self._sync()
            if len(self.delegations) >= 8 and intent.goal not in self.delegations:
                raise ValueError("delegation_capacity")
            self.delegations[intent.goal] = intent

    def delegation_built(self, child, *, goal):
        # A built child is already issued. Only a committed unissued planning node
        # can establish an F05 opportunity at the next normal request boundary.
        return None

    def commit_research_pass(self, record):
        # A caller-created ResearchPass is not native finite membership or a
        # main-agent disposition commitment. DependencyOwner owns that ledger.
        return False

    def propose_expansion(self, **kwargs):
        # Legacy fact setters cannot establish optionality or pass completeness.
        return None

    def record_check(self, check, receipt, *, validated):
        """Bind a successful native verification receipt, NEVER a generic shell result."""
        self.runtime._assert_owner(tool_worker=True)
        if not isinstance(check, VerificationCheck) or validated is not True:
            return False
        if (not isinstance(receipt, dict) or receipt.get("kind") != "verify" or receipt.get("status") != "passed"
                or type(receipt.get("id")) is not int or receipt["id"] <= 0 or not _text(receipt.get("root"))
                or receipt.get("session_id") != getattr(self.runtime.agent(), "session_id", None)):
            return False
        with self.lock:
            self._sync()
            self.checks[check.id] = (check, dict(receipt))
            while len(self.checks) > 16:
                self.checks.popitem(last=False)
        return True

    def propose_check(self, pending, *, current, exact=False):
        """A check-owner API returning a bound prior receipt, never verification success.

        Only plugin-owned OPTIONAL checks may reuse. Main-agent checks receive an
        advisory separately, and all required checks preserve baseline. current()
        revalidates the owner's fingerprints at the final consumption boundary.
        """
        self.runtime._assert_owner(tool_worker=True)
        if not isinstance(pending, VerificationCheck) or pending.protected or not callable(current):
            return None
        with self.lock:
            self._sync()
            candidates = [(c, r) for c, r in self.checks.values() if c.id != pending.id
                and set(pending.claim_ids) <= set(c.claim_ids)
                and all(getattr(c, k) == getattr(pending, k) for k in pending.fingerprint_fields())]
        if not candidates:
            return None
        prior, receipt = candidates[-1]
        from agent.verification_evidence import with_current_verification_receipt
        from hermes_constants import hermes_home_key

        def owner_current():
            # Session/profile can change without advancing this runtime's revision.
            # Recheck the entire contract (including mandatory flags and claims),
            # never merely the old pending object's fingerprint.
            try:
                return (receipt["session_id"] == getattr(self.runtime.agent(), "session_id", None)
                        and hermes_home_key() == self.runtime.revision.profile
                        and self.checks.get(prior.id) == (prior, receipt)
                        and current() == pending)
            except Exception:
                return False  # unavailable owner state means run the original check

        if not owner_current() or not with_current_verification_receipt(receipt, lambda: True):
            return None
        # Only an identical owner contract is deterministic; matching command
        # names or partial fingerprints never establish this branch.
        if exact and pending.plugin_owned and pending.text == prior.text and pending.claim_ids == prior.claim_ids:
            from agent.supervision_facade import registrations_for_scope
            def consume_exact():
                if (owner_current() and not self.runtime.closed
                        and not getattr(self.runtime.agent(), "_interrupt_requested", False)
                        and any("reuse_receipt" in r.grants for r in registrations_for_scope(self.runtime.revision.profile))):
                    return dict(receipt)
                return None
            return with_current_verification_receipt(receipt, consume_exact)
        receipt_id = "verification:" + str(receipt["id"])
        facts = {"pending": pending.facts(), "prior": {**prior.facts(), "receipt_id": receipt_id,
            "source_ref": receipt_id, "status": "succeeded", "validated": True}, "exact_reusable": False,
            "changed": True, "evidence_refs": [receipt_id, *pending.claim_ids]}
        rt = self.runtime
        deadline = rt.shared_deadline()
        snapshot = rt.observe("verification_proposed", facts, target_id=pending.id,
            actions=(Action.REUSE_RECEIPT if pending.plugin_owned else Action.ADVISE,),
            evidence_refs=(receipt_id, *pending.claim_ids),
            owner="efficiency.check", completeness=OpportunityCompleteness(),
            deadline=deadline, data_class="project_excerpt")
        if snapshot is None:
            return None
        rt._wait_for(pending.id, snapshot.deadline, snapshot.revision)
        result = []
        def apply(proposal):
            with self.lock:
                m = proposal.metadata
                if not owner_current():
                    return "stale"
                claims = m.get("claim_ids")
                if (proposal.feature_id != "F07" or m.get("receipt_id") != receipt_id
                        or m.get("source_ref") != receipt_id or m.get("snapshot") != pending.snapshot
                        or not _ids(claims) or set(claims) != set(pending.claim_ids)
                        or set(proposal.evidence_refs) != {receipt_id, *pending.claim_ids}
                        or m.get("verification_waived") is not False
                        or m.get("feature_action") != "reuse_exact_receipt"):
                    return "rejected"
                def admit():
                    if not owner_current():
                        return "stale"
                    result.append(dict(receipt))
                    return "applied"
                return with_current_verification_receipt(receipt, admit) or "stale"
        try:
            if pending.plugin_owned:
                rt.consume_owner_action(pending.id, Action.REUSE_RECEIPT, apply)
                return result[0] if result else None
            self.guards[pending.id] = lambda: bool(with_current_verification_receipt(receipt, owner_current))
            return self.consume(pending.id)
        finally:
            self.guards.pop(pending.id, None)
            rt.mark_dispatched(pending.id)  # late answers cannot replace the owner's baseline


def observe_native(agent, event, *args, **kwargs):
    """Best-effort producer hooks must not break mandatory native execution.

    Event names are fixed core call sites, not provider-selected methods. Direct
    owner API calls remain strict so invalid caller contracts are diagnosable.
    """
    if event not in {"read_proposed", "tool_result", "delegation_built", "heartbeat_stopped"}:
        raise ValueError("unknown_efficiency_event")
    try:
        owner = for_agent(agent)
        if owner is None:
            return
        if event == "read_proposed":
            hint = owner.before_read(*args, **kwargs)
            if hint:
                owner.hints.append(hint)
        elif event == "tool_result":
            name, arguments, result = args
            owner.read_completed(kwargs["call_id"], failed=kwargs["failed"], result=result)
            hint = owner.failed_tool(name, arguments, result, **kwargs)
            if hint:
                owner.hints.append(hint)
        elif event == "delegation_built":
            hint = owner.delegation_built(*args, **kwargs)
            if hint:
                owner.hints.append(hint)
        else:
            owner.heartbeat_stopped(*args, **kwargs)
    except Exception:
        # Neither raw exceptions nor source content become logs, and the native
        # guardrail, launch, result or heartbeat transition still owns its effect.
        return


def for_agent(agent):
    """No optional supervisor registration means no owner allocation or I/O."""
    from agent.supervision_policy import runtime_for_agent
    runtime = runtime_for_agent(agent)
    if runtime is None:
        return None
    with runtime.lock:
        owner = getattr(runtime, "efficiency", None)
        if owner is None:
            owner = EfficiencyOwner(runtime)
            setattr(runtime, "efficiency", owner)
        return owner
