"""Real-chain regressions for unresolved named-provider credentials (#117743)."""

import json
import socket
import sys
from types import SimpleNamespace

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.auth import AuthError
from hermes_cli.config import atomic_config_replace


def _profile(tmp_path, monkeypatch, request, entry, style, secrets=None, pools=None, name="relay", model_cfg=None):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"plugins": {"enabled": []}, "model": {
        "provider": "custom:relay", "default": "probe-model", **(model_cfg or {}),
    }}
    config.update({"providers": {name: entry}} if style == "providers" else
                  {"custom_providers": [{"name": name, **entry}]})
    atomic_config_replace(home / "config.yaml", config)
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": pools or {}}))
    token = set_secret_scope(secrets or {}, profile_home=str(home))
    request.addfinalizer(lambda: reset_secret_scope(token))

    connections = []

    def no_network(*args, **kwargs):
        connections.append(args)
        raise AssertionError("credential resolution must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    return connections


def _pool_row(key, source="manual"):
    return {"id": "probe", "label": "probe", "auth_type": "api_key", "priority": 0,
            "source": source, "access_token": key}


def _resolve_both(provider="custom:relay", **overrides):
    from agent.auxiliary_client import resolve_provider_client
    from hermes_cli.runtime_provider import resolve_runtime_provider

    def auxiliary_key():
        client, _model = resolve_provider_client(provider, "probe-model", **overrides)
        assert client is not None
        return getattr(client, "_api_key_provider", None) or client.api_key

    outcomes = {}
    for name, resolve in {
        "main": lambda: resolve_runtime_provider(requested=provider, **overrides)["api_key"],
        "auxiliary": auxiliary_key,
    }.items():
        try:
            outcomes[name] = resolve()
        except AuthError as exc:
            outcomes[name] = exc
    return outcomes


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("credential", [{"key_env": "RELAY_API_KEY"}, {"api_key_env": "RELAY_API_KEY"},
                                         {"api_key": "your_api_key_here"}, {"api_key": "${RELAY_API_KEY}"}])
@pytest.mark.parametrize("name,base,mode", [("relay", "https://api.anthropic.com", "anthropic_messages"),
                                           ("relay", "http://127.0.0.1:8123/v1", "chat_completions"),
                                           ("deepseek", "https://api.deepseek.com/v1", "chat_completions"),
                                           ("ollama", "http://127.0.0.1:8123/v1", "chat_completions")])
@pytest.mark.parametrize("override_base", [False, True])
def test_declared_missing_credentials_fail_locally(tmp_path, monkeypatch, request, style, credential, name, base, mode, override_base):
    from agent.chat_completion_helpers import _fallback_entry_key, _should_skip_fallback_candidate

    connections = _profile(tmp_path, monkeypatch, request,
             {"base_url": base, "api_mode": mode, **credential}, style,
             secrets={"OPENAI_API_KEY": "unrelated-openai-key", "ANTHROPIC_API_KEY": "unrelated-anthropic-key",
                      "DEEPSEEK_API_KEY": "unrelated-deepseek-key"},
             pools={"openrouter": [_pool_row("unrelated-pool-key")]}, name=name)
    overrides = {"explicit_base_url": base, "explicit_api_key": "no-key-required"} if override_base else {}
    provider = name if name == "ollama" else f"custom:{name}"
    outcomes = _resolve_both(provider, **overrides)
    agent = SimpleNamespace(provider="custom:primary", model="primary-model", base_url="https://primary.example/v1")
    fallback = {"provider": provider, "model": "probe-model", "base_url": base}
    unavailable = set()
    skipped = _should_skip_fallback_candidate(agent, fallback, _fallback_entry_key(fallback),
                                              provider, "probe-model", unavailable)
    assert all(isinstance(outcome, AuthError) for outcome in outcomes.values()), outcomes
    for error in outcomes.values():
        assert error.code == "missing_api_key"
        assert error.provider.removeprefix("custom:") in ({name, "custom"} if name == "ollama" else {name})
        assert name in str(error)
        assert ("RELAY_API_KEY" if "api_key" not in credential else "api_key") in str(error)
        assert "unrelated" not in str(error)
    assert skipped
    assert _fallback_entry_key(fallback) in unavailable
    assert not connections


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("source", ["no-declaration", "environment", "inline", "pool", "explicit", "command", "broken-command",
                                   "pool-with-env", "pool-with-command"])
@pytest.mark.parametrize("base", ["http://127.0.0.1:8123/v1", "https://relay.example/v1"])
def test_own_credentials_and_keyless_entries_stay_usable(tmp_path, monkeypatch, request, style, source, base):
    from agent.chat_completion_helpers import _fallback_entry_key, _should_skip_fallback_candidate
    from agent.command_token_source import CommandTokenError

    key = "synthetic-provider-key"
    entry = {"base_url": base, "key_env": "RELAY_API_KEY"}
    modifications = {
        "no-declaration": {}, "environment": {}, "inline": {"api_key": key}, "pool": {}, "explicit": {},
        "command": {"key_cmd": f'"{sys.executable}" -c "print({key!r})"'},
        "broken-command": {"key_cmd": f'"{sys.executable}" -c "raise SystemExit(7)"'},
        "pool-with-env": {}, "pool-with-command": {"key_cmd": f'"{sys.executable}" -c "print(123456789)"'},
    }
    if source == "no-declaration":
        entry.pop("key_env")
    entry.update(modifications[source])
    pool_key = "relay" if style == "providers" else "custom:relay"
    pools = {pool_key: [_pool_row(key)]} if source in {"pool", "pool-with-env", "pool-with-command"} else {}
    _profile(tmp_path, monkeypatch, request, entry, style,
             secrets={"RELAY_API_KEY": "separate-env-key" if source == "pool-with-env" else key}
             if source in {"environment", "pool-with-env"} else {}, pools=pools)
    outcomes = _resolve_both(**({"explicit_api_key": key} if source == "explicit" else {}))
    if source == "broken-command":
        for token_source in outcomes.values():
            assert callable(token_source)
            with pytest.raises(CommandTokenError, match="exited 7"):
                token_source()
    else:
        expected = "no-key-required" if source == "no-declaration" else key
        assert {name: value() if callable(value) else value for name, value in outcomes.items()} == {
            "main": expected, "auxiliary": expected,
        }
    agent = SimpleNamespace(provider="custom:primary", model="primary-model", base_url="https://primary.example/v1")
    fallback = {"provider": "custom:relay", "model": "probe-model", "base_url": base}
    # A per-call override does not configure the separate fallback-chain entry.
    assert _should_skip_fallback_candidate(agent, fallback, _fallback_entry_key(fallback),
                                           "custom:relay", "probe-model", set()) == (source == "explicit")


def _fallback_agent(request, fallback):
    from run_agent import AIAgent

    agent = AIAgent(
        provider="custom:primary", model="gpt-4o-mini", api_mode="chat_completions",
        base_url="https://primary.example/v1", api_key="synthetic-primary-key",
        fallback_model=fallback, enabled_toolsets=[], quiet_mode=True,
        skip_context_files=True, skip_memory=True, save_trajectories=False,
    )
    request.addfinalizer(agent.close)
    return agent


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("field", ["api_key", "key_env", "api_key_env"])
@pytest.mark.parametrize("usable", [True, False])
@pytest.mark.parametrize("base", ["http://127.0.0.1:8123/v1", "https://relay.example/v1"])
def test_fallback_entry_credentials_match_real_activation(tmp_path, monkeypatch, request, style, field, usable, base):
    from agent.chat_completion_helpers import _fallback_entry_key
    from hermes_cli.fallback_config import resolve_entry_api_key

    key = "synthetic-fallback-key" if usable else "your_api_key_here"
    fallback = {"provider": "custom:relay", "model": "gpt-4o-mini", "base_url": base,
                field: key if field == "api_key" else "FALLBACK_RELAY_KEY"}
    connections = _profile(tmp_path, monkeypatch, request, {"base_url": base, "key_env": "RELAY_API_KEY"}, style,
             secrets={"FALLBACK_RELAY_KEY": key})
    outcomes = _resolve_both(explicit_api_key=resolve_entry_api_key(fallback))
    assert not connections
    agent = _fallback_agent(request, fallback)
    connections.clear()  # Startup metadata probes are separate from fallback admission.
    activated = agent._try_activate_fallback()
    if usable:
        assert outcomes == {"main": key, "auxiliary": key}
        assert activated, agent._unavailable_fallback_keys
        assert (agent.provider, agent.model, agent.api_key) == ("custom:relay", "gpt-4o-mini", key)
        assert str(agent.client.base_url).rstrip("/") == base
        assert not agent._unavailable_fallback_keys
    else:
        assert all(isinstance(value, AuthError) and value.code == "missing_api_key" for value in outcomes.values())
        assert not activated
        assert agent.provider == "custom:primary"
        assert _fallback_entry_key(fallback) in agent._unavailable_fallback_keys
        assert not connections


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("credential", [{"key_env": "RELAY_UNUSED_KEY"}, {"api_key_env": "RELAY_UNUSED_KEY"}])
@pytest.mark.parametrize("mode", ["bedrock_converse", "codex_app_server", "codex-opt-in",
                                  "chat_completions", "codex_responses", "anthropic_messages"])
def test_separate_auth_credentials_match_real_activation(tmp_path, monkeypatch, request, style, credential, mode):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    base = "https://relay.example/v1"
    entry_mode = "chat_completions" if mode == "codex-opt-in" else mode
    connections = _profile(tmp_path, monkeypatch, request, {"base_url": base, "api_mode": entry_mode, **credential}, style,
             model_cfg={"openai_runtime": "codex_app_server" if mode == "codex-opt-in" else "auto"})
    outcomes = _resolve_both()
    assert not connections
    agent = _fallback_agent(request, {"provider": "custom:relay", "model": "gpt-4o-mini"})
    connections.clear()
    activated = agent._try_activate_fallback()
    if mode in {"bedrock_converse", "codex_app_server", "codex-opt-in"}:
        assert outcomes == {"main": "no-key-required", "auxiliary": "no-key-required"}
        assert resolve_runtime_provider(requested="custom:relay")["api_mode"] == (
            "codex_app_server" if mode == "codex-opt-in" else mode)
        assert activated, agent._unavailable_fallback_keys
        assert agent.provider == "custom:relay"
        assert agent.api_key == "no-key-required"
        assert not agent._unavailable_fallback_keys
    else:
        assert all(isinstance(value, AuthError) and value.code == "missing_api_key" for value in outcomes.values())
        assert not activated
        assert agent.provider == "custom:primary"
        assert agent._unavailable_fallback_keys
        assert not connections


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("name", ["ollama", "vllm", "llamacpp", "llama.cpp", "llama-cpp"])
@pytest.mark.parametrize("prefix", ["", "custom:"])
@pytest.mark.parametrize("mode", ["codex-opt-in", "bedrock_converse", "chat_completions"])
def test_alias_named_credentials_match_real_activation(tmp_path, monkeypatch, request, style, name, prefix, mode):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    provider = prefix + name
    base = "https://relay.example/v1"
    entry_mode = "chat_completions" if mode == "codex-opt-in" else mode
    connections = _profile(tmp_path, monkeypatch, request,
             {"base_url": base, "api_mode": entry_mode, "key_env": "RELAY_UNUSED_KEY"}, style, name=name,
             model_cfg={"openai_runtime": "codex_app_server" if mode == "codex-opt-in" else "auto"})
    outcomes = _resolve_both(provider)
    assert not connections
    agent = _fallback_agent(request, {"provider": provider, "model": "gpt-4o-mini"})
    connections.clear()  # Startup metadata probes do not belong to credential admission.
    activated = agent._try_activate_fallback()
    if mode == "chat_completions":
        assert all(isinstance(value, AuthError) and value.code == "missing_api_key" for value in outcomes.values())
        assert {value.provider for value in outcomes.values()} == {provider}
        assert not activated
        assert agent.provider == "custom:primary"
        assert agent._unavailable_fallback_keys
        assert not connections
    else:
        assert activated, (outcomes, agent._unavailable_fallback_keys)
        assert outcomes == {"main": "no-key-required", "auxiliary": "no-key-required"}
        assert resolve_runtime_provider(requested=provider)["api_mode"] == (
            "codex_app_server" if mode == "codex-opt-in" else mode)
        assert agent.provider == provider
        assert agent.api_key == "no-key-required"
        assert not agent._unavailable_fallback_keys


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("name", ["ollama", "vllm", "llamacpp", "llama.cpp", "llama-cpp"])
def test_main_auxiliary_alias_keeps_configured_identity(tmp_path, monkeypatch, request, style, name):
    from agent.auxiliary_client import resolve_provider_client

    connections = _profile(tmp_path, monkeypatch, request,
             {"base_url": "https://relay.example/v1", "key_env": "RELAY_UNUSED_KEY"}, style, name=name,
             model_cfg={"provider": f"custom:{name}", "openai_runtime": "codex_app_server"})
    client, _model = resolve_provider_client("main", "probe-model")
    assert client is not None
    assert client.api_key == "no-key-required"
    assert not connections
