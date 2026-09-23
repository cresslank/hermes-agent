"""Ordinary main plan writes -> real Jev registry/HTTP codec -> next API assembly.

All model transport is strict MockTransport. Files, SessionDB, tool-owner read
receipts, dependency graph, source parser and request assembly are real. No intent,
pass, opportunity or optionality setter is used as a producer.
"""
import copy
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.agent.supervision_test_support import rig as base_rig, accept
from tests.agent.test_supervision_efficiency import native as native, feature_calls, flush
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _make_agent, _make_tool_defs
from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_context import digest
from tools.todo_tool import todo_tool


@pytest.fixture
def rig(base_rig):
    previous = base_rig.agent
    agent = _make_agent()
    agent.session_id = previous.session_id
    agent._session_db = previous._session_db
    agent._todo_store = previous._todo_store
    agent._execution_thread_id = threading.get_ident()
    from agent.turn_context import _reset_per_turn_agent_state
    _reset_per_turn_agent_state(agent)
    base_rig.agent = agent
    yield base_rig


def ref(path, text, start=0, end=None):
    return dict(source_ref=str(path), source_revision=digest(text), start=start, end=len(text) if end is None else end)


@pytest.fixture
def planning(native, tmp_path, monkeypatch):
    return make_planning(native, tmp_path, monkeypatch)


def make_planning(native, tmp_path, monkeypatch, *, configure=None):
    from agent.owned_delegation import Consumer, OwnerGrant, install_owner, scoped_file_policy
    from tools.delegation_control_store import SQLiteControlStore
    from agent.tool_executor import _dispatch_authorized_once, _ManagedToolResult, _ToolCallRef, _finalize_tool_batch
    from tools.file_tools import write_file_tool, read_file_tool
    from tools.budget_config import DEFAULT_BUDGET
    from agent.iteration_budget import IterationBudget

    a = native.rig.agent
    a.valid_tool_names = {"read_file", "write_file", "delegate_task", "todo_list"}
    a.tools = _make_tool_defs(*sorted(a.valid_tool_names))
    a.iteration_budget = IterationBudget(40)
    a.prefill_messages = None
    plan, goal, source, method = [tmp_path / n for n in ("plan.md", "goal.txt", "source.txt", "method.txt")]
    user = (f"- Maintain `{plan}` with `{goal}` and `{method}`.\n"
            f"- Assess `{source}` in a separate conversation, not continuing the parent transcript; native workspace guidance is allowed.\n"
            f"- Discretionary background research about `{source}` may be omitted; report uncertainty.")
    goal_text, input_text, method_text = "Assess the supplied source and return its limitations.", "Synthetic source: a bounded background observation.\n", "Read the same local source for optional background."
    native.rig.config["supervision"]["planning"] = {"allow_discretionary_readonly_labels": True}
    if configure is not None:
        user = configure(native, user, goal_text)
    (native.rig.home / "config.yaml").write_text(json.dumps(native.rig.config))
    # Model ordinary configuration loading before authenticated ingress, never
    # refresh the cache from an optional-control fence or install a fixture owner.
    from hermes_cli.config import load_config_readonly
    load_config_readonly()
    rt = accept(a, user)
    with bind_subagent_parent(a):
        listing = json.loads(todo_tool([
            {"id": "candidate", "content": f"Assess `{source}`", "status": "pending"},
            {"id": "parent", "content": f"Maintain `{plan}` and research `{source}`", "status": "in_progress"}], store=a._todo_store))
    clauses = listing["work_map_sources"]["requirements"]
    source_id = clauses[0]["source_message_id"]
    acceptance = ref(source_id, user, clauses[1]["start"], clauses[1]["end"])
    gap = ref(source_id, user, clauses[2]["start"], clauses[2]["end"])
    policy = scoped_file_policy((str(tmp_path),))
    gap_id = next(r.id for r in rt.requirements if r.start == gap["start"])
    consumers, revision = {}, [1]
    if configure is None:
        consumers = {k: Consumer(k, "optional", requirement_ids=(gap_id,)) for k in ("research", "parent:"+a.session_id)}
        owner = install_owner(a, store=SQLiteControlStore(lambda: sqlite3.connect(a._session_db.db_path)),
            grant=OwnerGrant(str(native.rig.home), "fixture-supervisor", True, True),
            consumer_resolver=consumers.get, policy=policy, revision_provider=lambda: tuple(revision),
            consumer_inventory=lambda: tuple(consumers.values()))
    else:
        from agent.owned_delegation import owner_of
        owner = owner_of(a)
        policy = owner._policy if owner else SimpleNamespace(policy_id="host.owned-parent-read.v1")
    monkeypatch.setattr("agent.tool_executor._pre_tool_block", lambda agent, r: (None, r.args))
    monkeypatch.setattr("agent.tool_executor._begin_tool_execution", lambda *args: None)
    monkeypatch.setattr("agent.tool_executor._run_with_activity_heartbeat", lambda agent, name, fn: fn())
    monkeypatch.setattr("agent.terminal_approval_batch.prepare_current_terminal", lambda *args: None)
    monkeypatch.setattr("agent.tool_executor.get_active_env", lambda _: None)
    monkeypatch.setattr("agent.tool_executor.enforce_turn_budget", lambda *a, **k: None)
    monkeypatch.setattr("tools.delegate_tool.is_spawn_paused", lambda: False)
    history = [{"role": "user", "content": user}]
    sequence = [0]
    def call(name, args, *, settle=True):
        sequence[0] += 1
        ident = "native-"+str(sequence[0])
        history.append({"role": "assistant", "content": None, "tool_calls": [{"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        state = _ManagedToolResult(None, args, [], False, False)
        handler = {"read_file": read_file_tool, "write_file": write_file_tool}[name]
        with bind_subagent_parent(a):
            result = _dispatch_authorized_once(a, state, _ToolCallRef(name, args, "default", ident, []),
                execute=lambda values: handler(**values), scope_block=None, display_index=None,
                begin_execution=None, authorization_gate=None)
        history.append({"role": "tool", "tool_call_id": ident, "name": name, "content": result})
        if settle:
            _finalize_tool_batch(a, history, "default", 1, DEFAULT_BUDGET)
        return ident, result
    for path, text in [(goal,goal_text),(source,input_text),(method,method_text)]:
        _, result = call("write_file", {"path": str(path), "content": text})
        assert not json.loads(result).get("error")
    wm = {"version": 1, "requirements": [{"source_message_id": source_id, "start": gap["start"], "end": gap["end"]}],
        "steps": [{"todo_id": "parent", "requirement_indexes": [0], "target_refs": [str(source)]}], "claims": []}
    def commit(rows):
        text = "```hermes-work-map-v1\n"+json.dumps(wm)+"\n```\n```hermes-planning-proposals-v1\n"+json.dumps({"version":1,"records":rows})+"\n```\n"
        return call("write_file", {"path":str(plan), "content":text})
    def inventory():
        with bind_subagent_parent(a):
            return json.loads(todo_tool(store=a._todo_store))["work_map_sources"]["planning_records"]
    read_op = {"tool_name":"read_file", "arguments":{"path":str(source)}, "route_id":"native:read_file"}
    refs = ["research"] if configure is None else ["job:research"]
    req = dict(obligation="optional", consumer_refs=refs, consumer_set_closed=True, effect_policy_id=policy.policy_id)
    delegation = dict(type="delegation_candidate",local_id="review",todo_id="candidate",parent_next_todo_id="parent",
        candidate_span=ref(goal,goal_text),acceptance_refs=[acceptance],input_refs=[ref(source,input_text)],dependency_todo_ids=[],
        required_resource_ref=acceptance, operation={"tool_name":"delegate_task","route_id":"native:delegate_task",
        "arguments":{"tasks":[{"goal":goal_text,"context":input_text,"supervision":req}]}})
    expansion = dict(type="optional_expansion",local_id="next",gap_refs=[gap],completion_criteria_refs=[gap],
        source_method_ref=ref(method,method_text),operation=read_op,consumer_refs=refs,effect_policy_id=policy.policy_id,obligation_request="optional")
    def pass_commit(index, *, close=True):
        # Distinct real line-window operations; an unchanged dedup stub is not a
        # complete native source inventory and is tested separately as ineligible.
        pass_op = {**read_op, "arguments": {**read_op["arguments"], "limit": index + 1}}
        d = dict(type="research_pass_open",local_id="pass-"+str(index),gap_refs=[gap],completion_criteria_refs=[gap],
                 source_method_ref=ref(method,method_text),operations=[pass_op])
        commit([d])
        opened = inventory()[-1]
        assert opened["status"] == "open"
        call("read_file",pass_op["arguments"])
        settled = inventory()[-1]
        assert settled["status"] == "settled", settled
        row = dict(type="research_pass_close",pass_id=settled["id"],member_dispositions=[dict(
            member_id=m["member_id"],source=m["source"],disposition="rejected",rationale_ref=gap) for m in settled["members"]])
        if close:
            commit([row])
            assert inventory()[-1]["status"] == "disposition_committed"
        return row
    return SimpleNamespace(native=native, a=a, rt=rt, call=call, commit=commit, inventory=inventory,
        history=history, delegation=delegation, expansion=expansion, pass_commit=pass_commit,
        consumers=consumers, revision=revision, policy=policy, owner=owner, source=source, plan=plan, user=user)


def test_f05_commit_plugin_next_request_then_ordinary_launch(planning, monkeypatch):
    p = planning
    from tools import delegate_tool as dt
    def forbidden(*a, **k):
        raise AssertionError("preflight must not construct, resolve credentials or charge spawn budget")
    with monkeypatch.context() as guard:
        for name in ("_build_child_preserving_parent_tools", "_build_child_agent", "_resolve_delegation_credentials", "_resolve_child_runtime", "_oneshot_spawn_budget"):
            guard.setattr(dt,name,forbidden)
        p.commit([p.delegation])
        assert p.inventory(), "candidate not admitted"
        graph = p.rt.dependencies.planning
        assert graph.facts(next(reversed(graph.nodes.values()))) is not None, (p.inventory(), graph._budget(), graph._preflight(p.delegation), p.rt.plan_steps())
        original = copy.deepcopy(p.history)
        assert not feature_calls(p.native,"F05")
        result = assemble(p.a,p.history)
        assert "Task-bound planning advisory" in str(result.api_messages), json.dumps({"inspect":p.native.bridge.supervisor.inspect(), "calls":p.native.calls, "requests":list(p.rt.dependencies.planning.requests), "receipts":[r.__dict__ for r in p.rt.receipts.values()], "view_type":str(type(getattr(p.a,"_supervision_views",None)))},default=str)
        assert p.history == original
        assert p.inventory()[-1]["status"] == "advised"
        assert not p.owner.list_owned()
        assert len(feature_calls(p.native,"F05")) == 1
        f = feature_calls(p.native,"F05")[0]["state"]["facts"]
        assert f["overhead_favorable"] is False and f["parallelism_favorable"] is False
        assert "native:delegate_task" in str(result.api_messages)
        assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    from tests.agent.test_owned_delegation_bridge import Child
    child = Child("after-advice")
    monkeypatch.setattr(dt,"_build_child_preserving_parent_tools",lambda **kw:child)
    creds=dict(model="synthetic",provider="synthetic",base_url=None,api_key=None,api_mode=None)
    monkeypatch.setattr(dt,"_resolve_delegation_credentials",lambda *a,**kw:creds)
    monkeypatch.setattr(dt,"_announce_batch",lambda *a:None)
    monkeypatch.setattr(dt,"_capture_origin",lambda:("","",None,None,False))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts",lambda *a,**kw:(None,[],[]))
    submissions=[]
    monkeypatch.setattr(dt,"_run_batch",lambda batch,background: submissions.append(batch) or json.dumps({"dispatched":True}))
    result=json.loads(dt.delegate_task(parent_agent=p.a,**p.delegation["operation"]["arguments"]))
    assert result == {"dispatched":True} and len(submissions)==1 and len(p.owner.list_owned())==1
    assert p.inventory()[-1]["status"] == "issued"
    assert not child.stopped.is_set()


def test_f08_native_finite_passes_dispositions_next_request_withdrawal(planning):
    p=planning
    p.pass_commit(1); p.pass_commit(2)
    assert not feature_calls(p.native,"F08")
    before=p.rt.requirements
    p.commit([p.expansion])
    assert p.inventory()[-1]["local_id"] == "next"
    graph=p.rt.dependencies.planning
    assert graph.facts(next(reversed(graph.nodes.values()))) is not None, (p.inventory(), graph._budget(), graph._preflight(p.expansion,optional=True),p.rt.action_requirements(p.expansion["operation"]["arguments"]))
    original=copy.deepcopy(p.history)
    result=assemble(p.a,p.history)
    assert "withdrawing this optional expansion" in str(result.api_messages), json.dumps({"inspect":p.native.bridge.supervisor.inspect(), "calls":p.native.calls, "requests":list(p.rt.dependencies.planning.requests), "receipts":[r.__dict__ for r in p.rt.receipts.values()], "view_type":str(type(getattr(p.a,"_supervision_views",None)))},default=str)
    assert p.history==original and p.rt.requirements==before and not p.rt.closed
    node=p.inventory()[-1]
    assert node["status"]=="advised" and not p.owner.list_owned()
    assert len(feature_calls(p.native,"F08"))==1
    passes=feature_calls(p.native,"F08")[0]["state"]["facts"]["passes"]
    assert passes[0]["rejected_evidence_ids"]==passes[1]["rejected_evidence_ids"]
    assert all(x["accepted_evidence_ids"]==[] for x in passes)
    p.commit([{"type":"withdraw_expansion","expansion_id":node["id"]}])
    assert p.inventory()[-1]["status"]=="withdrawn_by_parent"


@pytest.mark.parametrize("fault",["prefill","parent_dependency","generic","no_resource","missing_input","completed","no_tool","unknown_budget","source_change","no_owner"])
def test_f05_negative_native_authority(planning,fault):
    p=planning; d=copy.deepcopy(p.delegation)
    if fault=="prefill": p.a.prefill_messages=[{"role":"user","content":"inherited"}]
    elif fault=="parent_dependency": d["dependency_todo_ids"]=["parent"]
    elif fault=="generic": d["required_resource_ref"]=p.expansion["gap_refs"][0]; d["acceptance_refs"]=[d["required_resource_ref"]]
    elif fault=="no_resource": d["required_resource_ref"]["start"]+=1
    elif fault=="missing_input": d["input_refs"][0]["source_revision"]="0"*64
    elif fault=="completed":
        with bind_subagent_parent(p.a): todo_tool([{"id":"candidate","content":"Finished","status":"completed"}],merge=True,store=p.a._todo_store)
    elif fault=="no_tool": p.a.valid_tool_names.remove("delegate_task")
    elif fault=="unknown_budget": p.a.iteration_budget=SimpleNamespace(remaining=5)
    elif fault=="no_owner": p.a._owned_delegation_owner=None
    p.commit([d])
    if fault=="source_change": p.call("write_file",{"path":str(p.source),"content":"changed"})
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert not feature_calls(p.native,"F05")


@pytest.mark.parametrize("fault",["default_off","required","unknown","result","cleanup","effects","corroboration","unaccounted","extra_consumer","no_inventory","no_close","bad_partition","foreign_member","changed_pin"])
def test_f08_obligations_and_dispositions_veto(planning,fault):
    p=planning
    p.pass_commit(1)
    close=p.pass_commit(2,close=False)
    if fault=="bad_partition": close["member_dispositions"]*=2
    elif fault=="foreign_member": close["member_dispositions"][0]["member_id"]="foreign"
    elif fault=="changed_pin": close["member_dispositions"][0]["source"]["source_revision"]="0"*64
    if fault!="no_close": p.commit([close])
    if fault=="default_off":
        p.native.rig.config["supervision"].pop("planning")
        (p.native.rig.home/"config.yaml").write_text(json.dumps(p.native.rig.config))
    if fault in {"required","unknown"}: p.consumers["research"]=replace(p.consumers["research"],obligation=fault)
    if fault in {"result","cleanup","effects","corroboration"}: p.consumers["research"]=replace(p.consumers["research"],**{"requires_"+fault:True})
    if fault=="extra_consumer":
        p.consumers["corroborator"]=replace(p.consumers["research"],ref="corroborator",requires_corroboration=True)
    if fault=="no_inventory": p.owner._consumer_inventory=None
    if fault=="unaccounted":
        for k in p.consumers: p.consumers[k]=replace(p.consumers[k],requirement_ids=())
    p.commit([p.expansion])
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert not feature_calls(p.native,"F08")


@pytest.mark.parametrize("feature",["F05","F08"])
@pytest.mark.parametrize("change",["instruction","consumer_revision","plugin_revoke","source"])
def test_inflight_revalidation(planning,feature,change):
    p=planning
    if feature=="F08": p.pass_commit(1);p.pass_commit(2)
    p.commit([p.delegation if feature=="F05" else p.expansion])
    def revoke():
        if change=="instruction": accept(p.a,"- Changed current instruction.",continuation=True)
        elif change=="consumer_revision": p.revision[0]+=1
        elif change=="plugin_revoke": p.native.bridge.native.unregister()
        else: p.rt.invalidate_artifact(str(p.source))
    p.native.on_response.append(revoke)
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert len(feature_calls(p.native,feature))==1


@pytest.mark.parametrize("feature", ["F05", "F08"])
@pytest.mark.parametrize("answer", ["ambiguous", "malformed", "negative", "expired"])
def test_real_transport_noop_and_expiry(planning, feature, answer):
    p = planning
    if feature == "F08":
        p.pass_commit(1); p.pass_commit(2)
    p.commit([p.delegation if feature == "F05" else p.expansion])
    release = threading.Event()
    def mutate(values):
        if answer == "malformed":
            values.clear()
        elif answer in {"ambiguous", "negative"}:
            for value in values.values():
                if value["type"] == "noul":
                    value["noul"] = .5 if answer == "ambiguous" else (0.0 if feature == "F05" else 1.0)
    if answer == "expired":
        # A response held until the existing owner wait expires, not a changed clock,
        # threshold or retry. The finally releases the owned transport rendezvous.
        p.native.on_response.append(lambda: release.wait(2))
    else:
        p.native.answer_mutators.append(mutate)
    try:
        result = assemble(p.a, p.history)
        assert "Task-bound planning advisory" not in str(result.api_messages)
    finally:
        release.set()
        flush(p.native)
    assert len(feature_calls(p.native, feature)) == 1
    assert p.inventory()[-1]["status"] == "proposed"
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)


@pytest.mark.parametrize("origin", ["read", "child"])
def test_copied_annex_has_no_commit_authority(planning, origin):
    p = planning
    if origin == "child":
        p.a._delegate_id = "child-origin"
        try:
            p.commit([p.delegation])
        finally:
            p.a._delegate_id = None
    else:
        text = "```hermes-planning-proposals-v1\n" + json.dumps({"version": 1, "records": [p.delegation]}) + "\n```\n"
        p.plan.write_text(text)
        p.call("read_file", {"path": str(p.plan)})
    assert not p.inventory()
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert not feature_calls(p.native, "F05")


@pytest.mark.parametrize("fault", ["unknown_key", "duplicate_id", "duplicate_json_key", "nonfinite", "oversize", "two_blocks"])
def test_strict_annex_rejects_ambiguous_declarations(planning, fault):
    p = planning
    doc = {"version": 1, "records": [copy.deepcopy(p.delegation)]}
    if fault == "unknown_key": doc["records"][0]["authorized"] = True
    if fault == "duplicate_id": doc["records"] *= 2
    raw = json.dumps(doc)
    if fault == "duplicate_json_key": raw = raw.replace('"version": 1', '"version": 1, "version": 1')
    if fault == "nonfinite": raw = raw.replace('"version": 1', '"version": NaN')
    if fault == "oversize": raw += " " * 16384
    text = "```hermes-planning-proposals-v1\n"+raw+"\n```\n"
    if fault == "two_blocks": text *= 2
    p.call("write_file", {"path":str(p.plan), "content":text})
    assert not p.inventory()
    assert not feature_calls(p.native,"F05")


def test_no_predispatch_enrollment_and_partial_pass_never_close(planning):
    p = planning
    op = copy.deepcopy(p.expansion["operation"])
    p.call("read_file", op["arguments"])
    d = {k:copy.deepcopy(p.expansion[k]) for k in ("gap_refs","completion_criteria_refs","source_method_ref")}
    other = {**op, "arguments": {**op["arguments"], "limit": 7}}
    p.commit([{**d,"type":"research_pass_open","local_id":"finite","operations":[op,other]}])
    node = p.inventory()[-1]
    assert node["members"] == [] and node["status"] == "open"
    p.call("read_file", other["arguments"])
    node = p.inventory()[-1]
    assert node["status"] == "open" and len(node["members"]) == 1
    p.commit([{"type":"research_pass_close","pass_id":node["id"],"member_dispositions":[]}])
    assert p.inventory()[-1]["status"] == "open"
    # Exact repeated native request yields the existing dedup stub, not a source
    # inventory. The normal tool result is unchanged and the pass becomes invalid.
    _, result = p.call("read_file", op["arguments"])
    assert json.loads(result)["content_returned"] is False
    assert p.inventory()[-1]["status"] == "invalid"


def test_declared_operation_already_issued_in_same_batch_has_no_advice(planning):
    p = planning
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    p.call("read_file", p.expansion["operation"]["arguments"])
    assert p.inventory()[-1]["status"] == "issued"
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert not feature_calls(p.native,"F08")


def test_canonical_rehydration_deletion_reset_and_retention(planning):
    from agent.supervision_dependencies import DependencyOwner
    from agent.supervision_planning_records import load, prune, RETENTION_SECONDS
    p = planning
    p.pass_commit(1); p.pass_commit(2)
    p.commit([p.expansion])
    before = p.inventory()
    restored = DependencyOwner(p.rt)
    assert restored.planning.inventory() == before
    rows = load(p.rt)
    assert {r["kind"] for r in rows} == {"research_pass", "gap_obligation"}
    assert not p.a._session_db.delete_session_if_empty(p.a.session_id)
    with sqlite3.connect(p.a._session_db.db_path) as conn:
        latest = conn.execute("SELECT max(updated_at) FROM supervision_owner_records").fetchone()[0]
        prune(conn, latest + RETENTION_SECONDS + 1)
        # Historical closed passes may age out, never an uncertain proposed expansion.
        assert conn.execute("SELECT status FROM supervision_owner_records").fetchall() == [("proposed",)]
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id=?",(p.a.session_id,))
    assert load(p.rt) == ()  # canonical generation revocation refuses recovery
    assert DependencyOwner(p.rt).planning.inventory() == []
    with sqlite3.connect(p.a._session_db.db_path) as conn:
        assert conn.execute("SELECT status FROM supervision_owner_records").fetchall() == [("stale",)]
        conn.execute("DELETE FROM sessions WHERE id=?",(p.a.session_id,))
        assert conn.execute("SELECT count(*) FROM supervision_owner_records").fetchone()[0] == 0
    assert load(p.rt) == ()


def test_canonical_writer_busy_fails_closed_without_side_database(planning):
    p = planning
    with sqlite3.connect(p.a._session_db.db_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        p.commit([p.delegation])
        assert not p.inventory()
    assert "Task-bound planning advisory" not in str(assemble(p.a,p.history).api_messages)
    assert not feature_calls(p.native,"F05")


def test_absent_plugin_baseline_does_not_change_request_or_launch(planning):
    p=planning
    p.native.bridge.close()
    p.commit([p.delegation])
    original=copy.deepcopy(p.history)
    result=assemble(p.a,p.history)
    assert p.history==original
    assert "Task-bound planning advisory" not in str(result.api_messages)
    assert not feature_calls(p.native,"F05")


def test_actual_constructor_and_run_share_isolation_recipe(planning, monkeypatch):
    from unittest.mock import MagicMock
    from tools import delegate_tool as dt
    from tools.delegate_context_recipe import constructor_context, run_context, separate_conversation
    p=planning
    built=[]
    child=MagicMock()
    def construct(**kwargs):
        built.append(kwargs)
        return child
    monkeypatch.setattr("run_agent.AIAgent",construct)
    monkeypatch.setattr(dt,"_resolve_child_runtime",lambda *a,**kw:{"model":"synthetic", "provider":"synthetic", "base_url":"https://synthetic.invalid"})
    monkeypatch.setattr(dt,"_resolve_child_credential_pool",lambda *a,**kw:None)
    created=dt._build_child_agent(task_index=0,goal="Assess source",context="supplied only",toolsets=None,
        model="synthetic",max_iterations=2,parent_agent=p.a,task_count=1)
    assert created is child and len(built)==1
    kwargs=built[0]
    try:
        recipe=constructor_context(p.a)
        assert all(kwargs[key]==value for key,value in recipe.items())
        assert kwargs["skip_context_files"] is True and kwargs["skip_memory"] is True
        assert kwargs["prefill_messages"] is None and kwargs["iteration_budget"] is None
        assert separate_conversation(p.a)
        assert set(run_context("goal","child-task",None)) == {"user_message","task_id","stream_callback"}
        assert "conversation_history" not in kwargs
    finally:
        if kwargs["session_db"] is not None: kwargs["session_db"].close()


def test_canonical_delegation_recovery_keeps_source_bodies_with_original_owner(planning):
    from agent.supervision_dependencies import DependencyOwner
    from agent.supervision_planning_records import load
    p=planning
    p.commit([p.delegation])
    stored,=load(p.rt)
    args=stored["declaration"]["operation"]["arguments"]
    assert set(args["tasks"][0])=={"supervision"}
    assert "Assess the supplied source" not in json.dumps(stored)
    graph=DependencyOwner(p.rt).planning
    assert graph.inventory()==p.inventory()
    assert graph.facts(next(iter(graph.nodes.values()))) is not None
    p.rt.invalidate_artifact(str(p.source))
    unavailable=DependencyOwner(p.rt).planning
    assert unavailable.inventory()[0]["status"]=="stale"


def test_same_epoch_canonical_cap_refuses_instead_of_evicting(planning):
    from agent.supervision_planning_records import MAX_RECORDS, load
    p=planning
    for index in range(MAX_RECORDS + 1):
        d=copy.deepcopy(p.delegation); d["local_id"]="bounded-"+str(index)
        p.commit([d])
    rows=load(p.rt)
    assert len(rows)==MAX_RECORDS
    assert any(r["declaration"]["local_id"]=="bounded-0" for r in rows)
    assert all(r["declaration"]["local_id"]!="bounded-"+str(MAX_RECORDS) for r in rows)
    assert not feature_calls(p.native,"F05")
