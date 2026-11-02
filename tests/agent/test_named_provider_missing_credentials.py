"""Real-chain regressions for unresolved named-provider credentials (#117743)."""

import json
import socket
import sys
from types import SimpleNamespace

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.auth import AuthError
from hermes_cli.config import atomic_config_replace


def _profile(tmp_path, monkeypatch, request, entry, style, secrets=None, pools=None, name="relay"):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"plugins": {"enabled": []}, "model": {"provider": "custom:relay", "default": "probe-model"}}
    config.update({"providers": {name: entry}} if style == "providers" else
                  {"custom_providers": [{"name": name, **entry}]})
    atomic_config_replace(home / "config.yaml", config)
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": pools or {}}))
    token = set_secret_scope(secrets or {}, profile_home=str(home))
    request.addfinalizer(lambda: reset_secret_scope(token))

    def no_network(*args, **kwargs):
        raise AssertionError("credential resolution must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    return home


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

    _profile(tmp_path, monkeypatch, request,
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
