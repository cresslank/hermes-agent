"""R05/R06: actual extract entrypoints, strict inference HTTP, real SessionDB.

Transport/DNS and backend configuration use synthetic fixtures. No owner
selection, acknowledgement, grant, deadline or database write is substituted.
"""
import asyncio
import hashlib
import json
import sqlite3
import threading

import pytest

from tests.agent.test_supervision_evidence_owners import vertical, LinkedExtractClient


@pytest.fixture
def mux(vertical, monkeypatch):
    from web.muxyard.provider import MuxyardWebSearchProvider
    from tools import web_tools as web
    from tools import website_policy
    v = vertical
    monkeypatch.setenv("HERMES_SESSION_ID", "synthetic-session")
    threads = []

    class Client(LinkedExtractClient):
        def extract(self, urls, **kwargs):
            threads.append(threading.get_ident())
            return super().extract(urls, **kwargs)

    provider = MuxyardWebSearchProvider(Client(), supervision=v.mux)
    provider.search("Which complete source states the limitation?", limit=10)
    monkeypatch.setattr(web, "_get_extract_backend", lambda: "muxyard")
    monkeypatch.setattr(web, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web, "_resolve_extract_provider", lambda name: (provider, None))
    async def safe(url):
        return True
    monkeypatch.setattr(web, "async_is_safe_url", safe)
    monkeypatch.setattr(website_policy, "check_website_access", lambda url: None)
    # Real cache I/O uses tmp_path/HERMES_HOME and remains exercised below.
    return v, provider, threads


def receipt_rows(v):
    with sqlite3.connect(v.agent._session_db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM supervision_receipts")]


def generic(urls, **kwargs):
    from tools.web_tools import web_extract_tool
    return json.loads(asyncio.run(web_extract_tool(urls, **kwargs)))


URLS = ["https://example.invalid/1", "https://example.invalid/2"]


@pytest.mark.parametrize("entrypoint", ["generic", "dedicated"])
def test_actual_entrypoint_consumes_final_canonical_view(mux, monkeypatch, entrypoint):
    from agent import supervision_receipts
    from web.muxyard.provider import muxyard_extract_json
    v, provider, threads = mux
    transitions = []
    original = supervision_receipts.persist
    def persist(conn, *args, **kwargs):
        original(conn, *args, **kwargs)
        transitions.extend(conn.execute("SELECT status,reason FROM supervision_receipts").fetchall())
    monkeypatch.setattr(supervision_receipts, "persist", persist)
    if entrypoint == "generic":
        from tools.web_tools import web_extract_tool
        encoded = asyncio.run(web_extract_tool(URLS))
        result = json.loads(encoded)
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
        effect = result
    else:
        encoded = asyncio.run(muxyard_extract_json({"urls": URLS}, provider=provider))
        result = json.loads(encoded)
        effect = result
    assert len(v.calls) == 1, (result, v.bridge.supervisor.inspect())
    assert [r["url"] for r in result["results"]] == URLS[::-1]
    for index, row in enumerate(result["results"]):
        meta = row["metadata"]
        assert meta["input_index"] == 1 - index
        origin = meta["source_provenance"]
        assert origin["origin_trace_id"] == "tr_search"
        assert origin["origin_candidate_id"] == f"c{2-index}"
        assert origin["origin_impression_id"] == f"i{2-index}"
        record = provider._triage_queries[("synthetic-session", f"c{2-index}", f"i{2-index}")]
        assert origin["search_request_id"] == record["_logical_request_id"]
        assert origin["scope"] == record["scope"]
        assert origin["url"] == row["url"]
    # Read actual committed state, including selected -> accepted/owner_selected.
    assert any(status == "selected" for status, _ in transitions)
    assert ("accepted", "owner_selected") in transitions
    digest = hashlib.sha256(json.dumps(effect, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    # Both native tool renderers consume exact post-shaped UTF-8.
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    receipt, = receipt_rows(v)
    assert receipt["status"] == "consumed"
    assert receipt["reason"] == "owner_consumed:" + digest
    assert json.loads(receipt["evidence_json"]) == ["c1", "c2"]


@pytest.mark.parametrize("entrypoint", ["generic", "dedicated"])
@pytest.mark.parametrize("reuse_trace", [False, True])
def test_same_query_distinct_searches_are_not_one_origin(mux, entrypoint, reuse_trace):
    from web.muxyard.provider import muxyard_extract_json
    v, provider, _ = mux
    original = provider._client.search
    def another(query, limit, **kwargs):
        result = original(query, limit, **kwargs)
        result["trace_id"] = "tr_search" if reuse_trace else "tr_search_second"
        for row, identity in zip(result["results"], result["result_ids"]):
            row["representative"]["url"] += "-second"
            identity["candidate_id"] += "-second"
            identity["impression_id"] += "-second"
        return result
    provider._client.search = another
    provider.search("Which complete source states the limitation?", limit=10)
    urls = [URLS[0], URLS[1]+"-second"]
    result = generic(urls) if entrypoint == "generic" else json.loads(asyncio.run(
        muxyard_extract_json({"urls": urls}, provider=provider)))
    assert [r["url"] for r in result["results"]] == urls
    assert not v.calls and not receipt_rows(v)
    expected_traces = {"tr_search"} if reuse_trace else {"tr_search", "tr_search_second"}
    assert {r["metadata"]["source_provenance"]["origin_trace_id"] for r in result["results"]} == expected_traces
    assert len({r["metadata"]["source_provenance"]["search_request_id"] for r in result["results"]}) == 2


@pytest.mark.parametrize("mutation", ["origin", "query", "scope"])
def test_origin_invalidation_after_selection_vetoes_consumption(mux, mutation):
    v, provider, _ = mux
    def invalidate():
        with provider._origin_lock:
            if mutation == "origin":
                provider._origin_sidecar[("synthetic-session", URLS[0])] = None
            else:
                record = provider._triage_queries[("synthetic-session", "c1", "i1")]
                if mutation == "query":
                    record["query"] = "Changed query"
                else:
                    record["scope"]["profile_name"] = "foreign"
    v.effects.before_response = invalidate
    result = generic(URLS)
    assert len(v.calls) == 1
    assert [r["url"] for r in result["results"]] == URLS
    assert not any(r["status"] == "consumed" for r in receipt_rows(v))


@pytest.mark.parametrize("mode", ["cache", "partial_cache", "blocked", "duplicate", "binary", "base64", "truncated", "drop", "metadata", "duplicate_index", "missing"])
def test_downstream_nonconsumption_never_acknowledges(mux, monkeypatch, mode):
    from tools import web_tools as web, web_result_cache as cache
    v, provider, _ = mux
    urls: list = list(URLS)
    if mode in {"cache", "partial_cache"}:
        for url in (urls if mode == "cache" else urls[:1]):
            cache.extract_cache_put(url, "Cached source without search provenance", "Source", provider="muxyard")
    elif mode == "blocked":
        urls.append(None)
    elif mode == "duplicate":
        urls = [urls[0], urls[0]]
    elif mode in {"binary", "base64", "truncated", "duplicate_index", "missing"}:
        original = provider._client.extract
        def altered(urls, **kwargs):
            data = original(urls, **kwargs)
            if mode == "duplicate_index":
                data["outcomes"][1]["index"] = 0
                return data
            if mode == "missing":
                data["outcomes"].pop()
                return data
            data["outcomes"][0]["item"]["content"] = {
                "binary": "SQLite format 3\x00" + "x" * 100,
                "base64": "![image](data:image/png;base64,QUFBQQ==)",
                "truncated": "long source " * 500,
            }[mode]
            return data
        provider._client.extract = altered
    else:
        original = web._trim_results
        def trimmed(rows):
            result = original(rows)
            if mode == "drop":
                return []
            for row in result:
                row.pop("metadata", None)
            return result
        monkeypatch.setattr(web, "_trim_results", trimmed)
    result = generic(urls, char_limit=2000)
    assert not v.calls and not receipt_rows(v), result
    if mode == "truncated":
        assert result["results"][0]["metadata"]["truncated"] is True
    if mode == "binary":
        assert result["results"][0]["error"]


def test_arbitrary_inherited_context_does_not_authorize_worker(mux):
    v, provider, _ = mux
    # Only the final owner callback is eligible. Direct worker calls still fail
    # native _assert_owner; the production change never widens its thread set.
    result = asyncio.run(asyncio.to_thread(provider.extract, URLS))
    assert [r["url"] for r in result] == URLS
    assert not v.calls and not receipt_rows(v)


@pytest.mark.parametrize("entrypoint", ["generic", "dedicated"])
def test_strict_source_http_and_native_inference_http(mux, monkeypatch, entrypoint):
    import httpx
    from types import SimpleNamespace
    from web.muxyard import provider as module
    from tools import web_tools as web
    v, _, _ = mux
    source_calls = []
    source_fixture = LinkedExtractClient()
    async def source_http(request):
        assert request.url.host == "muxyard.invalid"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        source_calls.append((str(request.url), payload))
        if request.url.path == "/v1/search":
            return httpx.Response(200, json=source_fixture.search(payload["query"], payload["top_k"]))
        assert request.url.path == "/v1/extract/batch"
        assert payload["urls"] == URLS
        assert [origin["origin_candidate_id"] for origin in payload["origins"]] == ["c1", "c2"]
        return httpx.Response(200, json=source_fixture.extract(payload["urls"]))
    # Patch only the source transport factory; Jev keeps its separate strict
    # HTTP transport established by vertical. Both native codecs stay real.
    monkeypatch.setattr(module, "httpx", SimpleNamespace(
        AsyncClient=lambda: httpx.AsyncClient(transport=httpx.MockTransport(source_http)),
        HTTPError=httpx.HTTPError))
    client = module.MuxyardClient(transport="http", base_url="https://muxyard.invalid")
    provider = module.MuxyardWebSearchProvider(client, supervision=v.mux)
    search = provider.search("Which complete source states the limitation?", limit=10)
    assert search["success"], (search, source_calls)
    monkeypatch.setattr(web, "_resolve_extract_provider", lambda name: (provider, None))
    result = generic(URLS) if entrypoint == "generic" else json.loads(asyncio.run(
        module.muxyard_extract_json({"urls": URLS}, provider=provider)))
    assert len(source_calls) == 2 and len(v.calls) == 1, result
    assert [row["url"] for row in result["results"]] == URLS[::-1]
    assert receipt_rows(v)[0]["status"] == "consumed"


@pytest.mark.parametrize("entrypoint", ["generic", "dedicated"])
def test_window_lineage_digest_and_generic_missing_full_source_abstention(mux, entrypoint):
    from tests.agent.test_supervision_evidence_owners import ExtractClient
    from web.muxyard.provider import muxyard_extract_json
    v, provider, _ = mux
    client = ExtractClient()
    client.extract_max_chars = 100
    provider._client = client
    result = generic(URLS[:1]) if entrypoint == "generic" else json.loads(asyncio.run(
        muxyard_extract_json({"urls": URLS[:1], "max_content_chars": 100}, provider=provider)))
    row = result["results"][0]
    assert row["metadata"]["source_provenance"]["origin_candidate_id"] == "c1"
    if entrypoint == "generic":
        assert not v.calls and not receipt_rows(v)
        assert "full_output_ref" not in row["metadata"]
    else:
        assert len(v.calls) == 1
        assert row["raw_content"] == client.raw
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        assert receipt_rows(v)[0]["reason"] == "owner_consumed:" + digest
