"""Current working source use for native retrieval, never adoption or truth.

The narrow producer requires a committed whole literal claim, an accepted linked
requirement whose exact text is this query, and the source in this native batch.
It reads only selected refs; it neither searches nor renews publication handles.
Other owners and unstructured/ambiguous links keep their query-only baseline.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from agent.supervision_claim_uses import comparable
from agent.supervision_literal_sources import (
    LiteralSourceRegistration, WORKING_PREMISE_VERSION, _valid_record,
)
from agent.supervision_types import Action

CLASSES = frozenset({"history_excerpt", "project_excerpt", "task_text"})


def authorized(source, recipient):
    policy = recipient.egress_policy
    return (recipient.active and "observe" in recipient.grants
            and Action.RANK_CANDIDATES.value in recipient.grants
            and CLASSES <= recipient.data_policy
            and recipient.plugin_id in source.working_recipients
            and policy.get("sources", {}).get("premise") == WORKING_PREMISE_VERSION
            and "premise" in policy.get("fields", {}))


@dataclass(frozen=True)
class WorkingPremise:
    runtime: Any
    source: Any
    engine: Any
    binding: Any
    recipient: Any
    request: Any
    node: dict
    record: Any
    records: tuple
    deadline: float

    def read_current(self):
        # Called before entering runtime/recipient/native lifecycle fences.
        return all(self.source.provider.working_literal_source(self.engine, r.exact_ref) == r
                   for r in self.records)

    @contextmanager
    def fence(self, recipient):
        rt, source = self.runtime, self.source
        with source.lock, source.provider.literal_source_binding_fence(self.engine, self.binding) as current:
            yield (current and recipient is self.recipient and source.current(rt)
                and getattr(rt.agent(), "context_compressor", None) is self.engine
                and rt.revision == self.request.revision and rt.clock() < self.deadline
                and authorized(source, recipient)
                and rt.dependencies.claim_uses._eligible(self.node, self))


def prepare(runtime, action, request, deadline):
    """Common owner admission; no feature snapshot or model-supplied premise."""
    if (action != Action.RANK_CANDIDATES or request.owner != "lcm"
            or request.event != "retrieval_candidates" or not request.requires_ack
            or request.output_contract != "supervision.retrieval-presentation.v1"
            or "premise" in request.facts):
        return None
    engine = getattr(runtime.agent(), "context_compressor", None)
    source = getattr(getattr(engine, "supervision", None), "_literal_source_registration", None)
    if type(source) is not LiteralSourceRegistration or not source.current(runtime):
        return None
    scope = request.facts.get("scope", {})
    if (scope.get("session_scope") not in {"current", "all"}
            or scope.get("current_session_id") != getattr(engine, "current_session_id", None)):
        return None
    recipients = [r for r in runtime._registrations() if authorized(source, r)]
    if len(recipients) != 1:
        return None  # do not broadcast a private working use or arbitrate recipients
    uses = runtime.dependencies.claim_uses
    with runtime.lock:
        uses.graph._load()
        nodes = []
        for n in uses.graph.nodes.values():
            if n["kind"] != "claim_delivery" or n["status"] != "claim_declared":
                continue
            try:
                if any(uses.graph.resolve(ref) == request.facts.get("query")
                       for ref in n["declaration"]["requirement_refs"]):
                    nodes.append(n)
            except (KeyError, ValueError, TypeError):
                continue
    if len(nodes) != 1:
        return None
    node = nodes[0]
    refs = tuple(c.get("ref") for c in request.candidates)
    if (node["declaration"]["source_ref"] not in refs or len(set(refs)) != len(refs)
            or not 1 <= len(refs) <= 8
            or any(c.get("qualifiers_complete") is not True or c.get("constraints_match") is not True
                   for c in request.candidates)):
        return None
    binding = source.provider.literal_source_binding(engine)
    records = tuple(source.provider.working_literal_source(engine, ref) for ref in refs)
    if any(not _valid_record(r) or len(r.source_bytes.decode("utf-8")) > 1200 for r in records):
        return None
    record = records[refs.index(node["declaration"]["source_ref"])]
    if (any(r != record and not comparable(record, r) for r in records)
            or any(r.source_bytes.decode("utf-8") != c.get("excerpt")
                   for r, c in zip(records, request.candidates))):
        return None
    premise = WorkingPremise(runtime, source, engine, binding, recipients[0], request,
                             node, record, records, deadline)
    with runtime.lock, recipients[0].fence, premise.fence(recipients[0]) as current:
        return premise if current else None


def current_read(runtime, target):
    with runtime.lock:
        opportunity = runtime.opportunities.get(target, {})
        premise = opportunity.get("working_premise")
    return premise, premise is None or premise.read_current()


@contextmanager
def consumption_fence(opportunity, recipient, read):
    premise = opportunity.get("working_premise") if opportunity else None
    if premise is None:
        yield read == (None, True)
    elif read[0] is not premise or not read[1]:
        yield False
    else:
        with premise.fence(recipient) as current:
            yield current
