"""Native final admission and explicit committed child finding owner.

Only immutable source objects, canonical child control links and current work-map
steps supply facts. Provider answers choose dispositions, never source authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import time

from agent import supervision_store as store
from agent.supervision_types import Action, Completeness, SettlementV1

@dataclass(frozen=True)
class DeliveryCompleteness(Completeness):
    complete: bool = True
    relevant: bool = True


FINAL_ACTIONS_VERSION = "supervision.final-actions.v1"
FINAL_ACTIONS = {"bounded_view": "final_bounded_view", "deliver": "deliver_final"}


def runtime_for_session(session_id):
    from agent.supervision_policy import _registry_lock, _runtimes
    from hermes_constants import hermes_home_key
    with _registry_lock:
        matches = {id(r): r for r in _runtimes.values()
                   if r.revision.profile == hermes_home_key() and r.agent() is not None
                   and getattr(r.agent(), "session_id", None) == session_id}
    return next(iter(matches.values())) if len(matches) == 1 else None


def _read(runtime):
    from pathlib import Path
    db = getattr(runtime.agent(), "_session_db", None)
    path = getattr(db, "db_path", None)
    if not isinstance(path, (str, Path)):
        raise store.AdmissionError("source database unavailable")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
    conn.row_factory = sqlite3.Row
    return conn


def pending_decisions(runtime, target_refs):
    """Exact proposed work-map links, not topical similarity or list position."""
    todos, revision = runtime.plan_steps()
    decisions = []
    for path, work_map in runtime.work_maps.items():
        if runtime.work_map_revisions.get(path) != revision:
            continue
        for step in work_map["steps"]:
            todo = todos.get(step["todo_id"])
            if not todo or todo.get("status") not in {"pending", "in_progress"}:
                continue
            if not set(step["target_refs"]) & set(target_refs):
                continue
            text = todo.get("content")
            if not isinstance(text, str) or not 0 < len(text) <= 1200:
                continue
            for index in step["requirement_indexes"]:
                requirement = work_map["requirement_spans"][index]
                if len(requirement.text) <= 1200:
                    decisions.append({"id": requirement.id, "text": requirement.text,
                        "unresolved": True, "next_action_id": step["todo_id"], "next_action": text})
    unique = {(d["id"], d["next_action_id"]): d for d in decisions}
    return list(unique.values()) if len(unique) <= 3 else []


def offer_child_finding(delegation_id, object_id):
    """Source commit calls once; scheduling only, with no wait or transcript mutation."""
    from tools.async_delegation import _DB_LOCK, _transaction
    from agent.supervision_context import refs_in_text
    with _DB_LOCK, _transaction() as conn:
        launch = conn.execute("SELECT parent_session_id,task_json,control_child_ids,state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        source = conn.execute("SELECT source_id,subtype FROM delegation_result_objects WHERE object_id=? AND launch_id=?", (object_id, delegation_id)).fetchone()
        if not launch or not source or source[1] not in {"child", "tool"} or launch[3] != "running":
            return
        raw = store.get_result_object(conn, object_id)
        controls = store.get_launch_control(conn, delegation_id)["children"]
        if conn.execute("SELECT 1 FROM supervision_admissions WHERE launch_id=? AND source_id=? AND subtype='finding'",
                        (delegation_id, "finding:" + object_id)).fetchone():
            return
    runtime = runtime_for_session(launch[0])
    if runtime is None or runtime.closed:
        return
    entry = json.loads(raw)
    index = entry.get("task_index")
    task, children = json.loads(launch[1]), json.loads(launch[2])
    indexes = task.get("task_indexes") or list(range(len(task.get("goals") or [])))
    if type(index) is not int or index not in indexes or len(children) != len(indexes):
        return
    slot = indexes.index(index)
    goals = task.get("goals") or []
    if (index >= len(goals) or not isinstance(goals[index], str) or len(controls) != len(children)
            or not any(c.get("ref") == "parent:" + launch[0] for c in controls[slot].get("consumers", ()))):
        return
    binding = task.get("supervision_revision", {})
    if any(binding.get(k) != getattr(runtime.revision, k) for k in ("profile", "lineage", "work_id", "instruction_event", "requirements")):
        return
    refs = refs_in_text(goals[index])
    decisions = pending_decisions(runtime, refs)
    text = entry.get("summary")
    if (not decisions or entry.get("status") not in {"success", "completed"}
            or not isinstance(text, str) or not 0 < len(text) <= 1200 or entry.get("error")):
        return
    # The immutable child result proves the source bytes, not the truth of its claims.
    finding = "finding:" + object_id
    with runtime.lock:
        if finding in runtime.opportunities or finding in runtime.closed_targets:
            return
    delivery = store.digest(store.profile_key(), runtime.revision.lineage, delegation_id, finding, "finding")
    facts = {"changed": True, "evidence_refs": [object_id], "finding": {
        "id": finding, "text": text, "source_ref": object_id, "source_receipt": object_id,
        "immutable_object_id": object_id, "delivery_id": delivery, "job_id": delegation_id,
        "launch_id": delegation_id, "committed": True, "job_finished": False,
        "already_delivered": False, "deterministic_critical": False, "verification": "provisional"},
        "decisions": decisions}
    deadline = runtime.shared_deadline()
    snapshot = runtime.observe("finding_committed", facts, target_id=finding,
        actions=(Action.DELIVER_FINDING,), evidence_refs=(object_id,), owner="finding_admission",
        deadline=deadline, completeness=DeliveryCompleteness(), data_class="project_excerpt")
    if snapshot is not None:
        with runtime.lock:
            runtime.opportunities[finding]["delivery_source"] = (delegation_id, object_id, text, tuple(refs), decisions)


def record_tool_findings(runtime, messages):
    """Completed child read-owner facts, joined to their persisted tool rows.

    Text in a tool result cannot name its destination, grant, decision or receipt.
    Only the real assistant call and canonical launch/control link choose those.
    """
    child_id = getattr(runtime.agent(), "_subagent_id", None)
    if not isinstance(child_id, str) or not child_id:
        return
    from agent.supervision_context import refs_in_text
    from tools.async_delegation import _DB_LOCK, _transaction
    opener = next((i for i in range(len(messages) - 1, -1, -1)
                   if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")), None)
    if opener is None:
        return
    bodies = {m.get("tool_call_id"): m.get("content") for m in messages[opener+1:] if m.get("role") == "tool"}
    produced = []
    try:
        with _DB_LOCK, _transaction() as conn:
            launches = conn.execute("""SELECT delegation_id,task_json,control_child_ids FROM async_delegations
                WHERE state='running' AND json_valid(control_child_ids)
                AND EXISTS (SELECT 1 FROM json_each(control_child_ids) WHERE value=?) LIMIT 2""", (child_id,)).fetchall()
            if len(launches) != 1:
                return
            launch, task_json, child_json = launches[0]
            task, children = json.loads(task_json), json.loads(child_json)
            indexes = task.get("task_indexes") or list(range(len(task.get("goals") or [])))
            if len(indexes) != len(children):
                return
            index = indexes[children.index(child_id)]
            goals = task.get("goals") or []
            if index >= len(goals):
                return
            targets = refs_in_text(goals[index])
            for call in messages[opener]["tool_calls"][:8]:
                call_id, function = call.get("id"), call.get("function", {})
                if function.get("name") not in {"read_file", "web_search", "web_extract"}:
                    continue
                body = bodies.get(call_id)
                if not isinstance(body, str) or not 0 < len(body) <= 1200:
                    continue
                args = json.loads(function.get("arguments", "{}"))
                # Declared resource arguments only. A query string is not a source ref.
                refs = ([args["path"]] if function["name"] == "read_file" and isinstance(args.get("path"), str)
                        else args.get("urls", []) if function["name"] == "web_extract" else [])
                if not set(refs) & set(targets):
                    continue
                parsed = json.loads(body)
                if not isinstance(parsed, dict) or parsed.get("error") or parsed.get("success") is False:
                    continue
                exact = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_call_id=? ORDER BY id DESC LIMIT 1",
                                     (runtime.session_id, call_id)).fetchone()
                if not exact or exact[0] != body:
                    continue
                payload = json.dumps({"task_index": index, "status": "completed", "summary": body,
                    "tool_call_id": call_id, "tool_name": function["name"]}).encode()
                source = store.put_result_object(conn, launch_id=launch,
                    source_id="tool:" + child_id + ":" + call_id, subtype="tool", payload=payload,
                    session_id=runtime.session_id)
                produced.append((launch, source))
        for launch, source in produced:
            offer_child_finding(launch, source)
    except (ValueError, TypeError, KeyError, sqlite3.Error, store.AdmissionError):
        return  # Optional early delivery cannot impede mandatory tool/final results.


def drain_findings(runtime):
    """Owner safe point queues an independently durable finding for NEXT native admission.

    It does not append during an active turn or ACK the final batch. The existing
    surface route/auth, reservation and SessionDB turn-lease checks still run.
    """
    from tools.async_delegation import commit_finding
    for target, opportunity in tuple(runtime.opportunities.items()):
        source = opportunity.get("delivery_source")
        if not source:
            continue
        entry = runtime._take(target, {Action.DELIVER_FINDING})
        if entry is None:
            continue
        proposal, registration = entry
        with registration.fence:
            failure = runtime._validate(proposal, registration)
            launch, object_id, text, refs, decisions = source
            meta = proposal.metadata
            valid = (proposal.expires_at_monotonic == opportunity["deadline"]
                and proposal.feature_id == "F03" and meta.get("feature_action") == "admit_evidence_once"
                and meta.get("source_receipt") == object_id and meta.get("immutable_object_id") == object_id
                and meta.get("source_ref") == object_id and meta.get("finding_id") == target
                and meta.get("launch_id") == launch and meta.get("final_completion_ack") is False
                and meta.get("verification") == "provisional" and meta.get("audience") == "owner"
                and set(meta.get("decision_ids", ())) <= {d["id"] for d in decisions}
                and bool(meta.get("decision_ids")) and pending_decisions(runtime, refs) == decisions)
            if failure or not valid:
                runtime._settle(proposal, *(failure or ("rejected", "admission_invalid")))
                continue
            try:
                commit_finding(launch, finding_id=target, source_receipt=object_id,
                               payload=text.encode(), _selection=(runtime, proposal, registration))
            except (store.AdmissionError, OSError, sqlite3.Error, ValueError):
                runtime._settle(proposal, "rejected", "admission_invalid")
            else:
                runtime.incidents.add(proposal.incident_id)
            runtime.closed_targets.add(target)


def final_decision(event, target_session):
    """After surface auth/route checks, before any presentation, wake or ACK."""
    from agent.completion_admission import CompletionDecision
    runtime = runtime_for_session(target_session)
    baseline = CompletionDecision()
    if runtime is None or event.get("finding_id"):
        return baseline
    conn = _read(runtime)
    try:
        store.generation(conn, target_session)
        launch = conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (event["delegation_id"],)).fetchone()
        if not launch or launch["parent_session_id"] != target_session or not launch["event_json"]:
            return baseline
        binding = json.loads(launch["task_json"] or "{}").get("supervision_revision", {})
        if any(binding.get(k) != getattr(runtime.revision, k) for k in ("profile", "lineage", "work_id", "instruction_event", "requirements")):
            return baseline
        saved = json.loads(launch["event_json"])
        object_id = saved.get("source_object_id")
        if not object_id or event.get("source_object_id") != object_id:
            return baseline
        raw = store.get_result_object(conn, object_id)
        if len(raw) > 1200 or event.get("error") or launch["state"] not in {"success", "completed"}:
            return baseline
        # Read an actual committed final, never a pre-final candidate or model assertion.
        old = conn.execute("""SELECT id,content FROM messages WHERE session_id=? AND role='assistant'
            AND tool_calls IS NULL AND content IS NOT NULL AND timestamp<=? AND timestamp>=?
            ORDER BY id DESC LIMIT 1""", (target_session, launch["completed_at"], launch["dispatched_at"])).fetchone()
        if not old or not 0 < len(old["content"]) <= 1200:
            return baseline
        control = store.get_launch_control(conn, event["delegation_id"])
        effect = conn.execute("SELECT effect_pending FROM delegation_result_objects WHERE object_id=?", (object_id,)).fetchone()[0]
        root = store.lineage(conn, target_session)
        delivery = store.digest(store.profile_key(), root, event["delegation_id"], "final", "final")
        available = True
        try:
            store.protect_optional(conn, object_id)
        except store.RetentionCapacity:
            available = False
    finally:
        conn.close()
    with runtime.lock:
        if delivery in runtime.closed_targets:
            return baseline
    finding = object_id
    reqs = [{"id": r.id, "text": r.text} for r in runtime.requirements if len(r.text) <= 1200]
    if len(reqs) > 4 or len(reqs) != len(runtime.requirements):
        return baseline
    result = {"id": delivery, "delivery_id": delivery, "source_event_id": event["delegation_id"],
        "immutable_object_id": object_id, "source_ref": object_id, "late": True,
        "identity_already_disposed": False, "ordinary_delivery_authorized": True, "optional": control["retainable"],
        "retainable": control["retainable"], "retention_available": available,
        "required_obligation": control["obligation"] != "optional", "effect_or_cleanup_pending": bool(effect)}
    facts = {"changed": True, "evidence_refs": [object_id], "result": result,
        "conclusion": {"id": "message:" + str(old["id"]), "text": old["content"]},
        "requirements": reqs, "findings": [{"id": finding, "text": raw.decode(),
            "source_ref": object_id, "verification": "provisional", "novel_claim": True}]}
    # One original admission deadline, not a fresh allowance per proposal/feature.
    issued = runtime.clock()
    deadline = min(issued + .150, runtime.round_deadline) if not runtime.closed and runtime.round_deadline else issued + .150
    actions = {Action.RETAIN_RESULT, Action.FINAL_BOUNDED_VIEW, Action.DELIVER_FINAL}
    snap = runtime.observe("completion_ready", facts, target_id=delivery, actions=actions,
        evidence_refs=(object_id,), owner="completion_admission", deadline=deadline,
        deadline_issued_at=issued, completeness=DeliveryCompleteness(), data_class="project_excerpt")
    if snap is None:
        return baseline
    runtime._wait_for(delivery, deadline, runtime.revision)
    with runtime.lock:
        entry = runtime._take(delivery, actions)
        if entry is None:
            runtime.closed_targets.add(delivery)
            return baseline
        proposal, registration = entry
        meta = proposal.metadata
        # Rechecked again inside the admission transaction at the actual owner effect.
        expected = {k: result[k] for k in ("delivery_id", "source_event_id", "immutable_object_id", "source_ref")}
        feature_action = {Action.RETAIN_RESULT: "retain_without_wake", Action.FINAL_BOUNDED_VIEW: "bounded_view", Action.DELIVER_FINAL: "deliver"}[proposal.action]
        if (proposal.feature_id != "F02" or meta.get("feature_action") != feature_action
                or any(meta.get(k) != v for k, v in expected.items())
                or tuple(meta.get("finding_ids", ())) != (finding,)
                or (proposal.action == Action.FINAL_BOUNDED_VIEW and tuple(meta.get("selected_finding_ids", ())) != (finding,))):
            runtime._settle(proposal, "rejected", "admission_invalid")
            return baseline
        dispositions = {Action.RETAIN_RESULT: "retain_without_wake", Action.FINAL_BOUNDED_VIEW: "deliver_bounded_view",
                        Action.DELIVER_FINAL: "deliver_unchanged"}
        return CompletionDecision(dispositions[proposal.action], control["retainable"],
            (0, len(raw)) if proposal.action == Action.FINAL_BOUNDED_VIEW else None, deadline,
            selection=(runtime, proposal, registration), comparison=(old["id"], store.digest(old["content"])))


def persist_selection(conn, decision, row):
    """In the same transaction as the native disposition; selection is NOT consumption."""
    if decision.selection is None:
        return
    from agent.supervision_receipts import persist
    runtime, proposal, _registration = decision.selection
    status = "applied" if row["state"] == "parked" else "selected"
    if row["disposition"] != decision.disposition or decision.declined:
        status = "no_op"
    receipt = SettlementV1(proposal.proposal_id, status,
        "admission_parked" if status == "applied" else "admission_persisted", runtime.sequence, proposal.proposal_id)
    persist(conn, runtime, row["target_session_id"], proposal, receipt, delivery_id=row["delivery_id"])
