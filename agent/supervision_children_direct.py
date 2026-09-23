"""Recurring scoped native child lifecycle controller; no main-model blocking.

Only accepted task text, native todo progress, content identities and owned child
milestones are projected. No hidden reasoning, system prompts, environment,
credentials, arbitrary tool arguments or inferred external consumers are read.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import threading
import time
import uuid

from agent.owned_delegation import ControlDenied, SemanticEvidence, control_fence
from agent.owned_delegation_direct import CONTRACT, POLICY, RELATIONS, VERSION, input_identity
from agent.supervision_types import Action, project


def active(runtime):
    bridge = getattr(runtime, "children", None)
    return (bridge is not None and getattr(bridge.owner, "direct_enabled", False)
            and not getattr(runtime, "control_revoked", False))


class RecurringChildControl:
    def __init__(self, bridge):
        self.bridge, self.runtime, self.owner = bridge, bridge.runtime, bridge.owner
        self.stop = threading.Event()
        self.thread = None
        self.targets = {}
        self.next_due = {}
        self.roots = json.loads(self.owner.policy_pin)["read_roots"]
        self.sequence = 0

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            from agent.memory_provider import spawn_context_thread
            self.thread = spawn_context_thread(target=self.run, name="owned-child-control", daemon=True)
            self.thread.start()

    def run(self):
        rt = self.runtime
        tid = threading.get_ident()
        try:
            while not self.stop.is_set() and active(rt) and self.owner.current():
                with rt.lock:
                    handles = tuple(self.bridge.launches.values())
                    rt.child_control_waiters[tid] = frozenset(h.child_id for h, _ in handles)
                live = []
                for handle, _ in handles:
                    try:
                        s = self.owner.status(handle)
                        if not s["settled"]:
                            live.append(handle)
                            if s["cancel_requested"]:
                                self.owner.signal_semantic_cancel(handle)
                    except (ValueError, OSError, sqlite3.Error):
                        continue
                if not live:
                    return
                now = time.monotonic()
                due = [h for h in live if now >= self.next_due.get(h.child_id, 0)]
                # Filesystem work is on this scoped thread, not the parent model path.
                identity = input_identity(self.roots) if due else None
                for handle in due:
                    self.next_due[handle.child_id] = now + POLICY["interval_seconds"]
                    try:
                        self.observe(handle, identity)
                    except (ValueError, OSError, sqlite3.Error):
                        continue
                self.drain()
                # Promptly consume timely decisions while the main model is busy,
                # joining, or idle. The 2s clock governs inference, not safe points.
                with rt.ready:
                    rt.ready.wait(timeout=.05)
        finally:
            with rt.lock:
                rt.child_control_waiters.pop(tid, None)

    def observe(self, handle, identity):
        from agent.supervision_children import ChildCompleteness, _guard, _scope
        rt, owner = self.runtime, self.owner
        with rt.lock, owner._lock:
            if not owner.semantic_authorized(handle):
                return
            live = owner._get(handle)
            s = live.snapshot
            if s.get("direct_control") != VERSION or s["settled"] or s["cancel_requested"]:
                return
            if s.get("current_input_ref") != identity:
                owner._commit(live, lambda n: n.update(current_input_ref=identity, result_applicability="invalidated"))
                s = live.snapshot
            instructions = "\n".join(rt.sources.values())
            if not instructions or len(instructions) > 8000 or len(rt.sources) >= 12:
                return  # never truncate a material qualifier
            progress = []
            for other in owner._live.values():
                if (other.handle != handle and len(progress) < 8
                        and owner.semantic_authorized(other.handle)
                        and len(other.snapshot["objective"]) <= 2400):
                    other_state = other.snapshot
                    progress.append(dict(id=other.handle.child_id, state="finished" if other_state["worker_finished"] else "active",
                        objective=other_state["objective"], milestone=other_state["latest_milestone"],
                        input_ref=other_state.get("current_input_ref")))
            # Existing sibling/result identifiers are available, not guessed duplicate
            # relations; JEV must select duplicate and the native owner rechecks census.
            duplicate_refs = ["child:" + p["id"] for p in progress]
            if s.get("duplicate_refs") != duplicate_refs:
                owner._commit(live, lambda n: n.update(duplicate_refs=duplicate_refs))
                s = live.snapshot
            todos, revision = rt.plan_steps()
            main = [dict(id=k, content=t.get("content", ""), status=t.get("status", "unknown")) for k, t in todos.items()]
            if len(main) > 16 or len(json.dumps(main)) > 4000:
                main = None
            self.sequence += 1
            refs = ["child:" + handle.child_id, s["obligation_ref"], *duplicate_refs]
            facts = dict(changed=True, evidence_refs=refs, child_relevance_contract=CONTRACT,
                direct_control=dict(POLICY), observation_id=uuid.uuid4().hex, current_instructions=instructions,
                main_progress=dict(todo_revision=revision, todos=main), sibling_progress=progress,
                job=dict(id=handle.child_id, active=True, objective=s["objective"], milestone=s["latest_milestone"],
                    generation=int(handle.generation[:15], 16), control_revision=s["control_revision"],
                    obligation=s["obligation"], obligation_ref=s["obligation_ref"], replaceable=s["replaceable"],
                    effect_class="readonly" if s["effect_class"] == "read_only" else "unknown",
                    effect_policy_id=s["effect_policy_id"], owner_cancel_grant=True, consumer_set_closed=s["consumer_set_closed"],
                    consumers=[dict(id=c["ref"], obligation=c["obligation"]) for c in s["consumers"]],
                    pending_handoff=bool(s["handoffs"]), pending_effect=bool(s["inflight"]), pending_cleanup=s["cleanup_pending"],
                    input_mode=s["input_mode"], original_input_ref=s["original_input_ref"], current_input_ref=s["current_input_ref"],
                    duplicate_refs=duplicate_refs))
            self.targets[handle.child_id] = (handle, _scope(rt.revision), _guard(s), identity, tuple(duplicate_refs))
            rt.observe("child_control_tick", facts, target_id=handle.child_id, owner="owned_delegation",
                actions=(Action.CANCEL_CHILD, Action.REPRIORITIZE_CHILD), evidence_refs=refs,
                required_data_classes=("project_excerpt", "history_excerpt"),
                completeness=ChildCompleteness(scope="enumerated_items", complete=True))

    def drain(self):
        for target in tuple(self.targets):
            for action in (Action.CANCEL_CHILD, Action.REPRIORITIZE_CHILD):
                self.runtime.consume_owner_action(target, action, self.apply)

    def apply(self, proposal):
        from agent.supervision_children import _guard, _scope, _meaningful
        from hermes_constants import hermes_home_key
        rt, owner = self.runtime, self.owner
        try:
            handle, scope, guard, identity, duplicates = self.targets[proposal.target_id]
            meta = project(proposal.metadata)
            observation = meta.get("semantic_observation")
            if (hermes_home_key() != handle.profile or proposal.feature_id != "F01"
                    or proposal.plugin_generation != owner.registration_generation
                    or not isinstance(observation, dict) or set(observation) != {"version", "policy_version", "answer"}
                    or observation["version"] != CONTRACT or observation["policy_version"] != VERSION):
                return "rejected"
            answer = observation["answer"]
            if (not isinstance(answer, dict) or set(answer) != {"type", "choice", "confidence", "probabilities"}
                    or answer["type"] != "choice" or answer["choice"] not in RELATIONS
                    or not isinstance(answer["probabilities"], dict) or set(answer["probabilities"]) != RELATIONS):
                return "rejected"
            values = [answer["confidence"], *answer["probabilities"].values()]
            if (any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values)
                    or abs(sum(answer["probabilities"].values()) - 1) > 1e-5
                    or answer["probabilities"][answer["choice"]] != max(answer["probabilities"].values())):
                return "rejected"
            cancel = proposal.action == Action.CANCEL_CHILD
            if meta.get("feature_action") != ("cancel_replaceable_readonly" if cancel else "priority_update"):
                return "rejected"
            with control_fence(owner._lock, proposal.expires_at_monotonic):
                live = owner._get(handle)
                s = live.snapshot
                if (_scope(rt.revision) != scope or _guard(s) != guard or s.get("current_input_ref") != identity
                        or tuple(s.get("duplicate_refs", ())) != duplicates):
                    return "stale"
                if not cancel:
                    return "no_op"
                evidence = SemanticEvidence(_meaningful(rt.revision), tuple((c["ref"], 0.) for c in s["consumers"]),
                    answer["choice"], answer["probabilities"][answer["choice"]], answer["confidence"])
                receipt = owner.request_semantic_cancel(handle, expected_revision=s["control_revision"], evidence=evidence,
                    idempotency_key=proposal.proposal_id, deadline=proposal.expires_at_monotonic, _defer_signal=True)
            if receipt.accepted:
                owner.signal_semantic_cancel(handle)
                return "applied"
            return "no_op"
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return "rejected"
