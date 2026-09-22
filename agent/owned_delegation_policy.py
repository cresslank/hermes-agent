"""Configured native owner and authenticated, parent-only consumer contracts.

Profile policy is permission, never evidence of optionality. The separate v1
input contract is accepted only at authenticated ingress; model tool arguments,
work maps and todo prose cannot issue it. No persisted worker is adopted.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3

from agent.owned_delegation import (
    CapabilityContract, Consumer, ControlDenied, OwnedDelegationOwner, OwnerGrant,
    ReadOnlyPolicy, binding_of, control_fence, scoped_file_policy, validate_request,
)

VERSION = "supervision.owned-delegation.v1"
CONSUMERS = "authenticated-parent-only.v1"


@dataclass(frozen=True)
class ParentConsumerContract:
    ref: str
    goal: str
    consumer: Consumer
    source: str
    instruction_event: int
    work_id: str
    requires_handoff: bool


def accept_consumer_contracts(runtime, origin):
    """Called under the native instruction fence, never by an observer callback.

    Each new instruction invalidates existing launch authority, even if it lacks
    a replacement contract. New contracts authorize NEW launches only.
    """
    from agent.supervision_context import _strict_object, is_accepted_origin
    from agent.owned_delegation_planning import accept_inventory
    runtime.owned_consumer_contracts = {}
    runtime.owned_planning_inventory = None
    if accept_inventory(runtime, origin):
        return
    if not is_accepted_origin(origin) or len(origin.text.encode("utf-8")) > 1200:
        return
    blocks = re.findall(r"(?m)^```hermes-owned-delegation-v1\n(.*?)^```\s*$", origin.text, re.S)
    if len(blocks) != 1:
        return
    try:
        body = json.loads(blocks[0], object_pairs_hook=_strict_object)
        if (type(body) is not dict or set(body) != {"version", "jobs"}
                or type(body["version"]) is not int or body["version"] != 1
                or type(body["jobs"]) is not list or not 1 <= len(body["jobs"]) <= 4):
            return
        spans = {(r.start, r.end): r.id for r in runtime.requirements if r.source_message_id == origin.message_id}
        records = {}
        for row in body["jobs"]:
            if (type(row) is not dict or set(row) != {"ref", "goal", "requirements", "consumer_scope", "obligation",
                    "requires_result", "requires_effects", "requires_cleanup", "requires_handoff"}
                    or row["consumer_scope"] != "parent_only"
                    or type(row["ref"]) is not str or not re.fullmatch(r"job:[A-Za-z0-9_-]{1,64}", row["ref"])
                    or row["ref"] in records or type(row["goal"]) is not str or not 0 < len(row["goal"]) <= 240
                    or type(row["requirements"]) is not list or not 1 <= len(row["requirements"]) <= 4
                    or type(row["requires_handoff"]) is not bool):
                return
            links = []
            for span in row["requirements"]:
                if (type(span) is not dict or set(span) != {"start", "end"}
                        or any(type(v) is not int for v in span.values())):
                    return
                links.append(spans[(span["start"], span["end"])])
            consumer = Consumer(row["ref"], row["obligation"], row["requires_result"],
                row["requires_effects"], row["requires_cleanup"], tuple(links))
            records[row["ref"]] = ParentConsumerContract(row["ref"], row["goal"], consumer,
                origin.message_id, runtime.revision.instruction_event, runtime.revision.work_id, row["requires_handoff"])
        runtime.owned_consumer_contracts = records
    except (KeyError, TypeError, ValueError):
        return  # malformed/partial input grants nothing


def _policy(runtime, registration, *, deadline=None):
    from agent.owned_delegation_planning import CONSUMERS_V2
    from hermes_cli.config_cached import current_config_readonly
    from hermes_constants import hermes_home_key
    if runtime.closed or hermes_home_key() != runtime.revision.profile or not registration.active:
        return None
    config = current_config_readonly(deadline=deadline)
    supervision = config.get("supervision") if isinstance(config, dict) else None
    if not isinstance(supervision, dict) or supervision.get("enabled") is not True:
        return None
    plugins = supervision.get("plugins")
    entry = plugins.get(registration.plugin_id) if isinstance(plugins, dict) else None
    policy = entry.get("owned_delegation") if isinstance(entry, dict) else None
    grants = entry.get("grants") if isinstance(entry, dict) else None
    needed = {"observe", "reprioritize_child", "cancel_child"}
    if (not isinstance(grants, list) or any(type(g) is not str for g in grants)
            or not needed <= set(grants) or not needed <= registration.grants
            or type(policy) is not dict
            or set(policy) != {"version", "allow_optional_readonly", "consumer_contract", "read_roots"}
            or policy["version"] != VERSION or policy["allow_optional_readonly"] is not True
            or policy["consumer_contract"] not in (CONSUMERS, CONSUMERS_V2)
            or type(policy["read_roots"]) is not list or not 1 <= len(policy["read_roots"]) <= 8
            or any(type(p) is not str or not Path(p).is_absolute() for p in policy["read_roots"])):
        return None
    return json.dumps(policy, sort_keys=True)


def _plan_args(args):
    # The only write permitted by this policy is the child's own native todo
    # bookkeeping. Its labels are observations, not consumer attestations.
    return (type(args) is dict and "todos" in args and not set(args) - {"todos", "merge"}
            and type(args.get("merge", False)) is bool and type(args["todos"]) is list
            and 0 < len(args["todos"]) <= 4
            and all(type(t) is dict and set(t) == {"id", "content", "status"}
                and all(type(v) is str and 0 < len(v) <= 240 for v in t.values())
                and t["status"] in {"pending", "in_progress", "completed", "cancelled"} for t in args["todos"]))


class ConfiguredDelegationOwner(OwnedDelegationOwner):
    # Milestones arise after scheduling. Record observations, not a fictitious
    # applied scheduling effect on a running worker. Cancellation is separate.
    priority_effects_supported = False

    def __init__(self, runtime, registration, policy_pin, db, session_generation):
        from tools.delegation_control_store import SQLiteControlStore
        self.runtime, self.registration, self.policy_pin, self.db = runtime, registration, policy_pin, db
        self.session_generation = session_generation
        self.registration_generation = registration.generation
        self.launch_contracts = {}
        self.resolving = {}
        path = Path(db.db_path).resolve()
        # Bind to the supplied canonical SessionDB, never ambient async state or
        # a new DB. mode=rw cannot recreate a deleted profile/session database.
        def connect():
            if not db.control_generation_available():
                raise ControlDenied("Configured owner database generation changed")
            # Optional controls run under the instruction/revocation fences.
            # Never start a 5s wait. Mandatory exits retain authentic completion
            # facts for the owner's separate nonwaiting reconciliation path.
            conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0)
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        file_policy = scoped_file_policy(tuple(json.loads(policy_pin)["read_roots"]))
        policy = ReadOnlyPolicy("host.owned-parent-read.v1", (*file_policy.contracts,
            CapabilityContract("todo_list", "child-plan.v1", _plan_args)))
        super().__init__(parent_session_id=str(runtime.agent().session_id), store=SQLiteControlStore(connect, authorize=self.current,
                lifecycle_authorize=self.storage_current, wait_for_writer=False),
            grant=OwnerGrant(runtime.revision.profile, registration.plugin_id, True, True),
            consumer_resolver=self.resolving.get, policy=policy,
            revision_provider=lambda: (runtime.revision.instruction_event, runtime.revision.requirements, runtime.revision.evidence))

    def storage_current(self, *, connection=None):
        # Cleanup outlives config/registration revocation, but never its original
        # database/session generation. This grants no semantic or launch rights.
        return self.db.control_session_generation(self.parent_session_id,
            connection=connection) == self.session_generation

    def current(self, *, deadline=None, connection=None):
        agent = self.runtime.agent()
        try:
            return (agent is not None and str(agent.session_id) == self.parent_session_id
                and getattr(agent, "_session_db", None) is self.db and self.db.read_only is False
                and getattr(agent, "_supervision_runtime", None) is self.runtime
                and self.registration.generation == self.registration_generation
                and _policy(self.runtime, self.registration, deadline=deadline) == self.policy_pin
                and self.db.control_session_generation(self.parent_session_id,
                    deadline=deadline, connection=connection) == self.session_generation)
        except (OSError, TypeError, ValueError, sqlite3.Error):
            return False

    def semantic_authorized(self, handle, *, deadline=None):
        record = self.launch_contracts.get(handle.child_id)
        live = self._live.get(handle.child_id)
        return (live is not None and live.handle == handle
            and self.current(deadline=deadline) and record is not None
            and self.runtime.revision.work_id == record.work_id
            and self.runtime.revision.instruction_event == record.instruction_event
            and getattr(self.runtime, "owned_consumer_contracts", {}).get(record.ref) == record)

    def request_semantic_cancel(self, handle, **kwargs):
        # Raw native callers use the same instruction/registration/control lock
        # order as the bridge; authenticated steering cannot race this commit.
        deadline = kwargs['deadline']
        with control_fence(self.runtime.lock, deadline), control_fence(self.registration.fence, deadline):
            return super().request_semantic_cancel(handle, **kwargs)

    def planning_preflight(self, parent, request, *, require_inventory=False, goal="", operation=None):
        from agent.owned_delegation_planning import planning_preflight
        return planning_preflight(self, parent, request, require_inventory=require_inventory, goal=goal, operation=operation)

    def launch(self, parent, child, request=None, *, goal=""):
        request = validate_request(request)
        # Legacy launches remain genuinely legacy, including on an enabled host.
        # Nested owned work must still go through the inherited dispatch fence.
        if request is None and binding_of(parent) is None:
            return None
        with self.runtime.lock, self.registration.fence, self._lock:
            if not self.current():
                raise ControlDenied("Configured owner authority unavailable")
            from agent.owned_delegation_planning import launch_resolution
            record, resolved = launch_resolution(self, parent, request, goal)
            self.resolving.clear()
            self.resolving.update(resolved)
            try:
                handle = super().launch(parent, child, request, goal=goal)
                if record is not None and record.goal == goal:
                    self.launch_contracts[handle.child_id] = record
                return handle
            finally:
                # Later enrollments cannot reuse an unrelated launch's resolution.
                self.resolving.clear()


def configured_owner(parent):
    """Normal turn/launch installation; absent prerequisites grant no authority."""
    from agent.supervision_policy import runtime_for_agent
    from hermes_state import SessionDB
    runtime = runtime_for_agent(parent)
    db = getattr(parent, "_session_db", None)
    if runtime is None or binding_of(parent) is not None or not isinstance(db, SessionDB):
        return None
    try:
        if db.read_only is not False or Path(db.db_path).resolve() != Path(runtime.revision.profile) / "state.db":
            return None
        session_generation = db.control_session_generation(str(parent.session_id))
        if session_generation is None:
            return None
        with runtime.lock:
            existing = getattr(parent, "_owned_delegation_owner", None)
            if existing is not None:
                return existing
            candidates = [(r, _policy(runtime, r)) for r in runtime._registrations()]
            candidates = [(r, pin) for r, pin in candidates if pin is not None]
            if len(candidates) != 1:
                return None  # never choose among competing owner policies
            registration, pin = candidates[0]
            owner = ConfiguredDelegationOwner(runtime, registration, pin, db, session_generation)
            parent._owned_delegation_owner = owner
            return owner
    except (OSError, TypeError, ValueError, sqlite3.Error):
        return None
