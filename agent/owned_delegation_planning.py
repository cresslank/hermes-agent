"""Authenticated planning inventory and nonmutating configured launch preflight.

v1 launch declarations deliberately do not establish corroboration absence or a
whole-work census. The additional v2 ingress contract does, for bounded NEW
parent-only work only. Neither a work-map nor model tool arguments can issue it.
"""
from dataclasses import dataclass, replace
import json
import re

from agent.owned_delegation import Consumer, binding_of, validate_request

CONSUMERS_V2 = "authenticated-parent-only.v2"


@dataclass(frozen=True)
class ConsumerInventory:
    records: tuple
    source: str
    source_revision: str
    instruction_event: int
    work_id: str


def accept_inventory(runtime, origin):
    """Return whether v2 was present; malformed/mixed versions grant nothing.

    Invoked only by the existing authenticated-ingress owner, under its fence.
    Each instruction clears both declarations and inventory before this call.
    """
    from agent.owned_delegation_policy import ParentConsumerContract
    from agent.supervision_context import _strict_object, digest, is_accepted_origin
    if not is_accepted_origin(origin) or "```hermes-owned-delegation-v2" not in origin.text:
        return False
    if len(origin.text.encode("utf-8")) > 8192 or "```hermes-owned-delegation-v1" in origin.text:
        return True
    blocks = re.findall(r"(?m)^```hermes-owned-delegation-v2\n(.*?)^```\s*$", origin.text, re.S)
    if len(blocks) != 1:
        return True
    try:
        body = json.loads(blocks[0], object_pairs_hook=_strict_object)
        if (type(body) is not dict or set(body) != {"version", "inventory_scope", "complete", "jobs"}
                or type(body["version"]) is not int or body["version"] != 2
                or body["inventory_scope"] != "current_work_parent_only" or body["complete"] is not True
                or type(body["jobs"]) is not list or not 1 <= len(body["jobs"]) <= 4):
            return True
        spans = {(r.start, r.end): r.id for r in runtime.requirements if r.source_message_id == origin.message_id}
        records = {}
        for row in body["jobs"]:
            if (type(row) is not dict or set(row) != {"ref", "goal", "requirements", "consumer_scope", "obligation",
                    "requires_result", "requires_effects", "requires_cleanup", "requires_handoff", "requires_corroboration"}
                    or row["consumer_scope"] != "parent_only"
                    or type(row["ref"]) is not str or not re.fullmatch(r"job:[A-Za-z0-9_-]{1,64}", row["ref"])
                    or row["ref"] in records or type(row["goal"]) is not str or not 0 < len(row["goal"]) <= 240
                    or type(row["requirements"]) is not list or not 1 <= len(row["requirements"]) <= 4
                    or type(row["requires_handoff"]) is not bool):
                return True
            links = []
            for span in row["requirements"]:
                if (type(span) is not dict or set(span) != {"start", "end"}
                        or any(type(v) is not int for v in span.values())):
                    return True
                links.append(spans[(span["start"], span["end"])])
            consumer = Consumer(row["ref"], row["obligation"], row["requires_result"], row["requires_effects"],
                                row["requires_cleanup"], tuple(links), row["requires_corroboration"])
            records[row["ref"]] = ParentConsumerContract(row["ref"], row["goal"], consumer, origin.message_id,
                runtime.revision.instruction_event, runtime.revision.work_id, row["requires_handoff"])
        runtime.owned_consumer_contracts = records
        runtime.owned_planning_inventory = ConsumerInventory(tuple(records.values()), origin.message_id,
            digest(origin.text), runtime.revision.instruction_event, runtime.revision.work_id)
    except (KeyError, TypeError, ValueError):
        pass
    return True


def _inventory_records(owner):
    """Positive complete census, not requested refs, labels or persisted workers."""
    from agent.supervision_context import digest
    rt = owner.runtime
    inventory = getattr(rt, "owned_planning_inventory", None)
    if (not isinstance(inventory, ConsumerInventory) or not 1 <= len(inventory.records) <= 4
            or inventory.instruction_event != rt.revision.instruction_event or inventory.work_id != rt.revision.work_id
            or digest(rt.sources.get(inventory.source, "")) != inventory.source_revision
            or {r.ref: r for r in inventory.records} != getattr(rt, "owned_consumer_contracts", {})
            or json.loads(owner.policy_pin)["consumer_contract"] != CONSUMERS_V2):
        return None
    return inventory.records


def launch_resolution(owner, parent, request, goal):
    """Shared exact launch/preflight binding; no mutation of resolution state."""
    if not request or len(request["consumer_refs"]) != 1 or binding_of(parent) is not None:
        return None, {}
    record = getattr(owner.runtime, "owned_consumer_contracts", {}).get(request["consumer_refs"][0])
    inventory = getattr(owner.runtime, "owned_planning_inventory", None)
    v2_policy = json.loads(owner.policy_pin)["consumer_contract"] == CONSUMERS_V2
    if (record is None or record.goal != goal or record.instruction_event != owner.runtime.revision.instruction_event
            or record.work_id != owner.runtime.revision.work_id
            or (v2_policy and (not _inventory_records(owner) or inventory is None or record not in inventory.records))
            or (not v2_policy and inventory is not None)):
        return None, {}
    consumer = replace(record.consumer, obligation="required") if record.requires_handoff else record.consumer
    return record, {record.ref: consumer,
                    "parent:" + owner.parent_session_id: replace(consumer, ref="parent:" + owner.parent_session_id)}


def _no_native_workers(owner, parent):
    """Veto unaccounted legacy/async consumers using native registries, not labels.

    Nonblocking reads avoid adding a lock-order dependency to scheduler/steering
    code. Unavailable/oversized inventories are unknown, never an empty census.
    Async completed rows are retained too: delivery/cleanup is not inferred here.
    """
    from tools import async_delegation, delegate_tool_registry
    lock = getattr(parent, "_active_children_lock", None)
    if lock is None or not lock.acquire(blocking=False):
        return False
    try:
        if getattr(parent, "_active_children", None) != []:
            return False
    finally:
        lock.release()
    session_ids = {str(parent.session_id), owner.runtime.revision.lineage}
    for lock, rows, owner_field in (
        (delegate_tool_registry._active_subagents_lock, delegate_tool_registry._active_subagents, "owner_agent_session_id"),
        (async_delegation._records_lock, async_delegation._records, "parent_session_id"),
    ):
        if not lock.acquire(blocking=False):
            return False
        try:
            if len(rows) > 1024 or any(not r.get(owner_field) or str(r[owner_field]) in session_ids
                    or delegate_tool_registry._is_descendant_of(r.get("agent"), parent) for r in rows.values()):
                return False
        finally:
            lock.release()
    return True


def planning_preflight(owner, parent, request, *, goal="", operation=None, require_inventory=False):
    """Read native authority under launch fences; never construct/reserve/resolve keys."""
    from agent.supervision_context import digest
    request = validate_request(request)
    with owner.runtime.lock, owner.registration.fence, owner._lock:
        if (parent is not owner.runtime.agent() or not owner.current() or binding_of(parent)
                or len(owner._live) >= 1024 or request is None
                or type(operation) is not dict or set(operation) != {"tool_name", "route_id", "arguments"}):
            return None
        records, record = (), None
        if require_inventory:
            # No active/adopted/previously launched work is asserted absent by an
            # input census of proposed jobs. Keep that case baseline, even settled.
            records = _inventory_records(owner)
            if records is None or owner._live or not _no_native_workers(owner, parent):
                return None
            if (operation["tool_name"] != "read_file" or operation["route_id"] != "native:read_file"
                    or not owner._policy.permits("read_file", operation["arguments"])):
                return None
            resolved = {r.ref: replace(r.consumer, obligation="required") if r.requires_handoff else r.consumer for r in records}
            if set(request["consumer_refs"]) != set(resolved):
                return None  # a proposed subset cannot hide a required/unknown consumer
            # All entries name this parent's uses; the implicit native parent is
            # their conservative union, not a second guessed optionality flag.
            consumers = tuple(resolved.values())
            links = tuple(sorted({i for c in consumers for i in c.requirement_ids}))
            if len(links) > 4:
                return None
            obligation = ("required" if any(c.obligation == "required" for c in consumers) else
                          "unknown" if any(c.obligation == "unknown" for c in consumers) else "optional")
            parent_ref = "parent:" + owner.parent_session_id
            resolved[parent_ref] = Consumer(parent_ref, obligation,
                any(c.requires_result for c in consumers), any(c.requires_effects for c in consumers),
                any(c.requires_cleanup for c in consumers), links, any(c.requires_corroboration for c in consumers))
        else:
            if (operation["tool_name"] != "delegate_task" or operation["route_id"] != "native:delegate_task"
                    or type(operation["arguments"]) is not dict or set(operation["arguments"]) != {"tasks"}):
                return None
            tasks = operation["arguments"]["tasks"]
            if (type(tasks) is not list or len(tasks) != 1 or type(tasks[0]) is not dict
                    or set(tasks[0]) != {"goal", "context", "supervision"} or tasks[0]["goal"] != goal
                    or tasks[0]["supervision"] != request or type(tasks[0]["context"]) is not str):
                return None
            record, resolved = launch_resolution(owner, parent, request, goal)
            if record is None:
                return None
            records = (record,)
        consumers, closed, obligation, restricted = owner._launch_controls(parent, request, resolver=resolved.get)
        if not restricted or not closed:
            return None
        return dict(consumers=consumers, closed=closed, obligation=obligation,
            policy_id=owner._policy.policy_id, contracts={c.tool_name: c.version for c in owner._policy.contracts},
            revision=owner._revision(), operation_pin=digest(json.dumps(operation, sort_keys=True)),
            request_pin=digest(json.dumps(request, sort_keys=True)), goal=goal,
            source_pins=[(r.source, digest(owner.runtime.sources.get(r.source, "")))
                         for r in records])
