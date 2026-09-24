"""Execute an already sealed lifecycle command after dispatch exits.

Inference expiry bounds acceptance only. This command never reuses inference,
widens authority, adopts a restored worker, or claims an interrupt is settlement.
"""
import time

from agent.interrupt_compat import request_hard_interrupt


def signal(owner, handle):
    with owner._lock:
        live = owner._get(handle)
        s = live.snapshot
        record = s.get("supervisor_control")
        pending = getattr(live, "signal_outcome_pending", None)
        if pending is not None:
            _persist_outcome(owner, live, pending)
            s, record = live.snapshot, live.snapshot.get("supervisor_control")
        if not record or not s["cancel_requested"]:
            return
        if s["worker_finished"] or live.finish_pending or s["settled"]:
            return
        if s["inflight"] or s["handoffs"] or s["cleanup_pending"]:
            return
        if record["state"] in {"requested", "signalling", "stopped", "already_finished"}:
            return
        # Signal failures are visible and retried only while this live generation
        # remains attached, at most three times with the policy tick between them.
        if record.get("signal_attempts", 0) >= 3 or time.monotonic() < getattr(live, "signal_retry_at", 0):
            return
        def begin(n):
            n["supervisor_control"]["state"] = "signalling"
            n["supervisor_control"]["signal_attempts"] += 1
        owner._commit(live, begin, finalizer=True)
        child = live.child
    try:
        delivered = request_hard_interrupt(child, "Owned worker instance no longer contributes", tool_reason="owned semantic cancellation")
    except Exception:
        delivered = False
    with owner._lock:
        live = owner._get(handle)
        live.signal_retry_at = time.monotonic() + 2
        live.signal_outcome_pending = delivered
        _persist_outcome(owner, live, delivered)


def _persist_outcome(owner, live, delivered):
    def end(n):
        record = n["supervisor_control"]
        record["signal_delivered"] = delivered
        # Completion which beat signal confirmation wins, conservatively.
        if n["worker_finished"]:
            record["state"] = "already_finished"
        else:
            record["state"] = "requested" if delivered else "signal_failed"
    owner._commit(live, end, finalizer=True)
    live.signal_outcome_pending = None
