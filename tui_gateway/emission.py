"""Private observation metadata travels beside frames, never on the RPC wire."""
from agent.native_emission import capture_context, prepare, restore_context, transport_generation


class QueuedFrame(dict):
    def __init__(self, frame, scope, current):
        super().__init__(frame)
        self.emission_scope = scope
        self.emission_current = current


class WireFrame(str):
    def __new__(cls, line, pending, current):
        obj = super().__new__(cls, line)
        obj.pending = pending
        obj.current = current
        return obj

    def emitted(self):
        if self.pending and self.current():
            self.pending.succeeded()


def prepare_frame(obj, line, transport, peer):
    scope = obj.emission_scope if type(obj) is QueuedFrame else capture_context()
    current = obj.emission_current if type(obj) is QueuedFrame else lambda: True
    from agent.native_emission import MAX_PAYLOAD_BYTES
    if scope is None or len(line) > MAX_PAYLOAD_BYTES:
        return line
    # serialize_frame may have replaced an invalid event with an error reply.
    # Classify the actual bounded frame, never its failed pre-serialization input.
    import json
    try:
        wire = json.loads(line)
        params = wire.get("params") if wire.get("method") == "event" else None
    except (ValueError, AttributeError):
        return line
    params = params if isinstance(params, dict) else {}
    # Never certify a history/replay result as a fresh assistant emission.
    from agent.supervision_original_output import origin_of, codec, native_text_sink
    original_payload = (obj.get("params") or {}).get("payload") or {}
    original = original_payload.get("text")
    origin = origin_of(original)
    payload = params.get("payload") or {}
    from agent.supervision_corrections import notice_owner
    correction = notice_owner(original)
    if correction is not None:
        target = (("peer", str(peer)), ("session", str(params.get("session_id", ""))))
        with restore_context(scope):
            generation = transport_generation(transport)
        if (params.get("type") != "notification.show" or peer != "stdio"
                or not native_text_sink(transport) or not current()
                or payload.get("text") != original or "rendered" in payload
                or not correction.reserve("tui", target, generation)):
            return None
        with restore_context(scope):
            pending = prepare(line, surface="tui", target=target, generation=generation,
                operation="notification.show", frame_id=str(params.get("seq", "")),
                origin=codec(original, line, "tui.correction.v1", ("params", "payload", "text")),
                current=current)
        return WireFrame(line, pending, current)
    if params.get("type") not in {"message.complete", "message.delta"}:
        return line
    proof = None
    if (peer == "stdio" and native_text_sink(transport) and origin is not None and "rendered" not in payload
            and payload.get("text") == original
            and params.get("type") == ("message.complete" if origin.stage == "final" else "message.delta")
            and json.dumps(wire, ensure_ascii=False) + "\n" == line):
        proof = codec(original, line, "tui.stdio-text.v1", ("params", "payload", "text"))
    from agent.native_emission import capture_guards
    with restore_context(scope, guards=capture_guards()):
        pending = prepare(line, surface="tui", generation=transport_generation(transport),
                          target=(("peer", str(peer)), ("session", str(params.get("session_id", "")))),
                          operation=str(params["type"]),
                          frame_id=str(params.get("seq", "")), origin=proof, current=current)
    return WireFrame(line, pending, current)


def emitted_frame(line):
    if type(line) is WireFrame:
        try:
            line.emitted()
        except Exception:
            pass  # stale/unavailable observer is not a transport failure


def queued_frame(frame, original, current):
    scope = original.emission_scope if type(original) is QueuedFrame else capture_context()
    if scope is None:
        return frame
    prior = original.emission_current if type(original) is QueuedFrame else lambda: True
    return QueuedFrame(frame, scope, lambda: current() and prior())
