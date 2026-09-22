"""Bounded evidence-owner mapping codec; source data never supplies host authority.

Owners supply domain facts, including explicit unknowns. This codec does not
certify freshness, semantic qualifiers, corpus coverage or source constraints.
Revision, disclosure policy and the shared deadline are native host state.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import math

from agent.supervision_types import Action, Completeness, OwnerRequestV1, freeze, project

OWNERS = {"hermes-lcm": "lcm", "web-muxyard": "muxyard"}
EVENTS = {
    Action.RANK_CANDIDATES: "retrieval_candidates",
    Action.SELECT_WINDOWS: "oversized_structured_result",
    Action.EXPAND_ONE_OWNED_REF: "missing_history_slot",
}
LOCAL_CLASSES = {"lcm": "history_excerpt", "muxyard": "public_source"}


def bounded_facts(value):
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 2048 or depth > 8:
            raise ValueError("owner_bounds")
        if isinstance(item, Mapping):
            if len(item) > 64:
                raise ValueError("owner_bounds")
            for key, child in item.items():
                if type(key) is not str or not 0 < len(key) <= 128:
                    raise ValueError("owner_key")
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 64:
                raise ValueError("owner_bounds")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            if len(item) > 2400:
                raise ValueError("owner_bounds")
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is not float or not math.isfinite(item):
            raise ValueError("owner_value")

    if not isinstance(value, Mapping):
        raise ValueError("owner_facts")
    visit(value)
    value = project(freeze(value))
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 32768:
        raise ValueError("owner_bounds")
    return value


def decode_request(action, value, *, plugin_id, runtime):
    if (set(value) - {"output_contract"} != {"protocol", "owner", "event", "request_id", "deadline", "facts", "completeness"}
            or value["protocol"] != "supervision.v1"
            or value["owner"] != OWNERS.get(plugin_id)
            or value["event"] != EVENTS.get(action)
            or (action == Action.EXPAND_ONE_OWNED_REF and value["owner"] != "lcm")
            or (action == Action.SELECT_WINDOWS and value["owner"] != "muxyard")):
        raise ValueError("owner_protocol")
    from agent.supervision_retrieval_presentation import VERSION
    output_contract = value.get("output_contract")
    if output_contract is not None and (action != Action.RANK_CANDIDATES or output_contract != VERSION):
        raise ValueError("owner_output_contract")
    request_id = value["request_id"]
    if type(request_id) is not str or not 0 < len(request_id) <= 128:
        raise ValueError("owner_request_id")
    deadline = value["deadline"]
    if type(deadline) not in (float, int) or not math.isfinite(deadline) or deadline <= runtime.clock():
        raise ValueError("owner_deadline")
    facts = bounded_facts(value["facts"])
    rows = facts.get("candidates")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("owner_candidates")
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    if len(ids) != len(rows) or any(type(i) is not str or not 0 < len(i) <= 256 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("owner_candidates")
    refs = [r.get("ref") for r in rows] if action != Action.SELECT_WINDOWS else [facts.get("source_ref")]
    if any(type(ref) is not str or not 0 < len(ref) <= 256 for ref in refs):
        raise ValueError("owner_refs")
    completeness = bounded_facts(value["completeness"])
    # A full local enumeration is not semantic or archive completeness. Unknown
    # source metadata remains in the owner, not upgraded by a no-truncation guess.
    complete = completeness.get("source_complete") is True
    required = ids if action == Action.RANK_CANDIDATES else facts.get("mandatory_ids", [])
    if not isinstance(required, (list, tuple)) or any(type(i) is not str for i in required):
        raise ValueError("owner_required_ids")
    candidates = tuple({**r, "excerpt": r.get("excerpt", r.get("text", ""))} for r in rows)
    target = value["owner"] + ":" + request_id
    if "target_id" in facts and facts["target_id"] != target:
        raise ValueError("owner_target")
    return OwnerRequestV1(
        value["owner"], target, runtime.revision, candidates,
        runtime.shared_deadline(deadline), required_ids=tuple(required),
        completeness=Completeness(complete=complete, omitted=not complete),
        data_policy=(LOCAL_CLASSES[value["owner"]],),
        relations=("states_missing_decision",) if action == Action.EXPAND_ONE_OWNED_REF else (),
        event=value["event"], requires_ack=True, facts=facts, evidence_refs=tuple(dict.fromkeys(refs)),
        output_contract=output_contract,
    )


def encode_decision(action, request_id, request, decision):
    if not decision.selected or not decision.receipt_id:
        return None
    ids = list(decision.candidate_ids)
    offered = {c["id"] for c in request.candidates}
    if len(ids) != len(set(ids)) or not set(ids) <= offered:
        return None
    result = {"request_id": request_id, "receipt_id": decision.receipt_id,
              "candidate_ids": ids, "consumption": "supervision.owner-consumption.v1"}
    if action == Action.RANK_CANDIDATES:
        if set(ids) != offered:
            return None
        result["candidate_ids"] = ids
        from agent.supervision_retrieval_presentation import VERSION, validate
        try:
            blocks = validate(decision.metadata, offered)
        except ValueError:
            return None
        for key, values in zip(("conflict_ids", "isolated_ids"), blocks):
            result[key] = list(values)
        if request.output_contract == VERSION:
            result["output_contract"] = VERSION
    elif action == Action.SELECT_WINDOWS:
        if not ids or not set(request.required_ids) <= set(ids):
            return None
        result["window_ids"] = ids
    else:
        if (len(ids) != 1 or type(decision.metadata.get("max_expansions")) is not int or
                decision.metadata.get("max_expansions") != 1):
            return None
        result.update(candidate_id=ids[0], relation="states_missing_decision")
    return result
