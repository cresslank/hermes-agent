"""Real renderer/write boundaries; no callbacks manually acknowledged by tests."""
import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest

from agent.native_emission import (
    EmissionIdentity, NativeEmissionScope, MAX_OBSERVATIONS, MAX_PAYLOAD_BYTES,
    exact_emitted_span,
)


def scope(receipts, current=lambda: True):
    return NativeEmissionScope(EmissionIdentity("p", "l", "w", 1, "t"), receipts.append, current=current)


class Broken(io.StringIO):
    def __init__(self, where):
        super().__init__()
        self.where = where

    def write(self, text):
        if self.where == "write":
            raise BrokenPipeError()
        result = super().write(text)
        return result - 1 if self.where == "short" else result

    def flush(self):
        if self.where == "flush":
            raise OSError("closed")
        super().flush()


@pytest.mark.parametrize("where", ["write", "flush", "short"])
def test_json_swallowed_failure_is_not_emitted(monkeypatch, where):
    from hermes_cli.stream_json import StreamJsonEmitter
    receipts = []
    monkeypatch.setattr(sys, "stdout", Broken(where))
    with scope(receipts).activate():
        emitter = StreamJsonEmitter(session_id="s")
        assert emitter.emit_result({"final_response": "claim"}) == 0
    assert receipts == []


def test_json_exact_wire_and_identity(monkeypatch):
    from hermes_cli.stream_json import StreamJsonEmitter
    output, receipts = io.StringIO(), []
    monkeypatch.setattr(sys, "stdout", output)
    lease = scope(receipts)
    with lease.activate():
        emitter = StreamJsonEmitter(session_id="s")
        emitter.on_text_delta("claim é")
        emitter.emit_result({"final_response": "claim é"})
    assert b"".join(r.payload for r in receipts) == output.getvalue().encode()
    assert [json.loads(r.payload)["type"] for r in receipts] == ["system", "text", "result"]
    for r in receipts:
        assert r.payload_sha256 == hashlib.sha256(r.payload).hexdigest()
        assert r.end == len(r.payload) and r.start == 0
    receipt = receipts[-1]
    args = dict(identity=lease.identity, target=receipt.target, generation=receipt.transport_generation)
    assert exact_emitted_span(receipt, "claim é".encode(), **args) is not None
    assert exact_emitted_span(receipt, b"claim e", **args) is None
    assert exact_emitted_span(receipt, b"claim", **{**args, "generation": "old"}) is None
    assert exact_emitted_span(receipt, b"claim", **{**args, "target": (("stream", "elsewhere"),)}) is None
    with pytest.raises(Exception):
        receipt.end = 0


def test_exact_span_rejects_overlapping_occurrences(monkeypatch):
    from hermes_cli.stream_json import StreamJsonEmitter
    receipts = []
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    owner = scope(receipts)
    with owner.activate():
        StreamJsonEmitter().on_text_delta("aaa")
    receipt = receipts[-1]
    args = dict(identity=owner.identity, target=receipt.target, generation=receipt.transport_generation)
    assert exact_emitted_span(receipt, b"aaa", **args) is not None
    assert exact_emitted_span(receipt, b"aa", **args) is None


def test_json_bounds_closed_and_stale(monkeypatch):
    from hermes_cli.stream_json import StreamJsonEmitter
    receipts = []
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    lease = scope(receipts)
    with lease.activate():
        emitter = StreamJsonEmitter()
        emitter.on_text_delta("x" * (MAX_PAYLOAD_BYTES + 1))
        for _ in range(MAX_OBSERVATIONS + 2):
            emitter.on_text_delta("x")
    assert len(receipts) == MAX_OBSERVATIONS
    lease.close()
    with lease.activate():
        emitter.on_text_delta("closed")
    with scope(receipts, current=lambda: False).activate():
        emitter.on_text_delta("stale")
    assert len(receipts) == MAX_OBSERVATIONS


def test_panel_actual_prompt_toolkit_rendered_writes():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    output, receipts = io.StringIO(), []
    cli = SimpleNamespace(_stream_started=False, _stream_box_opened=False,
                          final_response_markdown="strip", _scrollback_box_width=lambda: 80)
    turn = SimpleNamespace(result={}, use_streaming_tts=False, box_opened=False)
    with create_app_session(output=PlainTextOutput(output)), scope(receipts).activate():
        CLIChatTurnMixin._chat_print_response_panel(cli, turn, "**Claim**: 7")
    assert receipts
    assert b"".join(r.payload for r in receipts) == output.getvalue().encode()
    assert b"Claim: 7" in output.getvalue().encode()
    assert b"**Claim**" not in output.getvalue().encode()


def test_stream_observes_rendered_lines_not_buffer_or_model_delta():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    from hermes_cli.cli_stream_mixin import CLIStreamMixin
    cli = CLIStreamMixin()
    cli.show_reasoning = False
    cli.show_timestamps = False
    cli.final_response_markdown = "strip"
    cli._scrollback_box_width = lambda: 80
    cli._reset_stream_state()
    output, receipts = io.StringIO(), []
    with create_app_session(output=PlainTextOutput(output)), scope(receipts).activate():
        cli._stream_delta("**Claim**")
        assert not any(b"Claim" in r.payload for r in receipts)
        cli._stream_delta(": 7\n")
        cli._flush_stream()
    assert b"".join(r.payload for r in receipts) == output.getvalue().encode()
    assert any(b"Claim: 7" in r.payload for r in receipts)
    assert not any(b"**Claim**" in r.payload for r in receipts)


def test_quiet_single_query_real_print_boundary(monkeypatch):
    from hermes_cli.cli_single_query import _run_quiet_single_query
    import hermes_cli.quiet_single_query as quiet
    result = {"final_response": "claim", "messages": []}
    cli = SimpleNamespace(agent=SimpleNamespace(run_conversation=lambda **kw: result, session_id="s"),
                          session_id="s", conversation_history=[])
    # No background work, inference, reports or process exit: the ordinary caller
    # still selects and prints its own result through the real stdout boundary.
    monkeypatch.setattr(quiet, "adopt_unanswered_turn", lambda *a: None)
    monkeypatch.setattr(quiet, "continue_quiet_notify_completions", lambda *a, **k: None)
    monkeypatch.setattr(quiet, "exit_single_query", lambda code: None)
    output, receipts = io.StringIO(), []
    monkeypatch.setattr(sys, "stdout", output)
    with scope(receipts).activate():
        _run_quiet_single_query(cli, "question")
    assert output.getvalue() == "claim\n"
    assert [r.payload for r in receipts] == [b"claim\n"]


@pytest.mark.parametrize("closed", [False, True])
def test_cprint_queue_admission_is_not_emission(monkeypatch, closed):
    from contextvars import copy_context
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    import prompt_toolkit.application as application
    from hermes_cli.cli_render import _cprint
    queued, receipts = [], []
    class Loop:
        def call_soon_threadsafe(self, callback):
            context = copy_context()  # asyncio's native submission semantics
            queued.append(lambda: context.run(callback))
    monkeypatch.setattr(application, "get_app_or_none", lambda: SimpleNamespace(_is_running=True, loop=Loop()))
    monkeypatch.setattr(application, "run_in_terminal", lambda callback: callback())
    output = io.StringIO()
    owner = scope(receipts)
    with create_app_session(output=PlainTextOutput(output)), owner.activate():
        _cprint("queued claim")
        assert not receipts and output.getvalue() == ""
        if closed:
            owner.close()
        queued.pop()()
    assert "queued claim" in output.getvalue()
    assert len(receipts) == (0 if closed else 1)


@pytest.mark.parametrize("terminal", [False, True])
def test_optional_observation_preserves_actual_rendered_bytes(terminal):
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.output.plain_text import PlainTextOutput
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    from hermes_cli.cli_render import _cprint
    output, observed, receipts = io.StringIO(), io.StringIO(), []
    def renderer(stream):
        return Vt100_Output(stream, lambda: Size(24, 80), term="xterm") if terminal else PlainTextOutput(stream)
    with create_app_session(output=renderer(output)):
        _cprint("\033[31mclaim é\033[0m")
    with create_app_session(output=renderer(observed)), scope(receipts).activate():
        _cprint("\033[31mclaim é\033[0m")
    assert observed.getvalue() == output.getvalue()
    assert b"".join(r.payload for r in receipts) == observed.getvalue().encode()


def test_native_installed_owner_survives_finish_but_not_reset(monkeypatch):
    from agent.native_emission import install_for_runtime
    from agent.supervision_policy import SupervisionRuntime
    from hermes_cli.stream_json import StreamJsonEmitter
    class Agent:
        session_id, _current_turn_id = "native-cli", "turn"
    agent = Agent()
    runtime = agent._supervision_runtime = SupervisionRuntime(agent, "p", "l")
    output, receipts = io.StringIO(), []
    monkeypatch.setattr(sys, "stdout", output)
    emitter = StreamJsonEmitter().attach(agent)
    install_for_runtime(runtime, receipts.append)
    runtime.finish_turn()  # real normal ordering: final print is AFTER agent return
    emitter.emit_result({"final_response": "claim"})
    assert len(receipts) == 1
    runtime.revoke()  # real native reset/revocation, not a forged callback predicate
    emitter.emit_result({"final_response": "stale"})
    assert len(receipts) == 1
    assert '"text": "stale"' in output.getvalue()  # observation does not suppress delivery


def test_headless_public_chat_return_and_canonical_transcript_do_not_emit(tmp_path):
    from run_agent import AIAgent
    from hermes_state import SessionDB
    agent = AIAgent.__new__(AIAgent)
    # Stub inference only: exercise the real public return and canonical message
    # persistence, neither of which is a native presentation sink.
    agent.run_conversation = lambda *a, **k: {"final_response": "claim"}
    receipts = []
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="cli")
        with scope(receipts).activate():
            assert agent.chat("question") == "claim"
            db.append_message("s", "assistant", "claim")
        assert db.get_messages("s")[-1]["content"] == "claim"
        assert not receipts
    finally:
        db.close()


def test_no_scope_preserves_json_and_observer_failure_cannot_repeat_output(monkeypatch):
    from hermes_cli.stream_json import StreamJsonEmitter
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    emitter = StreamJsonEmitter()
    lease = NativeEmissionScope(EmissionIdentity("p", "l", "w", 1, "t"),
                               lambda r: (_ for _ in ()).throw(RuntimeError()), current=lambda: True)
    with lease.activate():
        emitter.emit_result({"final_response": "once"})
    assert output.getvalue().count('"text": "once"') == 1
