"""Bounded exact-source producers; parsing never confers truth, permission or global coverage.

Origin grants are private in-process values issued only by accepted-input owners. A
serialized dict, a role label, an OOB wrapper or a plugin-injected string is not one.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import uuid
import threading
import weakref
from typing import Mapping

from agent.supervision_types import Completeness, ExactSpan, freeze

MAX_TEXT_BYTES = 16 * 1024
MAX_CANDIDATES = 32
_ORIGIN_SEAL = object()
_origin_owners = weakref.WeakKeyDictionary()
_origin_lock = threading.Lock()
_origin_context = ContextVar("supervision_accepted_origin", default=None)


@dataclass(frozen=True)
class _InputOrigin:
    seal: object
    text: str
    kind: str
    message_id: str
    continuation: bool = False
    delivery_digest: str | None = None


def accepted_input_origin(text: str, *, kind: str, message_id: str | None = None,
                          continuation: bool = False):
    """HOST ONLY: call after local submission / transport authentication, never from a hook."""
    if type(text) is not str or not text.strip() or kind not in {"cli", "tui", "gateway", "authorized_api"}:
        return None
    return _InputOrigin(_ORIGIN_SEAL, text, kind, message_id or uuid.uuid4().hex, continuation)


class AcceptedInputText(str):
    """Private queue carrier; origin never enters transcript or semantic projection."""
    def __new__(cls, text, *, kind="cli"):
        instance = super().__new__(cls, text)
        instance._supervision_origin = accepted_input_origin(text, kind=kind)
        return instance


def steer_from_user(agent, text, *, kind="cli", redirect=False):
    """Accepted surface control, preserving the ABI of third-party agents."""
    from agent.interrupt_control import InterruptControlMixin
    method = agent.redirect if redirect else agent.steer
    if isinstance(agent, InterruptControlMixin):
        origin = accepted_input_origin(str(text), kind=kind, continuation=True)
        return method(text, origin=origin)
    return method(text)


def is_accepted_origin(origin) -> bool:
    return isinstance(origin, _InputOrigin) and origin.seal is _ORIGIN_SEAL


@contextmanager
def accepted_origin_scope(origin):
    token = _origin_context.set(origin if is_accepted_origin(origin) else None)
    try:
        yield
    finally:
        _origin_context.reset(token)


def bind_origin_delivery(origin, text):
    """HOST ONLY: preserve original user bytes through an owned steer wrapper."""
    return replace(origin, continuation=True, delivery_digest=digest(text.strip())) if is_accepted_origin(origin) else None


def origin_matches_delivery(origin, text):
    return is_accepted_origin(origin) and (origin.delivery_digest == digest(text.strip())
        if origin.delivery_digest is not None else origin.text.strip() == text.strip())


def accept_pending_input(agent, origin=None):
    from agent.supervision_policy import runtime_for_agent
    origin = origin if origin is not None else _origin_context.get()
    if not is_accepted_origin(origin):
        return None
    runtime = runtime_for_agent(agent, create=True)
    if runtime is None:
        return None
    with _origin_lock:
        owner = _origin_owners.get(origin)
        if owner is not None and owner() is not agent:
            return None  # copied context into a child is not another authentic user submission
        _origin_owners[origin] = weakref.ref(agent)
    runtime.accept_instruction(origin)
    return origin


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_text(text: str) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    return raw[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), len(raw) > MAX_TEXT_BYTES


def _visible_lines(text):
    """Yield exact line offsets outside Markdown fenced/indented code and block quotes."""
    offset, fence, heading = 0, None, ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        elif fence is None and not line.startswith(("    ", "\t")) and not stripped.startswith(">"):
            if re.match(r"#{1,6}\s", stripped):
                heading = line.rstrip("\r\n")
            yield offset, line, heading
        offset += len(line)


def exact_span(source_id, revision, text, start, end, *, kind="candidate", heading=""):
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise ValueError("invalid_span")
    sid = digest(f"{source_id}\0{revision}\0{start}\0{end}")
    return ExactSpan(sid, source_id, revision, start, end, text[start:end], heading, kind)


def enumerate_requirements(text: str, source_id: str):
    bounded, omitted = bounded_text(text)
    revision = digest(text)
    spans = []
    for offset, line, heading in _visible_lines(bounded):
        if re.match(r"\s*(?:[-+*]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)\S", line):
            if len(spans) == MAX_CANDIDATES:
                omitted = True
                break
            end = offset + len(line.rstrip("\r\n"))
            next_offset = offset + len(line)
            # Preserve indented qualifiers belonging to the same item. Truncating at
            # the first physical line can turn 'only if ...' into an unconditional ask.
            for continuation in bounded[next_offset:].splitlines(keepends=True):
                stripped = continuation.lstrip()
                if (not continuation.strip() or not continuation[:1].isspace()
                        or re.match(r"(?:[-+*]\s|\d+[.)]\s|>|#{1,6}\s|```|~~~)", stripped)):
                    break
                end = next_offset + len(continuation.rstrip("\r\n"))
                next_offset += len(continuation)
            spans.append(exact_span(source_id, revision, text, offset, end,
                                    kind="requirement", heading=heading))
    return tuple(spans), Completeness(complete=not omitted, omitted=omitted)


def claim_candidates(text: str, source_id: str, *, required_refs=(), previous_text=None):
    """Only literal linked spans; no inferred semantic decomposition or topic matching."""
    bounded, omitted = bounded_text(text)
    revision, spans = digest(text), []
    unchanged = ({s.text for s in claim_candidates(previous_text, source_id, required_refs=required_refs)[0]}
                 if previous_text is not None else set())
    for offset, line, heading in _visible_lines(bounded):
        for match in re.finditer(r".+?(?:[.!?](?=\s|$)|$)", line):
            body = match.group()
            if not body.strip() or body in unchanged or not any(ref in body for ref in required_refs):
                continue
            if len(spans) == MAX_CANDIDATES:
                omitted = True
                break
            spans.append(exact_span(source_id, revision, text, offset + match.start(),
                                    offset + match.end(), kind="claim_candidate", heading=heading))
    return tuple(spans), Completeness(scope="linked_candidates", complete=False, omitted=omitted)


def literal_refs(text: str) -> tuple[tuple[str, str], ...]:
    # Exact spelling only. Neither URI dereference nor canonicalization confers authorization.
    return tuple(dict.fromkeys(re.findall(r"`([^`\n]+)`|\b(https?://[^\s<>]+)", text)))


def refs_in_text(text: str) -> tuple[str, ...]:
    return tuple(a or b for a, b in literal_refs(text))


def _strict_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate_key")
        out[key] = value
    return out


def parse_work_map(text: str, *, artifact_ref: str, designated_refs: set[str],
                   sources: Mapping[str, str], todos: Mapping[str, dict],
                   artifacts: Mapping[str, str]):
    """Validate the optional committed main-agent plan format against owner sources.

    Caller must establish committed bytes and main-agent ownership before calling.
    Unknown keys, out-of-range refs, freeform targets and capped input reject the block.
    """
    if artifact_ref not in designated_refs or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return None
    blocks = re.findall(r"(?m)^```hermes-work-map-v1\s*\n(.*?)^```\s*$", text, re.S)
    if len(blocks) != 1:
        return None
    try:
        value = json.loads(blocks[0], object_pairs_hook=_strict_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if not isinstance(value, dict) or set(value) != {"version", "requirements", "steps", "claims"}:
            return None
        if type(value["version"]) is not int or value["version"] != 1:
            return None
        if any(type(value[k]) is not list or len(value[k]) > MAX_CANDIDATES for k in ("requirements", "steps", "claims")):
            return None
        requirements = []
        for ref in value["requirements"]:
            if set(ref) != {"source_message_id", "start", "end"} or ref["source_message_id"] not in sources:
                return None
            source = sources[ref["source_message_id"]]
            requirements.append(exact_span(ref["source_message_id"], digest(source), source,
                                           ref["start"], ref["end"], kind="linked_requirement"))
        def indices(row):
            ids = row["requirement_indexes"]
            return (type(ids) is list and bool(ids) and
                    all(type(i) is int and 0 <= i < len(requirements) for i in ids))
        for step in value["steps"]:
            if set(step) != {"todo_id", "requirement_indexes", "target_refs"} or not indices(step):
                return None
            if step["todo_id"] not in todos or type(step["target_refs"]) is not list:
                return None
            # Targets must have been designated by authentic source or an existing todo.
            allowed = set(designated_refs) | set(refs_in_text(str(todos[step["todo_id"]].get("content", ""))))
            if not step["target_refs"] or not all(type(r) is str and r in allowed for r in step["target_refs"]):
                return None
        claim_spans = []
        for claim in value["claims"]:
            if set(claim) != {"artifact_ref", "start", "end", "requirement_indexes"} or not indices(claim):
                return None
            artifact = artifacts[claim["artifact_ref"]]
            claim_spans.append(exact_span(claim["artifact_ref"], digest(artifact), artifact, claim["start"], claim["end"], kind="linked_claim"))
        return freeze({**value, "requirement_spans": requirements, "claim_spans": claim_spans})
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def parse_planning_proposals(text, *, artifact_ref, designated_refs):
    """Separate optional annex. This parser grants no source or planner authority.

    Only a verified main-agent commit may pass its result to DependencyOwner.
    Version one's closed records intentionally do not extend work-map-v1.
    """
    if artifact_ref not in designated_refs or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return None
    blocks = re.findall(r"(?m)^```hermes-planning-proposals-v1\s*\n(.*?)^```\s*$", text, re.S)
    if len(blocks) != 1 or len(blocks[0].encode()) > 16384:
        return None
    keys = {
        "delegation_candidate": {"local_id", "todo_id", "parent_next_todo_id", "candidate_span", "acceptance_refs", "input_refs", "dependency_todo_ids", "operation", "required_resource_ref"},
        "research_pass_open": {"local_id", "gap_refs", "completion_criteria_refs", "source_method_ref", "operations"},
        "research_pass_close": {"pass_id", "member_dispositions"},
        "optional_expansion": {"local_id", "gap_refs", "completion_criteria_refs", "source_method_ref", "operation", "consumer_refs", "effect_policy_id", "obligation_request"},
        "withdraw_expansion": {"expansion_id"},
    }
    def finite_float(raw):
        import math
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value
    try:
        value = json.loads(blocks[0], object_pairs_hook=_strict_object, parse_float=finite_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if (type(value) is not dict or set(value) != {"version", "records"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["records"]) is not list or len(value["records"]) > 32):
            return None
        seen = set()
        for row in value["records"]:
            if type(row) is not dict or type(row.get("type")) is not str:
                return None
            kind = row["type"]
            if kind not in keys or set(row) != keys[kind] | {"type"}:
                return None
            identity = row.get("local_id", row.get("pass_id", row.get("expansion_id")))
            if type(identity) is not str or not 0 < len(identity) <= 256 or (kind, identity) in seen:
                return None
            seen.add((kind, identity))
        return value["records"]
    except (ValueError, TypeError, RecursionError):
        return None


def visible_exact_occurrence(text, candidate):
    """Locate one literal claim outside code/quotes without semantic decomposition."""
    if not candidate or text.count(candidate) != 1:
        return False
    start = text.index(candidate)
    end = start + len(candidate)
    covered = sum(max(0, min(end, offset + len(line)) - max(start, offset))
                  for offset, line, _ in _visible_lines(text))
    return covered == len(candidate)


def complete_requirement_context(runtime, span):
    """Only a complete enumerated item (including indented qualifiers), or an
    entire bounded authenticated source, can be a semantic requirement window.
    Work-map substring validity alone does not prove qualifier completeness.
    """
    source = runtime.sources.get(span.source_message_id)
    if source is None or digest(source) != span.source_revision:
        return False
    if span.start == 0 and span.end == len(source):
        return True
    return any(r.source_message_id == span.source_message_id
               and r.source_revision == span.source_revision
               and r.start == span.start and r.end == span.end for r in runtime.requirements)


def action_conflict_facts(runtime, tool_name, arguments, call_id, targets):
    """Project an actual changed plan link, keeping unknown obligations explicit.

    current_authorization describes current accepted requirement provenance, not
    permission to execute the tool. The ordinary dispatcher still owns permission.
    A work map cannot declare absence of prerequisites or cleanup under v1.
    """
    records = [runtime.invalidated_action_facts[t] for t in targets
               if t in runtime.invalidated_action_facts]
    if len(records) != 1:
        return None
    path, pin, revision, todo_id, requirements = records[0]
    todos, current_revision = runtime.plan_steps()
    if (runtime.artifact_pins.get(path) != pin or revision != current_revision
            or todo_id not in todos or todos[todo_id].get("status") == "completed"
            or not 1 <= len(requirements) <= 4):
        return None
    for req in requirements:
        source = runtime.sources.get(req.source_message_id)
        if (not complete_requirement_context(runtime, req) or source is None
                or digest(source) != req.source_revision or source[req.start:req.end] != req.text):
            return None
    action = json.dumps({"tool_name": tool_name, "arguments": arguments}, ensure_ascii=False)
    step = todos[todo_id].get("content")
    if not isinstance(step, str) or not step or max(len(action), len(step), *(len(r.text) for r in requirements)) > 2400:
        return None
    refs = tuple(r.id for r in requirements)
    return {"changed": True, "evidence_refs": refs, "requirement_refs": refs,
        "action": {"id": call_id, "text": action, "issued": False},
        "current_step": {"id": todo_id, "text": step},
        "requirements": [{"id": r.id, "text": r.text, "source_ref": r.id} for r in requirements],
        "authorized_scope": "Current accepted source clauses; tool permission is separately host-checked.",
        "link": {"id": digest(path + pin + todo_id), "validated": True,
            "requirement_ids": refs, "conflict": "invalidated_known_edge",
            "current_authorization": True, "valid_prerequisite": None, "required_cleanup": None}}


def completed_batch_facts(messages, *, revision, requirements, target_refs):
    """Extract bounded source facts from the actual paired, fully settled tool batch.

    Ordinary delegated summaries remain derived candidates (never verified effects).
    Shell/execute_code/opaque connector results have only an unknown effect receipt.
    """
    from agent.supervision_types import EffectReceiptV1, FindingV1
    opener = next((i for i in range(len(messages) - 1, -1, -1)
                   if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")), None)
    if opener is None:
        return (), (), Completeness(scope="tool_batch")
    calls = {c.get("id"): c for c in messages[opener]["tool_calls"] if isinstance(c, dict)}
    rows = {r.get("tool_call_id"): r for r in messages[opener + 1:] if r.get("role") == "tool"}
    if not calls.keys() <= rows.keys():
        return (), (), Completeness(scope="tool_batch")
    receipts, findings, used_bytes, omitted = [], [], 0, False
    for call_id, call in calls.items():
        body = rows[call_id].get("content")
        if not isinstance(body, str):
            continue
        raw = body.encode("utf-8")
        if len(raw) + used_bytes > MAX_TEXT_BYTES or len(receipts) >= MAX_CANDIDATES:
            omitted = True
            continue
        used_bytes += len(raw)
        name = call.get("function", {}).get("name")
        receipt = EffectReceiptV1(digest(str(call_id) + "\0" + body), str(call_id),
                                  "tool-result:" + str(call_id), digest(body))
        receipts.append(receipt)
        if name not in {"delegate_task", "web_search", "web_extract", "read_file"} or not requirements:
            continue
        # Exact serialized result spans are recoverable from the existing canonical tool row.
        # A topic similarity is not a consumer link; only a requested exact ref qualifies.
        for ref in target_refs:
            start = body.find(ref)
            if start < 0 or len(findings) >= MAX_CANDIDATES:
                continue
            begin, end = max(0, start - 400), min(len(body), start + len(ref) + 800)
            span = exact_span("tool-result:" + str(call_id), digest(body), body, begin, end,
                              kind="finding_candidate")
            consumers = tuple(r.id for r in requirements if ref in r.text)
            if not consumers:
                continue
            findings.append(FindingV1(span.id, receipt.receipt_id, revision, span, consumers))
    return tuple(receipts), tuple(findings), Completeness(scope="tool_batch", complete=not omitted, omitted=omitted)


def work_map_source_refs(store):
    """Optional exact refs on an already-requested todo result for a designated plan.

    This gives the main agent real source IDs for the optional work-map format without
    a new visible tool, prompt hook, synthetic turn or guessed fixture-only ID.
    """
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    agent = get_active_subagent_parent()
    if agent is None or getattr(agent, "_todo_store", None) is not store:
        return None
    runtime = runtime_for_agent(agent)
    if runtime is None or not runtime.requirements:
        return None
    todos, _ = runtime.plan_steps()
    if not any(set(refs_in_text(str(t.get("content", "")))) & runtime.designated_refs for t in todos.values()):
        return None
    return {"version": 1, "scope": "enumerated_items", "global_coverage": "unknown",
            "planning_records": runtime.dependencies.planning.inventory(),
            "requirements": [{"id": r.id, "source_message_id": r.source_message_id,
                              "start": r.start, "end": r.end, "text": r.text}
                             for r in runtime.requirements]}


def record_file_owner_read(path, result, *, offset, redacted, snapshot):
    """Retain only exact complete bytes already returned by the native read owner.

    The native read hashes bytes on the same descriptor. Strip only its literal
    line gutters, then require that digest; truncated, redacted, extracted, remote
    and newline-normalized output cannot masquerade as an exact source window.
    """
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    agent = get_active_subagent_parent()
    runtime = runtime_for_agent(agent) if agent is not None else None
    if runtime is None or path not in runtime.designated_artifact_refs():
        return
    native_snapshot = isinstance(snapshot, tuple) and len(snapshot) == 6 and type(snapshot[-1]) is bytes
    # A changed descriptor digest invalidates the old complete source even when
    # this particular read returns only a page, or its new text is redacted.
    if (native_snapshot and path in runtime.artifact_pins
            and runtime.artifact_pins[path] != snapshot[-1].hex()):
        runtime.invalidate_artifact(path)
    if (offset != 1 or redacted or not native_snapshot
            or result.get("truncated") or result.get("truncated_lines") or result.get("error")
            or result.get("is_binary") or result.get("extracted_document")
            or type(result.get("file_size")) is not int or not 0 < result["file_size"] <= MAX_TEXT_BYTES):
        return
    formatted = result.get("content")
    if not isinstance(formatted, str) or len(formatted.encode("utf-8")) > 2 * MAX_TEXT_BYTES:
        return
    lines = formatted.split("\n")
    if any(not line.startswith(str(i) + "|") for i, line in enumerate(lines, 1)):
        return
    body = "\n".join(line.split("|", 1)[1] for line in lines)
    for candidate in (body, body + "\n"):
        raw = candidate.encode("utf-8")
        if len(raw) == result["file_size"] and hashlib.sha256(raw).digest() == snapshot[-1]:
            from agent.supervision_planning import publish_read
            publish_read(path, candidate)
            runtime.record_artifact(path, candidate, verified=False, main_agent=False)
            return


def record_file_owner_commit(tool_name, path, *, task_id="default", source_ref=None):
    """Called INSIDE the canonical local file owner's successful commit/path lock.

    This is the positive patch receipt path: post-tool status alone cannot prove it.
    """
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    agent = get_active_subagent_parent()
    runtime = runtime_for_agent(agent) if agent is not None else None
    if runtime is None:
        return
    source_ref = source_ref or path
    designated = runtime.designated_artifact_refs()
    if source_ref not in designated and path not in designated:
        return
    from tools.file_tools_paths import container_backend_for_task
    if container_backend_for_task(task_id) is not None:
        return
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            runtime.invalidate_artifact(source_ref)
            return
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        runtime.invalidate_artifact(source_ref)
        return
    runtime.record_artifact(source_ref, text, verified=True,
        main_agent=getattr(agent, "platform", None) != "subagent" and not getattr(agent, "_delegate_id", None))


def record_committed_write(agent, tool_name, args, result, *, landed, task_id=None):
    """Read at most one changed local file's capped bytes, and verify write_file's exact bytes.

    Shell/remote effects and partial patches never become verified receipts. No scan.
    A local patch may supply candidates, but without a content pin its effects stay unknown.
    """
    from agent.supervision_policy import runtime_for_agent
    runtime = runtime_for_agent(agent)
    if runtime is None or not landed:
        return
    from tools.file_tools_paths import container_backend_for_task
    if container_backend_for_task(task_id or "default") is not None:
        return
    from agent.turn_explainers import _file_mutation_identity
    path = args.get("path")
    if not isinstance(path, str):
        return
    identity = _file_mutation_identity(path, task_id)
    # Only already-linked artifacts can create an opportunity or expose project text.
    designated = runtime.designated_artifact_refs()
    if path not in designated and identity not in designated:
        return
    try:
        with Path(identity).open("rb") as handle:
            raw = handle.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            runtime.invalidate_artifact(path)
            return
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        runtime.invalidate_artifact(path)
        return
    exact_write = tool_name == "write_file" and type(args.get("content")) is str and text == args["content"]
    runtime.record_artifact(path, text, verified=exact_write,
                            main_agent=getattr(agent, "platform", None) != "subagent"
                            and not getattr(agent, "_delegate_id", None))
