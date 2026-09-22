"""Gateway native payload boundary. Adapter success flags are deliberately unused."""
import json
from contextlib import contextmanager
from contextvars import ContextVar

from agent.native_emission import capture_context, for_agent, prepare, transport_generation


_completion_gate: ContextVar[object] = ContextVar("telegram_emission_completion", default=None)


class _CompletionGate:
    """One native API completion, released only by its actual deadline owner.

    Cancellation-shielded SDK tasks can finish after their caller timed out. Their
    late successes must not publish a receipt for an abandoned attempt.
    """
    def __init__(self):
        self.closed = False
        self.pending = None

    def offer(self, pending, message_id, current):
        if not self.closed and self.pending is None:
            self.pending = (pending, message_id, current)

    def finish(self, success=False):
        if self.closed:
            return
        self.closed = True
        pending, self.pending = self.pending, None
        if success and pending and pending[2]():
            pending[0].succeeded(message_id=pending[1])


@contextmanager
def telegram_completion_gate():
    gate = _CompletionGate() if capture_context() is not None else None
    token = _completion_gate.set(gate)
    try:
        yield gate
    finally:
        if gate:
            gate.finish()
        _completion_gate.reset(token)


@contextmanager
def final_emission_scope(adapter, session_key):
    agent = None
    try:
        store = getattr(adapter, "_session_store", None)
        session_id = store.peek_session_id(session_key) if store is not None else None
        if isinstance(session_id, str) and session_id:
            from agent.supervision_delivery import runtime_for_session
            runtime = runtime_for_session(session_id)
            agent = runtime.agent() if runtime is not None else None
    except Exception:
        pass  # observation lookup cannot interrupt or redirect the final send
    with for_agent(agent):
        yield


async def telegram_text_call(adapter, method, **kwargs):
    """Observe the actual post-format/post-fallback API text and effective routing.

    The SDK's successful send/edit return is the native platform boundary, not a
    claim of HTTP wire bytes or human rendering. A changed bot instance during
    the await invalidates observation; ordinary error/retry behavior is untouched.
    """
    bot = adapter._bot
    rich = method == "do_api_request"
    async def invoke():
        if rich:
            return await bot.do_api_request(kwargs["endpoint"], api_kwargs=kwargs["api_kwargs"])
        return await getattr(bot, method)(**kwargs)
    if capture_context() is None:
        return await invoke()
    native = kwargs.get("api_kwargs", {}) if rich else kwargs
    rich_payload = native.get("rich_message")
    payload = native.get("text")
    if rich:
        from agent.native_emission import MAX_PAYLOAD_BYTES
        if (type(rich_payload) is dict and len(rich_payload) <= 4
                and all(type(k) is str and len(k) <= 64 for k in rich_payload)
                and all(type(v) is bool or (type(v) is str and len(v) <= MAX_PAYLOAD_BYTES)
                        for v in rich_payload.values())):
            payload = json.dumps(rich_payload, ensure_ascii=False)
        else:
            payload = None
    pending = prepare(payload, surface="gateway.telegram", generation=transport_generation(bot),
        target=tuple((key, str(native[key])) for key in
                     ("chat_id", "message_thread_id", "direct_messages_topic_id", "reply_to_message_id", "reply_parameters")
                     if key in native and native[key] is not None),
        operation=(str(kwargs.get("endpoint")) + ":native-rich-json" if rich
                   else method + ":" + str(kwargs.get("parse_mode") or "plain")),
        frame_id=str(native.get("message_id") or ""))
    result = await invoke()
    if pending and adapter._bot is bot and result is not False and result is not None:
        message_id = native.get("message_id") or (
            result.get("message_id") or (result.get("result") or {}).get("message_id")
            if isinstance(result, dict) else getattr(result, "message_id", None))
        gate = _completion_gate.get()
        if message_id is not None and type(gate) is _CompletionGate:
            gate.offer(pending, str(message_id), lambda: adapter._bot is bot)
    return result
