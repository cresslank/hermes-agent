"""Optional local context acquisition owned by the host, not main tool dispatch.

The public facade executes a bounded read, not a fact-registration API. Profile
policy authorizes exact paths and an explicit supplemental-only purpose. A current
work-map supplies the consumer edge. Neither a plugin request nor source prose can
make a mandatory/independent read optional. No work-map grammar is extended.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import uuid
import weakref

from agent.supervision_efficiency import OpportunityCompleteness, _pin
from agent.supervision_types import Action, Revision

VERSION = "supervision.optional-read.v1"
MAX_BYTES = 64 * 1024
_OWNERS = weakref.WeakSet()


@dataclass(frozen=True)
class ReadReceipt:
    id: str
    revision: Revision
    plugin_id: str
    path: str
    requirement_id: str
    snapshot: str
    policy_pin: str
    objective: str
    acceptance: str
    offset: int
    limit: int
    payload: bytes

    def operation(self):
        return {"id": self.id, "objective": self.objective, "acceptance": self.acceptance + f" (lines {self.offset}..{self.offset + self.limit - 1})",
                "requirement_id": self.requirement_id, "task_id": self.revision.work_id,
                "target": self.path, "scope": "supplemental_context", "snapshot": self.snapshot,
                "cwd": str(Path(self.path).parent), "consumer_ids": [self.requirement_id],
                "independent_check": False, "effect_class": "readonly", "owner": "plugin",
                "optional": True, "issued": False}

    def facts(self):
        return {**self.operation(), "issued": True, "status": "succeeded", "source_ref": self.id,
                "result_valid": True, "authorized_reuse": True}

    def result(self):
        # Original bytes and identity, never a synthetic new read/check success.
        return {"source_ref": self.id, "path": self.path, "snapshot": self.snapshot,
                "requirement_id": self.requirement_id, "content": self.payload.decode("utf-8"),
                "offset": self.offset, "limit": self.limit,
                "sha256": hashlib.sha256(self.payload).hexdigest()}


def _snapshot(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError("optional_read_not_bounded_regular_file")
    return _pin((path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


class OptionalReadOwner:
    """One turn-local bounded source ledger. No providers, shell, retry or new DB."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.receipts = OrderedDict()
        self.epoch = None
        _OWNERS.add(self)

    def clear(self, plugin_id=None):
        with self.runtime.lock:
            if plugin_id is None:
                self.receipts.clear()
            else:
                self.receipts = OrderedDict((key, value) for key, value in self.receipts.items()
                                            if value.plugin_id != plugin_id)

    def _policy(self, facade, path):
        from hermes_cli.config import load_config_readonly
        from hermes_constants import hermes_home_key
        rt, reg = self.runtime, facade._registration
        if (reg is None or not reg.active or reg.scope != rt.revision.profile
                or hermes_home_key() != rt.revision.profile or rt.closed
                or getattr(rt.agent(), "_interrupt_requested", False)):
            return None
        config = load_config_readonly() or {}
        supervision = config.get("supervision", {})
        if not isinstance(supervision, dict) or supervision.get("enabled") is not True:
            return None
        plugins = supervision.get("plugins", {})
        if not isinstance(plugins, dict):
            return None
        entry = plugins.get(facade._context.plugin_id, {})
        policy = entry.get("optional_reads") if isinstance(entry, dict) else None
        if (not isinstance(policy, dict) or set(policy) != {"version", "purpose", "paths", "max_bytes"}
                or policy["version"] != VERSION or policy["purpose"] != "supplemental_context"
                or type(policy["paths"]) is not list or not 1 <= len(policy["paths"]) <= 16
                or any(type(p) is not str for p in policy["paths"]) or path not in policy["paths"]
                or type(policy["max_bytes"]) is not int or not 0 < policy["max_bytes"] <= MAX_BYTES):
            return None
        # Both file safety and exact current operation-to-requirement linkage are
        # host predicates; a singleton requirement is not a link.
        from agent.file_safety import get_read_block_error
        from agent.redact import _is_secret_file_arg
        if get_read_block_error(path) or _is_secret_file_arg(path):
            return None
        links = rt.action_requirements({"path": path})
        if len(links) != 1:
            return None
        return (_pin(policy), policy["max_bytes"], links[0], reg.generation)

    def read(self, facade, request):
        rt = self.runtime
        rt._assert_owner(tool_worker=True)
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
            return None
        if (type(request) is not dict or set(request) != {"version", "path", "objective", "acceptance", "offset", "limit"}
                or request["version"] != VERSION
                or any(type(request[k]) is not str or not 0 < len(request[k]) <= 1200
                       for k in ("path", "objective", "acceptance"))
                or any(type(request[k]) is not int or not 1 <= request[k] <= 2000
                       for k in ("offset", "limit"))):
            return None
        path = request["path"]
        # Exact absolute spelling only; do not normalize user identifiers or allow
        # a symlink to redirect a profile grant to another source.
        policy = self._policy(facade, path)
        if policy is None or not Path(path).is_absolute():
            return None
        try:
            if str(Path(path).resolve()) != path:
                return None
        except (OSError, RuntimeError):
            return None
        revision = rt.revision
        if self.epoch != revision:
            self.receipts.clear()
            self.epoch = revision
        try:
            snapshot = _snapshot(path)
            if os.lstat(path).st_size > policy[1]:
                return None
        except (OSError, ValueError):
            return None
        plugin_id = facade._context.plugin_id
        def current():
            try:
                return (rt.revision == revision and self._policy(facade, path) == policy
                        and _snapshot(path) == snapshot)
            except (OSError, ValueError):
                return False
        candidates = [r for r in self.receipts.values() if r.revision == revision
                      and r.plugin_id == plugin_id and r.path == path and r.snapshot == snapshot
                      and r.requirement_id == policy[2] and r.policy_pin == policy[0] and r.payload][-4:]
        # Byte-identical request replay needs no semantic question or effect receipt.
        exact = next((r for r in candidates if (r.offset, r.limit) ==
                      (request["offset"], request["limit"])), None)
        if exact is not None:
            with rt.lock, facade._registration.fence:
                if current() and self.receipts.get(exact.id) == exact:
                    return exact.result()
        target = "optional-read:" + uuid.uuid4().hex
        pending = ReadReceipt(target, revision, plugin_id, path, policy[2], snapshot, policy[0],
                              request["objective"], request["acceptance"], request["offset"], request["limit"], b"")
        result = []
        if candidates:
            facts = {"proposed": pending.operation(), "candidates": [r.facts() for r in candidates],
                     "exact_reusable": False, "changed": True,
                     "evidence_refs": [policy[2], *(r.id for r in candidates)]}
            observation = rt.observe("operation_proposed", facts, target_id=target,
                actions=(Action.REUSE_CANDIDATE,), evidence_refs=tuple(facts["evidence_refs"]),
                owner="efficiency.optional_read", completeness=OpportunityCompleteness(),
                deadline=rt.shared_deadline(), data_class="project_excerpt")
            if observation is not None:
                rt._wait_for(target, observation.deadline, observation.revision)
                def apply(proposal):
                    metadata = proposal.metadata
                    prior = next((r for r in candidates if r.id == metadata.get("candidate_id")), None)
                    if (set(metadata) != {"feature_action", "candidate_id", "source_ref", "requirement_id",
                                          "f01_eligibility_candidate", "cancellation_authorized"}
                            or set(proposal.evidence_refs) != set(facts["evidence_refs"])
                            or proposal.feature_id != "F04" or metadata.get("feature_action") != "reuse_candidate"
                            or metadata.get("source_ref") != getattr(prior, "id", None)
                            or metadata.get("requirement_id") != policy[2]
                            or metadata.get("cancellation_authorized") is not False
                            or metadata.get("f01_eligibility_candidate") is not None):
                        return "rejected"
                    if prior is None or self.receipts.get(prior.id) != prior or not current():
                        return "stale"
                    result.append(prior.result())
                    return "applied"
                rt.consume_owner_action(target, Action.REUSE_CANDIDATE, apply)
        rt.mark_dispatched(target)
        if result:
            return result[0]
        if not current():
            return None
        # Baseline is a REAL bounded read. O_NOFOLLOW plus pre/post descriptor and
        # path pins prevent symlink replacement and detect ordinary concurrent edits.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return None
                fd_pin = _pin((path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
                if fd_pin != snapshot:
                    return None
                payload = stream.read(policy[1] + 1)
                payload.decode("utf-8")
            if len(payload) > policy[1] or not current():
                return None
        except (OSError, UnicodeError):
            return None
        lines = payload.splitlines(keepends=True)
        payload = b"".join(lines[request["offset"] - 1:request["offset"] - 1 + request["limit"]])
        receipt = ReadReceipt(target, revision, plugin_id, path, policy[2], snapshot, policy[0],
                              request["objective"], request["acceptance"], request["offset"], request["limit"], payload)
        with rt.lock, facade._registration.fence:
            if not current():
                return None
            self.receipts[receipt.id] = receipt
            while len(self.receipts) > 16:
                self.receipts.popitem(last=False)
            return receipt.result()


def clear_optional_reads(scope, plugin_id):
    for owner in tuple(_OWNERS):
        if owner.runtime.revision.profile == scope:
            owner.clear(plugin_id)


def read_optional_context(facade, request):
    runtime = facade._active_runtime()
    if runtime is None:
        return None
    owner = getattr(runtime, "optional_reads", None)
    if owner is None:
        owner = OptionalReadOwner(runtime)
        runtime.optional_reads = owner
    try:
        return owner.read(facade, request)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None  # an unavailable optional owner must not become required work
