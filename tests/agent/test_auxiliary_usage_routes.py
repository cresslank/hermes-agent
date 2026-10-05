"""Successful auxiliary attempts persist the serving route, not the failed/ambient route (#133397)."""

import asyncio
import io
import json
import urllib.request
from typing import Any

import httpx
import pytest

import agent.auxiliary_client as aux
from agent.aux_accounting import reset_accounting_context, set_accounting_context
from hermes_cli.auth import write_credential_pool
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


@pytest.fixture
def route_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "builtin-route-test-key")
    # Rejection memory outlives client shutdown; keep each recovery lane independent.
    aux._TEMPERATURE_REJECTED_ROUTES.clear()
    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    try:
        yield home
    finally:
        aux.shutdown_cached_clients()
        aux._reset_aux_unhealthy_cache()
        reset_hermes_home_override(token)


def _anthropic_stream(model, speed=None):
    events = [
        {"type": "message_start", "message": {
            "id": "msg-summary", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 0,
                      "cache_read_input_tokens": 40, "cache_creation_input_tokens": 20, **({"speed": speed} if speed else {})},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Summary."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


@pytest.fixture
def usage_wire(monkeypatch):
    """Only the external SDK/OAuth HTTP edges are fake; routing and accounting stay real."""
    seen, refreshes = [], []
    scenario = {}

    def respond(request):
        body = json.loads(request.content)
        seen.append((request.url.host, request.url.path, body, dict(request.headers)))
        model = body["model"]
        attempt = sum(item[2]["model"] == model for item in seen)
        recovery = scenario["recovery"]
        if model == "unavailable-model":
            return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
        if recovery == "transient" and attempt == 1:
            raise httpx.ConnectError("Connection reset by peer", request=request)
        if recovery == "parameter" and "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: temperature"}})
        if recovery in {"pool", "auth"} and attempt == 1:
            return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "Unauthorized"}})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_stream(model, scenario.get("speed")))
        assert request.url.path.endswith("/chat/completions"), request.url
        return httpx.Response(200, json={
            "id": "summary", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Summary."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        })

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: transport.handle_request(request))

    async def async_send(self, request):
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_send)

    def token_response(request, **kwargs):
        assert request.full_url == "https://platform.claude.com/v1/oauth/token"
        refreshes.append(request.data)
        return io.BytesIO(json.dumps({
            "access_token": "cc-refreshed-route-test", "refresh_token": "refresh-next", "expires_in": 3600,
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", token_response)
    return scenario, seen, refreshes


def _configure(home, *, mode, recovery, fallback, builtin=False, key_env=False, model="summary-model"):
    # A named entry sharing a built-in's slug still owns its destination after auth refresh.
    name = "anthropic" if recovery == "auth" else "summary-api"
    provider = f"custom:{name}"
    host = "api.anthropic.com" if recovery == "auth" else f"{home.parent.name.replace('_', '-')}.summary.test"
    base = f"https://{host}/v1"
    entry = {"base_url": base, "transport": {"chat_completions": "openai_chat", "anthropic_messages": "anthropic_messages"}[mode]}
    if recovery not in {"pool", "auth"}:
        entry["key_env" if key_env else "api_key"] = "ROUTE_TEST_API_KEY" if key_env else "route-test-key"
    if builtin:
        name, provider, base = "anthropic", "anthropic", "https://api.anthropic.com"
    task = {"provider": provider, "model": model}
    if not key_env and not builtin:
        task["api_mode"] = mode
    if fallback:
        task = {
            "provider": "custom:primary", "model": "unavailable-model",
            "fallback_chain": [dict(task)],
        }
    main = {"provider": "custom:primary", "default": "main-model"}
    if recovery == "auto_parameter":
        main = {"provider": provider, "default": "summary-model"}
        task = {"provider": "auto", "api_mode": mode}
    (home / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": []},
        "model": main,
        "providers": {**({} if builtin else {name: entry}), "primary": {
            "base_url": "https://primary.test/v1", "api_key": "primary-test-key",
        }},
        "auxiliary": {"transient_retries": 1, "compression": task},
    }), encoding="utf-8")
    if recovery in {"pool", "auth"}:
        rows = [{"id": "first", "source": "manual", "auth_type": "api_key", "priority": 0,
                 "access_token": "route-first", "base_url": base}]
        if recovery == "auth":
            rows[0].update(auth_type="oauth", access_token="cc-stale-route-test", refresh_token="refresh-first",
                           expires_at_ms=4_000_000_000_000)
        else:
            rows.append({"id": "second", "source": "manual", "auth_type": "api_key", "priority": 1,
                         "access_token": "route-second", "base_url": base})
        write_credential_pool(name, rows)
    return provider, base


def _call_and_read_usage(home, *, async_mode):
    db = SessionDB(home / "state.db")
    db.create_session("summary-session", source="cli", model="main-model")
    db.update_session_billing_route("summary-session", provider="ambient-main", base_url="https://main.test/v1")
    token = set_accounting_context(db, "summary-session")
    try:
        kwargs: dict[str, Any] = dict(task="compression", messages=[{"role": "user", "content": "Summarize."}],
                      temperature=0.25, max_tokens=256, timeout=5)
        response = asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)
        assert response.choices[0].message.content == "Summary."
    finally:
        reset_accounting_context(token)
        db.close()
    # A fresh connection proves this is durable state, not an accounting test double.
    reopened = SessionDB(home / "state.db")
    try:
        rows = [dict(row) for row in reopened._conn.execute(
            "SELECT * FROM session_model_usage WHERE session_id = ?", ("summary-session",))]
        main = reopened.get_session("summary-session")
        assert main["billing_provider"] == "ambient-main"
        assert main["input_tokens"] == 0
        return rows
    finally:
        reopened.close()


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode,builtin", [("chat_completions", False), ("anthropic_messages", False),
                                        ("anthropic_messages", True)], ids=["named-chat", "named-anthropic", "builtin-anthropic"])
@pytest.mark.parametrize("recovery", ["primary", "transient", "parameter", "auto_parameter", "pool", "auth"])
def test_auxiliary_success_persists_its_route(route_home, usage_wire, async_mode, mode, builtin, recovery):
    scenario, seen, refreshes = usage_wire
    scenario["recovery"] = "parameter" if recovery == "auto_parameter" else recovery
    provider, base = _configure(route_home, mode=mode, recovery=recovery, fallback=False, builtin=builtin)
    rows = _call_and_read_usage(route_home, async_mode=async_mode)
    expected_attempts = 1 if recovery == "primary" else 2
    assert len(seen) == expected_attempts
    assert len(refreshes) == (1 if recovery == "auth" else 0)
    assert {host for host, *_ in seen} == {httpx.URL(base).host}
    assert len(rows) == 1
    row = rows[0]
    assert (row["billing_provider"], row["billing_base_url"].rstrip("/")) == (provider, base)
    assert (row["task"], row["model"], row["api_call_count"]) == ("compression", "summary-model", 1)
    assert (row["input_tokens"], row["output_tokens"]) == (100, 10)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == ((40, 20) if mode == "anthropic_messages" else (0, 0))
    assert row["billing_mode"] not in {"chat_completions", "anthropic_messages"}
    assert seen[-1][1].endswith("/messages" if mode == "anthropic_messages" else "/chat/completions")
    if recovery in {"pool", "auth"}:
        expected_key = "route-second" if recovery == "pool" else "cc-refreshed-route-test"
        assert expected_key in (seen[-1][3].get("authorization", "") + seen[-1][3].get("x-api-key", ""))


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode,builtin", [("chat_completions", False), ("anthropic_messages", False),
                                        ("anthropic_messages", True)], ids=["named-chat", "named-anthropic", "builtin-anthropic"])
@pytest.mark.parametrize("recovery", ["primary", "parameter", "auth"])
def test_auxiliary_fallback_persists_destination_route(route_home, usage_wire, async_mode, mode, builtin, recovery):
    scenario, seen, refreshes = usage_wire
    scenario["recovery"] = "parameter" if recovery == "auto_parameter" else recovery
    provider, base = _configure(route_home, mode=mode, recovery=recovery, fallback=True, builtin=builtin)
    rows = _call_and_read_usage(route_home, async_mode=async_mode)
    assert seen[0][0] == "primary.test"
    assert [host for host, *_ in seen[1:]] == [httpx.URL(base).host] * (1 if recovery == "primary" else 2)
    assert len(refreshes) == (1 if recovery == "auth" else 0)
    assert len(rows) == 1
    row = rows[0]
    assert (row["billing_provider"], row["billing_base_url"].rstrip("/")) == (provider, base)
    assert (row["task"], row["model"], row["api_call_count"]) == ("compression", "summary-model", 1)
    assert (row["input_tokens"], row["output_tokens"]) == (100, 10)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == ((40, 20) if mode == "anthropic_messages" else (0, 0))
    assert row["billing_mode"] not in {"chat_completions", "anthropic_messages"}
    assert seen[-1][1].endswith("/messages" if mode == "anthropic_messages" else "/chat/completions")
    if recovery in {"pool", "auth"}:
        expected_key = "route-second" if recovery == "pool" else "cc-refreshed-route-test"
        assert expected_key in (seen[-1][3].get("authorization", "") + seen[-1][3].get("x-api-key", ""))


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["chat_completions", "anthropic_messages"])
def test_auxiliary_key_env_transport_config(route_home, usage_wire, monkeypatch, async_mode, mode):
    scenario, seen, _ = usage_wire
    scenario["recovery"] = "parameter"
    monkeypatch.setenv("ROUTE_TEST_API_KEY", "route-env-test-key")
    provider, base = _configure(route_home, mode=mode, recovery="parameter", fallback=True, key_env=True)
    rows = _call_and_read_usage(route_home, async_mode=async_mode)
    assert len(rows) == 1
    row = rows[0]
    assert (row["billing_provider"], row["billing_base_url"].rstrip("/")) == (provider, base)
    assert (row["input_tokens"], row["output_tokens"]) == (100, 10)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == ((40, 20) if mode == "anthropic_messages" else (0, 0))
    assert seen[-1][1].endswith("/messages" if mode == "anthropic_messages" else "/chat/completions")
    assert "route-env-test-key" in (seen[-1][3].get("authorization", "") + seen[-1][3].get("x-api-key", ""))


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_auxiliary_anthropic_preserves_served_speed_price(route_home, usage_wire, async_mode):
    scenario, seen, _ = usage_wire
    scenario.update(recovery="primary", speed="fast")
    _configure(route_home, mode="anthropic_messages", recovery="primary", fallback=False,
               builtin=True, model="claude-opus-5-5")
    rows = _call_and_read_usage(route_home, async_mode=async_mode)
    assert len(rows) == 1
    row = rows[0]
    assert row["billing_provider"] == "anthropic"
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (100, 10, 40, 20)
    # Served fast rates, not standard rates: 100*8 + 10*40 + 40*.4 + 20*10 per million.
    assert row["estimated_cost_usd"] == pytest.approx(0.001416)
    assert seen[-1][1] == "/v1/messages"
