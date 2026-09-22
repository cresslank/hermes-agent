"""Real Gateway callers and Telegram formatting/splitting/edit owners over loopback."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent.native_emission import EmissionIdentity, NativeEmissionScope, complete_exact_batch
from gateway.config import PlatformConfig, Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class Flood(Exception):
    retry_after = 120.0


def lease(receipts):
    return NativeEmissionScope(EmissionIdentity("p", "l", "w", 1, "t"), receipts.append, current=lambda: True)


@asynccontextmanager
async def bot_peer(mode="ok"):
    requests, landed, handlers = [], [], set()
    async def receive(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            while line := await reader.readline():
                call = json.loads(line)
                requests.append(call)
                error = None
                if mode == "partial" and len(requests) == 2:
                    error = "flood"
                if mode == "parse" and call.get("parse_mode"):
                    error = "parse error"
                if mode == "failure":
                    error = "message too long"
                if not error:
                    landed.append(call)
                writer.write((json.dumps({"error": error, "message_id": len(requests)}) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())
    listener = await asyncio.start_server(receive, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
    class Bot:
        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.release.set()
            self.shield_cancel = False
            self.cancelled = asyncio.Event()
        async def invoke(self, method, kwargs):
            self.task = asyncio.current_task()
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.shield_cancel:
                    raise
                self.cancelled.set()
                await self.release.wait()
            writer.write((json.dumps({"method": method, **kwargs}, default=str) + "\n").encode())
            await writer.drain()
            response = json.loads(await reader.readline())
            if response["error"] == "flood":
                raise Flood("fixture flood")
            if response["error"]:
                raise ValueError(response["error"])
            return SimpleNamespace(message_id=response["message_id"])
        async def send_message(self, **kwargs):
            return await self.invoke("send", kwargs)
        async def edit_message_text(self, **kwargs):
            return await self.invoke("edit", kwargs)
        async def send_message_draft(self, **kwargs):
            return await self.invoke("draft", kwargs)
        async def do_api_request(self, endpoint, *, api_kwargs):
            result = await self.invoke(endpoint, api_kwargs)
            return {"message_id": result.message_id}
    bot = Bot()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="offline-fixture", typing_indicator=False))
    adapter._rich_send_disabled = True
    adapter._bot = bot
    try:
        yield adapter, bot, requests, landed
    finally:
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


def test_transport_generation_distinguishes_equal_transport_instances():
    from agent.native_emission import transport_generation
    # SDK clients such as telegram.Bot compare equal by remote account identity.
    class Transport:
        def __eq__(self, other):
            return isinstance(other, Transport)
        def __hash__(self):
            return 4242
    first, second = Transport(), Transport()
    assert first == second and first is not second
    with lease([]).activate():
        original = transport_generation(first)
        assert original and transport_generation(first) == original
        assert transport_generation(second) != original


@pytest.mark.asyncio
async def test_rich_send_and_edit_observe_native_payload_not_original_markdown():
    receipts = []
    async with bot_peer() as (adapter, bot, requests, landed):
        adapter._rich_messages_enabled = True
        adapter._rich_send_disabled = False
        content = "| A | B |\n|---|---|\n|one|two|"
        with lease(receipts).activate():
            assert (await adapter.send("4242", content, metadata={"notify": True})).success
            assert (await adapter.edit_message("4242", "1", content, finalize=True)).success
        assert len(receipts) == 2
        for receipt, request in zip(receipts, requests):
            assert json.loads(receipt.payload) == request["rich_message"]
            assert receipt.operation.endswith(":native-rich-json")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 6000, 846608, "timeout", "late_preview"])
async def test_wecom_real_native_queue_and_platform_ack(code):
    from plugins.platforms.wecom.adapter import WeComAdapter
    receipts = []
    async with bot_peer() as (_, bot, requests, landed):
        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._last_chat_req_ids["chat"] = "req"
        class Socket:
            closed = False
            async def send_json(self, payload):
                await bot.invoke("frame", payload)
                finish = payload["body"]["stream"]["finish"]
                if code == "late_preview":
                    # The preview's ACK arrives only after its unchanged native
                    # drain deadline and the final send reuses the same req_id.
                    if finish:
                        adapter._resolve_reply_ack(payload["headers"]["req_id"], {"errcode": 0})
                elif not (finish and code == "timeout"):
                    adapter._resolve_reply_ack(payload["headers"]["req_id"], {"errcode": code if finish else 0})
        adapter._ws = Socket()
        try:
            with lease(receipts).activate():
                assert await adapter.send_stream_frame("claim", chat_id="chat", turn_id="turn")
                assert not receipts  # intermediate preview is not final delivery
                await adapter.send_stream_frame("claim", chat_id="chat", turn_id="turn", finalize=True)
            assert len(receipts) == (1 if code == 0 else 0)
            if receipts:
                assert receipts[0].payload.decode() == requests[-1]["body"]["stream"]["content"]
                assert receipts[0].payload.endswith("\u200b".encode())
                assert dict(receipts[0].target)["chat_id"] == "chat"
        finally:
            tasks = list(adapter._control_workers.values()) + list(adapter._chat_workers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def long_text():
    return "\n".join(" ".join(f"w{i * 20 + j}" for j in range(20)) for i in range(80))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "partial", "failure", "parse"])
async def test_normal_final_exact_split_partial_and_fallback(mode):
    receipts = []
    async with bot_peer(mode) as (adapter, bot, requests, landed):
        event = MessageEvent(text="question", source=SessionSource(Platform.TELEGRAM, "4242"), message_id="11")
        content = long_text() if mode in {"ok", "partial"} else "**claim**: 7"
        with lease(receipts).activate():
            result, delivered_by = await adapter.send_final_ledgered(
                event, "s", content, {"notify": True}, reply_to=None, is_ephemeral_response=True)
        assert delivered_by is adapter
        assert [r.payload.decode() for r in receipts] == [c["text"] for c in landed]
        assert [r.message_or_frame_id for r in receipts] == [str(requests.index(c) + 1) for c in landed]
        assert all(dict(r.target)["chat_id"] == "4242" for r in receipts)
        if mode == "ok":
            assert len(receipts) >= 3
            assert complete_exact_batch(receipts)
            assert not complete_exact_batch(receipts[:-1])
        elif mode == "partial":
            assert not result.success and result.raw_response["partial_overflow"]
            assert len(receipts) == 1 and not complete_exact_batch(receipts)
        elif mode == "failure":
            assert not result.success and not receipts
        else:
            assert result.success and len(receipts) == 1
            assert receipts[0].payload != content.encode()
            assert receipts[0].source_sha256 != receipts[0].payload_sha256
            assert not complete_exact_batch(receipts)


@pytest.mark.asyncio
async def test_normal_final_uses_native_session_runtime_without_manual_scope():
    from agent.native_emission import install_for_runtime
    from agent.supervision_policy import SupervisionRuntime
    from hermes_constants import hermes_home_key
    class Agent:
        session_id, _current_turn_id = "native-gateway", "turn"
    agent = Agent()
    runtime = agent._supervision_runtime = SupervisionRuntime(agent, hermes_home_key(), "lineage")
    receipts = []
    install_for_runtime(runtime, receipts.append)
    runtime.finish_turn()
    async with bot_peer() as (adapter, bot, requests, landed):
        adapter._session_store = SimpleNamespace(peek_session_id=lambda key: agent.session_id if key == "route" else None)
        event = MessageEvent(text="question", source=SessionSource(Platform.TELEGRAM, "4242"))
        result, _ = await adapter.send_final_ledgered(event, "route", "claim", {"notify": True},
                                                       reply_to=None, is_ephemeral_response=True)
        assert result.success and len(receipts) == 1
        runtime.revoke()
        # Edit has no session lookup and no active scope; a successful SendResult
        # alone must not manufacture a receipt for the retired owner.
        assert (await adapter.edit_message("4242", "1", "stale", finalize=True)).success
        assert len(receipts) == 1


@pytest.mark.asyncio
async def test_stream_consumer_run_finish_uses_exact_native_send():
    receipts = []
    async with bot_peer() as (adapter, bot, requests, landed):
        from agent.native_emission import install_for_runtime
        from agent.supervision_policy import SupervisionRuntime
        class Agent:
            session_id, _current_turn_id = "s", "t"
        agent = Agent()
        runtime = agent._supervision_runtime = SupervisionRuntime(agent, "p", "l")
        install_for_runtime(runtime, receipts.append)
        consumer = GatewayStreamConsumer(adapter, "4242", StreamConsumerConfig(buffer_only=True, cursor=""),
                                         emission_agent=lambda: agent)
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("draft")
        consumer.finish("**final claim**")
        await task
        assert receipts
        assert [r.payload.decode() for r in receipts] == [c["text"] for c in landed]
        assert all(b"draft" not in r.payload for r in receipts)


@pytest.mark.asyncio
async def test_stream_edit_and_overflow_exact_successful_segments():
    receipts = []
    async with bot_peer() as (adapter, bot, requests, landed):
        consumer = GatewayStreamConsumer(adapter, "4242", StreamConsumerConfig(cursor=""))
        consumer._message_id = "1"
        with lease(receipts).activate():
            assert await consumer._send_or_edit(long_text(), finalize=True)
        assert len(receipts) >= 3
        assert requests[0]["method"] == "edit"
        assert [r.payload.decode() for r in receipts] == [c["text"] for c in landed]
        assert receipts[0].message_or_frame_id == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["replacement", "cancel", "reset"])
async def test_late_native_success_cannot_adopt_wrong_generation(ending):
    receipts = []
    scope = lease(receipts)
    async with bot_peer() as (adapter, bot, requests, landed):
        bot.release.clear()
        with scope.activate():
            task = asyncio.create_task(adapter.edit_message("4242", "1", "claim", finalize=True))
        await asyncio.wait_for(bot.entered.wait(), 2)
        assert not receipts
        if ending == "replacement":
            adapter._bot = SimpleNamespace()
        elif ending == "reset":
            scope.close()
        else:
            task.cancel()
        bot.release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).success
        assert not receipts


@pytest.mark.asyncio
async def test_cancel_shielded_sdk_late_success_remains_unacknowledged():
    receipts = []
    async with bot_peer() as (adapter, bot, requests, landed):
        bot.release.clear()
        bot.shield_cancel = True
        with lease(receipts).activate():
            task = asyncio.create_task(adapter.edit_message("4242", "1", "claim", finalize=True))
        await asyncio.wait_for(bot.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(bot.cancelled.wait(), 2)
        assert not receipts
        bot.release.set()
        await bot.task  # drain the exact abandoned SDK task, not a sleep-to-green
        assert landed and not receipts


@pytest.mark.asyncio
async def test_draft_and_optimistic_final_mark_are_not_native_emissions():
    receipts = []
    async with bot_peer() as (adapter, bot, requests, landed):
        consumer = GatewayStreamConsumer(adapter, "4242")
        with lease(receipts).activate():
            consumer._mark_final_delivered("not sent")
            # Real adapter draft transport; even a successful preview isn't a claim receipt.
            await adapter.send_draft("4242", 1, "draft claim")
        assert not receipts
