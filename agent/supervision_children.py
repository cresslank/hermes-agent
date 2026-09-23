"""Native F01 join: exact consumer links -> observation -> fenced owner effects.

No provider imports. Observations are distinct from priority proposals. The owner
persists the first validated observation, not the plugin's private confirmation.
No callback runs with the control lock held; settlement takes runtime then control.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import math
import sqlite3

from agent.supervision_types import Action, Completeness, project
from agent.owned_delegation import ControlDenied, SemanticEvidence, control_fence

VERSION = "supervision.child-relevance.v1"
RELATIONS = {"current", "reusable_only", "superseded", "no_remaining_consumer", "insufficient"}


@dataclass(frozen=True)
class ChildCompleteness(Completeness):
    relevant: bool = True


def _scope(revision):
    return tuple(getattr(revision, k) for k in ("profile", "lineage", "work_id", "run_generation", "catalog"))


def _meaningful(revision):
    return (revision.instruction_event, revision.requirements, revision.evidence)


def _guard(s):
    return hashlib.sha256(json.dumps({k: s[k] for k in (
        "generation", "obligation", "consumers", "consumer_set_closed", "effect_policy_id",
        "effect_class", "handoffs", "cleanup_pending")}, sort_keys=True).encode()).hexdigest()


def _answers(value, requirements):
    """Strict native validation of the finite F01 wire, never trust a strong flag."""
    ids = {"F01.contribution." + r for r in requirements}
    if not isinstance(value, dict) or set(value) != ids | {"F01.relation"}:
        raise ControlDenied("Invalid relevance answers")
    def probability(v):
        return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
    values = {}
    for key in ids:
        a = value[key]
        if not isinstance(a, dict) or set(a) != {"type", "noul"} or a["type"] != "noul" or not probability(a["noul"]):
            raise ControlDenied("Invalid contribution")
        values[key.removeprefix("F01.contribution.")] = a["noul"]
    a = value["F01.relation"]
    if (not isinstance(a, dict) or set(a) != {"type", "choice", "probabilities", "confidence"}
            or a["type"] != "choice" or a["choice"] not in RELATIONS
            or not probability(a["confidence"]) or not isinstance(a["probabilities"], dict)
            or set(a["probabilities"]) != RELATIONS
            or not all(probability(v) for v in a["probabilities"].values())
            or abs(sum(a["probabilities"].values()) - 1) > 1e-5
            or a["probabilities"][a["choice"]] != max(a["probabilities"].values())):
        raise ControlDenied("Invalid relation")
    return values, a


class ChildRelevanceOwner:
    def __init__(self, runtime, owner):
        self.runtime, self.owner = runtime, owner
        self.launches = {}
        self.targets = {}
        self.last_task = None
        self.direct = None
        if getattr(owner, "direct_enabled", False):
            from agent.supervision_children_direct import RecurringChildControl
            self.direct = RecurringChildControl(self)

    def launched(self, handle):
        # A launch alone does not manufacture a semantic task change.
        with self.runtime.lock:
            self.launches[handle.child_id] = (handle, _scope(self.runtime.revision))
            if self.last_task is None and self.runtime.sources:
                self.last_task = next(reversed(self.runtime.sources.values()))
            self.runtime.revision = replace(self.runtime.revision,
                control_revision=self.runtime.revision.control_revision + 1)
            if self.direct is not None:
                self.direct.start()

    def _snapshot(self, handle, scope, plugin_generation):
        with self.owner._lock:
            live = self.owner._get(handle)
            prior = live.snapshot.get('semantic_observation')
            invalid_priority = (live.snapshot.get('priority', 0) and
                (live.snapshot.get('priority_scope') != list(scope)
                 or live.snapshot.get('priority_plugin_generation') != plugin_generation))
            if invalid_priority or (prior and (prior['scope'] != list(scope) or prior['guard'] != _guard(live.snapshot)
                          or prior.get('plugin_generation') != plugin_generation)):
                self.owner._commit(live, lambda s: s.update(candidate=None, priority=0))
            return self.owner.status(handle)

    def changed(self, event, handle=None):
        if self.direct is not None:
            # Recurring observations coalesce changed facts at their next bounded
            # tick, without cancelling another child's in-flight inference.
            with self.runtime.ready:
                self.runtime.ready.notify_all()
            return
        runtime = self.runtime
        from hermes_constants import hermes_home_key
        with runtime.lock:
            if runtime.closed or hermes_home_key() != runtime.revision.profile:
                return
            if event == "task_revision":
                text = next(reversed(runtime.sources.values()), None)
                if text == self.last_task:
                    return
                self.last_task = text
            else:
                runtime.revision = replace(runtime.revision, evidence=runtime.revision.evidence + 1)
            registration = next((r for r in runtime._registrations()
                                 if r.plugin_id == self.owner._grant.plugin_id), None)
            if registration is None:
                return
            for ident, (owned, launch_scope) in tuple(self.launches.items()):
                if handle is not None and owned != handle:
                    continue
                scope = _scope(runtime.revision)
                if launch_scope[:3] != scope[:3]:
                    continue  # never adopt a child into unrelated semantic work
                try:
                    s = self._snapshot(owned, scope, registration.generation)
                except (ValueError, OSError, sqlite3.Error):
                    continue
                if not self.owner.semantic_authorized(owned) or s["settled"] or s["cancel_requested"]:
                    continue
                consumers = s["consumers"]
                if not 0 < len(consumers) <= 8 or any(not c.get("requirement_ids") for c in consumers):
                    continue  # missing links are unknown, not singleton inferred links
                ids = sorted({i for c in consumers for i in c["requirement_ids"]})
                reqs = {r.id: r for r in runtime.requirements}
                if not 0 < len(ids) <= 4 or not set(ids) <= reqs.keys():
                    continue
                if any(len(reqs[i].text) > 2400 for i in ids) or not 0 < len(s["objective"]) <= 2400:
                    continue
                instructions = list(runtime.sources.values())
                # Preserve all retained accepted qualifiers; never silently select
                # only the newest instruction or truncate a material scope clause.
                instruction_text = "\n".join(instructions)
                if not instructions or len(instructions) >= 12 or len(instruction_text) > 1200:
                    continue
                refs = [*ids, "child:" + ident]
                prior = s.get("semantic_observation")
                if prior and (prior["scope"] != list(scope) or prior["guard"] != _guard(s)):
                    prior = None
                facts = dict(changed=True, meaningful_change=True, evidence_refs=refs,
                    changed_requirement_ids=ids, explicit_owner_stop=False, explicit_supersession=False,
                    current_instructions=instruction_text, child_relevance_contract=VERSION,
                    native_observation={"receipt_id": prior["receipt_id"], "revision": prior["revision"]} if prior else None,
                    requirements=[dict(id=i, text=reqs[i].text, source_ref=reqs[i].source_message_id, unresolved=True) for i in ids],
                    job=dict(id=ident, active=True, objective=s["objective"], scope="\n".join(reqs[i].text for i in ids),
                        milestone=s["latest_milestone"] or s["objective"], requirement_ids=ids,
                        generation=int(owned.generation[:15], 16), control_revision=s["control_revision"],
                        obligation=s["obligation"], effect_class="readonly" if s["effect_class"] == "read_only" else "unknown",
                        effect_policy_id=s["effect_policy_id"], owner_cancel_grant=self.owner._grant.allow_optional_readonly,
                        consumer_set_closed=s["consumer_set_closed"],
                        consumers=[dict(id=c["ref"], obligation=c["obligation"], requirement_ids=c["requirement_ids"]) for c in consumers],
                        pending_handoff=bool(s["handoffs"]), pending_effect=bool(s["inflight"]), pending_cleanup=s["cleanup_pending"]))
                # Stored target contains exact native bindings, never taken back from plugin metadata.
                self.targets[ident] = (owned, s["control_revision"], ids, scope, _guard(s), prior)
                runtime.observe(event, facts, target_id=ident, owner="owned_delegation",
                    actions=(Action.REPRIORITIZE_CHILD, Action.CANCEL_CHILD), evidence_refs=refs,
                    completeness=ChildCompleteness(scope="enumerated_items", complete=True))

    def drain(self):
        if self.direct is not None:
            self.direct.drain()
        for target in tuple(self.targets):
            for action in (Action.REPRIORITIZE_CHILD, Action.CANCEL_CHILD):
                self.runtime.consume_owner_action(target, action, self.apply)

    def apply(self, proposal):
        owner, runtime = self.owner, self.runtime
        from hermes_constants import hermes_home_key
        if hermes_home_key() != runtime.revision.profile:
            return "stale"
        try:
            handle, expected, ids, scope, guard, prior = self.targets[proposal.target_id]
            meta = project(proposal.metadata)
            observation = meta.get("semantic_observation")
            if (proposal.feature_id != "F01" or proposal.expected.profile != handle.profile
                    or not isinstance(observation, dict) or observation.get("version") != VERSION
                    or set(observation) != {"version", "answers", "prior_receipt_id"}):
                return "rejected"
            # Registration grants are checked by the runtime; also bind the installed
            # control grant to this plugin, not any observer in the same profile.
            from agent.supervision_facade import registrations_for_scope
            regs = registrations_for_scope(runtime.revision.profile, blocking=False)
            if not any(r.plugin_id == handle.plugin_id and r.generation == proposal.plugin_generation for r in regs):
                return "rejected"
            rows = observation["answers"]
            if (not isinstance(rows, list) or len(rows) != len(ids) + 1
                    or any(not isinstance(r, dict) or set(r) != {"question_id", "answer"} for r in rows)
                    or len({r["question_id"] for r in rows}) != len(rows)):
                return "rejected"
            values, relation = _answers({r["question_id"]: r["answer"] for r in rows}, ids)
            probability = relation["probabilities"][relation["choice"]]
            strong = (relation["choice"] == "no_remaining_consumer" and probability >= .97
                      and relation["confidence"] >= .90 and all(v <= .05 for v in values.values()))
            priority = meta.get("priority", "unchanged")
            expected_priority = "unchanged"
            if probability >= .85 and relation["confidence"] >= .80:
                if relation["choice"] == "current" and any(v >= .90 for v in values.values()):
                    expected_priority = "retain"
                elif relation["choice"] in {"reusable_only", "no_remaining_consumer"} and all(v <= .10 for v in values.values()):
                    expected_priority = "deprioritize"
            feature_action = {Action.REPRIORITIZE_CHILD: 'priority_update', Action.CANCEL_CHILD: 'cancel_optional_readonly'}
            if (meta.get('feature_action') != feature_action.get(proposal.action)
                    or priority not in {'retain', 'deprioritize', 'unchanged'}
                    or (proposal.action == Action.REPRIORITIZE_CHILD and priority != expected_priority)):
                return "rejected"
            with control_fence(owner._lock, proposal.expires_at_monotonic):
                live = owner._get(handle)
                s = live.snapshot
                if s["control_revision"] != expected or _scope(runtime.revision) != scope or _guard(s) != guard:
                    return "stale"
                if not owner.semantic_authorized(handle, deadline=proposal.expires_at_monotonic) or s["cancel_requested"] or s["settled"] or s["inflight"]:
                    return "no_op"
                # A priority proposal without this independently validated observation
                # can never prime cancellation. Nor can a claimed prior receipt.
                native_prior = s.get("semantic_observation")
                if prior != native_prior or observation["prior_receipt_id"] != (prior["receipt_id"] if prior else None):
                    return "stale"
                cancel = proposal.action == Action.CANCEL_CHILD
                if cancel and (not strong or prior is None or prior["guard"] != guard
                        or prior["scope"] != list(scope) or tuple(prior["revision"]) == _meaningful(runtime.revision)):
                    return "rejected"
                if not strong:
                    owner._commit(live, lambda n: n.update(candidate=None, semantic_observation=None),
                                  deadline=proposal.expires_at_monotonic)
                elif proposal.action == Action.REPRIORITIZE_CHILD and prior is not None:
                    # Repeated inference on identical evidence never creates another vote.
                    pass
                else:
                    evidence = SemanticEvidence(_meaningful(runtime.revision),
                        tuple((c["ref"], max(values[i] for i in c["requirement_ids"])) for c in s["consumers"]),
                        relation["choice"], probability, relation["confidence"])
                    if not cancel:
                        # A legacy/raw caller's candidate is not a native first observation.
                        owner._commit(live, lambda n: n.update(candidate=None), deadline=proposal.expires_at_monotonic)
                    receipt = owner.request_semantic_cancel(handle, expected_revision=live.snapshot["control_revision"],
                        evidence=evidence, idempotency_key=proposal.proposal_id,
                        deadline=proposal.expires_at_monotonic, _defer_signal=True)
                    if receipt.reason not in {"await_distinct_revision", "cancel_requested"}:
                        return "no_op"
                    if not cancel:
                        record = dict(receipt_id=proposal.proposal_id, revision=list(evidence.revision),
                            full_revision=project(proposal.expected), control_revision=expected,
                            plugin_generation=proposal.plugin_generation,
                            child_generation=handle.generation, requirement_ids=list(ids),
                            evidence_refs=list(proposal.evidence_refs), answers=rows,
                            scope=list(scope), guard=guard, evidence=project(evidence))
                        owner._commit(live, lambda n: n.update(semantic_observation=record), deadline=proposal.expires_at_monotonic)
                    elif not receipt.accepted:
                        return "no_op"
                if not cancel and not getattr(owner, "priority_effects_supported", True):
                    return "no_op"  # observation persisted; no scheduler consumed a priority effect
                if not cancel and expected_priority != "unchanged":
                    # Required and unknown workers retain ordinary scheduling regardless
                    # of semantic priority advice. Only optional attested work may yield.
                    optional = s["obligation"] == "optional" and s["effect_class"] == "read_only" and s["consumer_set_closed"]
                    rank = 1 if expected_priority == "deprioritize" and optional else 0
                    owner._commit(live, lambda n: n.update(priority=rank, priority_scope=list(scope),
                        priority_plugin_generation=proposal.plugin_generation,
                        priority_revision=project(proposal.expected)), deadline=proposal.expires_at_monotonic)
            if cancel:
                owner.signal_semantic_cancel(handle)
            return "applied"
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return "rejected"


def attach(parent, owner, handle):
    from agent.supervision_policy import runtime_for_agent
    if str(getattr(parent, 'session_id', '')) != owner.parent_session_id:
        return  # nested launches must not replace the root owner's bridge
    runtime = runtime_for_agent(parent)
    if runtime is None or runtime.revision.profile != owner._grant.profile:
        return
    bridge = getattr(runtime, "children", None)
    if bridge is None:
        bridge = runtime.children = ChildRelevanceOwner(runtime, owner)
        owner._relevance_bridge = bridge
    if bridge.owner is owner:
        bridge.launched(handle)


def committed_child_plan(store):
    """Actual child-local todo commit, not progress prose or a caller's fact map.

    Only the currently bound child's own store qualifies. The exact bounded plan
    is evidence of its declared remaining work, never proof of effects/authority.
    Dispatch defers publication until its existing owner fence has settled.
    """
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.owned_delegation import binding_of
    child = get_active_subagent_parent()
    binding = binding_of(child)
    if binding is None or getattr(child, '_todo_store', None) is not store:
        return
    items = store.read()
    if not 0 < len(items) <= 4:
        return
    if len(json.dumps(items, sort_keys=True, ensure_ascii=False)) > 1000:
        return  # do not truncate a plan qualifier or an obligation
    # Row IDs and reordering are bookkeeping, not a changed declared deliverable.
    # Preserve the full content/status multiset; no semantic thresholds change.
    milestone = json.dumps(sorted((item["content"], item["status"]) for item in items), ensure_ascii=False)
    try:
        binding[0].record_progress(binding[1], milestone)
    except (ValueError, OSError, sqlite3.Error):
        # A completed todo write keeps its ordinary result even when optional
        # observation persistence is unavailable. No semantic effect was granted.
        return


def scheduling_priority(child):
    """Live dequeue rank; never delay running work or alter its delivery."""
    from agent.owned_delegation import binding_of
    binding = binding_of(child)
    if not binding:
        return 0
    state = binding[0].status(binding[1])
    bridge = getattr(binding[0], '_relevance_bridge', None)
    from hermes_constants import hermes_home_key
    if (bridge is None or not binding[0].semantic_authorized(binding[1])
            or bridge.runtime.closed or state['obligation'] != 'optional'
            or hermes_home_key() != bridge.runtime.revision.profile
            or state.get('priority_revision') != project(bridge.runtime.revision)
            or state.get('priority_scope') != list(_scope(bridge.runtime.revision))
            or not any(r.plugin_id == binding[1].plugin_id and r.generation == state.get('priority_plugin_generation')
                       and 'reprioritize_child' in r.grants for r in bridge.runtime._registrations())):
        return 0
    return state.get('priority', 0)
