"""Native LCM entrypoint -> real SQLite -> ordinary tools -> owner/recipient gate.

Run with the separately checked-out hermes_lcm package importable. No handcrafted
SourceProposition or provider facts stand in for the ordinary producer.
"""
from dataclasses import FrozenInstanceError, replace
import json
import time
from types import SimpleNamespace

import pytest

lcm = pytest.importorskip("hermes_lcm")
from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import VERSION, lookup_literal_source
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from tests.agent.supervision_test_support import Agent, accept


def record():
    return {"schema": "lcm.literal-record.v1", "entity": {"namespace": "asset", "id": "A"},
        "predicate": {"namespace": "metric", "id": "mass"},
        "time": {"basis": "event_interval", "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        "scope": {"namespace": "lab", "id": "L", "conditions": [{"namespace": "env", "id": "dry", "value": True}]},
        "quantity": {"kind": "quantity", "unit": "kg"}, "value": 3,
        "polarity": "positive", "modality": "asserted"}


@pytest.fixture
def native(tmp_path, monkeypatch):
    network_calls = []
    def deny_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError("literal-source validation must make zero network/inference calls")
    monkeypatch.setattr("socket.getaddrinfo", deny_network)
    monkeypatch.setattr("socket.socket.connect", deny_network)
    monkeypatch.setattr("httpx.Client.send", deny_network)
    monkeypatch.setattr("httpx.AsyncClient.send", deny_network)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LCM_DATABASE_PATH", str(home / "lcm.db"))
    monkeypatch.setenv("LCM_EMBEDDINGS_ENABLED", "false")
    monkeypatch.setenv("LCM_ASSERTIONS_ENABLED", "false")
    config = {"supervision": {"enabled": True, "plugins": {
        "hermes-lcm": {"literal_sources": {"version": VERSION, "recipients": ["recipient"]}},
        "recipient": {"grants": ["observe"], "data_policy": ["history_excerpt"]},
        "foreign": {"grants": ["observe"], "data_policy": ["history_excerpt"]}}}}
    (home / "config.yaml").write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name="hermes-lcm"), manager)
    # Actual plugin entrypoint performs context-engine + source owner registration.
    lcm.register(ctx)
    prototype = manager._context_engine
    assert ctx.supervision._literal_source_registration.engine is prototype
    engine = prototype.clone_for_agent()
    engine.on_session_start("session-A", platform="cli")
    recipient = PluginContext(PluginManifest(name="recipient"), manager).supervision
    events = []
    recipient.register(consumer=events.append, requested_grants=["observe"])
    foreign = PluginContext(PluginManifest(name="foreign"), manager).supervision
    foreign.register(consumer=events.append, requested_grants=["observe"])
    agent = Agent()
    agent.context_compressor = engine
    runtime = accept(agent, "Read the exact source record.")
    # Accepted user observation is unrelated to source publication; no inference
    # scheduling is permitted from this slice. The empty consumer has no egress.
    from hermes_lcm import tools
    monkeypatch.setattr(tools, "lcm_recall", lambda *a, **k: pytest.fail("extra retrieval"))
    get = engine._store.get
    reads = []
    def point_read(store_id):
        reads.append(store_id)
        return get(store_id)
    monkeypatch.setattr(engine._store, "get", point_read)
    value = SimpleNamespace(home=home, manager=manager, ctx=ctx, engine=engine,
        recipient=recipient, foreign=foreign, agent=agent, runtime=runtime, reads=reads,
        source=ctx.supervision._literal_source_registration, events=events)
    yield value
    ctx.supervision.unregister()
    recipient.unregister()
    foreign.unregister()
    runtime.revoke()
    engine.shutdown()
    prototype.shutdown()
    assert network_calls == []


def append(native, text=None, role="user"):
    text = text if text is not None else json.dumps(record(), ensure_ascii=False)
    sid = native.engine._store.append("session-A", {"role": role, "content": text, "timestamp": 1700000000})
    return sid, text


def call(native, sid, text, mode="pack", *, quote=None, selection=None):
    ref = f"lcm:{sid}:0-{len(text)}"
    args = {"question": "What is the mass of asset A?", "baseline_refs": [{"exact_ref": ref}],
            "budgets": {"max_retrieval_calls": 0}}
    name = "lcm_evidence_pack"
    if quote is not None:
        args["baseline_refs"][0]["quote"] = quote
    if mode != "pack":
        name = "lcm_compile_evidence"
        args["mode"] = mode
        if mode == "proposal":
            args["proposal"] = {"version": "evidence-selector-v1", "requested_facets": [],
                "selections": [{"claim_id": "c", "facet": "answer", "exact_ref": ref,
                                "quote": text if quote is None else quote, **(selection or {})}], "missing_facets": []}
    before_events = len(native.events)
    with bind_subagent_parent(native.agent):
        payload = json.loads(native.engine.handle_tool_call(name, args))
        refs = payload.get("source_propositions", [])
        values = [native.recipient.literal_source(version=VERSION, ref=r) for r in refs]
    assert len(native.events) == before_events  # ZERO provider scheduling from publication/lookup
    return payload, refs, values


@pytest.mark.parametrize("mode", ["pack", "proposal", "auto"])
@pytest.mark.parametrize("role", ["user", "tool"])
def test_original_record_ordinary_tools_publish_immutable_source(native, mode, role):
    sid, text = append(native, role=role)
    payload, refs, values = call(native, sid, text, mode)
    assert len(refs) == 1, payload
    p, = values
    assert p is not None
    assert p.owner_id == "hermes-lcm" and p.revision == native.runtime.revision
    assert p.record.source_bytes == text.encode() and p.record.record_span == (0, len(text))
    pins = {k: json.loads(v) for k, v in p.record.coordinate_pins}
    assert pins == {k: v for k, v in record().items() if k != "schema"}
    attribution = {k: json.loads(v) for k, v in p.record.source_attribution}
    assert attribution["role"] == role and attribution["observed_at"] == 1700000000
    assert pins["time"]["start"] == "2026-01-01T00:00:00Z"  # NOT observed-at
    assert set(native.reads) == {sid}
    with pytest.raises(FrozenInstanceError):
        p.owner_id = "fake"
    with bind_subagent_parent(native.agent):
        assert native.foreign.literal_source(version=VERSION, ref=refs[0]) is None
        assert native.recipient.literal_source(version="v2", ref=refs[0]) is None
        assert native.recipient.literal_source(version=VERSION, ref=vars(p)) is None
        assert native.ctx.supervision.publish_literal_sources(version=VERSION,
            invocation={"id": p.invocation_id}, source_refs=tuple(refs)) == ()


@pytest.mark.parametrize("mode", ["pack", "proposal", "auto"])
@pytest.mark.parametrize("variant", ["assistant", "prose", "partial", "unknown-unit", "empty-unit", "missing-unit",
    "missing-time", "observation-time", "null-interval", "reversed", "unknown-key", "duplicate-key", "bad-version",
    "nested-condition", "duplicate-condition", "nonfinite", "oversized", "multiple", "unknown-value", "alias-object"])
def test_unsupported_never_mints_source(native, mode, variant):
    data = record()
    role, quote = "user", None
    if variant == "assistant": role = "assistant"
    elif variant == "unknown-unit": data["quantity"]["unit"] = "stone"
    elif variant == "empty-unit": data["quantity"]["unit"] = ""
    elif variant == "missing-unit": del data["quantity"]["unit"]
    elif variant == "missing-time": del data["time"]
    elif variant == "observation-time": data["time"]["basis"] = "observation_time"
    elif variant == "null-interval": data["time"]["end"] = None
    elif variant == "reversed": data["time"]["start"] = data["time"]["end"]
    elif variant == "unknown-key": data["validated"] = True
    elif variant == "bad-version": data["schema"] = "lcm.literal-record.v2"
    elif variant == "nested-condition": data["scope"]["conditions"][0]["value"] = {"qualifier": "maybe"}
    elif variant == "duplicate-condition": data["scope"]["conditions"] *= 2
    elif variant == "nonfinite": data["value"] = float("nan")
    elif variant == "unknown-value": data["value"] = None
    elif variant == "alias-object": data["entity"]["aliases"] = ["A"]
    text = json.dumps(data)
    if variant == "partial": quote = '"kg"'
    elif variant == "prose": text = "The mass is 3 kg. " + text
    elif variant == "duplicate-key": text = text[:-1] + ', "value": 4}'
    elif variant == "oversized": text += " " * 2401
    elif variant == "multiple": text += text
    sid, text = append(native, text, role)
    payload, refs, values = call(native, sid, text, mode, quote=quote)
    assert not refs and not values, payload
    assert not native.source.records and not native.source.provider.records
    assert set(native.reads) <= {sid}


@pytest.mark.parametrize("field", ["content", "role", "source", "session_id", "observed_at", "tool_name", "conversation_id"])
def test_native_row_mutation_revokes_at_consumption(native, field):
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values and values[0]
    # Direct fixture-owned SQL deliberately simulates source corruption/revocation.
    value = 1700000001 if field == "observed_at" else "changed"
    native.engine._store._conn.execute(f"UPDATE messages SET {field}=? WHERE store_id=?", (value, sid))
    native.engine._store._conn.commit()
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None


@pytest.mark.parametrize("revoke", ["source", "recipient", "grant", "data", "capability", "engine", "profile", "revision", "expired", "source-grant"])
def test_current_native_owner_and_recipient_required_at_consumption(native, revoke):
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values and values[0]
    if revoke == "source": native.ctx.supervision.unregister()
    elif revoke == "recipient": native.recipient.unregister()
    elif revoke == "grant": native.recipient._registration.grants = frozenset()
    elif revoke == "data": native.recipient._registration.data_policy = frozenset()
    elif revoke == "capability": native.manager._context_engine = object()
    elif revoke == "engine": native.agent.context_compressor = object()
    elif revoke == "profile": native.engine._hermes_home = str(native.home / "other")
    elif revoke == "revision": native.runtime.revision = replace(native.runtime.revision, evidence=native.runtime.revision.evidence + 1)
    elif revoke == "expired": time.sleep(max(0, values[0].deadline - time.monotonic()) + .01)
    elif revoke == "source-grant": native.source.recipients = frozenset()
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None


def test_forged_provider_registration_and_foreign_profile_are_not_authority(native, tmp_path):
    fake = SimpleNamespace(owns_engine=lambda _: True, resolve_literal_source=lambda *a: record())
    assert native.ctx.supervision.register_literal_source_owner(version=VERSION, engine=native.manager._context_engine, provider=fake) is None
    assert native.foreign.register_literal_source_owner(version=VERSION, engine=native.manager._context_engine,
        provider=native.source.provider) is None
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values and values[0]
    foreign_runtime = SimpleNamespace(agent=lambda: native.agent, closed=False,
        revision=replace(native.runtime.revision, profile=str(tmp_path / "foreign")))
    assert lookup_literal_source(foreign_runtime, refs[0], native.recipient._registration) is None


def test_selection_metadata_is_not_coordinate_authority(native):
    sid, text = append(native)
    _, refs, values = call(native, sid, text, "proposal", selection={"entity": "kg", "value": 3, "unit": "kg"})
    assert refs and values[0]
    assert json.loads(dict(values[0].record.coordinate_pins)["entity"]) == record()["entity"]


def test_publication_rechecks_source_after_hydration(native, monkeypatch):
    sid, text = append(native)
    original = native.ctx.supervision.publish_literal_sources
    def mutate(**kwargs):
        native.engine._store._conn.execute("UPDATE messages SET role='assistant' WHERE store_id=?", (sid,))
        native.engine._store._conn.commit()
        return original(**kwargs)
    monkeypatch.setattr(native.ctx.supervision, "publish_literal_sources", mutate)
    payload, refs, values = call(native, sid, text)
    assert not refs and not values, payload
    assert not native.source.records and not native.source.provider.records


def test_overflow_does_not_publish_truncated_prefix(native):
    rows = [append(native) for _ in range(9)]
    with bind_subagent_parent(native.agent):
        result = json.loads(native.engine.handle_tool_call("lcm_evidence_pack", {
            "question": "Describe mass.", "baseline_refs": [{"exact_ref": f"lcm:{sid}:0-{len(text)}"} for sid, text in rows],
            "budgets": {"max_retrieval_calls": 0}}))
    assert not result.get("source_propositions")
    assert not native.source.records and not native.source.provider.records


@pytest.mark.parametrize("mode", ["pack", "proposal", "auto"])
def test_explicit_nonquantity_and_atemporal_are_not_missing_defaults(native, mode):
    data = record()
    data["time"] = {"basis": "atemporal", "start": None, "end": None}
    data["quantity"] = {"kind": "non_quantity", "unit": None}
    data["scope"]["conditions"] = []
    data["value"] = "ready é"
    sid, text = append(native, json.dumps(data, ensure_ascii=False))
    payload, refs, values = call(native, sid, text, mode)
    assert refs and values[0], payload
    assert values[0].record.source_bytes == text.encode()
    assert dict(values[0].record.coordinate_pins)["quantity"] == '{"kind":"non_quantity","unit":null}'


@pytest.mark.parametrize("coordinate", ["entity", "predicate", "time", "scope", "quantity", "polarity", "modality"])
def test_source_coordinates_preserve_differences_without_alias_or_overlap_inference(native, coordinate):
    data = record()
    if coordinate in ("entity", "predicate", "scope"):
        data[coordinate]["namespace"] = "other"
    elif coordinate == "time": data[coordinate]["end"] = "2026-03-01T00:00:00Z"
    elif coordinate == "quantity": data[coordinate]["unit"] = "m"
    elif coordinate == "polarity": data[coordinate] = "negative"
    else: data[coordinate] = "possible"
    sid, text = append(native, json.dumps(data))
    payload, refs, values = call(native, sid, text)
    assert refs and values[0], payload
    pins = {k: json.loads(v) for k, v in values[0].record.coordinate_pins}
    assert pins[coordinate] != record()[coordinate]
    # Parent comparability must require exact equality, never alias/overlap matching.


@pytest.mark.parametrize("stage", ["publish", "lookup"])
def test_revocation_during_source_validation_is_fenced(native, monkeypatch, stage):
    sid, text = append(native)
    if stage == "lookup":
        _, refs, values = call(native, sid, text)
        assert values[0]
    original = native.source.provider.resolve_literal_source
    def revoke(*args):
        value = original(*args)
        if stage == "publish": native.source.active = False
        else: native.recipient.unregister()
        return value
    monkeypatch.setattr(native.source.provider, "resolve_literal_source", revoke)
    if stage == "publish":
        payload, refs, values = call(native, sid, text)
        assert not refs, payload
    else:
        with bind_subagent_parent(native.agent):
            assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None
    assert not native.source.records and not native.source.provider.records


def test_absent_capability_preserves_baseline_output(native):
    sid, text = append(native)
    native.ctx.supervision.unregister()
    payload, refs, values = call(native, sid, text)
    assert not refs and not values
    assert payload["evidence"][0]["quote"] == text


def test_copied_invocation_cannot_publish_or_cancel_owned_capture(native):
    sid, text = append(native)
    with bind_subagent_parent(native.agent):
        invocation = native.ctx.supervision.begin_literal_sources(version=VERSION, engine=native.engine)
        assert invocation is not None
        copied = replace(invocation)
        assert native.ctx.supervision.publish_literal_sources(version=VERSION, invocation=copied, source_refs=("fake",)) == ()
        native.ctx.supervision.cancel_literal_sources(version=VERSION, invocation=copied)
        assert native.source.pending[invocation.id] is invocation
        native.ctx.supervision.cancel_literal_sources(version=VERSION, invocation=invocation)
        assert not native.source.pending


@pytest.mark.parametrize("coordinate", ["entity", "predicate", "scope", "time", "quantity", "value", "modality", "polarity"])
def test_valid_replacement_record_is_not_the_original_source_version(native, coordinate):
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values[0]
    data = record()
    if coordinate in ("entity", "predicate", "scope"): data[coordinate]["id"] = "B"
    elif coordinate == "time": data[coordinate]["end"] = "2026-03-01T00:00:00Z"
    elif coordinate == "quantity": data[coordinate]["unit"] = "m"
    elif coordinate == "value": data[coordinate] = 4
    elif coordinate == "modality": data[coordinate] = "necessary"
    else: data[coordinate] = "negative"
    native.engine._store._conn.execute("UPDATE messages SET content=? WHERE store_id=?", (json.dumps(data), sid))
    native.engine._store._conn.commit()
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is None


def test_deadline_cannot_be_renewed_at_publication(native, monkeypatch):
    sid, text = append(native)
    original = native.ctx.supervision.publish_literal_sources
    def expire(**kwargs):
        time.sleep(max(0, kwargs["invocation"].deadline - time.monotonic()) + .01)
        return original(**kwargs)
    monkeypatch.setattr(native.ctx.supervision, "publish_literal_sources", expire)
    payload, refs, values = call(native, sid, text)
    assert not refs and not values, payload
    assert not native.source.records and not native.source.provider.records
    assert not native.source.pending and not native.source.provider.invocations


def test_fake_handle_cannot_replace_native_hydration(native):
    with bind_subagent_parent(native.agent):
        invocation = native.ctx.supervision.begin_literal_sources(version=VERSION, engine=native.engine)
        assert invocation
        assert native.ctx.supervision.publish_literal_sources(version=VERSION,
            invocation=invocation, source_refs=("a" * 64,)) == ()
        assert not native.source.records
        assert native.ctx.supervision.register_literal_source_owner(version="v2", engine=native.manager._context_engine,
            provider=native.source.provider) is None


def test_same_id_registration_copy_cannot_borrow_disclosure(native):
    sid, text = append(native)
    _, refs, values = call(native, sid, text)
    assert values[0]
    copied = replace(native.recipient._registration)
    assert lookup_literal_source(native.runtime, refs[0], copied) is None


def test_actual_foreign_profile_facade_cannot_borrow_recipient_name(native, tmp_path):
    foreign_home = tmp_path / "profile-B"
    foreign_home.mkdir()
    (foreign_home / "config.yaml").write_text(json.dumps({"supervision": {"enabled": True,
        "plugins": {"recipient": {"grants": ["observe"], "data_policy": ["history_excerpt"]}}}}))
    manager = PluginManager(scope_key=str(foreign_home))
    recipient = PluginContext(PluginManifest(name="recipient"), manager).supervision
    recipient.register(consumer=lambda _: None, requested_grants=["observe"])
    try:
        sid, text = append(native)
        _, refs, values = call(native, sid, text)
        assert values[0]
        with bind_subagent_parent(native.agent):
            assert recipient.literal_source(version=VERSION, ref=refs[0]) is None
            assert native.recipient.literal_source(version=VERSION, ref=refs[0]) is not None
    finally:
        recipient.unregister()


def test_host_source_grant_is_default_off_not_plugin_owned(native):
    native.ctx.supervision.unregister()
    (native.home / "config.yaml").write_text(json.dumps({"supervision": {"enabled": True, "plugins": {}}}))
    assert native.ctx.supervision.register_literal_source_owner(version=VERSION,
        engine=native.manager._context_engine, provider=native.source.provider) is None
    sid, text = append(native)
    payload, refs, _ = call(native, sid, text)
    assert not refs and payload["evidence"][0]["quote"] == text


def test_source_reads_are_outside_host_graph_and_registration_locks(native, monkeypatch):
    original = native.source.provider.resolve_literal_source
    reads = []
    def resolve(*args):
        assert not native.runtime.lock._is_owned()
        assert not native.source.lock._is_owned()
        reads.append(True)
        return original(*args)
    monkeypatch.setattr(native.source.provider, "resolve_literal_source", resolve)
    sid, text = append(native)
    payload, refs, values = call(native, sid, text)
    assert refs and values[0], payload
    assert len(reads) == 2  # publication and consumption of the SAME selected row
