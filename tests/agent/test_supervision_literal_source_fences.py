"""Regression proofs through the real LCM pack/proposal/auto producer and owner.

The original independent defect oracles are retained in the review evidence;
unsupported decimal tokens now deliberately abstain rather than mint false pins.
"""
from decimal import Decimal
import json

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import VERSION
from tests.agent.test_supervision_literal_sources import native, append, call, record


MODES = ["pack", "proposal", "auto"]


def number_record(token, field):
    data = record()
    target = data if field == "value" else data["scope"]["conditions"][0]
    target["value"] = "NUMBER_TOKEN"
    return json.dumps(data).replace('"NUMBER_TOKEN"', token)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("field", ["value", "condition"])
@pytest.mark.parametrize("token", ["1e-400", "0.100000000000000000001", "9007199254740991.1",
                                   "-1e-400", "-9007199254740991.1"])
def test_lossy_or_out_of_bound_original_numbers_abstain(native, mode, field, token):
    sid, text = append(native, number_record(token, field))
    payload, refs, values = call(native, sid, text, mode)
    assert not refs and not values, payload
    assert not native.source.records and not native.source.provider.records
    assert native.engine._store.get(sid)["content"] == text


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("field", ["value", "condition"])
@pytest.mark.parametrize("token", ["0.0", "0.1", "1.25", "1e-200", "9007199254740991.0", "-0.1"])
def test_supported_numbers_retain_original_meaning_and_bytes(native, mode, field, token):
    sid, text = append(native, number_record(token, field))
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0], payload
    source = values[0].record
    pin = dict(source.coordinate_pins)["value" if field == "value" else "scope"]
    number = json.loads(pin, parse_float=Decimal, parse_int=Decimal)
    if field == "condition":
        number = number["conditions"][0]["value"]
    assert number == Decimal(token)
    assert source.source_bytes == text.encode("utf-8")
    assert source.record_span == (0, len(text))
    assert source.exact_ref == f"lcm:{sid}:0-{len(text)}"


@pytest.mark.parametrize("mode", MODES)
def test_underflow_condition_cannot_alias_supported_zero_scope(native, mode):
    sid, text = append(native, number_record("1e-400", "condition"))
    payload, refs, values = call(native, sid, text, mode)
    assert not refs and not values, payload
    sid, text = append(native, number_record("0.0", "condition"))
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0], payload
    scope = json.loads(dict(values[0].record.coordinate_pins)["scope"])
    assert scope["conditions"][0]["value"] == 0.0


def rebind(engine, coordinate):
    if coordinate in ("session", "session-ABA"):
        engine.on_session_start("session-B", platform="cli")
        assert engine.current_session_id == "session-B"
    elif coordinate in ("conversation", "conversation-ABA"):
        engine.on_session_start("session-A", platform="cli", conversation_id="conversation-B")
        assert engine.current_conversation_id == "conversation-B"
    if coordinate.endswith("ABA") or coordinate == "same-IDs":
        engine.on_session_start("session-A", platform="cli", conversation_id="session-A")
        assert (engine.current_session_id, engine.current_conversation_id) == ("session-A", "session-A")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("stage", ["publication", "lookup"])
@pytest.mark.parametrize("coordinate", ["session", "conversation", "session-ABA", "conversation-ABA", "same-IDs"])
def test_real_lifecycle_after_provider_read_fences_acceptance_and_allows_new_invocation(
        native, monkeypatch, mode, stage, coordinate):
    sid, text = append(native)
    refs = []
    if stage == "lookup":
        payload, refs, values = call(native, sid, text, mode)
        assert refs and values[0], payload
    revision = native.runtime.revision
    deadline = native.runtime.shared_deadline()
    provider = native.source.provider
    original = provider.resolve_literal_source
    reads = []

    def resolve(*args):
        value = original(*args)  # unchanged production SQLite read/validator
        assert value is not None
        reads.append(value)
        rebind(native.engine, coordinate)
        return value

    monkeypatch.setattr(provider, "resolve_literal_source", resolve)
    if stage == "publication":
        payload, refs, values = call(native, sid, text, mode)
        assert not refs and not values, payload
    else:
        with bind_subagent_parent(native.agent):
            assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None
    assert len(reads) == 1
    assert native.runtime.revision == revision
    assert not native.source.records and not provider.records
    assert not native.source.pending and not provider.invocations

    monkeypatch.setattr(provider, "resolve_literal_source", original)
    text = json.dumps(record())
    sid = native.engine._store.append(native.engine.current_session_id,
        {"role": "user", "content": text, "timestamp": 1700000000})
    payload, fresh_refs, values = call(native, sid, text, mode)
    assert fresh_refs and values[0], payload
    assert values[0].deadline == deadline  # rebinding never renews the shared budget
    assert values[0].revision == revision
    with bind_subagent_parent(native.agent):
        for ref in refs:
            assert native.recipient.literal_source(version=VERSION, ref=ref) is None


@pytest.mark.parametrize("mode", MODES)
def test_invocation_cannot_cross_lifecycle_before_hydration(native, monkeypatch, mode):
    sid, text = append(native)
    original = native.engine._store.get
    rebound = []

    def get(store_id):
        row = original(store_id)
        if not rebound:
            rebound.append(True)
            rebind(native.engine, "session-ABA")
        return row

    monkeypatch.setattr(native.engine._store, "get", get)
    payload, refs, values = call(native, sid, text, mode)
    assert rebound and not refs and not values, payload
    assert not native.source.records and not native.source.provider.records


@pytest.mark.parametrize("mode", MODES)
def test_source_reads_are_outside_graph_and_lifecycle_locks(native, monkeypatch, mode):
    sid, text = append(native)
    original = native.engine._store.get
    reads = []

    def get(store_id):
        assert not native.runtime.lock._is_owned()
        assert not native.source.lock._is_owned()
        assert not native.source.provider.lock._is_owned()
        reads.append(store_id)
        return original(store_id)

    monkeypatch.setattr(native.engine._store, "get", get)
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0] and reads, payload


@pytest.mark.parametrize("mode", MODES)
def test_in_progress_lifecycle_blocks_old_lookup_and_new_capture(native, monkeypatch, mode):
    sid, text = append(native)
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0], payload
    original = native.engine._rebind_storage_for_home
    observed = []

    def during_rebind(home):
        # Real start has entered, but has not yet changed either public ID.
        assert native.engine.current_session_id == "session-A"
        with bind_subagent_parent(native.agent):
            assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None
            assert native.ctx.supervision.begin_literal_sources(version=VERSION, engine=native.engine) is None
        observed.append(True)
        return original(home)

    monkeypatch.setattr(native.engine, "_rebind_storage_for_home", during_rebind)
    native.engine.on_session_start("session-A", platform="cli", hermes_home=str(native.home))
    assert observed
    payload, new_refs, values = call(native, sid, text, mode)
    assert new_refs and values[0], payload
    assert new_refs != refs


@pytest.mark.parametrize("stage", ["publication", "lookup"])
def test_final_acceptance_holds_lifecycle_invalidation_fence(native, monkeypatch, stage):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    sid, text = append(native)
    refs = []
    if stage == "lookup":
        _, refs, values = call(native, sid, text)
        assert values[0]
    provider = native.source.provider
    original_fence, original_lifecycle = provider.literal_source_binding_fence, provider.lifecycle
    attempted, entered = Event(), Event()
    futures = []

    @contextmanager
    def lifecycle(*args, **kwargs):
        attempted.set()
        with original_lifecycle(*args, **kwargs):
            entered.set()
            yield

    with ThreadPoolExecutor(max_workers=1) as executor:
        @contextmanager
        def fence(*args):
            with original_fence(*args) as current:
                assert current
                futures.append(executor.submit(native.engine.on_session_start, "session-B", platform="cli"))
                assert attempted.wait(2), "lifecycle worker did not reach the real fence"
                assert not entered.is_set()  # it cannot invalidate during acceptance
                yield current

        monkeypatch.setattr(provider, "lifecycle", lifecycle)
        monkeypatch.setattr(provider, "literal_source_binding_fence", fence)
        with bind_subagent_parent(native.agent):
            if stage == "lookup":
                assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is not None
            else:
                # Avoid the helper's automatic lookup: the new generation may
                # already have invalidated the freshly accepted publication.
                payload = json.loads(native.engine.handle_tool_call("lcm_evidence_pack", {
                    "question": "What is the mass?", "baseline_refs": [{"exact_ref": f"lcm:{sid}:0-{len(text)}"}],
                    "budgets": {"max_retrieval_calls": 0}}))
                refs = payload["source_propositions"]
        assert len(futures) == 1
        futures[0].result(timeout=2)
    assert entered.is_set() and native.engine.current_session_id == "session-B"
    monkeypatch.setattr(provider, "literal_source_binding_fence", original_fence)
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None


@pytest.mark.parametrize("mode", MODES)
def test_legacy_adapter_without_binding_support_keeps_ordinary_output(native, monkeypatch, mode):
    from agent.supervision_literal_sources import LiteralSourceProviderV1
    from types import MethodType

    provider = native.source.provider
    monkeypatch.setattr(provider, "literal_source_binding",
        MethodType(LiteralSourceProviderV1.literal_source_binding, provider))
    sid, text = append(native)
    payload, refs, values = call(native, sid, text, mode)
    assert not refs and not values
    monkeypatch.setattr(native.engine, "_literal_source_provider", None)
    baseline, baseline_refs, baseline_values = call(native, sid, text, mode)
    # Independently timed calls need not share telemetry or its trace digest.
    # Compare the ordinary evidence, answer contract and result disposition.
    assert not baseline_refs and not baseline_values
    fields = {"pack": ("status", "evidence", "computation"),
              "proposal": ("status", "state", "evidence", "missing_facets"),
              "auto": ("status", "state", "contract", "evidence", "open_requirements")}[mode]
    for field in fields:
        assert field in payload and payload[field] == baseline[field]
    assert not native.source.pending and not provider.invocations


@pytest.mark.parametrize("mode", MODES)
def test_failed_lifecycle_cannot_reopen_capture(native, monkeypatch, mode):
    sid, text = append(native)
    _, refs, values = call(native, sid, text, mode)
    assert values[0]
    original = native.engine._rebind_storage_for_home

    def fail(_):
        raise OSError("fixture rebind failed")

    monkeypatch.setattr(native.engine, "_rebind_storage_for_home", fail)
    with pytest.raises(OSError, match="fixture rebind failed"):
        native.engine.on_session_start("session-A", hermes_home=str(native.home), platform="cli")
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None
        assert native.ctx.supervision.begin_literal_sources(version=VERSION, engine=native.engine) is None
    monkeypatch.setattr(native.engine, "_rebind_storage_for_home", original)
    native.engine.on_session_start("session-A", platform="cli")
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0], payload


@pytest.mark.parametrize("lifecycle", ["end", "reset"])
def test_session_end_and_reset_retire_generation_until_legitimate_start(native, lifecycle):
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values[0]
    if lifecycle == "end":
        native.engine.on_session_end("session-A", [])
    else:
        native.engine.on_session_reset()
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None
        assert native.ctx.supervision.begin_literal_sources(version=VERSION, engine=native.engine) is None
    native.engine.on_session_start("session-A", platform="cli")
    payload, refs, values = call(native, sid, text)
    assert refs and values[0], payload
