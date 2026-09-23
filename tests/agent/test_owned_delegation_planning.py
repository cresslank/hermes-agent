"""Configured native planning joins, not test-installed owners or inventory stubs.

Accepted input, canonical SessionDB, real file write/read/batch settlement, full
Jev registry/bridge/MockTransport and the actual next request assembly are used.
"""
import copy
import json

import pytest

from tests.agent.test_supervision_planning_contracts import (
    base_rig as base_rig, rig as rig, native as native, make_planning,  # pytest fixture exports
    test_f05_commit_plugin_next_request_then_ordinary_launch as prove_f05,
    test_f08_native_finite_passes_dispositions_next_request_withdrawal as prove_f08,
    test_real_transport_noop_and_expiry as prove_transport,
    test_canonical_writer_busy_fails_closed_without_side_database as prove_busy,
    test_canonical_rehydration_deletion_reset_and_retention as prove_storage,
    test_actual_constructor_and_run_share_isolation_recipe as prove_recipe,
)
from tests.agent.test_supervision_efficiency import feature_calls
from tests.agent.test_supervision_views import assemble
from tests.agent.supervision_test_support import accept
from agent.owned_delegation_policy import ConfiguredDelegationOwner, VERSION, CONSUMERS
from agent.owned_delegation_planning import CONSUMERS_V2


@pytest.fixture
def factory(native, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("configured tests must not install an owner or fixture inventory")
    monkeypatch.setattr("agent.owned_delegation.install_owner", forbidden)

    extra_facades = []
    def make(*, jobs=None, version=2, envelope=None, policy_change=None, input_change=None, competing=False):
        def configure(n, user, goal):
            lines = user.splitlines()
            start = user.index(lines[2])
            row = dict(ref="job:research", goal=goal, requirements=[dict(start=start, end=start+len(lines[2]))],
                consumer_scope="parent_only", obligation="optional", requires_result=False, requires_effects=False,
                requires_cleanup=False, requires_handoff=False)
            if version == 2:
                row["requires_corroboration"] = False
            rows = [row]
            if jobs:
                jobs(rows, user)
            body = dict(version=version, jobs=rows)
            if version == 2:
                body.update(inventory_scope="current_work_parent_only", complete=True)
            if envelope:
                envelope(body)
            user += "\n```hermes-owned-delegation-v" + str(version) + "\n" + json.dumps(body) + "\n```\n"
            policy = dict(version=VERSION, allow_optional_readonly=True,
                consumer_contract=CONSUMERS_V2 if version == 2 else CONSUMERS, read_roots=[str(tmp_path)])
            section = n.rig.config["supervision"]
            section["plugins"]["fixture-supervisor"]["owned_delegation"] = policy
            if policy_change:
                policy_change(section)
            if competing:
                from hermes_cli.plugins import PluginContext
                from hermes_cli.plugins_manifest import PluginManifest
                section["plugins"]["competitor"] = copy.deepcopy(section["plugins"]["fixture-supervisor"])
                (n.rig.home / "config.yaml").write_text(json.dumps(n.rig.config))
                facade = PluginContext(PluginManifest(name="competitor"), n.rig.manager).supervision
                facade.register(consumer=lambda event: None, requested_grants=["observe", "reprioritize_child", "cancel_child"])
                extra_facades.append(facade)
            (n.rig.home / "config.yaml").write_text(json.dumps(n.rig.config))
            return input_change(user) if input_change else user
        result = make_planning(native, tmp_path, monkeypatch, configure=configure)
        if policy_change is None and not competing:
            assert isinstance(result.owner, ConfiguredDelegationOwner)
        return result
    yield make
    for facade in extra_facades:
        facade.unregister()


def test_configured_f05_real_join_then_ordinary_launch(factory, monkeypatch):
    p = factory()
    assert isinstance(p.owner, ConfiguredDelegationOwner)
    assert p.owner.resolving == {} and p.owner._consumer_inventory is None
    before = p.a._session_db._conn.total_changes
    view = p.rt.dependencies.planning._preflight(p.delegation)
    assert view and view["closed"] and view["goal"] == p.delegation["operation"]["arguments"]["tasks"][0]["goal"]
    assert p.a._session_db._conn.total_changes == before and not p.owner.list_owned() and not p.owner.resolving
    prove_f05(p, monkeypatch)
    handle, = p.owner.list_owned()
    state = p.owner.status(handle)
    assert state["consumers"] == view["consumers"] and state["obligation"] == view["obligation"]
    assert state == p.owner._store.read(handle.child_id)


def test_configured_f08_actual_whole_inventory_then_parent_withdrawal(factory):
    p = factory()
    assert isinstance(p.owner, ConfiguredDelegationOwner)
    assert p.owner._consumer_inventory is None  # no caller-supplied callback
    prove_f08(p)


def test_configured_f08_advice_ignored_still_dispatches_and_records_issued(factory):
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    before = copy.deepcopy(p.history), p.rt.requirements
    assert "withdrawing this optional expansion" in str(assemble(p.a, p.history).api_messages)
    assert (p.history, p.rt.requirements) == before
    assert p.inventory()[-1]["status"] == "advised"
    _, returned = p.call("read_file", p.expansion["operation"]["arguments"])
    assert "Synthetic source:" in returned and p.inventory()[-1]["status"] == "issued"
    assert p.rt.requirements == before[1] and not p.rt.closed


@pytest.mark.parametrize("field,value", [
    ("obligation", "required"), ("obligation", "unknown"), ("requires_result", True),
    ("requires_effects", True), ("requires_cleanup", True), ("requires_handoff", True),
    ("requires_corroboration", True), ("requires_corroboration", "false"),
    ("consumer_scope", "foreign"), ("unexpected", True),
])
def test_f08_authenticated_obligations_veto(factory, field, value):
    p = factory(jobs=lambda rows, _: rows[0].update({field: value}))
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F08")


@pytest.mark.parametrize("fault", ["subset", "required", "unknown", "corroboration", "unaccounted"])
def test_f08_enumerates_all_authenticated_consumers_not_requested_subset(factory, fault):
    def jobs(rows, user):
        extra = {**rows[0], "ref": "job:second", "goal": "Another current use"}
        if fault in {"required", "unknown"}:
            extra["obligation"] = fault
        elif fault == "corroboration":
            extra["requires_corroboration"] = True
        elif fault == "unaccounted":
            line = user.splitlines()[1]
            extra["requirements"] = [dict(start=user.index(line), end=user.index(line)+len(line))]
        rows.append(extra)
    p = factory(jobs=jobs)
    if fault != "subset":
        p.expansion["consumer_refs"].append("job:second")
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F08")


def test_f08_complete_multiple_optional_consumers(factory):
    p = factory(jobs=lambda rows, _: rows.append({**rows[0], "ref": "job:second"}))
    p.expansion["consumer_refs"].append("job:second")
    prove_f08(p)


@pytest.mark.parametrize("fault", ["v1", "missing_corroboration", "incomplete", "unknown_scope", "duplicate", "overflow", "mixed", "unlinked"])
def test_f08_missing_source_facts_never_become_positive_absence(factory, fault):
    def jobs(rows, _):
        if fault == "missing_corroboration": rows[0].pop("requires_corroboration")
        if fault == "duplicate": rows.append(dict(rows[0]))
        if fault == "overflow": rows.extend({**rows[0], "ref": "job:n"+str(i)} for i in range(4))
        if fault == "unlinked": rows[0]["requirements"][0]["start"] += 1
    def envelope(body):
        if fault == "incomplete": body["complete"] = False
        if fault == "unknown_scope": body["inventory_scope"] = "requested_subset"
    def mutate(text):
        return text + '\n```hermes-owned-delegation-v1\n{"version":1,"jobs":[]}\n```\n' if fault == "mixed" else text
    p = factory(version=1 if fault == "v1" else 2, jobs=jobs, envelope=envelope, input_change=mutate)
    assert p.rt.owned_planning_inventory is None
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F08")


@pytest.mark.parametrize("feature", ["F05", "F08"])
@pytest.mark.parametrize("fault", ["grant", "disabled", "policy", "no_contract", "steering", "session", "foreign_profile", "no_db", "closed_db", "unload"])
def test_configured_authority_negative_vertical(factory, feature, fault, monkeypatch, tmp_path):
    def policy(section):
        if fault == "grant": section["plugins"]["fixture-supervisor"]["grants"].remove("cancel_child")
        if fault == "disabled": section["plugins"]["fixture-supervisor"]["owned_delegation"]["allow_optional_readonly"] = False
        if fault == "policy": section["plugins"]["fixture-supervisor"]["owned_delegation"]["consumer_contract"] = CONSUMERS
    p = factory(policy_change=policy, input_change=(lambda s: s.split("```", 1)[0]) if fault == "no_contract" else None)
    if fault not in {"grant", "disabled"}:
        assert isinstance(p.owner, ConfiguredDelegationOwner)
    if feature == "F08": p.pass_commit(1); p.pass_commit(2)
    p.commit([p.delegation if feature == "F05" else p.expansion])
    if fault == "steering": accept(p.a, "- Current instruction without a new census.", continuation=True)
    if fault == "session": p.a.session_id = "foreign-session"
    if fault == "foreign_profile": monkeypatch.setenv("HERMES_HOME", str(tmp_path / "foreign"))
    if fault == "no_db": p.a._session_db = None
    if fault == "closed_db":
        assert p.a._session_db is not None
        p.a._session_db.close()
    if fault == "unload": p.native.bridge.native.unregister()
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, feature)


@pytest.mark.parametrize("fault", ["wrong_goal", "wrong_route", "wrong_request", "prefill"])
def test_f05_binds_actual_candidate_and_native_recipe(factory, fault):
    p = factory(jobs=(lambda rows, _: rows[0].update(goal="Different authorized goal")) if fault == "wrong_goal" else None)
    if fault == "wrong_route": p.delegation["operation"]["route_id"] = "native:other"
    if fault == "wrong_request": p.delegation["operation"]["arguments"]["tasks"][0]["supervision"]["consumer_refs"] = ["job:foreign"]
    if fault == "prefill": p.a.prefill_messages = [{"role": "user", "content": "parent prefill"}]
    p.commit([p.delegation])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F05")


@pytest.mark.parametrize("feature", ["F05", "F08"])
@pytest.mark.parametrize("answer", ["ambiguous", "malformed", "negative", "expired"])
def test_configured_real_transport_noop_deadline(factory, feature, answer):
    prove_transport(factory(), feature, answer)


@pytest.mark.parametrize("feature", ["F05", "F08"])
def test_configured_inflight_authenticated_change_revalidates(factory, feature):
    p = factory()
    if feature == "F08": p.pass_commit(1); p.pass_commit(2)
    p.commit([p.delegation if feature == "F05" else p.expansion])
    p.native.on_response.append(lambda: accept(p.a, "- A new required result supersedes optional work.", continuation=True))
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert len(feature_calls(p.native, feature)) == 1


@pytest.mark.parametrize("feature", ["F05", "F08"])
def test_competing_configured_owners_do_not_select_authority(factory, feature):
    p = factory(competing=True)
    assert p.owner is None
    if feature == "F08": p.pass_commit(1); p.pass_commit(2)
    p.commit([p.delegation if feature == "F05" else p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, feature)


def test_real_accepted_v1_launch_contract_is_not_an_f08_inventory(factory):
    # Use a bounded valid v1 instruction, not an oversized/malformed negative.
    from tests.agent.test_owned_delegation_policy import instruction, GOAL
    p = factory(version=1)
    accept(p.a, instruction())
    record, = p.rt.owned_consumer_contracts.values()
    assert record.goal == GOAL and record.consumer.obligation == "optional"
    assert p.rt.owned_planning_inventory is None
    operation = copy.deepcopy(p.delegation["operation"])
    task = operation["arguments"]["tasks"][0]
    task["goal"] = GOAL
    task["supervision"]["consumer_refs"] = [record.ref]
    launch_view = p.owner.planning_preflight(p.a, task["supervision"], goal=GOAL, operation=operation)
    assert launch_view and launch_view["obligation"] == "optional"  # v1 launch semantics preserved
    assert p.owner.planning_preflight(p.a, task["supervision"],
        operation=p.expansion["operation"], require_inventory=True) is None


def test_prior_ordinary_launch_is_not_hidden_by_proposed_work_census(factory, monkeypatch):
    p = factory()
    prove_f05(p, monkeypatch)
    assert p.owner.list_owned()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F08")


def test_ordinary_legacy_worker_is_an_unaccounted_native_consumer(factory, monkeypatch):
    from contextvars import copy_context
    import threading
    from tools import delegate_tool as dt
    from tools.delegate_tool_child_run import _attach_child, _detach_child
    from tests.agent.test_owned_delegation_bridge import Child
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    started, release = threading.Event(), threading.Event()
    children, replies, errors = [], [], []
    def build(**kwargs):
        child = Child("legacy-native-worker")
        _attach_child(p.a, child)  # actual constructor's native registration
        children.append(child)
        return child
    def run(*, child, task_index, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return dict(task_index=task_index, status="completed", result="Unaccounted legacy result", duration_seconds=0)
        finally:
            _detach_child(p.a, child)
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k:
        dict(model="synthetic", provider="synthetic", base_url=None, api_key=None, api_mode=None))
    monkeypatch.setattr(dt, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: ("", "", None, None, False))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: (None, [], []))
    def launch():
        try:
            replies.append(json.loads(dt.delegate_task(parent_agent=p.a, goal="Legacy independent use")))
        except BaseException as exc:
            errors.append(exc)
    ctx = copy_context()
    thread = threading.Thread(target=lambda: ctx.run(launch))
    thread.start()
    try:
        assert started.wait(3), errors
        assert p.a._active_children and not p.owner.list_owned()
        assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
        assert not feature_calls(p.native, "F08")
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors and replies[0]["results"][0]["status"] == "completed"
    assert not children[0].stopped.is_set() and not p.a._active_children


@pytest.mark.parametrize("registry", ["attached", "live", "async"])
def test_native_inventory_lock_contention_is_unknown_not_empty(factory, registry):
    from tools import delegate_tool_registry, async_delegation
    p = factory()
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    lock = {"attached": p.a._active_children_lock, "live": delegate_tool_registry._active_subagents_lock,
            "async": async_delegation._records_lock}[registry]
    with lock:
        assert p.rt.dependencies.planning._preflight(p.expansion, optional=True) is None
    assert not feature_calls(p.native, "F08")


def test_untrusted_copy_cannot_restore_authenticated_inventory(factory):
    from agent.supervision_context import accept_pending_input
    p = factory()
    accept(p.a, "- The previous census is no longer current.", continuation=True)
    assert p.rt.owned_planning_inventory is None and not p.rt.owned_consumer_contracts
    assert accept_pending_input(p.a, {"text": p.user, "kind": "cli"}) is None
    assert p.rt.owned_planning_inventory is None and not p.rt.owned_consumer_contracts


def test_configured_canonical_busy_writer_baseline(factory):
    prove_busy(factory())


def test_configured_canonical_rehydration_deletion_reset_and_retention(factory):
    prove_storage(factory())


def test_configured_actual_constructor_recipe(factory, monkeypatch):
    prove_recipe(factory(), monkeypatch)


def test_f08_separate_optional_label_policy_default_off(factory):
    p = factory()
    p.native.rig.config["supervision"].pop("planning")
    (p.native.rig.home / "config.yaml").write_text(json.dumps(p.native.rig.config))
    from hermes_cli.config import load_config_readonly
    load_config_readonly()
    assert p.owner.current()  # worker grant is not optional planning permission
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a, p.history).api_messages)
    assert not feature_calls(p.native, "F08")
