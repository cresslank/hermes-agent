"""Ordinary source/adoption/final/sink chain, using native writes and canonical DB.

No receipt setter, observer installation, fake grant, model RPC or live network.
"""
import asyncio
import copy
import json
import os
import sqlite3
import threading
from types import MethodType, SimpleNamespace

import pytest

from agent.supervision_planning_records import load
from tests.agent.test_supervision_claim_uses import native, prepare  # noqa: F401
from tests.agent.test_supervision_final_use import configure, hydrate_and_commit, finish


def setup(n, monkeypatch, mode="pack", policy=True):
    from agent.turn_context import _bind_turn_identity
    from agent.stream_delivery import StreamDeliveryMixin
    from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent
    a = n.agent
    defaults = FakeAgent()
    for key, value in vars(defaults).items():
        if not hasattr(a, key):
            setattr(a, key, value)
    for name in ("_save_trajectory", "_cleanup_task_resources", "_drop_trailing_empty_response_scaffolding",
                 "_persist_session", "_apply_persist_user_message_override", "_sync_external_memory_for_turn"):
        setattr(a, name, MethodType(getattr(FakeAgent, name), a))
    a.iteration_budget = defaults.iteration_budget
    a._response_was_previewed = False
    a._file_mutation_verifier_enabled = lambda: False
    a._turn_completion_explainer_enabled = lambda: False
    a._reset_stream_delivery_tracking = MethodType(StreamDeliveryMixin._reset_stream_delivery_tracking, a)
    a._deliver_to_stream_callbacks = MethodType(StreamDeliveryMixin._deliver_to_stream_callbacks, a)
    a._call_quietly = StreamDeliveryMixin._call_quietly
    for name in ("_ensure_stream_writer_state", "_claim_stream_writer"):
        setattr(a, name, MethodType(getattr(StreamDeliveryMixin, name), a))
    a.stream_delta_callback = None
    _bind_turn_identity(a, "original-fixture", None, None, None, None)
    configure(n)
    config = json.loads((n.home / "config.yaml").read_text())
    if policy:
        config["supervision"]["original_output"] = {"version": "supervision.original-output.v1", "enabled": True}
    (n.home / "config.yaml").write_text(json.dumps(config))
    first, _ = prepare(n)
    messages, proposition = hydrate_and_commit(n, first, monkeypatch, mode)
    request(n, messages, monkeypatch)
    return messages, proposition


def request(n, messages, monkeypatch):
    from agent.turn_api_request import build_api_request
    a = n.agent
    a.api_mode, a.tools, a._empty_content_retries = "chat_completions", [], 0
    a._image_rejecting_models = set()
    a._force_ascii_payload = False
    a._reapply_reasoning_echo_for_provider = lambda _: None
    a._build_api_kwargs = lambda messages, **_: {"messages": copy.deepcopy(messages), "model": "fixture"}
    monkeypatch.setattr("agent.conversation_loop._redecorate_prompt_cache_for_provider",
        lambda a, messages, **kw: (messages, kw["moa_prepared"], kw["tools_for_api"]))
    built = build_api_request(a, api_messages=copy.deepcopy(messages), _moa_prepared_request=None,
        tools_for_api=[], system_message="unchanged", messages=messages,
        original_user_message="Answer the mass question", approx_tokens=0, total_chars=0,
        retry_count=0, api_call_count=1, api_request_id="request-original", api_start_time=0,
        effective_task_id=a._current_task_id, turn_id=a._current_turn_id)
    assert built.action == "fallthrough"
    a._claim_stream_writer()


def finalize(n, messages, monkeypatch, text=None, hook=None):
    from agent.turn_finalizer import finalize_turn
    text = n.claim_file.read_text() if text is None else text
    # The current native boundary transforms before its first durable flush.
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook",
        lambda event, **kw: [hook(kw["response_text"])] if hook and event == "transform_llm_output" else [])
    verdict = finish(n, text, messages)
    assert verdict.action == "break"
    result = finalize_turn(n.agent, final_response=verdict.final_response, api_call_count=1,
        interrupted=False, failed=False, messages=messages, conversation_history=[],
        effective_task_id=n.agent._current_task_id, turn_id=n.agent._current_turn_id,
        user_message="Answer the mass question", original_user_message="Answer the mass question",
        _should_review_memory=False, _turn_exit_reason="text_response(final)")
    n.rt.finish_turn()  # same ordinary sealing before surface callbacks
    return result


def original_rows(n):
    return [row for row in load(n.rt) if row.get("dimension") == "original_output"]


def cli_sink(n, result, monkeypatch, path, *, vt=False, width=80, writes=None, mode="strip", fail_after=None):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import DummyInput
    monkeypatch.setattr("shutil.get_terminal_size", lambda *a, **k: os.terminal_size((width, 24)))
    owner = SimpleNamespace(agent=n.agent, _stream_started=False, _stream_box_opened=False,
        final_response_markdown=mode, _scrollback_box_width=lambda: width)
    turn = SimpleNamespace(result=result, use_streaming_tts=False, box_opened=False)
    import io
    class Capture(io.FileIO):
        def write(self, data):
            if fail_after is not None and len(writes) >= fail_after:
                raise BrokenPipeError("fixture partial native output")
            count = super().write(data)
            if writes is not None:
                writes.append(bytes(data[:count]))
            return count
    with io.TextIOWrapper(Capture(path, "w"), encoding="utf-8", write_through=True) as stream:
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=width)) if vt else PlainTextOutput(stream)
        with create_app_session(input=DummyInput(), output=output):
            CLIChatTurnMixin._chat_print_response_panel(owner, turn, result["final_response"])
    return path.read_bytes()


def tui_sink(n, text, monkeypatch, path, *, kind="message.complete", extra=None):
    from tui_gateway import server
    from tui_gateway.transport import StdioTransport
    with path.open("w", encoding="utf-8") as stream:
        transport = StdioTransport(lambda: stream, threading.Lock())
        session = {"transport": transport, "agent": n.agent, "history_lock": threading.RLock()}
        monkeypatch.setitem(server._sessions, "original-ui", session)
        payload = {"text": text}
        if kind == "message.complete":
            st = SimpleNamespace(result={"final_response": text}, agent=n.agent,
                terminal_callback=None, receipt_committed=False)
            payload, raw, status = server._complete_turn_payload(session, st, None, 2048)
            assert raw == text and status == "complete"
        assert server._emit(kind, "original-ui", {**payload, **(extra or {})})
    return path.read_bytes()


async def telegram_sink(n, text, *, failure=None):
    from gateway.config import PlatformConfig, Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from plugins.platforms.telegram.adapter import TelegramAdapter
    calls = []
    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            if failure == "failure":
                raise ValueError("message too long")
            if failure == "bot-replaced":
                adapter._bot = Bot()
            if failure == "cancel":
                raise asyncio.CancelledError()
            return SimpleNamespace(message_id=19)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="offline-fixture", typing_indicator=False))
    adapter._bot = Bot()
    if failure == "changed-codec":
        formatter = adapter.format_message
        adapter.format_message = lambda content: formatter(content) + "changed"
    if failure == "split":
        adapter.MAX_MESSAGE_LENGTH = 128
    if failure == "topic":
        native_send = adapter._send_observed_text
        async def changed_send(**kwargs):
            kwargs["message_thread_id"] = 18
            return await native_send(**kwargs)
        adapter._send_observed_text = changed_send
    adapter._session_store = SimpleNamespace(peek_session_id=lambda _: n.agent.session_id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-4242", chat_type="group", thread_id="17")
    event = MessageEvent(text="question", source=source, message_id="8")
    from gateway.run import _sanitize_gateway_final_response, _normalize_empty_agent_response
    text = _sanitize_gateway_final_response(Platform.TELEGRAM,
        _normalize_empty_agent_response({"final_response": text}, text))
    extracted = await adapter._extract_response_content(text, event, "original-session", is_ephemeral_response=False)
    assert not extracted.images and not extracted.media_files and not extracted.local_files
    result, used = await adapter.send_final_ledgered(event, "original-session", extracted.text_content,
        {"thread_id": "17", "notify": True}, reply_to=None)
    assert result.success is (failure != "failure") and used is adapter
    return calls


@pytest.mark.parametrize("surface,mode,width,display", [
    ("cli", "pack", 80, "strip"), ("vt100", "auto", 80, "strip"),
    ("cli", "pack", 2048, "raw"), ("telegram", "proposal", 80, "strip"), ("tui", "pack", 80, "strip")])
def test_native_original_chain(native, monkeypatch, tmp_path, record_property, surface, mode, width, display):
    n = native
    messages, proposition = setup(n, monkeypatch, mode)
    rows = original_rows(n)
    assert len(rows) == 1 and rows[0]["observation"] is None
    result = finalize(n, messages, monkeypatch)
    assert result["completed"] and result["final_response"] == n.claim_file.read_text()
    # Post-turn callback is genuinely not the execution/tool owner thread.
    errors, payloads, writes = [], [], []
    def output():
        try:
            if surface in {"cli", "vt100"}:
                payloads.append(cli_sink(n, result, monkeypatch, tmp_path / "sink", vt=surface == "vt100", writes=writes, width=width, mode=display))
            elif surface == "telegram":
                payloads.extend(asyncio.run(telegram_sink(n, result["final_response"])))
            else:
                payloads.append(tui_sink(n, result["final_response"], monkeypatch, tmp_path / "sink"))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=output)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not errors, errors
    rows = original_rows(n)
    assert len(rows) == 1 and rows[0]["status"] == "original_emitted", rows
    row = rows[0]
    adopted = [r for r in load(n.rt) if r["status"] == "adopted"]
    assert len(adopted) == 1 and row["adoption"]["record_id"] == adopted[0]["id"]
    assert adopted[0]["emission"] == "unknown"  # use decision is not a delivery flag
    assert row["source"]["row_hash"] == proposition.record.row_hash
    assert row["observation"]["transport_generation"] and row["accepted_final"]
    import hashlib
    written = ([payloads[0]["text"].encode()] if surface == "telegram" else
        writes if surface in {"cli", "vt100"} else payloads)
    if writes:
        assert b"".join(writes) == payloads[0]  # actual descriptor readback
    observations = row["observation"].get("segments", [row["observation"]])
    assert all(item["payload_hash"] in {hashlib.sha256(part).hexdigest() for part in written}
        for item in observations)
    if surface in {"cli", "vt100"} and width == 80:
        assert len(observations) > 1  # ordinary 80-column, default strip renderer
    assert proposition.record.source_bytes.decode() not in json.dumps(row)
    n.rt.dependencies.planning.clear()
    assert original_rows(n) == rows
    assert not n.calls and not n.ctx.supervision._literal_source_registration.final_use.selections
    assert not n.ctx.supervision._literal_source_registration.provider.final_permissions
    record_property("original_output", dict(surface=surface, row=row,
        sink=[item.decode() if isinstance(item, bytes) else item for item in payloads]))


@pytest.mark.parametrize("failure", ["disabled", "hook", "lost-origin", "markdown", "rendered", "busy", "deleted", "reset", "revision", "predecessor-body", "callback-fault"])
def test_original_output_unknown_preserves_delivery(native, monkeypatch, tmp_path, failure):
    n = native
    messages, _ = setup(n, monkeypatch, policy=failure != "disabled")
    result = finalize(n, messages, monkeypatch, hook=(lambda text: "Not: " + text) if failure == "hook" else None)
    if failure == "lost-origin":
        result["final_response"] = str(result["final_response"])
    conn = None
    if failure == "busy":
        conn = sqlite3.connect(n.agent._session_db.db_path, timeout=0)
        conn.execute("BEGIN IMMEDIATE")
    elif failure == "deleted":
        n.agent._session_db.delete_session(n.agent.session_id)
    elif failure == "reset":
        n.agent._session_db._conn.execute("UPDATE supervision_owner_records SET status='stale'")
        n.agent._session_db._conn.commit()
    elif failure == "revision":
        n.rt.revoke()
    elif failure == "predecessor-body":
        row = original_rows(n)[0]
        row["render_hash"] = "changed"
        n.agent._session_db._conn.execute("UPDATE supervision_owner_records SET body_json=? WHERE record_id=?",
            (json.dumps(row, sort_keys=True, separators=(",", ":")), row["id"]))
        n.agent._session_db._conn.commit()
    elif failure == "callback-fault":
        def broken(*a, **k):
            raise RuntimeError("fixture storage unavailable")
        monkeypatch.setattr("agent.supervision_planning_records._save", broken)
    try:
        if failure == "markdown":
            raw = cli_sink(n, result, monkeypatch, tmp_path / "sink", mode="render")
        else:
            raw = tui_sink(n, result["final_response"], monkeypatch, tmp_path / "sink",
                extra={"rendered": "alternate representation"} if failure == "rendered" else None)
        assert raw
    finally:
        if conn:
            conn.rollback()
            conn.close()
    assert not any(row["observation"] for row in original_rows(n))


@pytest.mark.parametrize("vt", [False, True])
def test_wrapped_cli_partial_ack_never_certifies(native, monkeypatch, tmp_path, vt):
    messages, _ = setup(native, monkeypatch)
    result = finalize(native, messages, monkeypatch)
    writes = []
    raw = cli_sink(native, result, monkeypatch, tmp_path / "partial", vt=vt,
        writes=writes, fail_after=4)
    assert raw and len(writes) == 4
    assert original_rows(native)[0]["observation"] is None


@pytest.mark.parametrize("ending", ["accept", "hook", "replacement", "retry"])
def test_early_output_is_independent_of_adoption(native, monkeypatch, tmp_path, ending):
    n = native
    messages, _ = setup(n, monkeypatch)
    n.agent._stream_callback = lambda text: tui_sink(n, text, monkeypatch, tmp_path / "early", kind="message.delta")
    n.agent._deliver_to_stream_callbacks(n.claim_file.read_text())
    early = original_rows(n)[0]
    assert early["status"] == "original_observed" and early["adoption"] is None and early["accepted_final"] is None
    assert [r for r in load(n.rt) if r["status"] == "claim_declared"]
    if ending == "retry":
        request(n, messages, monkeypatch)
    result = finalize(n, messages, monkeypatch,
        text="Replacement final" if ending == "replacement" else None,
        hook=(lambda text: "Not: " + text) if ending == "hook" else None)
    assert result["completed"]
    saved = [r for r in original_rows(n) if r["id"] == early["id"]][0]
    assert saved["observation"] == early["observation"]
    assert (saved["status"] == "original_emitted") == (ending == "accept")
    # A plugin rewrite now precedes final-use: early emission is still retained,
    # but a changed final can no longer adopt the raw model candidate.
    assert (saved["adoption"] is not None) == (ending == "accept")


@pytest.mark.parametrize("failure", ["short", "write", "flush", "getter", "detached", "queued", "changed", "oversize", "cancel", "writer-delete", "writer-reset", "writer-sink", "lock-busy"])
def test_native_stdio_uncertain_completion_never_certifies(native, monkeypatch, tmp_path, failure):
    import io
    from tui_gateway import server
    from tui_gateway.transport import StdioTransport
    n = native
    messages, _ = setup(n, monkeypatch)
    result = finalize(n, messages, monkeypatch)
    held, release = threading.Event(), threading.Event()
    def hold_runtime():
        with n.rt.lock:
            held.set()
            release.wait(5)
    locker = None
    if failure == "lock-busy":
        locker = threading.Thread(target=hold_runtime)
        locker.start()
        assert held.wait(2)
    original = original_rows(n)[0]
    try:
        with (tmp_path / "sink").open("w", encoding="utf-8") as file:
            class Stream(io.TextIOBase):
                def fileno(self):
                    return file.fileno()
                def write(self, text):
                    if failure == "write":
                        raise BrokenPipeError()
                    count = file.write(text)
                    if failure == "getter":
                        active[0] = None
                    elif failure == "detached":
                        session["transport"] = None
                    elif failure == "cancel":
                        n.rt.revoke()
                    return count - 1 if failure == "short" else count
                def flush(self):
                    if failure == "flush":
                        raise BrokenPipeError()
                    file.flush()
            stream = Stream()
            active = [stream]
            transport = StdioTransport(lambda: active[0], threading.Lock())
            if failure == "queued":
                transport = SimpleNamespace(write=lambda obj: True)
            session = {"agent": n.agent, "transport": transport}
            monkeypatch.setitem(server._sessions, "uncertain-ui", session)
            if failure.startswith("writer-"):
                from agent import supervision_planning_records as records
                save = records.save_original
                def changed(owner, row, **kwargs):
                    if row.get("observation"):
                        if failure == "writer-sink":
                            session["transport"] = None
                        else:
                            sql = ("DELETE FROM supervision_owner_records WHERE record_id=?" if failure == "writer-delete"
                                   else "UPDATE supervision_owner_records SET status='stale' WHERE record_id=?")
                            n.agent._session_db._conn.execute(sql, (original["id"],))
                            n.agent._session_db._conn.commit()
                    return save(owner, row, **kwargs)
                monkeypatch.setattr(records, "save_original", changed)
            payload = {"text": result["final_response"]}
            if failure == "changed":
                payload["text"] = "Not: " + payload["text"]
            if failure == "oversize":
                payload["rendered"] = "x" * 20000
            delivered = server._emit("message.complete", "uncertain-ui", payload)
            assert delivered is (failure not in {"write", "flush"})
    finally:
        release.set()
        if locker:
            locker.join(2)
            assert not locker.is_alive()
    assert not any(row["observation"] for row in original_rows(n))


@pytest.mark.parametrize("native", ["coverage"], indirect=True)
def test_f20_early_observation_is_not_an_adopted_final(native, monkeypatch, tmp_path):
    n = native
    messages, _ = setup(n, monkeypatch)
    n.agent._stream_callback = lambda text: tui_sink(n, text, monkeypatch, tmp_path / "early", kind="message.delta")
    n.agent._deliver_to_stream_callbacks(n.claim_file.read_text())
    before = original_rows(n)[0]
    verdict = finish(n, n.claim_file.read_text(), messages)
    assert verdict.action == "continue" and n.rt.final_continuations == 1
    assert n.calls and all(k.startswith("F20/") for k in n.calls[-1]["questions"])
    after = original_rows(n)[0]
    assert after == before and after["status"] == "original_observed" and after["adoption"] is None


@pytest.mark.parametrize("failure", ["extra-stream", "superseded-writer", "claim-change", "origin-copy"])
def test_native_origin_does_not_retroactively_adopt_matching_text(native, monkeypatch, tmp_path, failure):
    n = native
    messages, _ = setup(n, monkeypatch)
    n.agent._stream_callback = lambda text: tui_sink(n, copy.deepcopy(text) if failure == "origin-copy" else text,
        monkeypatch, tmp_path / "early", kind="message.delta")
    n.agent._deliver_to_stream_callbacks(n.claim_file.read_text())
    if failure == "extra-stream":
        n.agent._deliver_to_stream_callbacks(" actually, reject that record")
    elif failure == "superseded-writer":
        n.agent._claim_stream_writer()
    elif failure == "claim-change":
        from tests.agent.test_supervision_native_relations import write
        write(n, n.plan, n.plan.read_text() + "\nChanged declaration")
    finalize(n, messages, monkeypatch)
    assert not any(row["status"] == "original_emitted" for row in original_rows(n))


@pytest.mark.parametrize("failure", ["failure", "bot-replaced", "cancel", "changed-codec", "split", "topic"])
def test_native_telegram_uncertain_or_unmapped_output(native, monkeypatch, failure):
    n = native
    messages, _ = setup(n, monkeypatch)
    result = finalize(n, messages, monkeypatch)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(telegram_sink(n, result["final_response"], failure=failure))
    else:
        assert asyncio.run(telegram_sink(n, result["final_response"], failure=failure))
    assert original_rows(n)[0]["observation"] is None
