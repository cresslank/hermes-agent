"""Configured MCP owner binding at the typed, authenticated transport return.

No inference on the MCP loop and no source-name-based authority. Operator policy
pins the actual connected route, native tool identity and account/source scope.
Adapters only project typed data; native code retains every row and metadata.
"""
from __future__ import annotations
import hashlib
import json
import uuid
from urllib.parse import urlsplit
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from contextlib import nullcontext
from agent.supervision_types import Action, Completeness, OwnerRequestV1, freeze, project


def source_grants(value):
    if not isinstance(value, list) or len(value) > 8:
        return ()
    accepted = []
    for row in value:
        common = {"server", "tool", "accounts", "sources", "modes", "remote_processing", "allow_live_fetch"}
        if not isinstance(row, dict) or set(row) not in (common | {"url"}, common | {"command", "args"}):
            continue
        if any(type(row[k]) is not str or not row[k] for k in ("server", "tool")):
            continue
        if "url" in row:
            if type(row["url"]) is not str:
                continue
            try:
                route = urlsplit(row["url"])
            except ValueError:
                continue
            if route.scheme not in {"http", "https"} or not route.hostname or route.username or route.password or route.query or route.fragment:
                continue
        elif (type(row["command"]) is not str or not row["command"] or
              not isinstance(row["args"], list) or len(row["args"]) > 32 or
              any(type(a) is not str or len(a) > 1024 for a in row["args"])):
            continue
        if any(not isinstance(row[k], list) or not row[k] or len(row[k]) > 32 or
               any(type(x) is not str or not 0 < len(x) <= 128 for x in row[k]) for k in ("accounts", "sources", "modes")):
            continue
        if set(row["modes"]) - {"stored", "live", "hybrid"} or any(type(row[k]) is not bool for k in ("remote_processing", "allow_live_fetch")):
            continue
        accepted.append(freeze(row))
    return tuple(accepted)


def _route_matches(server, grant):
    if "url" in grant:
        return server._config.get("url") == grant["url"] and not server._config.get("command")
    return (not server._config.get("url") and server._config.get("command") == grant["command"] and
            tuple(server._config.get("args", ())) == tuple(grant["args"]))


@dataclass(frozen=True)
class _RecipientBinding:
    """Host-only authority; never serialized into provider facts or egress policy."""
    registration: object
    generation: str
    grant: Mapping
    adapter: object
    scope: str
    key: str | tuple[str, str]
    server: Any
    session: object
    projection: Mapping

    def current(self, registration, *, transport_locked=False):
        from tools.mcp_tool_common import _core
        from tools.mcp_tool_scope import _key_visible_in_scope
        if (registration is not self.registration or not registration.active or
                registration.generation != self.generation or registration.scope != self.scope or
                registration.mcp_adapter is not self.adapter or self.grant not in registration.mcp_sources or
                not {"observe", "rank_candidates"} <= registration.grants or
                "project_excerpt" not in registration.data_policy):
            return False
        with nullcontext() if transport_locked else _core._lock:
            return (_core._servers.get(self.key) is self.server and
                    self.server.session is self.session and
                    _key_visible_in_scope(self.key, self.scope) and _route_matches(self.server, self.grant))


def recipient_authorized(bindings, registration, *, transport_locked=False):
    return any(binding.current(registration, transport_locked=transport_locked) for binding in bindings)


def consumption_fence(opportunity):
    # Serialize the final receipt with transport replacement as well as the
    # selected registration's existing revocation fence. No provider I/O here.
    if opportunity and opportunity.get("mcp_recipients"):
        from tools.mcp_tool_common import _core
        return _core._lock
    return nullcontext()


def _project_recipients(scope, key, server_name, server, session, tool, args, raw):
    from agent.supervision_facade import registrations_for_scope
    from agent.supervision_owner_protocol import bounded_facts
    from agent.supervision_retrieval_presentation import VERSION
    recipients = []
    projection = None
    for registration in registrations_for_scope(scope):
        if (not callable(registration.mcp_adapter) or
                not {"observe", "rank_candidates"} <= registration.grants or
                "project_excerpt" not in registration.data_policy):
            continue
        for grant in registration.mcp_sources:
            if (grant["server"] != server_name or grant["tool"] != tool or
                    not _route_matches(server, grant) or not grant["remote_processing"]):
                continue
            binding = _RecipientBinding(registration, registration.generation, grant,
                registration.mcp_adapter, scope, key, server, session, {})
            if not binding.current(registration):
                continue
            spec = registration.mcp_adapter({"tool": tool, "arguments": dict(args),
                "output_contract": VERSION,
                "result": {"structuredContent": json.loads(raw), "isError": False},
                "grant": project({k: grant[k] for k in ("accounts", "sources", "modes", "remote_processing", "allow_live_fetch")})})
            if (not isinstance(spec, Mapping) or set(spec) - {"output_contract"} != {"facts", "completeness", "rows_key", "id_key"}
                    or spec.get("output_contract") not in (None, VERSION)):
                continue
            facts = bounded_facts(spec["facts"])
            rows_key, id_key = spec["rows_key"], spec["id_key"]
            rows = json.loads(raw).get(rows_key)
            if not isinstance(rows, list) or not 2 <= len(rows) <= 8:
                continue
            ids = tuple(row.get(id_key) for row in rows)
            if (any(type(i) is not str for i in ids) or len(set(ids)) != len(ids) or
                    tuple(c["id"] for c in facts.get("candidates", [])) != ids):
                continue
            admitted = freeze({"facts": facts, "rows_key": rows_key, "id_key": id_key,
                               "output_contract": spec.get("output_contract")})
            # A shared opportunity is only meaningful for identical immutable
            # projections. Each recipient must independently admit it under its
            # OWN source/account/mode grant; the first parser grants no authority.
            if projection is not None and admitted != projection:
                continue
            projection = admitted
            recipients.append(replace(binding, projection=admitted))
            break
    return tuple(recipients), projection


def admit_result(server_name, server, tool, args, typed_result, invoked_session, baseline, operation_deadline):
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    from tools.mcp_tool_common import _core, mcp_field
    from tools.mcp_tool_scope import _resolve_server_key, _key_visible_in_scope
    runtime = runtime_for_agent(get_active_subagent_parent())
    if runtime is None:
        return baseline
    decision = None
    try:
        runtime._assert_owner(tool_worker=True)
        scope = runtime.revision.profile
        key = _resolve_server_key(server_name)
        # Actual acquired transport identity, not a forged server/tool string.
        with _core._lock:
            if (_core._servers.get(key) is not server or
                    not _key_visible_in_scope(key, scope) or server.session is None or server.session is not invoked_session):
                return baseline
        if mcp_field(typed_result, "is_error", "isError", False):
            return baseline
        structured = mcp_field(typed_result, "structured_content", "structuredContent")
        if not isinstance(structured, dict):
            return baseline
        raw = json.dumps(structured, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(raw.encode()) > 262144:
            return baseline
        pinned = json.loads(raw)
        recipients, projection = _project_recipients(scope, key, server_name, server,
            invoked_session, tool, args, raw)
        if not recipients or projection is None:
            return baseline
        facts = project(projection["facts"])
        candidates = facts["candidates"]
        rows_key, id_key = projection["rows_key"], projection["id_key"]
        rows = pinned[rows_key]
        ids = tuple(row[id_key] for row in rows)
        target = "mcp:" + uuid.uuid4().hex
        facts["target_id"] = target
        request = OwnerRequestV1("mcp", target, runtime.revision, tuple(candidates),
            runtime.shared_deadline(operation_deadline), required_ids=ids, data_policy=("project_excerpt",),
            event="retrieval_candidates", requires_ack=True, facts=facts,
            completeness=Completeness(complete=False, omitted=True),
            evidence_refs=tuple(c["ref"] for c in candidates))
        decision = runtime.owner_decision(Action.RANK_CANDIDATES, request, mcp_recipients=recipients)
        if not decision.selected:
            return baseline
        from agent.supervision_retrieval_presentation import VERSION, annotate, validate
        conflicts, isolated = validate(decision.metadata, ids)
        presentation = projection.get("output_contract") == VERSION and bool(conflicts or isolated)
        # Unsupported adapters remain order-only, including annotation-only no-op.
        if (len(decision.candidate_ids) != len(ids) or set(decision.candidate_ids) != set(ids)
                or (tuple(decision.candidate_ids) == ids and not presentation)):
            runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, None)
            return baseline
        by_id = dict(zip(ids, rows))
        result = {**pinned, rows_key: [by_id[i] for i in decision.candidate_ids]}
        # Parse only our native renderer's JSON framing, never its display
        # prose as evidence. Reuse it to avoid caching media blocks twice.
        envelope = json.loads(baseline)
        if not isinstance(envelope, dict) or "error" in envelope:
            runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, None)
            return baseline
        result_key = "result" if isinstance(envelope.get("result"), dict) else "structuredContent"
        envelope[result_key] = result
        if presentation:
            locations = {identity: (f"#/{result_key}/{rows_key}/{index}", identity)
                         for index, identity in enumerate(decision.candidate_ids)}
            envelope = annotate(envelope, decision.metadata, locations)
        rendered = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if (len(rendered.encode()) > 262144 or
                json.dumps(mcp_field(typed_result, "structured_content", "structuredContent"),
                           ensure_ascii=False, allow_nan=False, sort_keys=True) != raw):
            runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, None)
            return baseline
        digest = hashlib.sha256(rendered.encode()).hexdigest()
        receipt = runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, digest)
        return rendered if receipt and receipt.status == "applied" else baseline
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        if decision is not None and decision.selected:
            runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, None)
        return baseline
    return baseline
