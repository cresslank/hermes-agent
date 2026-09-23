"""Native correction authority survives Gateway callback scheduling and rendering.

Reuse qualified source/adoption/original receipts, then exercise the production
TurnRunner callback, scheduling, Gateway delivery and Telegram adapter. Only SDK
I/O is the existing in-memory Bot; queued revocation occurs before dispatch.
"""
import asyncio
import queue
from gateway.turn_context import TurnContext

import pytest

from tests.agent.test_supervision_claim_uses import native, append, record  # noqa: F401
from tests.agent.test_supervision_corrections import configured, sink, drain_source, rows
from tests.agent.test_supervision_original_output import setup, finalize


@pytest.mark.parametrize('route', ['direct-control', 'production-callback', 'revoked-before-dispatch'])
def test_gateway_correction_callback_preserves_native_authority(native, monkeypatch, tmp_path, request, route):
    from tools.process_registry import process_registry
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from gateway.run_turn_runner import TurnRunner
    from agent.supervision_corrections import notice_owner

    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path / 'sink', 'telegram') as output:
        output.original(finalize(n, messages, monkeypatch))
        data = record()
        data['value'] = 5
        append(n, data)
        drain_source(n, process_registry.completion_queue.get_nowait())
        before = len(output.calls)
        callback_inputs = []
        delivered_inputs = []
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {Platform.TELEGRAM: output.adapter}
        native_deliver = runner._deliver_platform_notice
        async def observe_delivery(source, text):
            delivered_inputs.append({'type': type(text).__name__, 'native_owner': notice_owner(text) is not None})
            return await native_deliver(source, text)
        monkeypatch.setattr(runner, '_deliver_platform_notice', observe_delivery)

        async def run():
            futures = []
            ctx = TurnContext(source=output.source, user_config={}, mute_notification_reply=False,
                _status_adapter=output.adapter, _run_still_current=lambda: True,
                _loop_for_step=asyncio.get_running_loop())
            turn = TurnRunner(runner, ctx)
            native_schedule = turn._schedule
            def observe_schedule(*args, **kwargs):
                future = native_schedule(*args, **kwargs)
                futures.append(future)
                return future
            monkeypatch.setattr(turn, '_schedule', observe_schedule)
            def callback(notice):
                callback_inputs.append({'type': type(notice.text).__name__, 'native_owner': notice_owner(notice.text) is not None})
                if route == 'direct-control':
                    futures.append(native_schedule(runner._deliver_platform_notice(output.source, notice.text), 'control'))
                else:
                    turn._notice_callback_sync(notice)
            n.agent.notice_callback = callback
            n.rt.bind_turn()
            n.rt.finish_turn()
            assert rows(n)[0]['status'] == 'contested'
            assert len(futures) == 1
            assert callback_inputs == [{'type': '_OriginalText', 'native_owner': True}]
            if route == 'revoked-before-dispatch':
                n.ctx.supervision._literal_source_registration.close()
            await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
        asyncio.run(run())
        result = {'route': route, 'callback_inputs': callback_inputs,
                  'delivered_inputs': delivered_inputs, 'additional_sdk_calls': len(output.calls) - before,
                  'canonical_status': rows(n)[0]['status'], 'calls': output.calls[before:]}
        request.node.user_properties.append(('result', result))
        if route == 'revoked-before-dispatch':
            assert len(output.calls) == before, result
        else:
            assert rows(n)[0]['status'] == 'emitted', result
            assert len(output.calls) == before + 1, result
