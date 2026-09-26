"""Pre-compaction observations must not inherit context or launder row provenance."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
from agent.conversation_compression import _fold_todo_snapshot
from agent.turn_context import compose_multimodal_context_part, compose_user_api_content
from tests.agent.test_turn_context import _FakeAgent, _build
from tools.todo_tool import TODO_INJECTION_HEADER, TodoStore


@pytest.fixture
def host(monkeypatch, tmp_path):
    # No runtime discovery, auxiliary requests, real DBs, or provider connections.
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None)
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("unexpected network"))
    agent = _FakeAgent()
    agent.session_id = tmp_path.name
    agent._skip_mcp_refresh = True
    agent._parent_session_id = "parent-session"
    agent._user_id = "sender"
    agent._todo_store = TodoStore()
    agent._todo_store.write([{"id": "pending", "content": "Check the answer", "status": "pending"}])
    return agent


def _compact_with_todo(host, monkeypatch, mode, events):
    host.compression_enabled = mode != "none"
    host.context_compressor = SimpleNamespace(
        protect_first_n=0, protect_last_n=0, threshold_tokens=1000,
        context_length=10000, should_compress=lambda tokens: tokens >= 1000,
    )
    monkeypatch.setattr("agent.turn_context._preflight_request_tokens",
                        lambda agent, messages, system: 2000 if len(messages) > 3 else 100)
    monkeypatch.setattr("agent.conversation_loop._maybe_grow_local_window", lambda *a: None)

    def compress(messages, system_message, **kwargs):
        events.append("compact")
        assert kwargs["task_id"] == host._current_task_id
        current = messages[-1]
        # Mutate a nested value before replacing history: shallow copies are insufficient.
        current["display_metadata"]["nested"].append("compacted")
        result = [
            {"role": "user", "content": "Summary", COMPRESSED_SUMMARY_METADATA_KEY: True},
            {"role": "assistant", "content": "Continue"},
        ]
        if mode == "missing_with_history":
            result[0] = {"role": "user", "content": "Old question", "timestamp": 99.0}
        elif mode != "missing":
            result.append(current)
        # The production compaction consumer appends pending TODOs to the current user row.
        _fold_todo_snapshot(host, result)
        if mode == "rotate":
            host.session_id = "rotated-session"
        host._last_compaction_in_place = mode != "rotate"
        return deepcopy(result), host._cached_system_prompt

    host._compress_context = compress


@pytest.mark.parametrize("mode", ["none", "in_place", "rotate", "missing", "missing_with_history"])
@pytest.mark.parametrize("multimodal", [False, True], ids=["text", "multimodal"])
def test_observation_is_detached_before_compaction_and_bound_after_it(host, monkeypatch, mode, multimodal):
    original = ([{"type": "text", "text": "Find the answer"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
                if multimodal else "Find the answer")
    clean = deepcopy(original)
    staged = {"role": "user", "content": original, "timestamp": 123.0,
              "display_metadata": {"nested": ["accepted"]}}
    expected_row = deepcopy(staged)
    host._pending_cli_user_message = staged
    history = [{"role": "user", "content": "Old question"},
               {"role": "assistant", "content": "Old answer"}] * 2
    history_before = deepcopy(history)
    events, delivered = [], []
    _compact_with_todo(host, monkeypatch, mode, events)

    def hook(name, **kwargs):
        assert name == "pre_llm_call"
        events.append("hook")
        observation = kwargs["current_user_observation"]
        delivered.append((deepcopy(kwargs), observation))
        before = deepcopy(kwargs["conversation_history"])
        # Plugins may mutate their observation, but never the host's row or other snapshot.
        observation["message"]["display_metadata"]["nested"].append("plugin")
        if multimodal:
            observation["message"]["content"][1]["image_url"]["url"] = "plugin-image"
            assert observation["original_user_message"] == clean
            observation["original_user_message"][0]["text"] = "plugin-text"
        assert kwargs["conversation_history"] == before
        return [{"context": "plugin context"}]

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    ctx = _build(host, user_message=original, conversation_history=history,
                 task_id="requested-task", summarize_user_message_for_log=lambda value: str(value))
    payload, retained = delivered[0]
    observation = payload["current_user_observation"]
    assert set(observation) == {"version", "session_id", "task_id", "turn_id",
                                "original_user_message", "message", "history_index"}
    assert observation["version"] == "hermes.current-user-observation.v1"
    assert observation["original_user_message"] == clean
    assert observation["message"] == expected_row
    assert observation["message"] != retained["message"]
    assert observation["session_id"] == payload["session_id"] == host.session_id
    assert (host.session_id == "rotated-session") == (mode == "rotate")
    assert observation["task_id"] == payload["task_id"] == ctx.effective_task_id == "requested-task"
    assert observation["turn_id"] == payload["turn_id"] == ctx.turn_id == host._current_turn_id
    assert payload["model"] == host.model and payload["platform"] == host.platform
    assert payload["parent_session_id"] == host._parent_session_id
    assert payload["sender_id"] == host._user_id
    assert payload["is_first_turn"] == (not bool(ctx.conversation_history))
    assert events == (["hook"] if mode == "none" else ["compact", "hook"])
    assert ctx.active_system_prompt == host._cached_system_prompt == "SYSTEM"
    assert history == history_before
    assert ctx.plugin_user_context == "plugin context"
    if mode in {"missing", "missing_with_history"}:
        assert observation["history_index"] is None
        assert ctx.current_turn_user_idx == (-1 if mode == "missing" else 0)
    else:
        assert observation["history_index"] == ctx.current_turn_user_idx
        assert observation["history_index"] == (len(history) if mode == "none" else 2)
        at_hook = payload["conversation_history"][observation["history_index"]]
        assert (TODO_INJECTION_HEADER in str(at_hook["content"])) == (mode != "none")
        current = ctx.messages[ctx.current_turn_user_idx]
        if multimodal:
            assert current["content"] == at_hook["content"] + [
                {"type": "text", "text": compose_multimodal_context_part("", "plugin context")}]
        else:
            assert current["api_content"] == compose_user_api_content(at_hook["content"], "", "plugin context")
        assert current["display_metadata"]["nested"] == (["accepted"] if mode == "none" else ["accepted", "compacted"])


@pytest.mark.parametrize("persist_disabled", [False, True], ids=["ordinary", "detached-fork"])
@pytest.mark.parametrize("provenance", [
    {},
    {"source": "background", "source_kind": "background"},
    {"display_kind": "auto_continue", "display_metadata": {"attempt": [2]}},
    {"synthetic": True},
    {"_todo_snapshot_synthetic": True},
], ids=["voice", "background-source", "typed-continuation", "synthetic", "todo-synthetic"])
def test_clean_persist_override_preserves_provenance_and_forks_skip_hook(host, monkeypatch, provenance, persist_disabled):
    clean = "Find the answer"
    api_text = "[Voice message] " + clean
    staged = {"role": "user", "content": clean, **deepcopy(provenance)}
    host._pending_cli_user_message = staged
    host._persist_disabled = persist_disabled
    delivered = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: delivered.append((name, kw)) or [])
    ctx = _build(host, user_message=api_text, persist_user_message=clean,
                 persist_user_display_kind=provenance.get("display_kind"),
                 persist_user_display_metadata=deepcopy(provenance.get("display_metadata")))
    assert ctx.original_user_message == clean
    assert ctx.messages[0]["content"] == api_text
    assert host._persist_user_message_override == clean
    assert ctx.active_system_prompt == "SYSTEM"
    if persist_disabled:
        assert delivered == []
        assert ctx.plugin_user_context == ""
        return
    (name, payload), = delivered
    assert name == "pre_llm_call"
    observation = payload["current_user_observation"]
    assert observation["original_user_message"] == payload["user_message"] == clean
    assert observation["message"]["content"] == api_text
    assert observation["history_index"] == 0
    assert observation["task_id"] == ctx.effective_task_id == host._current_task_id
    assert observation["turn_id"] == ctx.turn_id
    assert payload["is_first_turn"]
    for key, value in provenance.items():
        assert observation["message"][key] == value
    if "display_metadata" in provenance:
        staged["display_metadata"]["attempt"].append(3)
        assert observation["message"]["display_metadata"] == provenance["display_metadata"]
