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
        for claim in value["claims"]:
            if set(claim) != {"artifact_ref", "start", "end", "requirement_indexes"} or not indices(claim):
                return None
            artifact = artifacts[claim["artifact_ref"]]
            exact_span(claim["artifact_ref"], digest(artifact), artifact, claim["start"], claim["end"])
        return freeze({**value, "requirement_spans": requirements})
    except (ValueError, TypeError, KeyError, IndexError):
        return None


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
            "requirements": [{"id": r.id, "source_message_id": r.source_message_id,
                              "start": r.start, "end": r.end, "text": r.text}
                             for r in runtime.requirements]}


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
            return
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
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
            return
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return
    exact_write = tool_name == "write_file" and type(args.get("content")) is str and text == args["content"]
    runtime.record_artifact(path, text, verified=exact_write,
                            main_agent=getattr(agent, "platform", None) != "subagent")
