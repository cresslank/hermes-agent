"""Host-owned request/result/clarification views, independent of any supervisor plugin.

Integration seam: attach ``SupervisionViews(facade, scope=revision_key)`` as
``agent._supervision_views``. The facade's rank_candidates/select_windows/
evaluate_relation(request) must promptly return a concurrent Future or a validated
mapping. Only the owner waits, at most the remaining ONE shared 150ms cycle token.
The facade owns egress consent and worker admission; no model is called here.
Response: {revision: request.revision, selected_ids: [canonical IDs]} or for
relations {revision: ..., relation: finite_label}. None means normal baseline.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
import copy
import json
import re
import time
import threading
import uuid
from typing import Any

from agent.supervision_catalog import Catalog, SkillView, fingerprint

_CRITICAL = re.compile(r"error|fail|warn|not[ _-]run|partial|approv|denied|mutat|cleanup|cancel|timeout|exit|receipt|commit", re.I)


@dataclass(frozen=True)
class AuthorizedDefault:
    """Created by the action owner from trusted instructions, NEVER tool arguments."""
    question: str
    value: str
    scope: str
    evidence_ref: str
    low_stakes: bool = False
    authorized: bool = False
    secret: bool = False
    approval: bool = False
    source_text: str = ""
    interpretations: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ResultSource:
    """Invocation-local binding; a digest or a plugin-supplied path is not authority."""
    scope: str
    revision: str
    tool_name: str
    call_id: str
    original: str
    path: str


class SupervisionViews:
    def __init__(self, facade, *, scope: str, clock=time.monotonic, deadline_provider=None):
        self.facade, self.scope, self.clock = facade, scope, clock
        self.deadline_provider = deadline_provider
        self.required_tools = ()
        self.deadline: float | None = None
        self._budget_lock = threading.Lock()
        self.tool_selection = None
        self.skills = SkillView()
        self.defaults: dict[str, AuthorizedDefault] = {}
        self.cache_identity = ""
        self._result_lock = threading.Lock()
        self._result_sources: dict[str, ResultSource] = {}

    def result_source(self, ref: str, *, scope: str, revision: str):
        """Resolve only a currently retained reference in this exact owner/scope."""
        with self._result_lock:
            source = self._result_sources.get(ref)
            if source is not None and source.scope == scope == self.scope and source.revision == revision:
                return source
        return None

    def cycle_deadline(self):
        if self.deadline_provider is not None:
            return self.deadline_provider()
        with self._budget_lock:
            if self.deadline is None:
                self.deadline = self.clock() + .150
            return self.deadline

    def next_cycle(self):
        with self._budget_lock:
            self.deadline = None

    def reset(self, scope: str):
        with self._result_lock:
            self.scope = scope
            self._result_sources.clear()
        self.tool_selection = None
        self.required_tools = ()
        self.skills = SkillView()
        self.defaults.clear()
        self.next_cycle()

    def _decide(self, method: str, domain: str, facts: dict, *, revision: str):
        deadline, scope = self.cycle_deadline(), self.scope
        if self.clock() >= deadline:
            return None
        request = {"domain": domain, "scope": scope, "revision": revision,
                   "deadline": deadline, "facts": copy.deepcopy(facts)}
        try:
            answer = getattr(self.facade, method)(request)
            if isinstance(answer, Future):
                answer = answer.result(timeout=max(0, deadline - self.clock()))
            if self.clock() >= deadline or self.scope != scope:
                return None
            if not isinstance(answer, dict) or answer.get("revision") != revision:
                return None
            return answer
        except (Exception, TimeoutError):
            return None

    def propose_tools(self, ids: tuple[str, ...], *, catalog_revision: str, scope: str, include_deferred=False):
        self.tool_selection = (ids, catalog_revision, scope)
        self.include_deferred = include_deferred

    def tools(self, schemas, *, required_ids=(), rules_revision="", available_schemas=None):
        available = schemas if available_schemas is None else available_schemas
        try:
            catalog = Catalog.tools(available, required_ids=required_ids, rules_revision=rules_revision)
        except (TypeError, ValueError, AttributeError):
            self.cache_identity = ""
            return schemas
        result = schemas
        if self.tool_selection is not None:
            ids, revision, scope = self.tool_selection
            if scope == self.scope:
                candidate = catalog.select(available, ids, revision)
                if candidate is not available:
                    result = candidate
        self.cache_identity = fingerprint((self.scope, catalog.revision, result))
        return result

    def clarify(self, question: str, choices, *, multi_select=False):
        default = self.defaults.get(question)
        if default is None:
            handler = getattr(self.facade, "clarify_slot", None)
            if callable(handler):
                return handler(question, choices, multi_select=multi_select)
        if (not isinstance(default, AuthorizedDefault) or default.scope != self.scope
                or not default.authorized or not default.low_stakes or default.secret or default.approval
                or not default.evidence_ref or multi_select
                or (choices and default.value not in choices)):
            return None
        revision = fingerprint((self.scope, question, choices, default.value, default.evidence_ref))
        answer = self._decide("evaluate_relation", "clarification_proposed", {
            "question": question, "choices": choices, "authorized_default": default.value,
            "evidence_ref": default.evidence_ref, "low_stakes": True,
        }, revision=revision)
        if answer and answer.get("relation") == "use_authorized_default":
            # Never label this as an answer supplied by the user.
            return {"question": question, "choices_offered": choices, "user_response": "",
                    "resolution": "authorized_default", "resolved_value": default.value,
                    "evidence_ref": default.evidence_ref}
        return None

    def rank_retrieval(self, candidates: tuple[dict, ...], *, required_ids=(), complete: bool):
        """Explicit retrieval owner seam: no corpus search or expanded permissions.

        Candidate fields: id, excerpt, source_ref. Original records/ranks survive.
        Unknown/incomplete/oversized pools retain their original ranking.
        """
        if not complete or not 1 < len(candidates) <= 8:
            return candidates
        if any(not isinstance(c, dict) or not isinstance(c.get("id"), str)
               or not c.get("source_ref") or not isinstance(c.get("excerpt"), str)
               or len(c["excerpt"]) > 1200 for c in candidates):
            return candidates
        ids = tuple(c["id"] for c in candidates)
        if len(set(ids)) != len(ids) or not set(required_ids) <= set(ids):
            return candidates
        revision = fingerprint((self.scope, candidates, required_ids))
        answer = self._decide("rank_candidates", "retrieval_candidates", {
            "candidates": candidates, "required_ids": tuple(required_ids), "complete": True,
        }, revision=revision)
        selected = _selected(answer, ids)
        if not selected:
            return candidates
        by_id = dict(zip(ids, candidates))
        # Ranking is not evidence deletion. Keep all unselected original tail records.
        return tuple(by_id[i] for i in selected) + tuple(c for c in candidates if c["id"] not in selected)

    def result(self, original: Any, baseline: Any, *, tool_name: str, call_id: str, env, budget):
        """One canonical admission after dispatch, before hints/wrapping/transcript append.

        Only structured envelopes with a textual output are selectable. All sibling
        status/receipt/side-effect fields are copied exactly. Opaque, multimodal,
        malformed, oversized-line and incomplete critical-span pools use baseline.
        """
        scope = self.scope
        if not isinstance(original, str) or len(original) < 2400:
            return baseline
        try:
            envelope = json.loads(original)
        except (ValueError, TypeError):
            return baseline
        if not isinstance(envelope, dict):
            return baseline
        fields = [k for k in ("output", "stdout", "content") if isinstance(envelope.get(k), str)]
        if len(fields) != 1 or "supervision_view" in envelope:
            return baseline
        field = fields[0]
        text = envelope[field]
        blocks = _blocks(text)
        if not blocks or len(blocks) < 2:
            return baseline
        required = {b["id"] for b in blocks if b["critical"]}
        # No selection can erase neighbors of a failure or receipt.
        indices = {i for i, b in enumerate(blocks) if b["critical"]}
        required |= {blocks[j]["id"] for i in indices for j in (i - 1, i + 1) if 0 <= j < len(blocks)}
        if len(blocks) > 8:
            return baseline
        # The feature may only observe a persisted source. Failure keeps baseline
        # without dispatching a semantic selection.
        from tools.tool_result_storage import maybe_persist_tool_result, extract_persisted_path
        archive = maybe_persist_tool_result(original, tool_name, call_id + "-" + fingerprint(original),
                                            env=env, config=budget, threshold=0)
        path = extract_persisted_path(archive)
        if not path:
            return baseline
        revision = fingerprint((scope, call_id, original))
        source = ResultSource(scope, revision, tool_name, call_id, original, path)
        # Paths have no protocol-sized upper bound. Issue a fresh opaque key,
        # retaining the exact source rather than truncating or hashing authority.
        ref = "result:" + uuid.uuid4().hex
        with self._result_lock:
            if scope != self.scope or len(self._result_sources) >= 32:
                return baseline
            self._result_sources[ref] = source
        try:
            answer = self._decide("select_windows", "oversized_structured_result", {
                "tool_name": tool_name, "call_id": call_id, "candidates": tuple(blocks),
                "required_ids": tuple(sorted(required)), "complete": True, "source_ref": ref,
            }, revision=revision)
            if self.result_source(ref, scope=scope, revision=revision) is not source:
                return baseline
        finally:
            with self._result_lock:
                self._result_sources.pop(ref, None)
        selected = _selected(answer, tuple(b["id"] for b in blocks))
        if not selected:
            return baseline
        keep = set(selected) | required
        if len(keep) == len(blocks):
            return baseline
        chosen = [b for b in blocks if b["id"] in keep]
        view = dict(envelope)
        view[field] = "".join(b["excerpt"] for b in chosen)
        view["supervision_view"] = {
            "omitted": True, "full_output_ref": path, "original_chars": len(text),
            "spans": [{"id": b["id"], "start": b["start"], "end": b["end"]} for b in chosen],
        }
        return json.dumps(view, ensure_ascii=False)


def _selected(answer, known):
    ids = answer.get("selected_ids") if isinstance(answer, dict) else None
    if (not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids) or not set(ids) <= set(known)):
        return None
    return tuple(ids)


def _blocks(text):
    blocks, start, current = [], 0, ""
    for line in text.splitlines(keepends=True):
        if len(line) > 1200:
            return None
        if current and len(current) + len(line) > 1200:
            blocks.append({"id": f"{start}:{start + len(current)}", "start": start,
                           "end": start + len(current), "excerpt": current,
                           "critical": bool(_CRITICAL.search(current))})
            start += len(current)
            current = ""
        current += line
    if current:
        blocks.append({"id": f"{start}:{start + len(current)}", "start": start,
                       "end": start + len(current), "excerpt": current,
                       "critical": bool(_CRITICAL.search(current))})
    return blocks


def request_views(agent, api_messages, schemas):
    owner = getattr(agent, "_supervision_views", None)
    if not isinstance(owner, SupervisionViews):
        return api_messages, schemas
    # Batch facts are produced AFTER tool-result draining. Consume planning only
    # here, on a clone of the latest tool row, before cache/provider conversion.
    # Do not mutate skill state, the system prefix, canonical rows, or add a turn.
    from agent.supervision_policy import runtime_for_agent
    runtime = runtime_for_agent(agent)
    if runtime is not None and api_messages and api_messages[-1].get("role") == "tool" and isinstance(api_messages[-1].get("content"), str):
        advisory = runtime.dependencies.planning.advisory()
        if advisory:
            api_messages = [*api_messages[:-1], {**api_messages[-1], "content": api_messages[-1]["content"] + "\n\n" + advisory}]
    binding = getattr(agent, "_supervision_view_binding", None)
    if binding is not None:
        binding.prepare_catalogs()
    available = None
    if owner.tool_selection is not None and getattr(owner, "include_deferred", False):
        from agent.supervision_catalog import authorized_tool_schemas
        try:
            available = authorized_tool_schemas(agent)
        except Exception:
            owner.tool_selection = None
    tools = owner.tools(schemas, required_ids=tuple(set(owner.required_tools) | set(getattr(agent, "_supervision_required_tools", ()))),
                        rules_revision=getattr(agent, "_supervision_rules_revision", ""),
                        available_schemas=available)
    if owner.skills.hints:
        # Only the request clone changes. Historical skill bodies remain untouched.
        api_messages = list(api_messages)
        for index in range(len(api_messages) - 1, -1, -1):
            msg = api_messages[index]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                api_messages[index] = {**msg, "content": msg["content"] + "\n\n" + next(iter(owner.skills.hints.values()))}
                break
    owner.next_cycle()
    return api_messages, tools


def canonical_result_view(agent, original, baseline, **kwargs):
    owner = getattr(agent, "_supervision_views", None)
    if not isinstance(owner, SupervisionViews):
        return baseline
    try:
        result = owner.result(original, baseline, **kwargs)
        if result != baseline:
            path = json.loads(result)["supervision_view"]["full_output_ref"]
            guard = getattr(agent, "_tool_guardrails", None)
            if guard is not None:
                guard.record_persisted_result(kwargs["call_id"], path)
        return result
    except Exception:
        return baseline
