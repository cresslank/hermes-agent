"""LCM delivery/read boundaries through real registry, MockTransport and SessionDB."""
import hashlib
import json
import sqlite3
import threading

import pytest

from agent.supervision_history import capture_visibility, source_visibility
from tests.agent.supervision_test_support import accept
from tests.agent.test_supervision_evidence_owners import vertical, seed


def durable(v):
    # Independent read connection: in-memory runtime receipts are not durability.
    with sqlite3.connect(v.agent._session_db.db_path) as conn:
        return conn.execute("SELECT status,reason FROM supervision_receipts ORDER BY created_at").fetchall()


def archive(v):
    text = "Archive conclusion: retain the exact local decision."
    sid = v.engine._store.append("current", {"role": "user", "content": text})
    return f"lcm:{sid}:0-{len(text)}", text


def tool_pair(ref):
    return [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-visible", "type": "function", "function": {"name": "lcm_recall",
         "arguments": json.dumps({"query": "Archive", "detail": "answer_ready", "seen_refs": [ref]})}}]},
        {"role": "tool", "tool_call_id": "call-visible", "content": '{"hits": []}'}]


@pytest.mark.parametrize("mode", ["positive", "all_seen", "winner_seen", "empty_hydration",
    "failed_hydration", "cap", "serialization", "revoked", "stale", "expired", "ack_storage", "selection_storage"])
def test_recall_consumes_only_final_effect(vertical, monkeypatch, mode):
    from hermes_lcm import tools, recall_response
    from agent import supervision_receipts
    v = vertical
    seed(v)
    # Freeze retrieval time only to compare baseline response bytes/metadata,
    # never monotonic admission or timeout clocks.
    monkeypatch.setattr(tools.time, "time", lambda: 1_800_000_000.0)
    args = {"query": "Claim", "limit": 10, "detail": "answer_ready", "seen_refs": []}
    winner_seen = set()
    if mode == "all_seen":
        args["seen_refs"] = [f"lcm:{sid}:0-{len(v.engine._store.get(sid)['content'])}" for sid in range(1, 11)]
    elif mode == "winner_seen":
        # The real FTS/scope order can vary; make the native judge's last
        # candidate already seen without fabricating a selected response.
        rank = tools._lcm_recall_rerank
        def ranked(*a, **kw):
            incoming = a[2]
            sid = incoming[7]["hit"]["store_id"]
            winner_seen.add(f"lcm:{sid}:0-{len(v.engine._store.get(sid)['content'])}")
            return rank(*a, **kw)
        monkeypatch.setattr(tools, "_lcm_recall_rerank", ranked)
    shape = recall_response.shape_recall_response
    shapes = []
    def shaped(**kw):
        if mode == "winner_seen":
            kw["seen_refs"] = winner_seen
        if kw["rerank_status"] == "applied":
            assert durable(v) == [("accepted", "owner_selected")]
            if mode == "serialization":
                raise ValueError("synthetic serializer rejection")
        with monkeypatch.context() as local:
            if kw["rerank_status"] == "applied":
                if mode == "empty_hydration":
                    local.setattr(tools, "_lcm_recall_answer_ready_content", lambda *a, **k: {})
                    local.setattr(tools._LcmRecallStrictSelector, "take", lambda *a: [])
                    local.setattr(tools._LcmRecallStrictSelector, "exhausted", lambda *a: True)
                if mode == "failed_hydration":
                    def failed(*a, **k):
                        raise RuntimeError("synthetic bounded hydration failure")
                    local.setattr(tools, "_lcm_recall_answer_ready_content", failed)
            encoded = shape(**kw)
        shapes.append((kw["rerank_status"], encoded))
        if kw["rerank_status"] == "applied":
            if mode == "revoked":
                v.native.unregister()
            if mode == "stale":
                accept(v.agent, "Use the revised archive policy.", continuation=True)
            if mode == "expired":
                v.runtime.clock = lambda: v.runtime.round_deadline + 1
        return encoded
    monkeypatch.setattr(recall_response, "shape_recall_response", shaped)
    if mode == "cap":
        monkeypatch.setattr(tools, "_LCM_RECALL_RESPONSE_CHAR_CAP", 100)
    if mode in {"ack_storage", "selection_storage"}:
        record = supervision_receipts.record
        def persist(runtime, proposal, receipt, **kw):
            if ((mode == "ack_storage" and receipt.reason.startswith("owner_consumed:")) or
                    (mode == "selection_storage" and receipt.reason == "owner_selected")):
                return False
            return record(runtime, proposal, receipt, **kw)
        monkeypatch.setattr(supervision_receipts, "record", persist)
    encoded = tools.lcm_recall(args, engine=v.engine)
    result = json.loads(encoded)
    assert len(v.calls) == 1
    receipts = durable(v)
    if mode == "positive":
        assert result["hits"] and result["provenance"]["rerank"] == "applied"
        assert receipts == [("consumed", "owner_consumed:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest())]
        assert not v.runtime.owner_selections
        for hit in result["hits"]:
            row = v.engine._store.get(hit["store_id"])
            assert hit["content"] == row["content"] and hit["role"] == row["role"]
            assert hit["exact_ref"] == f"lcm:{hit['store_id']}:0-{len(row['content'])}"
    else:
        assert not any(status == "consumed" for status, _ in receipts)
        assert not any(r.status == "applied" for r in v.runtime.receipts.values())
        assert result["provenance"]["rerank"] == "disabled"
        # If a baseline was already serialized, veto returns those exact bytes.
        baselines = [raw for status, raw in shapes if status == "disabled"]
        assert encoded in baselines
        if mode == "all_seen":
            assert not result["hits"] and not result["delta"]["progress"]


@pytest.mark.parametrize("representation", ["tool_pair", "legacy", "responses", "anthropic",
    "structured_reasoning_key", "escaped_arguments", "source_text", "hidden_reasoning", "hidden_block", "over_cap"])
def test_grep_visibility_and_existing_direct_expansion(vertical, monkeypatch, representation):
    from hermes_lcm import tools
    v = vertical
    ref, text = archive(v)
    messages = {
        "tool_pair": tool_pair(ref),
        "legacy": [{"role": "assistant", "content": None, "function_call": {"name": "lcm_recall", "arguments": json.dumps({"seen_refs": [ref]})}}],
        "responses": [{"type": "function_call", "name": "lcm_recall", "arguments": json.dumps({"seen_refs": [ref]})}],
        "anthropic": [{"role": "assistant", "content": [{"type": "tool_use", "name": "lcm_recall", "input": {"seen_refs": [ref]}}]}],
        "structured_reasoning_key": [{"type": "function_call", "arguments": {"reasoning": {"ref": ref}}}],
        "escaped_arguments": [{"type": "function_call", "arguments": json.dumps({"seen_refs": [ref]}).replace("lcm:", r"lcm\u003a")}],
        "source_text": [{"role": "user", "content": text}],
        "hidden_reasoning": [{"role": "assistant", "content": "", "reasoning": ref, "reasoning_content": text}],
        "hidden_block": [{"role": "assistant", "content": [{"type": "thinking", "thinking": ref}, {"type": "text", "text": "Find the decision."}]}],
        "over_cap": [{"type": "function_call", "arguments": "x" * 1_000_001 + ref}],
    }[representation]
    capture_visibility(v.agent, messages)
    state = source_visibility(v.runtime, ref, text)
    hidden = representation in {"hidden_reasoning", "hidden_block"}
    if representation == "over_cap":
        assert state is None
    else:
        assert state == {"explicit_ref_available": not hidden and representation != "source_text",
                         "source_text_visible": representation == "source_text"}
    expand, reads = tools.lcm_expand, []
    def reader(args, **kw):
        reads.append(args)
        return expand(args, **kw)
    monkeypatch.setattr(tools, "lcm_expand", reader)
    result = json.loads(tools.lcm_grep({"query": "Archive", "missing_decision": "Which Archive decision?"}, engine=v.engine))
    assert len(v.calls) == len(reads) == int(hidden)
    assert ("recovered_history" in result) == hidden
    if not hidden:
        assert not durable(v)
        direct = json.loads(tools.lcm_expand({"store_id": 1, "content_offset": 0, "max_tokens": 600, "include_exact_ref": True}, engine=v.engine))
        assert direct["exact_ref"] == ref and direct["content"] == text
        assert not v.calls


@pytest.mark.parametrize("change", ["revocation", "instruction", "visibility", "none"])
def test_actual_grep_read_fenced_after_native_selection(vertical, monkeypatch, change):
    from hermes_lcm import tools
    v = vertical
    ref, text = archive(v)
    capture_visibility(v.agent, [{"role": "user", "content": "Find the Archive decision."}])
    selected = v.engine.supervision.expand_one_owned_ref
    selections = []
    def after_selection(request):
        decision = selected(request)
        assert decision and decision.get("receipt_id")
        selections.append(decision)
        assert durable(v) == [("accepted", "owner_selected")]
        if change == "revocation":
            v.native.unregister()
        elif change == "instruction":
            accept(v.agent, "Use the revised decision.", continuation=True)
        elif change == "visibility":
            capture_visibility(v.agent, tool_pair(ref))
        return decision
    monkeypatch.setattr(v.engine.supervision, "expand_one_owned_ref", after_selection)
    expand, reads = tools.lcm_expand, []
    def reader(args, **kw):
        # Independent thread observes BOTH fences unavailable throughout the
        # actual bounded reader, not just during a preceding supports() check.
        locked = []
        def inspect_fences():
            for lock in (v.runtime.lock, v.native._registration.fence):
                acquired = lock.acquire(blocking=False)
                locked.append(not acquired)
                if acquired:
                    lock.release()
        t = threading.Thread(target=inspect_fences)
        t.start()
        t.join(2)
        assert not t.is_alive() and locked == [True, True]
        assert v.native._registration.active
        with v.engine.supervision.begin_history_expansion(selections[0]) as replay:
            assert replay is False
        assert args == {"store_id": 1, "content_offset": 0, "max_tokens": 600, "include_exact_ref": True}
        reads.append(args)
        return expand(args, **kw)
    monkeypatch.setattr(tools, "lcm_expand", reader)
    result = json.loads(tools.lcm_grep({"query": "Archive", "missing_decision": "Which Archive decision?"}, engine=v.engine))
    assert len(v.calls) == 1
    assert len(reads) == int(change == "none")
    assert ("recovered_history" in result) == (change == "none")
    statuses = durable(v)
    assert len(statuses) == 1
    assert statuses[0][0] == ("consumed" if change == "none" else "stale" if change == "instruction" else "rejected")
    if change == "none":
        assert result["recovered_history"]["content"] == text
    assert not v.runtime.owner_selections and not v.runtime.owner_reads


@pytest.mark.parametrize("mode,provider", [("chat_completions", "openai"),
    ("codex_responses", "openai-codex"), ("anthropic_messages", "anthropic")])
def test_ordinary_request_assembly_keeps_known_tool_argument_ref(vertical, mode, provider):
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.supervision_policy import runtime_for_agent
    from agent.turn_context import _reset_per_turn_agent_state
    from tests.agent.test_tool_call_incremental_persistence import _make_agent
    from tests.agent.test_supervision_views import assemble
    from hermes_lcm.tools import lcm_grep, lcm_expand

    v = vertical
    ref, text = archive(v)
    agent = _make_agent()
    agent._session_db = v.agent._session_db
    agent.session_id = v.agent.session_id
    _reset_per_turn_agent_state(agent)
    agent.api_mode, agent.provider = mode, provider
    runtime = runtime_for_agent(agent, create=True)
    request = assemble(agent, [{"role": "user", "content": "Recover the Archive decision."}, *tool_pair(ref)])
    assert ref in json.dumps(request.api_messages)
    assert source_visibility(runtime, ref, text) == {
        "explicit_ref_available": True, "source_text_visible": False}
    with bind_subagent_parent(agent):
        result = json.loads(lcm_grep({"query": "Archive", "missing_decision": "Which Archive decision?"}, engine=v.engine))
        assert "recovered_history" not in result and not v.calls
        direct = json.loads(lcm_expand({"store_id": 1, "content_offset": 0, "max_tokens": 600,
                                       "include_exact_ref": True}, engine=v.engine))
        assert direct["exact_ref"] == ref and direct["content"] == text
    assert not durable(v)
