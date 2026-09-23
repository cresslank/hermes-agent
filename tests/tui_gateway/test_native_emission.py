"""Normal server emit -> stdio/WS/fanout; loopback sockets, no external peers."""
import asyncio
from contextlib import asynccontextmanager
import io
import json
import threading

import pytest

from agent.native_emission import EmissionIdentity, NativeEmissionScope
from tui_gateway import server
from tui_gateway.transport import FanoutTransport, StdioTransport
from tui_gateway.ws import WSTransport


def lease(receipts):
    return NativeEmissionScope(EmissionIdentity("p", "l", "w", 1, "t"), receipts.append, current=lambda: True)


def frame(kind="message.complete", text="claim"):
    return {"jsonrpc": "2.0", "method": "event", "params": {
        "type": kind, "session_id": "s", "payload": {"text": text}, "seq": 7}}


@asynccontextmanager
async def socket_peer():
    seen = []
    handlers = set()
    async def receive(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while line := await reader.readline():
                seen.append(line.rstrip(b"\n"))
                writer.write(b"ok\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
    listener = await asyncio.start_server(receive, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
    class Socket:
        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.release.set()
            self.fail = False
        async def send_text(self, text):
            self.entered.set()
            await self.release.wait()
            if self.fail:
                raise OSError("fixture peer gone")
            writer.write(text.encode() + b"\n")
            await writer.drain()
            assert await reader.readline() == b"ok\n"
    sock = Socket()
    try:
        yield sock, seen
    finally:
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


def test_server_emit_stdio_and_muted_emit_false(monkeypatch):
    output, receipts = io.StringIO(), []
    transport = StdioTransport(lambda: output, threading.Lock())
    from agent.native_emission import install_for_runtime
    from agent.supervision_policy import SupervisionRuntime
    class Agent:
        session_id, _current_turn_id = "native-tui", "turn"
    agent = Agent()
    runtime = agent._supervision_runtime = SupervisionRuntime(agent, "p", "l")
    install_for_runtime(runtime, receipts.append)
    monkeypatch.setitem(server._sessions, "s", {"transport": transport, "agent": agent})
    assert server._emit("message.complete", "s", {"text": "claim"})
    assert len(receipts) == 1
    assert receipts[0].payload == output.getvalue().encode()
    assert json.loads(receipts[0].payload)["params"]["payload"]["text"] == "claim"
    monkeypatch.setattr("agent.notification_presentation.event_presentation_muted", lambda *a: True)
    with lease(receipts).activate():
        assert server._emit("message.complete", "s", {"text": "suppressed"}) is False
    assert len(receipts) == 1


@pytest.mark.parametrize("failure", ["write", "flush", "short", "disabled_flush"])
def test_stdio_negative_completion(monkeypatch, failure):
    class Output(io.StringIO):
        def write(self, text):
            if failure == "write":
                raise BrokenPipeError()
            count = super().write(text)
            return count - 1 if failure == "short" else count
        def flush(self):
            if failure == "flush":
                raise BrokenPipeError()
    output, receipts = Output(), []
    monkeypatch.setattr("tui_gateway.transport._DISABLE_FLUSH", failure == "disabled_flush")
    with lease(receipts).activate():
        StdioTransport(lambda: output, threading.Lock()).write(frame())
    assert not receipts


@pytest.mark.parametrize("unsupported", ["queue", "serialization"])
def test_stdio_unknown_or_failed_frame_stays_unobserved(unsupported):
    output, receipts = io.StringIO(), []
    class Queue:
        def write(self, text):
            output.write(text)  # only enqueue acceptance, not a physical sink
            return len(text)
        def flush(self):
            pass
    stream = Queue() if unsupported == "queue" else output
    message = frame()
    if unsupported == "serialization":
        message["params"]["payload"]["text"] = object()
    with lease(receipts).activate():
        assert StdioTransport(lambda: stream, threading.Lock()).write(message)
    assert output.getvalue()
    assert not receipts


@pytest.mark.asyncio
async def test_ws_deadline_cancel_suppressed_by_socket_is_not_emission():
    from tui_gateway.ws import _WS_SEND_DEADLINE_S
    receipts = []
    async with socket_peer() as (sock, seen):
        native_send = sock.send_text
        cancelled = asyncio.Event()
        async def shielded_send(text):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
            await native_send(text)
        sock.send_text = shielded_send
        transport = WSTransport(sock, asyncio.get_running_loop(), peer="late-peer")
        try:
            with lease(receipts).activate():
                # Native deadline, real clock: wait_for may return normally when
                # send_text suppresses cancellation, but that is not timely ACK.
                await asyncio.wait_for(transport.write_async(frame()), _WS_SEND_DEADLINE_S + 5)
            assert cancelled.is_set() and seen
            assert not receipts
        finally:
            transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "close", "cancel", "reset"])
async def test_ws_enqueued_and_slow_send_are_not_receipts(ending):
    receipts = []
    scope = lease(receipts)
    async with socket_peer() as (sock, seen):
        sock.release.clear()
        transport = WSTransport(sock, asyncio.get_running_loop(), peer="owned-peer")
        with scope.activate():
            assert transport.write(frame()) is True  # scheduled, not emission
        await asyncio.wait_for(sock.entered.wait(), 2)
        assert not receipts
        if ending == "failure":
            sock.fail = True
        if ending == "close":
            transport.close()
        if ending == "reset":
            scope.close()
        if ending == "cancel":
            # Cancellation of an awaited send (not an enqueue bool).
            transport.close()
            second = WSTransport(sock, asyncio.get_running_loop(), peer="cancel-peer")
            with scope.activate():
                task = asyncio.create_task(second.write_async(frame()))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        sock.release.set()
        # Join the native send lock without changing any transport deadlines.
        await transport.write_async({"id": "barrier", "result": True})
        assert len(receipts) == (1 if ending == "success" else 0)
        if receipts:
            assert receipts[0].payload in seen
        transport.close()


@pytest.mark.asyncio
async def test_buffered_delta_failure_and_reconnect_generations():
    receipts = []
    async with socket_peer() as (sock, seen):
        first = WSTransport(sock, asyncio.get_running_loop(), peer="same-peer")
        with lease(receipts).activate():
            assert first.write(frame("message.delta", "buffered"))
            assert not receipts
            assert await first.write_async(frame())
        assert len(receipts) == 2
        old = receipts[-1].transport_generation
        first.close()
        second = WSTransport(sock, asyncio.get_running_loop(), peer="same-peer")
        with lease(receipts).activate():
            assert await second.write_async(frame())
        assert receipts[-1].transport_generation != old
        sock.fail = True
        with lease(receipts).activate():
            assert second.write(frame("message.delta", "will fail"))
            assert not await second.write_async(frame())
        assert len(receipts) == 3
        second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("detach", [False, True])
async def test_normal_emit_fanout_only_after_per_peer_send(monkeypatch, detach):
    receipts = []
    async with socket_peer() as (sock, seen):
        sock.release.clear()
        transport = WSTransport(sock, asyncio.get_running_loop(), peer="subscriber")
        fanout = FanoutTransport(transport)
        monkeypatch.setitem(server._sessions, "s", {"transport": fanout})
        with lease(receipts).activate():
            assert server._emit("message.complete", "s", {"text": "claim"})
        await asyncio.wait_for(sock.entered.wait(), 2)
        assert not receipts
        if detach:
            assert fanout.detach(transport)
            assert fanout.attach(transport)  # an old generation cannot certify reconnect
        sock.release.set()
        await transport.write_async({"id": "barrier", "result": True})
        assert len(receipts) == (0 if detach else 1)
        if receipts:
            assert receipts[0].payload in seen
        fanout.close()
        transport.close()


@pytest.mark.asyncio
async def test_worker_slow_write_true_then_socket_failure_is_not_emission():
    receipts = []
    async with socket_peer() as (sock, seen):
        sock.release.clear()
        transport = WSTransport(sock, asyncio.get_running_loop(), peer="slow-peer")
        with lease(receipts).activate():
            task = asyncio.create_task(asyncio.to_thread(transport.write, frame()))
        await asyncio.wait_for(sock.entered.wait(), 2)
        assert not receipts
        # Exercise the unchanged native ten-second worker wait. No clock,
        # threshold or deadline monkeypatch: True here means still in flight.
        assert await task is True
        assert not receipts
        sock.fail = True
        sock.release.set()
        assert not await transport.write_async({"id": "barrier", "result": True})
        assert not receipts
        transport.close()


@pytest.mark.asyncio
async def test_fanout_native_backlog_overflow_retires_pending_observations():
    receipts = []
    async with socket_peer() as (sock, seen):
        sock.release.clear()
        transport = WSTransport(sock, asyncio.get_running_loop(), peer="backlogged-peer")
        fanout = FanoutTransport(transport)
        with lease(receipts).activate():
            assert fanout.write(frame())
            await asyncio.wait_for(sock.entered.wait(), 2)
            for _ in range(FanoutTransport._MAX_PENDING_FRAMES):
                assert fanout.write(frame())
            assert not fanout.write(frame())
        assert not fanout.contains(transport) and not receipts
        sock.release.set()
        await transport.write_async({"id": "barrier", "result": True})
        assert not receipts
        fanout.close()
        transport.close()


def test_transcript_replay_is_not_fresh_emission():
    output, receipts = io.StringIO(), []
    with lease(receipts).activate():
        result = {"final_response": "claim", "messages": [{"role": "assistant", "content": "claim"}]}
        StdioTransport(lambda: output, threading.Lock()).write({"id": "history", "result": result})
    assert not receipts
