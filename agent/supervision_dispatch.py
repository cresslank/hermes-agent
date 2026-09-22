"""Revocable, one-use send admission in the existing bounded opportunity authority.

The linearization point is consuming the capability under runtime, registration,
and (for MCP) transport fences. Revocation before that point forbids a new send;
revocation afterwards does not recall admitted/in-flight remote work. Callers must
consume immediately before transport I/O, with no await in between. No lock escapes
this module and no inference result, retry, or durable external-effect claim lives here.
"""
from dataclasses import replace
from contextlib import nullcontext
import math
import uuid

from agent.supervision_types import Revision, freeze, project

VERSION = "supervision.dispatch-admission.v1"


def issue(runtime, registration, snapshot):
    with runtime.lock:
        opportunity = runtime.opportunities.get(snapshot.facts["target_id"])
        if opportunity is None or opportunity["revision"] != snapshot.revision:
            return None
        entries = opportunity.setdefault("dispatch", {})
        if registration.generation not in entries and len(entries) >= 32:
            return None
        capability = uuid.uuid4().hex
        entries[registration.generation] = (capability, registration, snapshot, False)
        return capability


def consume(facade, request):
    from agent.supervision_policy import runtime_for_revision
    from agent.supervision_mcp import consumption_fence, recipient_authorized
    if not isinstance(request, dict) or set(request) != {"capability", "revision", "target_id", "state", "deadline"}:
        return False
    try:
        revision = Revision(**request["revision"])
        state = freeze(request["state"])
    except (ValueError, TypeError):
        return False
    registration = facade._registration
    runtime = runtime_for_revision(revision)
    deadline = request["deadline"]
    if (registration is None or runtime is None or type(request["target_id"]) is not str or
            type(request["capability"]) is not str or type(deadline) not in (int, float) or
            not math.isfinite(deadline)):
        return False
    with runtime.lock:
        opportunity = runtime.opportunities.get(request["target_id"])
        if opportunity is None:
            return False
        claim_fence = (runtime.dependencies.claim_uses.dispatch_fence(request["target_id"], registration)
            if opportunity["owner"] == "claim_uses" else nullcontext(True))
        with registration.fence, consumption_fence(opportunity), claim_fence as claim_current:
            if not claim_current:
                return False
            entry = opportunity.get("dispatch", {}).get(registration.generation)
            if entry is None:
                return False
            capability, recipient, snapshot, used = entry
            if (used or recipient is not registration or request["capability"] != capability or
                    not registration.active or registration.scope != revision.profile or
                    runtime.revision != revision or snapshot.revision != revision or
                    getattr(runtime.agent(), "session_id", "") != runtime.session_id or
                    (runtime.closed and snapshot.owner != "completion_admission") or
                    request["target_id"] in runtime.closed_targets or
                    not runtime.clock() < deadline <= snapshot.deadline or
                    "observe" not in registration.grants or
                    not opportunity["data_classes"] <= registration.data_policy):
                return False
            policy = registration.egress_policy
            keys = snapshot.facts.keys()
            if not policy or not keys <= policy["fields"].keys():
                return False
            current = {**policy, "fields": {k: policy["fields"][k] for k in keys},
                       "sources": {k: policy["sources"][k] for k in keys if k in policy["sources"]}}
            if freeze(current) != snapshot.data_policy:
                return False
            if snapshot.owner == "mcp" and not recipient_authorized(
                    opportunity["mcp_recipients"], registration, transport_locked=True):
                return False
            expected = freeze({"facts": snapshot.facts, "completeness": project(snapshot.completeness)})
            if state != expected:
                return False
            opportunity["dispatch"][registration.generation] = (capability, recipient, snapshot, True)
            return True


def issue_skill_details(runtime, registration, request, reply):
    """A native authorized detail read replaces, never replays, the first send.

    Preserve its exact revision, recipient, event, policy and original deadline;
    only the source-owned candidate detail projection changes.
    """
    with runtime.lock:
        opportunity = runtime.opportunities.get(request["target_id"])
        entry = opportunity.get("dispatch", {}).get(registration.generation) if opportunity else None
        if entry is None or entry[1] is not registration:
            return None
        snapshot = entry[2]
        if (not registration.active or snapshot.revision != runtime.revision or
                snapshot.event_id != request["event_id"] or snapshot.owner != "native_views" or
                snapshot.facts.get("stage") not in {"catalog", "metadata"} or
                runtime.clock() >= snapshot.deadline):
            return None
        facts = {**snapshot.facts, "stage": "detail", "candidates": reply["candidates"]}
        return issue(runtime, registration, replace(snapshot, facts=facts))
