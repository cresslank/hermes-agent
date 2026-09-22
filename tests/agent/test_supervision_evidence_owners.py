"""Optional source-owner verticals: real facade, bridge, features and strict HTTP codec.

Set JEV_SUPERVISOR_SOURCE, LCM_SUPERVISION_SOURCE, MUXYARD_SUPERVISION_SOURCE
explicitly to isolated checkouts. The only substituted boundary is remote I/O;
all inputs are synthetic and every SQLite database belongs to tmp_path.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_policy import runtime_for_agent
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from tests.agent.supervision_test_support import Agent


@pytest.fixture
def vertical(tmp_path, monkeypatch):
    paths = [os.environ.get(k) for k in (
        "JEV_SUPERVISOR_SOURCE", "LCM_SUPERVISION_SOURCE", "MUXYARD_SUPERVISION_SOURCE")]
    if not all(paths):
        pytest.skip("explicit isolated Jev/LCM/Muxyard source checkouts required")
    # DNS is a remote I/O boundary too; do not resolve even synthetic URLs.
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    jev, lcm, mux = map(Path, paths)
    monkeypatch.syspath_prepend(str(jev / "src"))
    monkeypatch.syspath_prepend(str(mux / "integrations/hermes/plugins"))
    spec = importlib.util.spec_from_file_location("hermes_lcm", lcm / "__init__.py", submodule_search_locations=[str(lcm)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_lcm", module)
    # Submodule imports, just as LCM's documented test harness; no plugin install.
    from jev_supervisor.config import Config
    from jev_supervisor.host_adapter import NativeHostBridge
    from jev_supervisor.transport import Transport
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.store import MessageStore
    from hermes_lcm.dag import SummaryDAG

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    keys = ("query candidates scope required_ids baseline_ids needs_triage exact_answer_complete "
            "target_id question missing_slot visible_refs recovery_budget already_visible explicit_ref_available "
            "windows oversized structured source_immutable source_ref critical_fields_complete mandatory_ids "
            "omitted_count full_output_ref budget temporal_contract").split()
    policy = {"id": "synthetic-owner", "profile": str(home), "fixture": True,
              "fields": {k: "synthetic" for k in keys}, "sources": {}}
    (home / "config.yaml").write_text(json.dumps({"supervision": {"enabled": True, "plugins": {
        "jev-supervisor": {"grants": ["observe", "rank_candidates", "select_windows", "evaluate_relation", "expand_one_owned_ref"],
                           "data_policy": ["history_excerpt", "public_source", "project_excerpt"], "egress_policy": policy,
                           "mcp_sources": [{"server": "synthetic-sloom", "url": "https://switchloom.invalid/mcp", "tool": "search_context",
                                            "accounts": ["a"], "sources": ["mail"], "modes": ["stored"],
                                            "remote_processing": True, "allow_live_fetch": False}]}}}}))
    manager = PluginManager(scope_key=str(home))
    def facade(name):
        return PluginContext(PluginManifest(name=name), manager).supervision
    config = Config(str(home), enabled=True, policy_id="synthetic-owner", fixture_policy=True,
                    allowed_classes=frozenset({"synthetic"}))
    calls = []
    effects = SimpleNamespace(before_response=None, invalid=False, delay=0, winner_fit=True)

    async def http(request):
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert threading.current_thread().name == "jev-supervision"
        body = json.loads(request.content)
        calls.append(body)
        if effects.delay:
            await asyncio.sleep(effects.delay)
        if effects.before_response:
            effects.before_response()
        facts = json.loads(body["state"])["facts"] if isinstance(body["state"], str) else body["state"]["facts"]
        rows = facts["candidates"]
        chosen = rows[-1]["id"]
        answers = {}
        for key, question in body["questions"].items():
            if question["type"] == "choice":
                options = question["criteria"]
                answers[key] = {"type": "choice", "choice": chosen, "confidence": .99,
                                "probabilities": {k: 1.0 if k == chosen else 0.0 for k in options}}
            else:
                score = .99 if key.endswith(chosen) else .01
                if "fit:" in key and not effects.winner_fit:
                    score = .01 if key.endswith(chosen) else .99
                answers[key] = {"type": "noul", "noul": score}
        if effects.invalid:
            answers["invented"] = {"type": "noul", "noul": .99}
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {}, "answers": answers})

    class Credential:
        def bearer(self, profile, endpoint):
            assert profile == str(home)
            return "synthetic-test-token"
    transport = Transport(config, Credential(), http_transport=httpx.MockTransport(http))
    native = facade("jev-supervisor")
    bridge = NativeHostBridge(native, config, None, transport=transport)
    assert bridge.start()
    agent = Agent()
    from hermes_state import SessionDB
    agent._session_db = SessionDB(home / "state.db")
    agent._session_db.create_session(agent.session_id, "cli")
    runtime = runtime_for_agent(agent, create=True)
    store = MessageStore(str(tmp_path / "owner.db"))
    dag = SummaryDAG(str(tmp_path / "owner.db"))
    engine = SimpleNamespace(_config=LCMConfig(database_path=str(tmp_path / "owner.db"), embeddings_enabled=False),
                             _store=store, _dag=dag, _hermes_home=str(home), current_session_id="current",
                             supervision=facade("hermes-lcm"))
    value = SimpleNamespace(agent=agent, runtime=runtime, engine=engine, mux=facade("web-muxyard"),
                            native=native, bridge=bridge, calls=calls, effects=effects, facade=facade)
    try:
        with bind_subagent_parent(agent):
            yield value
    finally:
        bridge.close()
        agent._session_db.close()
        dag.close()
        store.close()


def seed(v, count=10):
    for i in range(count):
        v.engine._store.append("current", {"role": "user", "content": f"Claim {i}; exact limitation retained."})


def recall(v):
    from hermes_lcm.tools import lcm_recall
    return json.loads(lcm_recall({"query": "Claim", "limit": 10, "detail": "answer_ready", "seen_refs": []}, engine=v.engine))


def test_f14_real_lcm_recall_uses_native_proposal_and_preserves_sources(vertical):
    v = vertical
    seed(v)
    v.engine.supervision = None
    baseline = recall(v)
    v.engine.supervision = v.facade("hermes-lcm")
    actual = recall(v)
    assert len(v.calls) == 1, v.bridge.supervisor.inspect()
    assert actual["hits"][0]["store_id"] != baseline["hits"][0]["store_id"]
    # Existing post-rank diversity limits may select a different subset. Every
    # presented ref still resolves to unchanged owner bytes and role.
    for hit in actual["hits"]:
        row = v.engine._store.get(hit["store_id"])
        assert hit["exact_ref"] == f"lcm:{hit['store_id']}:0-{len(row['content'])}"
        assert "exact limitation retained" in row["content"]
    assert {h["role"] for h in actual["hits"]} == {"user"}
    assert actual.get("coverage") == baseline.get("coverage")
    assert actual.get("computation") == baseline.get("computation")
    # The integrated adapter acknowledges its consumed owner view.
    assert any(r.status == "applied" and r.reason.startswith("owner_consumed:")
               for r in v.runtime.receipts.values())
    assert not v.agent._pending_steer


@pytest.mark.parametrize("mode", ["invalid", "revoked", "stale", "no_policy", "foreign_profile", "expired"])
def test_f14_real_owner_fail_closed(vertical, mode):
    from dataclasses import replace
    v = vertical
    seed(v)
    v.engine.supervision = None
    baseline = recall(v)
    v.engine.supervision = v.facade("hermes-lcm")
    if mode == "invalid":
        v.effects.invalid = True
    elif mode == "revoked":
        v.effects.before_response = v.native.unregister
    elif mode == "stale":
        v.effects.before_response = lambda: setattr(v.runtime, "revision", replace(v.runtime.revision, evidence=1))
    elif mode == "no_policy":
        v.native._registration.egress_policy = {}
    elif mode == "foreign_profile":
        v.engine.supervision._context._manager.scope_key = "foreign-profile"
    elif mode == "expired":
        v.runtime.round_deadline = v.runtime.clock() - 1
        v.runtime.round_deadline_issued_at = v.runtime.round_deadline - .15
    actual = recall(v)
    assert [h["exact_ref"] for h in actual["hits"]] == [h["exact_ref"] for h in baseline["hits"]]
    assert not any(r.status == "applied" for r in v.runtime.receipts.values())


def test_f14_clipped_source_is_not_declared_qualifier_complete(vertical):
    v = vertical
    for i in range(10):
        v.engine._store.append("current", {"role": "user", "content": f"Claim {i}: " + "long " * 500})
    recall(v)
    assert not v.calls


class ExtractClient:
    timeout_ms = extract_timeout_ms = 1000
    traffic_class = "synthetic"
    origin_kind = "benchmark"
    raw = ("Decorative paragraph. " * 5 + "\n\n") * 4 + "Relevant context.\n\nWARNING: partial result; cleanup not run."
    truncated = False
    def extract(self, urls, **kwargs):
        item = {"url": urls[0], "content": self.raw, "provider": "fixture"}
        if self.truncated is not None:
            item["truncated"] = self.truncated
        return {"outcomes": [{"index": 0, "trace_id": "trace", "outcome": "success", "item": item}]}
    async def extract_async(self, urls, **kwargs):
        return self.extract(urls, **kwargs)
    def record_outcome(self, **kwargs):
        pytest.fail("selection is not an upstream outcome")


@pytest.mark.parametrize("async_path", [False, True])
def test_f16_real_muxyard_extract_consumes_exact_windows(vertical, async_path):
    from web.muxyard.provider import MuxyardWebSearchProvider
    v = vertical
    client = ExtractClient()
    provider = MuxyardWebSearchProvider(client, supervision=v.mux)
    args = (["https://example.invalid/synthetic"],)
    result = asyncio.run(provider.extract_async(*args, max_chars=100)) if async_path else provider.extract(*args, max_chars=100)
    assert len(v.calls) == 1, v.bridge.supervisor.inspect()
    item = result[0]
    assert item["raw_content"] == client.raw
    assert "WARNING" in item["content"] and "cleanup not run" in item["content"]
    assert item["metadata"]["full_output_ref"] == "#/raw_content"
    assert item["metadata"]["selected_spans"]
    assert any(r.status == "applied" and r.reason.startswith("owner_consumed:")
               for r in v.runtime.receipts.values())


def test_f16_unknown_truncation_keeps_original_baseline(vertical):
    from web.muxyard.provider import MuxyardWebSearchProvider
    client = ExtractClient()
    client.truncated = None
    provider = MuxyardWebSearchProvider(client, supervision=vertical.mux)
    result = provider.extract(["https://example.invalid/synthetic"], max_chars=100)
    assert not vertical.calls
    assert not result[0]["metadata"].get("selected_spans")


def test_shared_original_deadline_is_not_renewed_between_owners(vertical):
    v = vertical
    seed(v)
    recall(v)
    first = v.runtime.round_deadline
    issued = v.runtime.round_deadline_issued_at
    assert first == pytest.approx(issued + .150)
    from web.muxyard.provider import MuxyardWebSearchProvider
    MuxyardWebSearchProvider(ExtractClient(), supervision=v.mux).extract(["https://example.invalid/synthetic"], max_chars=100)
    assert v.runtime.round_deadline == first
    assert v.runtime.round_deadline_issued_at == issued


def test_f14_native_rank_preserves_every_object_score_and_tail(vertical):
    from hermes_lcm.tools import _lcm_recall_rerank
    v = vertical
    seed(v)
    incoming = []
    for sid in range(1, 11):
        row = v.engine._store.get(sid)
        incoming.append({"hit": {"kind": "message_excerpt", "store_id": sid, "session_id": "current",
                                  "role": row["role"], "snippet": row["content"]}, "_final_score": sid / 100})
    actual, status = _lcm_recall_rerank(None, "Claim", incoming, window=8, deadline=v.runtime.clock() + 1,
                                      config=v.engine._config, supervision=v.engine.supervision, engine=v.engine,
                                      scope={"session_scope": "all"})
    assert status == "applied" and actual[0] is incoming[7]
    assert actual[8:] == incoming[8:]
    assert len(actual) == len(incoming)
    assert all(any(item is original for original in incoming) for item in actual)
    assert len(v.calls) == 1


def test_f14_source_change_after_response_vetoes_native_owner_view(vertical, monkeypatch):
    from hermes_lcm.tools import _lcm_recall_rerank
    v = vertical
    seed(v)
    get = v.engine._store.get
    incoming = [{"hit": {"kind": "message_excerpt", "store_id": sid, "session_id": "current",
                          "role": "user", "snippet": get(sid)["content"]}} for sid in range(1, 11)]
    changed = [False]
    def current(sid):
        row = get(sid)
        return {**row, "content": "Changed source"} if changed[0] else row
    monkeypatch.setattr(v.engine._store, "get", current)
    v.effects.before_response = lambda: changed.__setitem__(0, True)
    actual, status = _lcm_recall_rerank(None, "Claim", incoming, window=8, deadline=v.runtime.clock() + 1,
                                      config=v.engine._config, supervision=v.engine.supervision, engine=v.engine,
                                      scope={"session_scope": "all"})
    assert len(v.calls) == 1
    assert actual is incoming and status == "disabled"
    assert not any(r.status == "applied" for r in v.runtime.receipts.values())
    assert any(r.reason == "owner_postvalidation" for r in v.runtime.receipts.values())


@pytest.mark.parametrize("veto", ["expired", "unloaded", "bad_token", "foreign_owner", "budget"])
def test_consumption_not_selection_receipts(vertical, monkeypatch, veto):
    from web.muxyard.provider import MuxyardWebSearchProvider
    v = vertical
    if veto == "budget":
        result = MuxyardWebSearchProvider(ExtractClient(), supervision=v.mux).extract(
            ["https://example.invalid/synthetic"], max_chars=1)
        assert not result[0]["metadata"].get("selected_spans")
    else:
        acknowledge = v.mux.acknowledge_owner
        def final_check(ack):
            assert not any(r.status == "applied" for r in v.runtime.receipts.values())
            if veto == "expired":
                v.runtime.clock = lambda: v.runtime.round_deadline + 1
            elif veto == "unloaded":
                v.native.unregister()
            elif veto == "bad_token":
                ack = {**ack, "receipt_id": "wrong-token"}
            else:
                return v.facade("hermes-lcm").acknowledge_owner(ack)
            return acknowledge(ack)
        monkeypatch.setattr(v.mux, "acknowledge_owner", final_check)
        result = MuxyardWebSearchProvider(ExtractClient(), supervision=v.mux).extract(
            ["https://example.invalid/synthetic"], max_chars=100)
        assert not result[0]["metadata"].get("selected_spans")
    assert v.calls
    assert not any(r.status == "applied" for r in v.runtime.receipts.values())


def history_slot(v, *, known=True):
    text = "Use the local archive; do not search remotely."
    sid = v.engine._store.append("current", {"role": "user", "content": text})
    return {"slot_id": "archive-policy", "question": "Which archive was selected?", "expansion_budget": 1,
            "explicit_ref_available": False, "visible_refs": [],
            "hits": [{"exact_ref": f"lcm:{sid}:0-{len(text)}", "excerpt": text,
                      **({"current": True, "superseded": False} if known else {})}]}


def test_f15_unknown_currency_does_not_invent_current_source(vertical):
    from hermes_lcm.decision_adapter import recover_missing_history
    v = vertical
    assert recover_missing_history(v.engine, history_slot(v, known=False), deadline=v.runtime.clock() + .15) is None
    assert not v.calls


def test_f15_real_bridge_exact_expansion_codec(vertical):
    from hermes_lcm.decision_adapter import recover_missing_history
    v = vertical
    slot = history_slot(v)
    result = recover_missing_history(v.engine, slot, deadline=v.runtime.clock() + .15)
    assert len(v.calls) == 1, v.bridge.supervisor.inspect()
    assert result and result["content"] == slot["hits"][0]["excerpt"]
    assert result["role"] == "user" and result["session_id"] == "current"
    assert any(r.status == "applied" for r in v.runtime.receipts.values())


@pytest.mark.parametrize("mode", ["positive", "visible", "unknown_visibility", "no_grant", "wrong_fit", "foreign", "supersession_unknown"])
def test_f15_ordinary_request_and_grep_recovery(vertical, mode, monkeypatch):
    from tests.agent.test_tool_call_incremental_persistence import _make_agent
    from tests.agent.test_supervision_views import assemble
    from agent.turn_context import _reset_per_turn_agent_state
    from hermes_lcm.tools import lcm_grep
    from hermes_lcm import tools as history_tools
    expansions = []
    expand = history_tools.lcm_expand
    def read_exact(args, **kwargs):
        expansions.append(dict(args))
        return expand(args, **kwargs)
    monkeypatch.setattr(history_tools, "lcm_expand", read_exact)
    v = vertical
    agent = _make_agent()
    _reset_per_turn_agent_state(agent)
    runtime = runtime_for_agent(agent, create=True)
    text = "Archive policy: use the local archive, not remote search."
    v.engine._store.append("foreign" if mode == "foreign" else "current", {"role": "user", "content": text})
    v.engine._store.append("current", {"role": "assistant", "content": "Archive topic mention, not a decision."})
    if mode != "unknown_visibility":
        assemble(agent, [{"role": "user", "content": text if mode == "visible" else "Recover the archive policy decision."}])
    if mode == "no_grant":
        v.native._registration.grants -= {"expand_one_owned_ref"}
    if mode == "wrong_fit":
        v.effects.winner_fit = False
    with bind_subagent_parent(agent):
        result = json.loads(lcm_grep({"query": "Archive", "missing_decision": "What archive policy was decided?", **({"role": "user"} if mode in {"visible", "foreign"} else {})}, engine=v.engine))
    if mode in {"positive", "supersession_unknown"}:
        assert len(expansions) == 1 and expansions[0]["content_offset"] == 0
        assert result["recovered_history"]["content"] == text
        assert result["recovered_history"]["role"] == "user"
        assert len(v.calls) == 1
        assert "supersession unknown" in result["recovery_scope"]
        assert any(r.reason.startswith("owner_consumed:") for r in runtime.receipts.values())
    else:
        assert "recovered_history" not in result
        assert not expansions
        assert not any(r.status == "applied" for r in runtime.receipts.values())


@pytest.mark.parametrize("damage", ["content", "role", "session_id", "exact_ref", "content_offset", "scope"])
def test_f15_actual_expansion_postvalidation_veto(vertical, monkeypatch, damage):
    from hermes_lcm import tools
    from hermes_lcm.decision_adapter import recover_missing_history
    v = vertical
    slot = history_slot(v)
    original = tools.lcm_expand
    calls = []
    def altered(args, **kwargs):
        calls.append(args)
        result = json.loads(original(args, **kwargs))
        if damage == "scope":
            v.engine.current_session_id = "different-session"
        else:
            result[damage] = 99 if damage == "content_offset" else "changed"
        return json.dumps(result)
    monkeypatch.setattr(tools, "lcm_expand", altered)
    result = recover_missing_history(v.engine, slot, deadline=v.runtime.clock() + .15)
    assert result is None
    assert len(calls) == 1
    assert not any(r.status == "applied" for r in v.runtime.receipts.values())
    assert any(r.reason == "owner_postvalidation" for r in v.runtime.receipts.values())


def test_f15_known_exact_ref_uses_existing_reader_without_judgment(vertical):
    from hermes_lcm.tools import lcm_expand
    v = vertical
    slot = history_slot(v)
    result = json.loads(lcm_expand({"store_id": 1, "content_offset": 0, "max_tokens": 600,
                                   "include_exact_ref": True}, engine=v.engine))
    assert result["exact_ref"] == slot["hits"][0]["exact_ref"]
    assert not v.calls and not v.runtime.receipts


class LinkedExtractClient(ExtractClient):
    def search(self, query, limit, **kwargs):
        return {"outcome": "success", "trace_id": "tr_search", "results": [
            {"rank": i, "representative": {"url": f"https://example.invalid/{i}", "title": "Source", "snippet": "Unknown clipped snippet"}}
            for i in range(1, 11)], "result_ids": [
            {"rank": i, "candidate_id": f"c{i}", "impression_id": f"i{i}"} for i in range(1, 11)]}
    def extract(self, urls, **kwargs):
        return {"outcomes": [{"index": i, "trace_id": f"tr_extract{i}", "outcome": "success",
            "item": {"url": url, "provider": "fixture", "content": f"Complete returned source {i}; limitation preserved.", "truncated": self.truncated}}
            for i, url in enumerate(urls)]}


@pytest.mark.parametrize("truncated", [False, True, None])
@pytest.mark.parametrize("async_path", [False, True])
def test_f14_muxyard_real_search_linked_extracts(vertical, monkeypatch, truncated, async_path):
    from web.muxyard.provider import MuxyardWebSearchProvider
    v = vertical
    monkeypatch.setenv("HERMES_SESSION_ID", "synthetic-session")
    client = LinkedExtractClient()
    client.truncated = truncated
    provider = MuxyardWebSearchProvider(client, supervision=v.mux)
    provider.search("Which complete source states the limitation?", limit=10)
    assert not v.calls  # ordinary snippets still cannot certify qualifiers
    assert provider._triage_queries, provider._origin_sidecar
    urls = ["https://example.invalid/1", "https://example.invalid/2"]
    result = asyncio.run(provider.extract_async(urls)) if async_path else provider.extract(urls)
    assert len(result) == 2
    if truncated is False:
        assert len(v.calls) == 1, (v.bridge.supervisor.inspect(), provider._triage_queries, result, list(v.runtime.opportunities.items()))
        assert result[0]["url"] == urls[1]
        assert result[0]["metadata"]["input_index"] == 1
        assert any(r.reason.startswith("owner_consumed:") for r in v.runtime.receipts.values())
    else:
        assert not v.calls and result[0]["url"] == urls[0]


def switchloom_payload():
    counts = ("provider_error_count", "source_read_error_count", "cache_error_count", "auth_error_count", "rate_limit_count", "timeout_error_count")
    items = [{"chunk_ref_id": f"chunk{i}", "ref_id": f"mail:a:{i}", "source": "mail", "account_id": "a",
              "title": "Source", "text": f"Source fact {i}", "returned_chars": 13, "original_chars": 13, "truncated": False,
              **{k: None for k in ("timestamp", "retrieved_at", "url", "citation", "heading")}} for i in range(2)]
    return {"run_id": "run", "items": items, "completeness": {"complete": True, "results_incomplete": False, **dict.fromkeys(counts, 0)},
            "provider_errors": [], "omissions": {"count": 1, "by_reason": {"candidate_budget": 1}}, "truncated": True,
            "returned_chars": 26, "original_selected_chars": 26}


@pytest.mark.parametrize("mode", ["positive", "stdio", "foreign_account", "route_mismatch", "unowned_transport", "no_structure", "small", "session_replaced", "live", "revoked"])
def test_f14_real_typed_mcp_transport_binding(vertical, monkeypatch, mode):
    from tools import mcp_tool as core
    from tools import mcp_tool_handlers as handlers
    from mcp.types import CallToolResult, TextContent
    v = vertical
    payload = switchloom_payload()
    if mode == "foreign_account":
        payload["items"][0]["account_id"] = "foreign"
    if mode == "small":
        payload["omissions"] = {"count": 0, "by_reason": {}}
        payload["truncated"] = False
    typed = CallToolResult(content=[TextContent(type="text", text="Display prose is not evidence")],
                           structuredContent=None if mode == "no_structure" else payload)
    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            assert name == "search_context"
            if mode == "session_replaced":
                server.session = Session()
            return typed
    server = SimpleNamespace(session=Session(), _rpc_lock=asyncio.Lock(), _pending_call_context=None,
                             _config={"url": "https://wrong.invalid/mcp" if mode == "route_mismatch" else "https://switchloom.invalid/mcp"})
    if mode == "stdio":
        from agent.supervision_mcp import source_grants
        policy = dict(v.native._registration.mcp_sources[0])
        policy.pop("url")
        policy.update(command="/synthetic/sloom", args=["mcp", "--api-url", "http://localhost:9999"])
        for key in ("accounts", "sources", "modes"):
            policy[key] = list(policy[key])
        v.native._registration.mcp_sources = source_grants([policy])
        server._config = {"command": policy["command"], "args": policy["args"]}
    scope = v.runtime.revision.profile
    monkeypatch.setattr(core, "_servers", {"synthetic-sloom": server} if mode != "unowned_transport" else {})
    monkeypatch.setattr(core, "_server_tool_scopes", {"synthetic-sloom": {scope}})
    monkeypatch.setattr(core, "_mcp_registry_scope", lambda: None)
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
    monkeypatch.setattr(handlers, "_acquire_call_server", lambda *a: (server, None))
    monkeypatch.setattr(handlers, "_tool_is_read_only", lambda *a: True)
    monkeypatch.setattr(handlers._loop, "_run_on_mcp_loop", lambda call, **kw: asyncio.run(call()))
    monkeypatch.setattr(handlers, "_record_call_outcome", lambda name, result: result)
    if mode == "revoked":
        v.effects.before_response = v.native.unregister
    result = json.loads(handlers._make_tool_handler("synthetic-sloom", "search_context", 1)(
        {"query": "Which source states the fact?", "mode": "live" if mode == "live" else "stored"}))
    if mode in {"positive", "stdio"}:
        assert len(v.calls) == 1, v.bridge.supervisor.inspect()
        assert result["structuredContent"]["items"][0]["chunk_ref_id"] == "chunk1"
        assert result["structuredContent"]["completeness"] == payload["completeness"]
        assert result["result"] == "Display prose is not evidence"
        assert any(r.reason.startswith("owner_consumed:") for r in v.runtime.receipts.values())
    else:
        assert not any(r.status == "applied" for r in v.runtime.receipts.values())
        if mode != "revoked":
            assert not v.calls
