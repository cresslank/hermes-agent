"""Ordinary native source -> real accepted text final -> canonical adoption.

No hand-constructed final permission, proposition, declaration node or adoption.
All inputs are synthetic and both source owner and native final consumer are real.
"""
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_context import digest
from agent.supervision_literal_sources import VERSION, lookup_literal_source
from agent.supervision_planning_records import load
from tests.agent.test_supervision_claim_uses import native, prepare  # noqa: F401
from tests.agent.supervision_test_support import accept

FINAL_POLICY = {"version": "supervision.literal-final-use.v1", "consumer": "native:accepted-final", "enabled": True}


def configure(n, policy=FINAL_POLICY):
    config = json.loads((n.home / "config.yaml").read_text())
    grant = config["supervision"]["plugins"]["hermes-lcm"]["literal_sources"]
    grant["final_use"] = policy
    (n.home / "config.yaml").write_text(json.dumps(config))
    old = n.ctx.supervision._literal_source_registration
    return n.ctx.supervision.register_literal_source_owner(version=VERSION,
        engine=n.manager._context_engine, provider=old.provider)


def hydrate_and_commit(n, first, monkeypatch, mode="pack"):
    from agent.tool_executor import _finalize_tool_batch
    from tools.budget_config import BudgetConfig
    args = dict(question="What is the mass of asset A?", baseline_refs=[dict(exact_ref=first[2])],
                budgets=dict(max_retrieval_calls=0))
    name = "lcm_evidence_pack"
    if mode != "pack":
        name = "lcm_compile_evidence"
        args["mode"] = mode
        if mode == "proposal":
            args["proposal"] = dict(version="evidence-selector-v1", requested_facets=[], missing_facets=[],
                selections=[dict(claim_id="mass", facet="answer", exact_ref=first[2], quote=first[1])])
    with bind_subagent_parent(n.agent):
        result = n.engine.handle_tool_call(name, args)
    refs = json.loads(result)["source_propositions"]
    proposition = lookup_literal_source(n.rt, refs[0], n.facade._registration)
    assert proposition is not None
    messages = [dict(role="assistant", content=None, tool_calls=[dict(id="source", type="function",
        function=dict(name=name, arguments=json.dumps(args)))]),
        dict(role="tool", tool_call_id="source", name=name, content=result)]
    # Only unrelated output-budget and steering callbacks are substituted.
    monkeypatch.setattr("agent.tool_executor.get_active_env", lambda _: None)
    monkeypatch.setattr("agent.tool_executor.enforce_turn_budget", lambda *a, **k: None)
    monkeypatch.setattr(n.agent, "_apply_pending_steer_to_tool_results", lambda *a: None, raising=False)
    _finalize_tool_batch(n.agent, messages, "native-final-use", 1, BudgetConfig())
    assert n.rt.revision.evidence == proposition.revision.evidence + 1
    assert lookup_literal_source(n.rt, refs[0], n.facade._registration) is None
    return messages, proposition


def finish(n, text, messages):
    from agent.turn_final_response import finish_text_response
    # Minimal ordinary agent shell: only presentation/parser/persistence glue,
    # never stop gates, supervision, canonical records or source authorization.
    a = n.agent
    a.quiet_mode = True
    a.api_mode = "chat_completions"
    a.valid_tool_names = set()
    a._has_content_after_think_block = bool
    a._emit_pending_fallback_notice = lambda: None
    a._clear_status_buffer = lambda: None
    a._strip_think_blocks = lambda value: value
    a._build_assistant_message = lambda message, reason: dict(role="assistant", content=message.content, finish_reason=reason)
    a._flush_messages_to_session_db = lambda *args: None
    a._interim_content_was_streamed = lambda text: False
    return finish_text_response(a, assistant_message=SimpleNamespace(content=text, tool_calls=[]),
        response=None, finish_reason="stop", messages=messages, api_messages=copy.deepcopy(messages),
        conversation_history=[], api_call_count=1, user_message="Answer the mass question",
        active_system_prompt="unchanged system", final_response=None, _turn_exit_reason=None,
        _preflight_compression_blocked=False, codex_ack_continuations=0, truncated_response_parts=[],
        length_continue_retries=0, _pending_verification_response=None,
        _pending_verification_response_previewed=False)


@pytest.mark.parametrize("mode,elapsed", [("pack", False), ("auto", False), ("proposal", False), ("pack", True)])
def test_accepted_final_adopts_only_new_local_permission(native, monkeypatch, record_property, mode, elapsed):
    n = native
    owner = configure(n)
    first, _ = prepare(n)
    messages, proposition = hydrate_and_commit(n, first, monkeypatch, mode)
    assert load(n.rt)[0]["status"] == "claim_declared"
    if elapsed:
        # Real elapsed time, no fake clock or changed production deadline.
        time.sleep(max(0, proposition.deadline - n.rt.clock()) + .01)
        assert n.rt.clock() > proposition.deadline
    text = n.claim_file.read_text()
    native_reads = []
    original = n.engine._store._get_for_local_final
    def point_read(sid):
        assert not n.rt.lock._is_owned() and not owner.lock._is_owned() and not owner.provider.lock._is_owned()
        assert not n.agent._session_db._conn.in_transaction and not n.engine._store._conn.in_transaction
        native_reads.append(sid)
        return original(sid)
    monkeypatch.setattr(n.engine._store, "_get_for_local_final", point_read)
    verdict = finish(n, text, messages)
    assert verdict.action == "break" and verdict.final_response == text
    assert messages[-1]["content"] == text
    rows = load(n.rt)
    assert len(rows) == 1 and rows[0]["status"] == "adopted", rows
    adopted = rows[0]
    assert adopted["revision"] == 2 and adopted["emission"] == adopted["correction_relevance"] == "unknown"
    assert adopted["final_use"]["source"]["row_hash"] == proposition.record.row_hash
    assert adopted["final_use"]["final_hash"] == digest(text)
    assert adopted["final_use"]["final_span"] == [0, len(text)]
    assert adopted["final_use"]["claim_id"] == adopted["claim_id"]
    assert adopted["final_use"]["final_revision"]["evidence"] == n.rt.revision.evidence
    assert adopted["final_use"]["selected_invocation"] == proposition.invocation_id
    assert native_reads == [first[0]] and not n.calls
    assert not owner.final_use.selections and not owner.provider.final_selections and not owner.provider.final_permissions
    assert lookup_literal_source(n.rt, proposition.ref, n.facade._registration) is None
    # Canonical reload and duplicate final do not create a new source grant or replay.
    n.rt.dependencies.planning.clear()
    finish(n, text, messages)
    assert load(n.rt) == rows and native_reads == [first[0]]
    record_property("native_final_adoption", dict(mode=mode, after_original_deadline=elapsed,
        status=adopted["status"], final_hash=digest(text), source_hash=proposition.record.row_hash,
        old_publication="unavailable", emission="unknown", correction_relevance="unknown"))


@pytest.mark.parametrize("failure", [
    "disabled", "missing-policy", "old-policy", "malformed-policy", "wrong-consumer-policy", "unsupported-provider", "unselected", "wrong-row",
    "source-bytes", "source-number", "source-attribution", "source-deleted", "source-owner",
    "consumer-revoked", "owner-replaced", "lifecycle-aba", "session", "turn", "instruction", "cancel",
    "declaration", "quote", "rejection", "negation", "mixed", "repeated", "canonical-busy",
    "canonical-deleted", "canonical-reset", "predecessor-body", "writer-revoke", "writer-lifecycle",
    "writer-final", "source-callback", "final-callback", "engine", "store", "profile",
    "requirements", "control", "catalog", "retention", "writer-delete", "writer-reset",
    "post-read-lifecycle", "post-read-instruction", "source-db-missing",
])
def test_unavailable_or_nonpositive_final_never_adopts(native, monkeypatch, failure):
    n = native
    policy = copy.deepcopy(FINAL_POLICY)
    if failure == "disabled":
        policy["enabled"] = False
    elif failure == "missing-policy":
        policy = None
    elif failure == "wrong-consumer-policy":
        policy["consumer"] = "fixture-claims"
    elif failure == "old-policy":
        policy = {"version": VERSION, "recipients": ["native:accepted-final"]}
    elif failure == "malformed-policy":
        policy["extra"] = True
    owner = configure(n, policy)
    if failure == "unsupported-provider":
        from agent.supervision_literal_sources import LiteralSourceProviderV1
        monkeypatch.setattr(owner.provider, "capture_final_source",
            LiteralSourceProviderV1.capture_final_source.__get__(owner.provider))
    if failure == "source-callback":
        def broken(*args):
            raise RuntimeError("optional capture unavailable")
        monkeypatch.setattr(owner.final_use, "capture", broken)
    first, second = prepare(n)
    messages, _ = hydrate_and_commit(n, second if failure == "unselected" else first, monkeypatch)
    text = n.claim_file.read_text()
    before = load(n.rt)
    conn = None
    saved_store = n.engine._store
    try:
        if failure == "engine":
            n.agent.context_compressor = n.manager._context_engine
        elif failure == "store":
            n.engine._store = n.manager._context_engine._store
        elif failure in {"profile", "requirements", "control", "catalog"}:
            from dataclasses import replace
            field = {"control": "control_revision"}.get(failure, failure)
            value = "foreign-profile" if field == "profile" else getattr(n.rt.revision, field) + 1
            n.rt.revision = replace(n.rt.revision, **{field: value})
        elif failure in {"post-read-lifecycle", "post-read-instruction"}:
            reader = n.engine._store._get_for_local_final
            def read(sid):
                row = reader(sid)
                if failure == "post-read-lifecycle":
                    with owner.provider.lifecycle(n.engine, resume=True):
                        pass
                else:
                    accept(n.agent, "Change the answer now.", continuation=True)
                return row
            monkeypatch.setattr(n.engine._store, "_get_for_local_final", read)
        elif failure == "source-db-missing":
            n.engine._store.db_path.rename(n.home / "removed-lcm.db")
        if failure == "wrong-row":
            original = n.engine._store._get_for_local_final
            monkeypatch.setattr(n.engine._store, "_get_for_local_final", lambda _: original(second[0]))
        if failure in {"source-bytes", "source-number", "source-attribution", "source-deleted"}:
            sql, args = "UPDATE messages SET content=? WHERE store_id=?", (first[1] + " ", first[0])
            if failure == "source-number":
                sql, args = "UPDATE messages SET content=? WHERE store_id=?", (first[1].replace('"value": 3', '"value": 4'), first[0])
            elif failure == "source-attribution":
                sql, args = "UPDATE messages SET role='assistant' WHERE store_id=?", (first[0],)
            elif failure == "source-deleted":
                sql, args = "DELETE FROM messages WHERE store_id=?", (first[0],)
            n.engine._store._conn.execute(sql, args)
            n.engine._store._conn.commit()
        if failure == "source-owner":
            owner.close()
        elif failure == "consumer-revoked":
            owner.final_use.policy = None
        elif failure == "owner-replaced":
            configure(n)
        elif failure == "lifecycle-aba":
            n.engine.on_session_start("session-B", platform="cli")
            n.engine.on_session_start("session-A", platform="cli")
        elif failure == "session":
            n.agent.session_id = "session-B"
        elif failure == "turn":
            n.rt.finish_turn()
            n.rt.bind_turn()
        elif failure == "instruction":
            accept(n.agent, "Use a different answer.", continuation=True)
        elif failure == "cancel":
            n.rt.revoke()
        elif failure == "declaration":
            from tests.agent.test_supervision_native_relations import write
            write(n, n.plan, n.plan.read_text() + "\nChanged declaration artifact")
        prefixes = {"quote": "> ", "rejection": "I reject this source: ", "negation": "Not: ", "mixed": "Context: "}
        text = prefixes.get(failure, "") + text
        if failure == "repeated":
            text += "\n" + text
        if failure == "canonical-busy":
            conn = sqlite3.connect(n.agent._session_db.db_path, timeout=0)
            conn.execute("BEGIN IMMEDIATE")
        elif failure == "canonical-deleted":
            n.agent._session_db.delete_session(n.rt.session_id)
        elif failure == "retention":
            n.agent._session_db._conn.execute("DELETE FROM supervision_owner_records")
            n.agent._session_db._conn.commit()
        elif failure == "canonical-reset":
            n.agent._session_db._conn.execute("UPDATE supervision_owner_records SET status='stale'")
            n.agent._session_db._conn.commit()
        elif failure in {"predecessor-body", "writer-revoke", "writer-lifecycle", "writer-final", "writer-delete", "writer-reset"}:
            from agent import supervision_planning_records as records
            original = records.save
            def save(rt, rows, **kwargs):
                if any(row["status"] == "adopted" for row in rows):
                    if failure == "predecessor-body":
                        changed = {**before[0], "claim_id": "foreign-claim"}
                        n.agent._session_db._conn.execute("UPDATE supervision_owner_records SET body_json=?",
                            (json.dumps(changed, sort_keys=True, separators=(",", ":")),))
                        n.agent._session_db._conn.commit()
                    elif failure in {"writer-delete", "writer-reset"}:
                        sql = ("DELETE FROM supervision_owner_records" if failure == "writer-delete"
                               else "UPDATE supervision_owner_records SET status='stale'")
                        n.agent._session_db._conn.execute(sql)
                        n.agent._session_db._conn.commit()
                    elif failure == "writer-revoke":
                        owner.final_use.policy = None
                    elif failure == "writer-lifecycle":
                        with owner.provider.lifecycle(n.engine, resume=True):
                            pass
                    else:
                        permission = next(iter(owner.provider.final_permissions))
                        permission.phase.message["content"] = "Changed final candidate"
                return original(rt, rows, **kwargs)
            monkeypatch.setattr(records, "save", save)
        elif failure == "final-callback":
            def broken(*args):
                raise RuntimeError("optional final unavailable")
            monkeypatch.setattr(owner.provider, "authorize_final_source", broken)
        verdict = finish(n, text, messages)
        assert verdict.action == "break" and verdict.final_response == text
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
        n.agent.session_id = "session-A"
        n.agent.context_compressor = n.engine
        n.engine._store = saved_store
        if failure == "source-db-missing":
            assert not saved_store.db_path.exists()
            (n.home / "removed-lcm.db").rename(saved_store.db_path)
    assert all(row["status"] != "adopted" for row in load(n.rt))
    assert not n.calls


@pytest.mark.parametrize("native", ["coverage"], indirect=True)
def test_f20_continuation_has_priority_over_local_adoption(native, monkeypatch):
    n = native
    owner = configure(n)
    first, _ = prepare(n)
    messages, _ = hydrate_and_commit(n, first, monkeypatch)
    text = n.claim_file.read_text()
    verdict = finish(n, text, messages)
    assert verdict.action == "continue" and verdict.final_response is None
    assert n.rt.final_continuations == 1
    assert n.calls and all(k.startswith("F20/") for k in n.calls[-1]["questions"])
    assert load(n.rt)[0]["status"] == "claim_declared"
    assert owner.final_use.selections and not owner.provider.final_permissions
