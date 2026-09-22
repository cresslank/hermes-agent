"""Planning nodes owned by DependencyOwner, not a second efficiency authority.

Only exact committed annexes produce nodes. Native pre-dispatch enrollment and
file-owner descriptors produce pass membership; later commits select dispositions.
Request-local advice is not execution, truth, withdrawal or requirement completion.
"""
from collections import OrderedDict
from contextvars import ContextVar
import copy
import json

from agent.supervision_context import digest, parse_planning_proposals
from agent.supervision_efficiency import _pin, OpportunityCompleteness
from agent.supervision_types import Action
from agent import supervision_planning_records as records

_capture: ContextVar[list | None] = ContextVar("planning_native_read", default=None)


def publish_read(path, text):
    capture = _capture.get()
    if capture is not None:
        capture.append(dict(source_ref=path, source_revision=digest(text), start=0, end=len(text)))


def run_capture(agent, name, arguments, call_id, execute):
    from agent.supervision_policy import runtime_for_agent
    rt = runtime_for_agent(agent)
    if rt is None or name != "read_file":
        return execute()
    capture = []
    token = _capture.set(capture)
    try:
        result = execute()
        rt.dependencies.planning.returned(call_id, arguments, result, capture)
        return result
    finally:
        _capture.reset(token)


def operation(value):
    if (type(value) is not dict or set(value) != {"tool_name", "arguments", "route_id"}
            or value["tool_name"] not in {"read_file", "delegate_task"}
            or value["route_id"] != "native:" + value["tool_name"]
            or type(value["arguments"]) is not dict):
        raise ValueError("operation")
    return _pin((value["tool_name"], value["arguments"]))


class PlanningGraph:
    def __init__(self, dependencies):
        self.owner = dependencies
        self.rt = dependencies.runtime
        self.nodes = OrderedDict()
        self.returns = {}
        self.requests = {}
        self.loaded_work = None

    def clear(self):
        self.nodes.clear()
        self.returns.clear()
        self.requests.clear()
        self.loaded_work = None

    def _load(self):
        work = self.rt.revision.work_id
        if self.loaded_work != work:
            self.clear()
            for row in records.load(self.rt):
                if row["kind"] not in {"planning", "research_pass", "gap_obligation"}:
                    continue
                if row["declaration"]["type"] == "delegation_candidate":
                    d = row["declaration"]
                    try:
                        task = d["operation"]["arguments"]["tasks"][0]
                        task.update(goal=self.resolve(d["candidate_span"]), context="\n".join(self.refs(d["input_refs"], 16)))
                        if operation(d["operation"]) != row["operation_pin"]:
                            raise ValueError("changed_operation")
                    except (KeyError, ValueError, TypeError):
                        row["status"] = "stale"  # cannot rehydrate missing original bytes
                self.nodes[row["id"]] = row
            self.loaded_work = work

    def epoch(self):
        r = self.rt.revision
        return [r.profile, r.lineage, r.work_id, r.run_generation, r.instruction_event, r.requirements]

    def resolve(self, ref, *, accepted=False):
        if (type(ref) is not dict or set(ref) != {"source_ref", "source_revision", "start", "end"}
                or type(ref["source_ref"]) is not str or type(ref["source_revision"]) is not str
                or type(ref["start"]) is not int or type(ref["end"]) is not int):
            raise ValueError("exact_ref")
        source = self.rt.sources.get(ref["source_ref"], self.rt.artifacts.get(ref["source_ref"]))
        if (source is None or digest(source) != ref["source_revision"]
                or not 0 <= ref["start"] < ref["end"] <= len(source)
                or ref["end"] - ref["start"] > 2400):
            raise ValueError("source_unavailable")
        if accepted and not any(r.source_message_id == ref["source_ref"] and
                r.start == ref["start"] and r.end == ref["end"] for r in self.rt.requirements):
            raise ValueError("complete_accepted_clause_required")
        return source[ref["start"]:ref["end"]]

    def refs(self, values, maximum, *, accepted=False):
        if type(values) is not list or not 1 <= len(values) <= maximum:
            raise ValueError("reference_count")
        if len({_pin(r) for r in values}) != len(values):
            raise ValueError("duplicate_ref")
        return [self.resolve(r, accepted=accepted) for r in values]

    def _save(self, rows):
        if records.save(self.rt, rows):
            self.nodes.update((r["id"], r) for r in rows)
            return True
        # Durability is eligibility, not an optional best-effort receipt.
        for row in rows:
            if row["id"] in self.nodes:
                self.nodes[row["id"]] = {**self.nodes[row["id"]], "status": "invalid"}
        return False

    def _transition(self, node, status, **updates):
        return self._save([{**node, **updates, "status": status, "revision": node["revision"] + 1}])

    def committed(self, path, text):
        self.rt._assert_owner(tool_worker=True)
        self._load()
        rt = self.rt
        declarations = parse_planning_proposals(text, artifact_ref=path,
            designated_refs=rt.designated_artifact_refs())
        # A replacement cannot silently preserve an old proposed candidate binding.
        for node in tuple(self.nodes.values()):
            if node["path"] == path and node["pin"] != digest(text) and node["status"] == "proposed":
                self._transition(node, "superseded")
        if declarations is None:
            return
        for declaration in declarations:
            try:
                kind = declaration["type"]
                if kind == "research_pass_close":
                    self._close(path, text, declaration)
                elif kind == "withdraw_expansion":
                    node = self.nodes.get(declaration["expansion_id"])
                    if (node and node["declaration"]["type"] == "optional_expansion"
                            and node["status"] in {"proposed", "advised", "superseded"}
                            and node["epoch"] == self.epoch()):
                        self._transition(node, "withdrawn_by_parent", withdrawal={"path": path, "pin": digest(text)})
                else:
                    self._open(path, text, declaration)
            except (KeyError, ValueError, TypeError, IndexError):
                continue  # malformed declarations never become opportunities

    def _open(self, path, text, d):
        rt = self.rt
        ident = "planning:" + _pin((self.epoch(), path, digest(text), d["local_id"]))
        if ident in self.nodes or len(self.nodes) + len(self.owner.edges) >= 64:
            return
        if any(n["declaration"].get("local_id") == d["local_id"] and n["epoch"] == self.epoch()
               for n in self.nodes.values()):
            return  # local identity is not rebound by a later write
        kind = d["type"]
        if kind == "delegation_candidate":
            if sum(n["status"] == "proposed" for n in self.nodes.values()) >= 8:
                return
            self.resolve(d["candidate_span"])
            self.refs(d["acceptance_refs"], 2, accepted=True)
            self.refs(d["input_refs"], 16)
            self.resolve(d["required_resource_ref"], accepted=True)
            if d["required_resource_ref"] not in d["acceptance_refs"]:
                raise ValueError("resource_acceptance")
            todos, _ = rt.plan_steps()
            if (d["todo_id"] not in todos or d["parent_next_todo_id"] not in todos
                    or d["todo_id"] == d["parent_next_todo_id"]
                    or type(d["dependency_todo_ids"]) is not list or len(d["dependency_todo_ids"]) > 16
                    or len(set(d["dependency_todo_ids"])) != len(d["dependency_todo_ids"])):
                raise ValueError("todo_binding")
            operation(d["operation"])
        else:
            self.refs(d["gap_refs"], 4, accepted=True)
            self.refs(d["completion_criteria_refs"], 4, accepted=True)
            self.resolve(d["source_method_ref"])
            ops = d["operations"] if kind == "research_pass_open" else [d["operation"]]
            if type(ops) is not list or not 1 <= len(ops) <= 16:
                raise ValueError("finite_operations")
            pins = [operation(op) for op in ops]
            if len(set(pins)) != len(pins) or any(op["tool_name"] != "read_file" for op in ops):
                raise ValueError("finite_read_operations")
        node = dict(id=ident, kind={"research_pass_open": "research_pass",
                    "delegation_candidate": "planning", "optional_expansion": "gap_obligation"}[kind],
                    declaration=copy.deepcopy(d), path=path, pin=digest(text), epoch=self.epoch(),
                    todo_revision=rt.plan_steps()[1], revision=1,
                    status="open" if kind == "research_pass_open" else "proposed", enrollment={}, members={})
        if kind == "optional_expansion":
            node["obligation_snapshot"] = self._preflight(d, optional=True)
        self._save([node])

    def _current(self, node, *, plan=True):
        rt = self.rt
        return (not rt.closed and node["epoch"] == self.epoch()
                and node["todo_revision"] == rt.plan_steps()[1] and records.current(rt, node)
                and (not plan or rt.artifact_pins.get(node["path"]) == node["pin"]))

    def dispatch(self, name, arguments, call_id):
        """Enroll BEFORE actual dispatch; never block, cancel or alter the operation."""
        with self.rt.lock:
            self._load()
            pin = _pin((name, arguments))
            matching = []
            for node in tuple(self.nodes.values()):
                d = node["declaration"]
                if node["status"] in {"proposed", "advised", "superseded"} and operation(d["operation"]) == pin:
                    self._transition(node, "issued", call_id=call_id)
                if node["status"] == "open" and self._current(node, plan=False):
                    for op in d["operations"]:
                        if operation(op) == pin:
                            matching.append(node)
            if len(matching) != 1:
                for node in matching:
                    self._transition(node, "invalid")
                return
            node = matching[0]
            if pin in node["enrollment"]:
                self._transition(node, "invalid")
                return
            self._transition(node, "open", enrollment={**node["enrollment"], pin: call_id})

    def returned(self, call_id, arguments, result, capture):
        with self.rt.lock:
            if (len(self.returns) < 16 and type(result) is str and len(capture) == 1
                    and capture[0]["source_ref"] == arguments.get("path")):
                self.returns[call_id] = (digest(result), capture[0])

    def settled(self, messages):
        with self.rt.lock:
            opener = next((i for i in range(len(messages)-1, -1, -1)
                           if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")), None)
            if opener is None:
                return
            rows = {m.get("tool_call_id"): m.get("content") for m in messages[opener+1:] if m.get("role") == "tool"}
            for node in tuple(self.nodes.values()):
                if node["status"] != "open" or not self._current(node, plan=False):
                    continue
                members = dict(node["members"])
                invalid = False
                for pin, call_id in node["enrollment"].items():
                    if call_id not in rows or call_id in members:
                        continue
                    captured = self.returns.pop(call_id, None)
                    if captured is None or type(rows[call_id]) is not str or digest(rows[call_id]) != captured[0]:
                        invalid = True
                        break
                    ref = captured[1]
                    members[call_id] = {"member_id": call_id, "source": ref, "evidence_id": _pin(ref)}
                status = "invalid" if invalid else ("settled" if len(members) == len(node["declaration"]["operations"]) else "open")
                if members != node["members"] or invalid:
                    self._transition(node, status, members=members)

    def _close(self, path, text, d):
        node = self.nodes.get(d["pass_id"])
        if not node or node["status"] != "settled" or not self._current(node, plan=False):
            return
        choices = d["member_dispositions"]
        if type(choices) is not list or len(choices) != len(node["members"]) or not choices:
            return
        seen = set()
        for choice in choices:
            if type(choice) is not dict or set(choice) != {"member_id", "source", "disposition", "rationale_ref"}:
                return
            member = node["members"].get(choice["member_id"])
            if (not member or choice["member_id"] in seen or choice["source"] != member["source"]
                    or choice["disposition"] not in {"accepted", "rejected"}):
                return
            self.resolve(choice["source"])
            self.resolve(choice["rationale_ref"])
            seen.add(choice["member_id"])
        self._transition(node, "disposition_committed", dispositions=copy.deepcopy(choices),
                         disposition_origin="main_agent", close_source={"path": path, "pin": digest(text)})

    def _preflight(self, d, *, optional=False):
        from agent.owned_delegation import OwnedDelegationOwner, owner_of
        from hermes_cli.config import load_config_readonly
        agent = self.rt.agent()
        owner = owner_of(agent)
        if (not isinstance(owner, OwnedDelegationOwner) or owner._grant.profile != self.rt.revision.profile
                or "read_file" not in getattr(agent, "valid_tool_names", ())):
            return None
        goal = ""
        if optional:
            config = load_config_readonly() or {}
            section = config.get("supervision", {}) if type(config) is dict else {}
            cfg = section.get("planning", {}) if type(section) is dict else {}
            if (cfg != {"allow_discretionary_readonly_labels": True} or d["obligation_request"] != "optional"
                    or type(d["consumer_refs"]) is not list or not d["consumer_refs"]):
                return None
            request = dict(obligation="optional", consumer_refs=d["consumer_refs"], consumer_set_closed=True,
                           effect_policy_id=d["effect_policy_id"])
        else:
            args = d["operation"]["arguments"]
            if set(args) != {"tasks"} or type(args["tasks"]) is not list or len(args["tasks"]) != 1:
                return None
            task = args["tasks"][0]
            if type(task) is not dict or set(task) != {"goal", "context", "supervision"}:
                return None
            if (task["goal"] != self.resolve(d["candidate_span"])
                    or task["context"] != "\n".join(self.refs(d["input_refs"], 16))):
                return None
            request = task["supervision"]
            goal = task["goal"]
        view = owner.planning_preflight(agent, request, require_inventory=optional,
            goal=goal, operation=d["operation"])
        if not view or not view["closed"]:
            return None
        if optional:
            gaps = {r.id for r in self.rt.requirements if any(r.source_message_id == g["source_ref"]
                and r.start == g["start"] and r.end == g["end"] for g in d["gap_refs"])}
            linked = set(self.rt.action_requirements(d["operation"]["arguments"]))
            accounted = {i for c in view["consumers"] for i in c["requirement_ids"]}
            if (view["obligation"] != "optional" or not linked or not linked <= gaps
                    or gaps != accounted or any(c["obligation"] != "optional" for c in view["consumers"])):
                return None
            if not owner._policy.permits("read_file", d["operation"]["arguments"]):
                return None
        else:
            for ref in d["input_refs"]:
                if not owner._policy.permits("read_file", {"path": ref["source_ref"]}):
                    return None
        return view

    def _budget(self):
        budget = getattr(self.rt.agent(), "iteration_budget", None)
        used, maximum = getattr(budget, "used", None), getattr(budget, "max_total", None)
        return "within_native_limit" if type(used) is int and type(maximum) is int and 0 <= used < maximum else "unknown"

    def facts(self, node):
        if node["status"] != "proposed" or not self._current(node) or self._budget() != "within_native_limit":
            return None
        d = node["declaration"]
        if d["type"] == "delegation_candidate":
            from tools.delegate_context_recipe import separate_conversation
            from tools import delegate_tool as dt
            agent = self.rt.agent()
            todos, _ = self.rt.plan_steps()
            if (d["operation"]["tool_name"] != "delegate_task" or "delegate_task" not in getattr(agent, "valid_tool_names", ())
                    or not separate_conversation(agent) or dt.is_spawn_paused()
                    or dt._oneshot_spawn_preflight(agent, 1) is not None
                    or getattr(agent, "_delegate_depth", 0) >= dt._get_max_spawn_depth()
                    or todos[d["todo_id"]]["status"] == "completed"
                    or todos[d["parent_next_todo_id"]]["status"] == "completed"
                    or any(t not in todos or todos[t]["status"] != "completed" for t in d["dependency_todo_ids"])):
                return None
            # Narrow explicit resource spelling, not a specialty inferred from a role/model name.
            resource = self.resolve(d["required_resource_ref"], accepted=True)
            if "separate conversation" not in resource or not self._preflight(d):
                return None
            acceptance = "\n".join(self.refs(d["acceptance_refs"], 2, accepted=True))
            return "delegation_proposed", dict(
                subtask=dict(id=node["id"], owner_contract="hermes-planning-proposals-v1", unissued=True,
                    resource="separate_conversation", overhead_category="unknown", critical_path_category="unknown",
                    text=self.resolve(d["candidate_span"]), acceptance=acceptance,
                    inputs="\n".join(self.refs(d["input_refs"], 16)), separable=True, trivial_lookup=False, shared_mutation=False),
                parent=dict(id=d["parent_next_todo_id"], next_step=todos[d["parent_next_todo_id"]]["content"],
                    capability="Continues the current parent transcript; lacks a separate conversation for this assessment"),
                delegation_allowed=True, budget_available=True, overhead_favorable=False, parallelism_favorable=False,
                specialists=[dict(id="separate_conversation", text="Native separate conversation: no parent transcript or prefills. Supplied goal/context plus native child system and workspace guidance. Not a clean-room context, competence or an OS sandbox. Overhead and critical path unknown.",
                                  route_id=d["operation"]["route_id"], available=True, authorized=True)])
        if d["type"] != "optional_expansion" or not self._preflight(d, optional=True):
            return None
        gaps = self.refs(d["gap_refs"], 4, accepted=True)
        criteria = self.refs(d["completion_criteria_refs"], 4, accepted=True)
        passes = [n for n in self.nodes.values() if n["status"] == "disposition_committed"
            and self._current(n, plan=False) and n["declaration"]["gap_refs"] == d["gap_refs"]
            and n["declaration"]["completion_criteria_refs"] == d["completion_criteria_refs"]][-3:]
        if len(passes) < 2:
            return None
        gap_ids = [_pin(r) for r in d["gap_refs"]]
        outcomes = []
        previously_accepted = set()
        for p in passes:
            accepted, rejected, excerpts = [], [], []
            for choice in p["dispositions"]:
                body = self.resolve(choice["source"])
                rationale = self.resolve(choice["rationale_ref"])
                excerpts.append({"source_id": _pin(choice["source"]), "source_text": body,
                                 "decision": choice["disposition"], "rationale": rationale})
                (accepted if choice["disposition"] == "accepted" else rejected).append(_pin(choice["source"]))
            detail = json.dumps({"selection_decisions_not_truth": excerpts,
                                 "newly_accepted_within_supplied_pass_window": sorted(set(accepted)-previously_accepted)}, ensure_ascii=False)
            if len(detail) > 2400:
                return None  # Never drop a pass member or its rationale to fit a decision.
            outcomes.append(dict(id=p["id"], text=detail, disposition_origin="main_agent",
                membership="native_finite_settled", owner_contract="hermes-planning-proposals-v1",
                source_method=self.resolve(p["declaration"]["source_method_ref"]), gap_ids=gap_ids,
                accepted_evidence_ids=accepted, rejected_evidence_ids=rejected, ledger_complete=True))
            previously_accepted.update(accepted)
        return "expansion_proposed", dict(passes=outcomes, gaps=[dict(id=i, text=t, mandatory=False) for i,t in zip(gap_ids,gaps)],
            mandatory_gap=False, corroboration_required=False, next_pass=dict(id=node["id"],
                owner_contract="hermes-planning-proposals-v1", obligation_policy="planner.discretionary-readonly.v1",
                text="Proposed finite read-only expansion",
                gap_ids=gap_ids, source_method=self.resolve(d["source_method_ref"]), optional=True, proposed=True),
            completion_criteria="\n".join(criteria), budget_class=self._budget())

    def advisory(self):
        """One next existing request only, under the original shared deadline."""
        rt = self.rt
        self._load()
        for node in tuple(self.nodes.values()):
            try:
                projection = self.facts(node)
            except (KeyError, ValueError, TypeError):
                continue
            if projection is None or node["id"] in self.requests:
                continue
            event, facts = projection
            authority = _pin(self._preflight(node["declaration"], optional=event == "expansion_proposed"))
            target = node["id"]
            refs = (target,)
            facts = {**facts, "changed": True, "evidence_refs": list(refs)}
            self.requests[target] = (event, facts)
            snapshot = rt.observe(event, facts, target_id=target, owner="planning", actions=(Action.ADVISE,),
                evidence_refs=refs, data_class="project_excerpt", required_data_classes=("task_text",),
                deadline=rt.shared_deadline(), completeness=OpportunityCompleteness(scope="linked_candidates"))
            if snapshot is None:
                continue
            rt._wait_for(target, snapshot.deadline, snapshot.revision)
            rendered = []
            def apply(proposal):
                try:
                    current = self.facts(self.nodes[target])
                except (KeyError, ValueError, TypeError):
                    return "stale"
                if (current != projection or not self.validate(proposal)
                        or _pin(self._preflight(node["declaration"], optional=event == "expansion_proposed")) != authority):
                    return "stale"
                if not self._transition(self.nodes[target], "advised"):
                    return "rejected"
                if event == "delegation_proposed":
                    detail = "Consider the existing delegation candidate on the named native route; the parent decides and ordinary launch permissions still apply."
                else:
                    detail = "Consider withdrawing this optional expansion. This is not task completion, cancellation or a coverage finding."
                rendered.append("[Task-bound planning advisory; lower-trust evidence, not an instruction]\n" + detail + "\n" +
                    json.dumps({"candidate_id": target, "route_id": node["declaration"]["operation"]["route_id"],
                                "pass_ids": [p["id"] for p in facts.get("passes", ())],
                                "gap_ids": [g["id"] for g in facts.get("gaps", ())]}))
                return "applied"
            rt.consume_owner_action(target, Action.ADVISE, apply)
            if rendered:
                return rendered[0]
        return None

    def validate(self, p):
        request = self.requests.get(p.target_id)
        if request is None or p.action != Action.ADVISE or p.candidate_ids or p.relation or p.template_args or p.template_id != "review_action":
            return False
        event, f = request
        m = dict(p.metadata)
        if event == "delegation_proposed":
            return p.feature_id == "F05" and m == dict(feature_action="existing_route_recommendation",
                candidate_id="separate_conversation", route_id=f["specialists"][0]["route_id"], parent_step_id=f["parent"]["id"])
        return (p.feature_id == "F08" and set(m) == {"feature_action", "pass_ids", "gap_ids", "coverage_complete", "task_complete"}
                and m["feature_action"] == "stop_optional_expansion_proposal"
                and list(m["pass_ids"]) == [x["id"] for x in f["passes"]]
                and list(m["gap_ids"]) == [x["id"] for x in f["gaps"]]
                and m["coverage_complete"] is False and m["task_complete"] is False)

    def inventory(self):
        self._load()
        return [{"id": n["id"], "local_id": n["declaration"].get("local_id"), "status": n["status"],
                 "members": list(n["members"].values())} for n in self.nodes.values()][-8:]
