"""Native F04 decisive dispatch, exact bytes and fail-open-to-real-read controls.

The real provider/HTTP MockTransport, configured native owner and read_file run;
only call counting and adversarial interleavings are injected.
"""
import json
import threading
from contextvars import copy_context
from contextlib import contextmanager

import pytest
from tests.agent.test_owned_delegation_planning import (
    base_rig as base_rig, rig as rig, native as native, factory as factory,
)
from tests.agent.test_supervision_read_supplier import requests, read
from tests.agent.test_supervision_efficiency import feature_calls


@contextmanager
def pending_native_launch(p, monkeypatch):
    """Public delegate_task owns its real admission reservation on another thread.
    Stop at synthetic credential resolution, before construction or network.
    """
    from tools import delegate_tool as dt
    entered, release = threading.Event(), threading.Event()
    replies, errors, tids = [], [], []
    def credentials(*args, **kwargs):
        tids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        raise ValueError('synthetic offline admission stop')
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', credentials)
    def launch():
        try:
            replies.append(json.loads(dt.delegate_task(parent_agent=p.a, goal='Independent current source consumer')))
        except BaseException as exc:
            errors.append(repr(exc))
    ctx = copy_context()
    thread = threading.Thread(target=lambda: ctx.run(launch))
    thread.start()
    try:
        assert entered.wait(3), errors
        assert tids == [thread.ident] and thread.ident != threading.get_ident()
        yield tids
    finally:
        release.set(); thread.join(3)
        assert not thread.is_alive() and not errors, errors
        assert 'synthetic offline admission stop' in str(replies), replies




@pytest.mark.parametrize("change", [
    "none", "source", "source_during_inference", "fresh", "independent", "readback",
    "missing_capture", "error", "ineligible", "unavailable", "grant", "dispatch_denied",
])
def test_native_reuse_skips_actual_read_or_executes_once(factory, monkeypatch, change):
    from agent import tool_executor
    from tests.agent.supervision_test_support import accept
    p = factory()
    rows = requests(p)
    p.commit(rows)
    if change == "error":
        source = p.source.read_text()
        p.source.unlink()
        first, original = p.call("read_file", rows[0]["operation"]["arguments"], settle=False)
        assert json.loads(original).get("error")
        from agent.supervision_efficiency import observe_native
        observe_native(p.a, "tool_result", "read_file", rows[0]["operation"]["arguments"], original,
                       call_id=first, failed=True)
        assert first not in p.native.owner.read_payloads
        p.source.write_text(source)
    else:
        first, original = read(p, rows[0])
        assert p.native.owner.read_payloads[first] == original.encode()
    evaluations = []
    def counted(agent, name, run):
        evaluations.append(name)
        return run()
    monkeypatch.setattr(tool_executor, "_run_with_activity_heartbeat", counted)
    if change == "source":
        p.source.write_text("Synthetic source: changed snapshot.\n")
    if change == "source_during_inference":
        p.native.on_response.append(lambda: p.source.write_text("Synthetic source: changed during decision.\n"))
    if change in {"fresh", "independent", "readback"}:
        instruction = {"fresh": "Read the source fresh now.", "independent": "Perform an independent corroborating read.",
                       "readback": "Read back the source after the external write."}[change]
        accept(p.a, instruction, continuation=True)
    if change == "missing_capture":
        # A failed/unavailable original is never eligible to become a source.
        p.native.owner.reads.clear()
        p.native.owner.read_payloads.clear()
    if change == "ineligible":
        p.commit([])
        evaluations.clear()
    if change == "unavailable":
        def malformed(answers):
            answers.clear()
        p.native.answer_mutators.append(malformed)
    if change == "grant":
        def revoke():
            reg = p.native.bridge.native._registration
            reg.grants = frozenset(g for g in reg.grants if g != "reuse_candidate")
        p.native.on_response.append(revoke)
    if change == "dispatch_denied":
        from agent.owned_delegation import ControlDenied
        @contextmanager
        def denied(*args):
            raise ControlDenied("fixture-denied")
            yield
        monkeypatch.setattr("agent.owned_delegation.dispatch_fence", denied)
    second, actual = p.call("read_file", rows[1]["operation"]["arguments"], settle=False)
    if change == "none":
        assert evaluations == []  # ZERO underlying read invocations
        envelope = json.loads(actual)
        assert envelope["executed"] is False and envelope["reused_from"] == "tool:" + first
        assert envelope["result"] == original
        assert second not in p.rt.dependencies.planning.returns  # no fake file-owner capture
        assert second not in p.native.owner.reads
        assert len(feature_calls(p.native, "F04")) == 1
        assert any(r.status == "applied" for r in p.rt.receipts.values())
    elif change == "dispatch_denied":
        assert evaluations == [] and "denied" in actual
        assert not any(r.status == "applied" for r in p.rt.receipts.values())
    else:
        assert evaluations == ["read_file"]  # exactly one real dispatch
        assert "reused_from" not in actual and "Synthetic source:" in actual
        assert not any(r.status == "applied" for r in p.rt.receipts.values())
    assert not p.rt.drain_at_safe_point()


@pytest.mark.parametrize("aba", [False, True])
def test_native_admission_after_census_vetoes_reuse(factory, monkeypatch, aba):
    from agent.supervision_read_reuse import ReadReuse
    p = factory(); rows = requests(p); p.commit(rows); read(p, rows[0])
    evaluations = []
    monkeypatch.setattr("agent.tool_executor._run_with_activity_heartbeat",
                        lambda agent, name, run: evaluations.append(name) or run())
    original = ReadReuse.consume
    def interleave(selected, owner):
        with pending_native_launch(p, monkeypatch):
            if not aba:
                return original(selected, owner)
        return original(selected, owner)  # native epoch catches completed admission too
    monkeypatch.setattr(ReadReuse, "consume", interleave)
    _, result = p.call("read_file", rows[1]["operation"]["arguments"], settle=False)
    assert evaluations == ["read_file"] and "reused_from" not in result
    assert len(feature_calls(p.native, "F04")) == 1
    assert not any(r.status == "applied" for r in p.rt.receipts.values())
