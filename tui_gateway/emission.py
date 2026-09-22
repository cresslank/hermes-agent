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
    if params.get("type") not in {"message.complete", "message.delta"}:
        return line
    with restore_context(scope):
        pending = prepare(line, surface="tui", generation=transport_generation(transport),
                          target=(("peer", str(peer)), ("session", str(params.get("session_id", "")))),
                          operation=str(params["type"]),
                          frame_id=str(params.get("seq", "")))
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
