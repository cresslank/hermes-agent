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


def admit_result(server_name, server, tool, args, typed_result, invoked_session, baseline, operation_deadline):
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    from agent.supervision_facade import registrations_for_scope
    from tools.mcp_tool_common import _core, mcp_field
    from tools.mcp_tool_scope import _resolve_server_key, _key_visible_in_scope
    runtime = runtime_for_agent(get_active_subagent_parent())
    if runtime is None:
        return baseline
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
        for registration in registrations_for_scope(scope):
            if not callable(registration.mcp_adapter) or "rank_candidates" not in registration.grants:
                continue
            for grant in registration.mcp_sources:
                if (grant["server"] != server_name or grant["tool"] != tool or
                        not _route_matches(server, grant) or not grant["remote_processing"]):
                    continue
                session = server.session
                spec = registration.mcp_adapter({"tool": tool, "arguments": dict(args),
                    "result": {"structuredContent": json.loads(raw), "isError": False},
                    "grant": project({k: grant[k] for k in ("accounts", "sources", "modes", "remote_processing", "allow_live_fetch")})})
                if not isinstance(spec, Mapping) or set(spec) != {"facts", "completeness", "rows_key", "id_key"}:
                    continue
                from agent.supervision_owner_protocol import bounded_facts
                facts = bounded_facts(spec["facts"])
                candidates = facts.get("candidates", [])
                rows_key, id_key = spec["rows_key"], spec["id_key"]
                rows = pinned.get(rows_key)
                if not isinstance(rows, list) or not 2 <= len(rows) <= 8:
                    continue
                ids = tuple(row.get(id_key) for row in rows)
                if any(type(i) is not str for i in ids) or len(set(ids)) != len(ids) or tuple(c["id"] for c in candidates) != ids:
                    continue
                target = "mcp:" + uuid.uuid4().hex
                facts["target_id"] = target
                request = OwnerRequestV1("mcp", target, runtime.revision, tuple(candidates),
                    runtime.shared_deadline(operation_deadline), required_ids=ids, data_policy=("project_excerpt",),
                    event="retrieval_candidates", requires_ack=True, facts=facts,
                    completeness=Completeness(complete=False, omitted=True),
                    evidence_refs=tuple(c["ref"] for c in candidates))
                decision = runtime.owner_decision(Action.RANK_CANDIDATES, request)
                if not decision.selected:
                    return baseline
                with _core._lock:
                    current = (_core._servers.get(key) is server and server.session is session and
                               _key_visible_in_scope(key, scope) and _route_matches(server, grant))
                if not current or set(decision.candidate_ids) != set(ids):
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
                envelope["result" if isinstance(envelope.get("result"), dict) else "structuredContent"] = result
                rendered = json.dumps(envelope, ensure_ascii=False)
                digest = hashlib.sha256(rendered.encode()).hexdigest()
                receipt = runtime.acknowledge_owner(target, decision.receipt_id, decision.candidate_ids, digest)
                return rendered if receipt and receipt.status == "applied" else baseline
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return baseline
    return baseline
