"""R01: typed MCP admission cannot lend one supervisor's source grant to another.

Set JEV_SUPERVISOR_SOURCE to an isolated standalone checkout. Two real native
bridges run the complete feature registry and strict HTTP MockTransport. Only
transport I/O and deterministic race rendezvous are substituted.
"""
import asyncio
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_policy import runtime_for_agent
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_state import SessionDB
from tests.agent.supervision_test_support import Agent
from tests.agent.test_supervision_evidence_owners import switchloom_payload


@contextmanager
def profile(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@contextmanager
def providers(home, monkeypatch, second="absent", *, private=False):
    source = os.environ.get("JEV_SUPERVISOR_SOURCE")
    if not source:
        pytest.skip("explicit isolated Jev source checkout required")
    monkeypatch.syspath_prepend(str(Path(source) / "src"))
    from jev_supervisor.config import Config
    from jev_supervisor.host_adapter import NativeHostBridge
    from jev_supervisor.transport import Transport

    home.mkdir()
    grant = {"server": "synthetic-sloom", "url": "https://switchloom.invalid/mcp", "tool": "search_context",
             "accounts": ["a"], "sources": ["mail"], "modes": ["stored"],
             "remote_processing": True, "allow_live_fetch": False}
    fields = "query candidates needs_triage exact_answer_complete baseline_ids scope required_ids target_id".split()
    policy = {"grants": ["observe", "rank_candidates"], "data_policy": ["project_excerpt"],
              "egress_policy": {"id": "synthetic-owner", "profile": str(home), "fixture": True,
                                "fields": dict.fromkeys(fields, "synthetic"), "sources": {}},
              "mcp_sources": [grant]}
    if private:
        policy["egress_policy"].update(fixture=False, fields=dict.fromkeys(fields, "private_project"),
                                      sources=dict.fromkeys(fields, "synthetic-mail-source"))
    other = copy.deepcopy(policy)
    if second in {"absent", "no_egress"}:
        other["mcp_sources"] = []
    if second == "no_egress":
        other.pop("egress_policy")
    damages = {"account": ("accounts", ["foreign"]), "source": ("sources", ["calendar"]),
               "mode": ("modes", ["live"]), "route": ("url", "https://other.invalid/mcp"),
               "tool": ("tool", "get_object"), "remote": ("remote_processing", False)}
    if second in damages:
        key, value = damages[second]
        other["mcp_sources"][0][key] = value
    (home / "config.yaml").write_text(json.dumps({"supervision": {"enabled": True,
        "plugins": {"jev-supervisor": policy, "other-provider": other}}}))
    manager = PluginManager(scope_key=str(home))
    config = Config(str(home), enabled=True, policy_id="synthetic-owner", fixture_policy=not private,
                    allowed_classes=frozenset({"private_project" if private else "synthetic"}),
                    source_grants=frozenset({"synthetic-mail-source"}) if private else frozenset())
    agent = Agent()
    agent._session_db = SessionDB(home / "state.db")
    agent._session_db.create_session(agent.session_id, "cli")
    v = SimpleNamespace(home=home, agent=agent, calls=[[], []], events=[[], []], natives=[], bridges=[],
                        before_response=None, silent=set(), ack_states=[], submissions=[])

    class Credential:
        def bearer(self, scope, endpoint):
            assert scope == str(home) and endpoint == "https://api.typesafe.ai/v1/systemone"
            return "synthetic-test-token"

    def transport(index):
        async def http(request):
            assert request.url == "https://api.typesafe.ai/v1/systemone"
            assert threading.current_thread().name == "jev-supervision"
            body = json.loads(request.content)
            v.calls[index].append(body)
            if v.before_response:
                v.before_response(index)
            state = json.loads(body["state"]) if isinstance(body["state"], str) else body["state"]
            chosen = state["facts"]["candidates"][-1]["id"]
            answers = {}
            for key, question in body["questions"].items():
                if question["type"] == "choice":
                    answers[key] = {"type": "choice", "choice": chosen, "confidence": .99,
                                    "probabilities": {k: float(k == chosen) for k in question["criteria"]}}
                else:
                    answers[key] = {"type": "noul", "noul": .99 if key.endswith(chosen) else .01}
            return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {}, "answers": answers})
        return Transport(config, Credential(), http_transport=httpx.MockTransport(http))

    with profile(home):
        try:
            for index, name in enumerate(("jev-supervisor", "other-provider")):
                native = PluginContext(PluginManifest(name=name), manager).supervision
                bridge = NativeHostBridge(native, config, None, transport=transport(index))
                assert bridge.start()
                v.natives.append(native)
                v.bridges.append(bridge)
                consumer = native._registration.consumer
                def observe(event, index=index, consumer=consumer):
                    v.events[index].append(event)
                    if index not in v.silent:
                        return consumer(event)
                monkeypatch.setattr(native._registration, "consumer", observe)
            v.runtime = runtime_for_agent(agent, create=True)
            yield v
        finally:
            for bridge in reversed(v.bridges):
                bridge.close()
            agent._session_db.close()


def invoke(v, monkeypatch, before_call=None):
    from tools import mcp_tool as core, mcp_tool_handlers as handlers
    from mcp.types import CallToolResult, TextContent
    payload = switchloom_payload()
    typed = CallToolResult(content=[TextContent(type="text", text="Display prose is not evidence")],
                           structuredContent=payload)
    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            assert name == "search_context"
            return typed
    server = SimpleNamespace(session=Session(), _rpc_lock=asyncio.Lock(), _pending_call_context=None,
                             _config={"url": "https://switchloom.invalid/mcp"})
    v.server = server
    from tools.mcp_tool_scope import _server_key
    with profile(v.home):
        key = _server_key("synthetic-sloom")
    monkeypatch.setattr(core, "_servers", {key: server})
    monkeypatch.setattr(core, "_server_tool_scopes", {key: {str(v.home)}})
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
    monkeypatch.setattr(handlers, "_acquire_call_server", lambda *a: (server, None))
    monkeypatch.setattr(handlers, "_tool_is_read_only", lambda *a: True)
    monkeypatch.setattr(handlers._loop, "_run_on_mcp_loop", lambda call, **kw: asyncio.run(call()))
    monkeypatch.setattr(handlers, "_record_call_outcome", lambda name, result: result)
    if before_call:
        before_call()
    with profile(v.home), bind_subagent_parent(v.agent):
        rendered = handlers._make_tool_handler("synthetic-sloom", "search_context", 1)(
            {"query": "Which source states the fact?", "mode": "stored"})
    return rendered, json.loads(rendered)["structuredContent"]


def stored(v):
    with sqlite3.connect(v.home / "state.db") as conn:
        return conn.execute("SELECT status,reason,plugin_generation FROM supervision_receipts ORDER BY updated_at").fetchall()


@pytest.mark.parametrize("case", [
    "absent", "no_egress", "account", "source", "mode", "route", "tool", "remote",
    "authorized_a", "authorized_b", "both_authorized", "independent_b_after_a_unload",
    "unload_before_dispatch", "unload_during_inference", "unload_before_ack",
    "grant_before_ack", "session_during_inference", "route_before_ack", "generation_before_ack",
    "instruction_before_ack",
    "forged_proposal", "unrelated_owner",
])
def test_recipient_authority_at_disclosure_proposal_and_consumption(tmp_path, monkeypatch, case):
    authorized_b = case in {"authorized_b", "independent_b_after_a_unload"}
    second = "authorized" if authorized_b or case == "both_authorized" else case if case in {
        "absent", "no_egress", "account", "source", "mode", "route", "tool", "remote"} else "absent"
    with providers(tmp_path / "profile", monkeypatch, second) as v:
        # Reproduce the audit's silent source-owner case: B must not receive any
        # facts or a generic egress grant, even though A has admitted the source.
        if case in {"absent", "no_egress", "account", "source", "mode", "route", "tool", "remote"} or authorized_b:
            v.silent.add(0)
        a, b = v.natives
        if case in {"absent", "independent_b_after_a_unload"}:
            v.before_response = lambda index: a.unregister()
        elif case == "unload_during_inference":
            v.before_response = lambda index: a.unregister()
        elif case == "session_during_inference":
            v.before_response = lambda index: setattr(v.server, "session", object())
        if case == "unload_before_dispatch":
            observe = v.runtime.observe
            def revoke_before_observe(*args, **kwargs):
                a.unregister()
                return observe(*args, **kwargs)
            monkeypatch.setattr(v.runtime, "observe", revoke_before_observe)
        if case in {"forged_proposal", "unrelated_owner"}:
            consume = a._registration.consumer
            def borrow(event):
                rows = event["facts"]["candidates"]
                p = dict(proposal_id="borrow", plugin_generation=b._registration.generation,
                         feature_id="F14", incident_id="borrow", expected=event["revision"],
                         owner="lcm" if case == "unrelated_owner" else event["owner"],
                         target_id=event["facts"]["target_id"], action="rank_candidates",
                         evidence_refs=[r["ref"] for r in rows], expires_at_monotonic=event["deadline"],
                         candidate_ids=[r["id"] for r in rows][::-1])
                v.submissions.append(b.submit(p))
                return consume(event)
            monkeypatch.setattr(a._registration, "consumer", borrow)
        ack = v.runtime.acknowledge_owner
        def acknowledge(*args, **kwargs):
            # Selected is pending, and the canonical SessionDB has not certified
            # consumption or completed the incident before this owner boundary.
            pending = stored(v)
            assert any(status == "accepted" and reason == "owner_selected" for status, reason, _ in pending)
            assert not any(status == "consumed" for status, _, _ in pending)
            assert not v.runtime.incidents
            v.ack_states.append(pending)
            if case == "unload_before_ack":
                a.unregister()
            elif case == "grant_before_ack":
                a._registration.mcp_sources = ()
            elif case == "route_before_ack":
                v.server._config["url"] = "https://replacement.invalid/mcp"
            elif case == "instruction_before_ack":
                from agent.supervision_context import steer_from_user
                steer_from_user(v.agent, "- Replacement task")
            elif case == "generation_before_ack":
                a.register(consumer=lambda event: None, requested_grants=["observe", "rank_candidates"],
                           mcp_adapter=a._registration.mcp_adapter)
            return ack(*args, **kwargs)
        monkeypatch.setattr(v.runtime, "acknowledge_owner", acknowledge)
        rendered, actual = invoke(v, monkeypatch)
        success = case in {"authorized_a", "authorized_b", "both_authorized", "independent_b_after_a_unload", "forged_proposal", "unrelated_owner"}
        expected = switchloom_payload()
        if success:
            expected["items"].reverse()
        assert actual == expected
        if case == "both_authorized":
            assert len(v.events[0]) == len(v.events[1]) == 1
            assert v.calls[0] or v.calls[1]
        elif authorized_b:
            assert len(v.calls[1]) == 1 and len(v.events[1]) == 1
        else:
            assert v.events[1] == [] and v.calls[1] == []
        if case == "unload_before_dispatch":
            assert not v.events[0] and not v.calls[0]
        if success:
            if case != "both_authorized":
                assert len(v.calls[1 if authorized_b else 0]) == 1
            digest = hashlib.sha256(rendered.encode()).hexdigest()
            generations = {n._registration.generation for n in v.natives} if case == "both_authorized" else {
                v.natives[1 if authorized_b else 0]._registration.generation}
            assert any(status == "consumed" and reason == "owner_consumed:" + digest and generation in generations
                       for status, reason, generation in stored(v))
            assert v.ack_states and v.runtime.incidents
        else:
            assert not any(row[0] == "consumed" for row in stored(v))
            assert not v.runtime.incidents
        if v.submissions:
            assert v.submissions[0]["status"] == "rejected"
        # The owner's original deadline remains one shared token, never one per
        # recipient or reissued after unload/replacement.
        if v.runtime.round_deadline is not None:
            from agent.supervision_types import DECISION_BUDGET_SECONDS
            assert v.runtime.round_deadline == pytest.approx(v.runtime.round_deadline_issued_at + DECISION_BUDGET_SECONDS)


def test_two_profile_a_b_a_keeps_mcp_authority_and_other_owners_separate(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from agent.supervision_types import Action, OwnerRequestV1
    set_multiplex_active(True)
    try:
        with ExitStack() as stack:
            a = stack.enter_context(providers(tmp_path / "A", monkeypatch, "authorized"))
            b = stack.enter_context(providers(tmp_path / "B", monkeypatch, "absent"))
            a.silent.add(0)  # A's independently authorized second provider wins.
            b.silent.add(0)  # B's second provider must never borrow profile A's grant.
            for v, reordered in ((a, True), (b, False), (a, True)):
                with profile(v.home), bind_subagent_parent(v.agent):
                    v.runtime.bind_turn()
                    rendered, actual = invoke(v, monkeypatch)
                    assert actual["items"][0]["chunk_ref_id"] == ("chunk1" if reordered else "chunk0")
            assert len(a.calls[1]) == 2 and len(a.events[1]) == 2
            assert not b.calls[1] and not b.events[1]
            # A typed request claiming MCP ownership without the authenticated
            # transport binding cannot mint a recipient/egress opportunity.
            with profile(a.home), bind_subagent_parent(a.agent):
                a.runtime.bind_turn()
                before = tuple(map(len, a.events))
                req = OwnerRequestV1("mcp", "forged:mcp", a.runtime.revision,
                    ({"id": "x", "ref": "x", "excerpt": "source"},), a.runtime.clock() + .15,
                    data_policy=("project_excerpt",), requires_ack=True)
                assert not a.runtime.owner_decision(Action.RANK_CANDIDATES, req).selected
                assert tuple(map(len, a.events)) == before
                # No global observer ban: an unrelated owner's authorized
                # project observation still follows its existing local contract.
                a.runtime.observe("unrelated", {"query": "synthetic"}, owner="other-owner",
                                  data_class="project_excerpt")
                assert tuple(map(len, a.events)) == tuple(n + 1 for n in before)
                assert not b.events[1]
    finally:
        set_multiplex_active(False)
