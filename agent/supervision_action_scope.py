"""Native semantic authorization at the existing pre-dispatch boundary.

Only authenticated ingress supplies scope. Jev owns the semantic relation;
this owner checks identity, grants and freshness, never votes on task meaning.
A missing service preserves existing execution policy, not a permission grant.
Explicit permission uncertainty survives an unavailable retry in the same work.
"""
import hashlib
import json

from agent.supervision_types import Action, freeze, project

RELATIONS = frozenset({"within_scope", "prerequisite", "outside_scope", "needs_approval", "insufficient"})
MESSAGES = {
    "outside_scope": "Action not executed: outside the accepted user scope. Do not report it as completed work.",
    "needs_approval": "Action not executed: required user permission is unresolved. Obtain approval before retrying.",
    "insufficient": "Action not executed: scope or permission could not be established. Resolve the missing evidence before retrying.",
    "stale": "Action not executed: the accepted instruction or proposed arguments changed during authorization. Reconsider against the current instruction.",
}
CLASSES = frozenset({"task_text", "project_excerpt"})


def operation(tool_name, arguments):
    # Preserve every actual argument; never guess effect/target from a shell string.
    value = freeze({"tool_name": tool_name, "arguments": arguments})
    encoded = json.dumps(project(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
    return value, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ActionScopeOwner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.complete = True
        self.blocked = {}
        self.overflow = False

    def admitted(self, *, continuation, omitted):
        if not continuation:
            self.complete = True
            self.blocked.clear()
            self.overflow = False
        self.complete = self.complete and not omitted

    def fallback(self, key):
        return self.blocked.get(key) or (MESSAGES["insufficient"] if self.overflow else None)

    def suppress(self, key, relation):
        message = MESSAGES[relation]
        if len(self.blocked) < 64 or key in self.blocked:
            self.blocked[key] = message
        else:
            # Never evict an unresolved permission just to make room for a new one.
            self.overflow = True
        return message

    def prepare(self, tool_name, arguments, tool_call_id):
        rt = self.runtime
        value, key = operation(tool_name, arguments)
        # Authorization and reuse are separate decisions about one dispatch.
        # Retiring the authorization must not close the later reuse target.
        target = "action-scope:" + hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()
        with rt.lock:
            fallback = self.fallback(key)
            if tool_call_id in rt.closed_targets or target in rt.closed_targets:
                return fallback or MESSAGES["stale"]
            recipients = [r for r in rt._registrations() if
                          {"observe", "authorize_action"} <= r.grants and CLASSES <= r.data_policy]
            if not recipients or not rt.sources:
                return fallback
            revision = rt.revision
            scope = [{"id": ref, "text": text} for ref, text in rt.sources.items()]
            refs = tuple(rt.sources)
            facts = {"changed": True, "evidence_refs": refs, "accepted_scope": scope,
                     "scope_complete": self.complete,
                     "action": {"id": target, "tool_call_id": tool_call_id, **project(value), "issued": False}}
            # Share the enclosing dispatch decision with any dependent native pass.
            deadline = rt.shared_deadline()
            issued = rt.decision_issued_at
        from agent.supervision_dependencies import LinkedOpportunity
        snapshot = rt.observe("action_proposed", facts, target_id=target,
                              actions=(Action.AUTHORIZE_ACTION,), evidence_refs=refs,
                              owner="action_scope", relations=tuple(sorted(RELATIONS)),
                              data_class="task_text", required_data_classes=tuple(CLASSES),
                              completeness=LinkedOpportunity(), expected_revision=revision,
                              deadline=deadline, deadline_issued_at=issued,
                              recipient=recipients[0])
        if snapshot is not None:
            rt._wait_for(target, deadline, revision)
        with rt.lock:
            if rt.revision != revision or operation(tool_name, arguments)[1] != key or rt.closed:
                rt.closed_targets.add(target)
                return MESSAGES["stale"]
            entry = rt._take(target, {Action.AUTHORIZE_ACTION})
            result = self.fallback(key)
            if not self.complete:
                result = self.suppress(key, "insufficient")
            if entry:
                proposal, registration = entry
                with registration.fence:
                    failure = rt._validate(proposal, registration)
                    if failure:
                        rt._settle(proposal, *failure)
                    elif not CLASSES <= registration.data_policy:
                        rt._settle(proposal, "rejected", "source_grant_revoked")
                    else:
                        relation = proposal.relation if self.complete else "insufficient"
                        allowed = relation in {"within_scope", "prerequisite"}
                        receipt = rt._settle(proposal, "applied", "action_authorized" if allowed else "action_suppressed")
                        if receipt.status == "applied":
                            rt.incidents.add(proposal.incident_id)
                            if allowed:
                                self.blocked.pop(key, None)
                                result = None
                            else:
                                result = self.suppress(key, relation)
                        else:
                            # A decision was received but could not be durably settled.
                            result = self.suppress(key, "insufficient")
            rt.closed_targets.add(target)
            return result


def valid_proposal(proposal):
    return (proposal.action == Action.AUTHORIZE_ACTION
            and proposal.metadata.get("feature_action") == "authorize_action"
            and proposal.relation in RELATIONS
            and proposal.metadata.get("relation") == proposal.relation
            and not proposal.candidate_ids and proposal.template_id is None and not proposal.template_args)
