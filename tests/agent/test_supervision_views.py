"""Native request assembly, canonical tool settlement and authorized clarify consumers."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.supervision_catalog import Catalog, SkillCandidate, SkillView, tool_id
from agent.supervision_views import SupervisionViews, AuthorizedDefault
from agent.turn_request_assembly import assemble_api_request
from tests.agent.test_tool_call_incremental_persistence import _make_agent, _mock_tool_call, _make_tool_defs
from tools.budget_config import DEFAULT_BUDGET


class Decisions:
    def __init__(self, pool, *, delay=.015, bad=False):
        self.pool, self.delay, self.bad = pool, delay, bad
        self.calls = []

    def select_windows(self, request):
        self.calls.append(request)
        def work():
            time.sleep(self.delay)
            return {"revision": "stale" if self.bad else request["revision"],
                    "selected_ids": [request["facts"]["candidates"][0]["id"]]}
        return self.pool.submit(work)

    def rank_candidates(self, request):
        self.calls.append(request)
        def work():
            time.sleep(self.delay)
            return {"revision": request["revision"],
                    "selected_ids": [request["facts"]["candidates"][-1]["id"]]}
        return self.pool.submit(work)

    def evaluate_relation(self, request):
        self.calls.append(request)
        return {"revision": request["revision"], "relation": "use_authorized_default"}


@pytest.fixture
def facade():
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield Decisions(pool)


@pytest.fixture
def agent(tmp_path, monkeypatch, facade):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    a = _make_agent()
    from agent.turn_context import _reset_per_turn_agent_state
    _reset_per_turn_agent_state(a)
    a._supervision_views = SupervisionViews(facade, scope="profile:lineage:run1:rules1")
    a._flush_messages_to_session_db = MagicMock(return_value=True)
    return a


def assemble(agent, messages):
    return assemble_api_request(agent, messages=messages, current_turn_user_idx=0,
        _ext_prefetch_cache="", _plugin_user_context="", moa_config=None,
        active_system_prompt="system", original_user_message="read the source",
        pending_moa_prepared_request=None, request_logger=logging.getLogger(__name__))


@pytest.mark.parametrize("caching", [False, True])
@pytest.mark.parametrize("mode,provider", [("chat_completions", "openai"), ("codex_responses", "openai-codex"),
                                          ("anthropic_messages", "anthropic")])
def test_native_request_selection_before_provider_conversion_and_cache(agent, mode, provider, caching):
    from agent.chat_completion_helpers import _prompt_cache_scope_for_agent
    agent.api_mode, agent.provider = mode, provider
    agent._use_prompt_caching = caching
    agent.tools = _make_tool_defs("write_file", "web_search", "read_file", "tool_search", "tool_describe", "tool_call")
    original = copy.deepcopy(agent.tools)
    catalog = Catalog.tools(agent.tools)
    agent._supervision_views.propose_tools(("web_search",), catalog_revision=catalog.revision,
                                         scope=agent._supervision_views.scope)
    history = [{"role": "user", "content": "read the source"}]
    result = assemble(agent, history)
    assert [tool_id(t) for t in result.tools_for_api] == ["web_search", "write_file", "read_file", "tool_search", "tool_describe", "tool_call"]
    assert agent.tools == original and history == [{"role": "user", "content": "read the source"}]
    key1 = _prompt_cache_scope_for_agent(agent)
    kwargs = agent._build_api_kwargs(result.api_messages, tools_for_api=result.tools_for_api)
    wire_names = [tool_id(t) for t in kwargs["tools"]]
    assert wire_names[0] == "web_search" and "write_file" in wire_names
    agent._supervision_views.propose_tools(("write_file",), catalog_revision=catalog.revision,
                                         scope=agent._supervision_views.scope)
    result2 = assemble(agent, history)
    assert _prompt_cache_scope_for_agent(agent) != key1
    assert result2.tools_for_api[0] == original[0]
    assert agent.tools == original


@pytest.mark.parametrize("fault", ["unknown", "stale", "scope", "schema"])
def test_tool_selection_fallback(agent, fault):
    agent.tools = _make_tool_defs("a", "b", "tool_search", "tool_describe", "tool_call")
    c = Catalog.tools(agent.tools)
    ids = ("unknown",) if fault == "unknown" else ("b",)
    agent._supervision_views.propose_tools(ids, catalog_revision="old" if fault == "stale" else c.revision,
                                         scope="other" if fault == "scope" else agent._supervision_views.scope)
    if fault == "schema":
        agent.tools[0]["function"]["description"] = "new contract"
    assert assemble(agent, [{"role": "user", "content": "request"}]).tools_for_api == agent.tools


def test_skill_hints_are_request_only_and_unload_cannot_erase_loaded_history(agent):
    view = agent._supervision_views.skills
    revision = view.catalog((SkillCandidate("exact", "required", True), SkillCandidate("optional", "candidate")), "phase1")
    assert view.rank(("optional",), revision=revision, plugin_id="plugin", ambiguous=True)
    assert view.ranked_ids == ("exact", "optional")
    history = [{"role": "user", "content": "loaded skill body stays here"}]
    result = assemble(agent, history)
    assert "Optional skill candidate: optional" in str(result.api_messages)
    view.remove_own_hint("another_plugin")
    assert view.hints
    view.remove_own_hint("plugin")
    assert "Optional skill candidate:" not in str(assemble(agent, history).api_messages)
    assert history[0]["content"] == "loaded skill body stays here"


def output():
    return json.dumps({"output": "".join(f"section {i}: " + "x" * 180 + "\n" for i in range(28)) +
                       "WARNING partial result; cleanup not run\n", "exit_code": 2,
                       "receipts": [{"mutation": "none", "approval": "not_required"}]})


@pytest.mark.parametrize("name,parallel", [("web_search", False), ("tool_call", False),
    ("execute_code", False), ("connectors_read", False), ("web_search", True), ("memory", False)])
def test_native_settlement_projects_once_and_retains_original(agent, facade, name, parallel, monkeypatch):
    raw = output()
    messages = []
    assistant = SimpleNamespace(content="", tool_calls=[_mock_tool_call(name=name, call_id="view-call")])
    with patch("model_tools.handle_function_call", return_value=raw), patch("tools.memory_tool.memory_tool", return_value=raw):
        if parallel:
            agent._execute_tool_calls_concurrent(assistant, messages, "task-1")
        else:
            agent._execute_tool_calls_sequential(assistant, messages, "task-1")
    tool = next(m for m in messages if m.get("role") == "tool")
    assert '"supervision_view"' in tool["content"]
    assert "WARNING partial result; cleanup not run" in tool["content"]
    assert '"exit_code": 2' in tool["content"]
    assert len(facade.calls) == 1
    files = list(Path(__import__("hermes_constants").get_hermes_home(), "cache", "spillover").glob("*"))
    assert len(files) == 1 and files[0].read_text() == raw
    assert agent._tool_guardrails._persisted_result_paths["view-call"] == str(files[0])
    assert agent._flush_messages_to_session_db.called


def test_uncached_retrieval_then_result_share_absolute_150ms_token(agent, facade):
    owner = agent._supervision_views
    # Model the admitted monotonic budget independently of OS descheduling
    # BETWEEN owner calls. Each decision still runs on a real delayed worker and
    # Future.result still has its real timeout; expiry is tested separately.
    budget_clock = [time.monotonic()]
    owner.clock = lambda: budget_clock[0]
    candidates = ({"id": "a", "excerpt": "topic", "source_ref": "ref:a"},
                  {"id": "b", "excerpt": "answer", "source_ref": "ref:b"})
    started = time.monotonic()
    ranked = owner.rank_retrieval(candidates, complete=True)
    assert ranked == (candidates[1], candidates[0])
    budget_clock[0] += facade.delay
    result = owner.result(output(), output(), tool_name="execute_code", call_id="once", env=None, budget=DEFAULT_BUDGET)
    assert "supervision_view" in result
    assert time.monotonic() - started >= .025
    assert facade.calls[0]["deadline"] == facade.calls[1]["deadline"]


@pytest.mark.parametrize("fault", ["stale", "expired", "opaque", "huge_line", "unknown"])
def test_result_fail_open(agent, facade, fault):
    owner, raw = agent._supervision_views, output()
    if fault == "stale": facade.bad = True
    if fault == "expired": facade.delay = .18
    if fault == "opaque": raw = "x" * 4000
    if fault == "huge_line": raw = json.dumps({"output": "x" * 5000})
    if fault == "unknown": facade.select_windows = lambda r: {"revision": r["revision"], "selected_ids": ["bad"]}
    assert owner.result(raw, "baseline", tool_name="tool_call", call_id="bad", env=None, budget=DEFAULT_BUDGET) == "baseline"


@pytest.mark.parametrize("blocked", [None, "secret", "approval", "low_stakes", "authorized", "scope"])
def test_native_inline_clarification_never_fabricates_user_answer(agent, blocked):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    owner = agent._supervision_views
    fields = dict(question="Format?", value="Bullets", scope=owner.scope, evidence_ref="instruction:1",
                  low_stakes=True, authorized=True)
    if blocked in ("secret", "approval"): fields[blocked] = True
    if blocked in ("low_stakes", "authorized"): fields[blocked] = False
    if blocked == "scope": fields[blocked] = "stale"
    owner.defaults["Format?"] = AuthorizedDefault(**fields)
    agent.clarify_callback = MagicMock(return_value={"answers": {"q0": "Paragraphs"}})
    result = json.loads(INLINE_TOOL_EXECUTORS["clarify"](agent,
        {"questions": [{"question": "Format?", "choices": ["Bullets", "Paragraphs"]}],
         "authorized": True}, InlineToolContext("task-1")))
    if blocked is None:
        assert not agent.clarify_callback.called
        assert result["responses"][0]["resolved_value"] == "Bullets"
        assert result["responses"][0]["user_response"] == ""
    else:
        assert agent.clarify_callback.called
        assert result["responses"][0]["user_response"] == "Paragraphs"


def test_native_deferred_discovery_selection_and_dispatch_scope(agent):
    import model_tools
    from tools.registry import registry
    from agent.supervision_catalog import authorized_tool_schemas
    name, outside, alternative = "mcp_jev_scoped", "mcp_jev_outside", "mcp_jev_alternative"
    for tool_name, toolset in ((name, "mcp-jev-scoped"), (outside, "mcp-jev-outside"),
                              (alternative, "mcp-jev-scoped")):
        registry.register(name=tool_name, toolset=toolset,
                          schema=_make_tool_defs(tool_name)[0]["function"],
                          handler=lambda args, **kw: output())
    try:
        model_tools._clear_tool_defs_cache()
        agent.enabled_toolsets = ["mcp-jev-scoped"]
        agent.disabled_toolsets = []
        agent.tools = _make_tool_defs("tool_search", "tool_describe", "tool_call")
        available = authorized_tool_schemas(agent)
        assert name in [tool_id(s) for s in available]
        assert outside not in [tool_id(s) for s in available]
        catalog = Catalog.tools(available)
        agent._supervision_views.propose_tools((name,), catalog_revision=catalog.revision,
            scope=agent._supervision_views.scope, include_deferred=True)
        result = assemble(agent, [{"role": "user", "content": "read source"}])
        assert result.tools_for_api[0] == next(s for s in available if tool_id(s) == name)
        assert alternative in [tool_id(s) for s in available]
        assert alternative not in [tool_id(s) for s in result.tools_for_api]
        assert [tool_id(s) for s in agent.tools] == ["tool_search", "tool_describe", "tool_call"]
        messages = []
        assistant = SimpleNamespace(content="", tool_calls=[_mock_tool_call(name="tool_call", call_id="actual-deferred",
            arguments=json.dumps({"calls": [{"name": name, "arguments": {}}]}))])
        agent._execute_tool_calls_sequential(assistant, messages, "task-1")
        assert '"supervision_view"' in messages[0]["content"]
        denied = model_tools.handle_function_call("tool_call", {"calls": [{"name": outside, "arguments": {}}]},
            enabled_toolsets=agent.enabled_toolsets, disabled_toolsets=[])
        assert "not available in this session" in denied
    finally:
        registry.deregister(name)
        registry.deregister(outside)
        registry.deregister(alternative)
        model_tools._clear_tool_defs_cache()


def test_adversarial_facade_cannot_replace_candidate_bytes(agent, facade):
    raw = output()
    def tamper(request):
        block = request["facts"]["candidates"][0]
        block["excerpt"] = "FORGED RESULT"
        return {"revision": request["revision"], "selected_ids": [block["id"]]}
    facade.select_windows = tamper
    result = agent._supervision_views.result(raw, raw, tool_name="execute_code", call_id="tamper", env=None, budget=DEFAULT_BUDGET)
    assert "FORGED RESULT" not in result and "supervision_view" in result
