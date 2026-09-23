"""Provider-neutral supervision.v1 values. No capability or provider types cross this wire."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import json
import re
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

VERSION = "supervision.v1"
METADATA_VERSION = "supervision.metadata.v1"

# One upper bound for a native decision and its dependent evidence pass.
# Results are consumed immediately; this is not a mandatory wait.
DECISION_BUDGET_SECONDS = 1.0


def bounded_metadata(value):
    """Lossless descriptor data, never executable authority; owners validate semantics.

    Bound before copying/encoding, including cycles, non-JSON types and nonfinite
    numbers. Selected IDs and relation also have typed top-level proposal fields.
    """
    if not isinstance(value, Mapping):
        raise ValueError("invalid_metadata")
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 512 or depth > 6:
            raise ValueError("metadata_bounds")
        if isinstance(item, Mapping):
            if len(item) > 64:
                raise ValueError("metadata_bounds")
            for key, child in item.items():
                if type(key) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
                    raise ValueError("invalid_metadata_key")
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 64:
                raise ValueError("metadata_bounds")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            if len(item) > 2400:
                raise ValueError("metadata_bounds")
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int:
            if not -(2**63) <= item < 2**63:
                raise ValueError("metadata_bounds")
        elif type(item) is not float or not math.isfinite(item):
            raise ValueError("invalid_metadata_value")

    visit(value)
    frozen = freeze(value)
    if len(json.dumps(project(frozen), ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8192:
        raise ValueError("metadata_bounds")
    return frozen


def field_data_policy(value, *, profile, fact_keys=None):
    """Validate an explicit HOST policy; empty means no remote disclosure grant.

    No content classifier, fixture heuristic, wildcard or legacy class-list upgrade.
    """
    if not isinstance(value, Mapping):
        raise ValueError("invalid_data_policy")
    if not value:
        return freeze({})
    if set(value) != {"id", "profile", "fields", "sources", "fixture"}:
        raise ValueError("invalid_data_policy")
    if (type(value["id"]) is not str or not 0 < len(value["id"]) <= 256
            or value["profile"] != profile or type(value["fixture"]) is not bool):
        raise ValueError("invalid_data_policy")
    # Registration holds a catalog spanning multiple producers; snapshots carry
    # only their exact fact keys. A larger catalog must not enlarge one request.
    entry_limit = 256 if fact_keys is None else 64
    for name in ("fields", "sources"):
        entries = value[name]
        if not isinstance(entries, Mapping) or len(entries) > entry_limit:
            raise ValueError("invalid_data_policy")
        if any(type(k) is not str or not 0 < len(k) <= 128 or
               type(v) is not str or not 0 < len(v) <= 256 for k, v in entries.items()):
            raise ValueError("invalid_data_policy")
    if not value["sources"].keys() <= value["fields"].keys():
        raise ValueError("invalid_data_policy")
    if any(kind.startswith("private") and key not in value["sources"] for key, kind in value["fields"].items()):
        raise ValueError("missing_source_grant")
    if fact_keys is not None and set(value["fields"]) != set(fact_keys):
        raise ValueError("unclassified_fact")
    return freeze(value)


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("non_string_key")
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(v) for v in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if is_dataclass(value) and getattr(value, "__dataclass_params__").frozen:
        return value
    raise ValueError("non_wire_value")


def project(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: project(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: project(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [project(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class Revision:
    profile: str
    lineage: str
    work_id: str
    run_generation: int = 1
    instruction_event: int = 0
    requirements: int = 0
    evidence: int = 0
    catalog: int = 0
    control_revision: int = 0

    def __post_init__(self):
        if not all(type(getattr(self, k)) is str and getattr(self, k) for k in ("profile", "lineage", "work_id")):
            raise ValueError("invalid_scope")
        for f in fields(self)[3:]:
            if type(getattr(self, f.name)) is not int or getattr(self, f.name) < 0:
                raise ValueError("invalid_revision")


@dataclass(frozen=True)
class ExactSpan:
    id: str
    source_message_id: str
    source_revision: str
    start: int
    end: int
    text: str
    heading: str = ""
    kind: str = "candidate"


@dataclass(frozen=True)
class Completeness:
    scope: str = "enumerated_items"
    complete: bool = False
    global_coverage: str = "unknown"
    omitted: bool = False


@dataclass(frozen=True)
class EffectReceiptV1:
    receipt_id: str
    tool_call_id: str
    resource: str
    snapshot_id: str
    effect_class: str = "unknown"
    outcome: str = "unknown"
    before_hash: str | None = None
    after_hash: str | None = None
    status: str = "unknown"
    partial: bool = False
    truncated: bool = False
    provenance: str = "owner"


@dataclass(frozen=True)
class FindingV1:
    finding_id: str
    source_receipt: str
    revision: Revision
    span: ExactSpan
    consumer_refs: tuple[str, ...]
    status: str = "candidate"
    child_id: str = ""
    launch_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "consumer_refs", tuple(self.consumer_refs))


@dataclass(frozen=True)
class DecisionSnapshotV1:
    event: str
    event_id: str
    sequence: int
    revision: Revision
    facts: Mapping[str, Any]
    completeness: Completeness
    deadline: float
    data_policy: Mapping[str, Any] | tuple[str, ...] = field(default_factory=dict)
    origin_kind: str = "unknown"
    deadline_class: str = "background"
    task_id: str = ""
    turn_id: str = ""
    api_request_id: str = ""
    tool_call_id: str = ""
    delegation_id: str = ""
    child_id: str = ""
    schema_version: str = VERSION
    owner: str = "agent"
    required_obligations: tuple[str, ...] = ()
    deadline_issued_at: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "facts", freeze(self.facts))
        # Retain old in-process tuple snapshots without turning them into egress grants.
        if isinstance(self.data_policy, Mapping):
            policy = field_data_policy(self.data_policy, profile=self.revision.profile, fact_keys=self.facts)
        elif isinstance(self.data_policy, (tuple, list)) and all(type(x) is str for x in self.data_policy):
            policy = tuple(self.data_policy)
        else:
            raise ValueError("invalid_data_policy")
        object.__setattr__(self, "data_policy", policy)
        obligations = tuple(self.required_obligations)
        if (isinstance(self.required_obligations, str) or len(obligations) > 64 or
                any(type(x) is not str or not 0 < len(x) <= 256 for x in obligations)):
            raise ValueError("invalid_obligations")
        object.__setattr__(self, "required_obligations", obligations)
        if type(self.owner) is not str or not 0 < len(self.owner) <= 256:
            raise ValueError("invalid_owner")
        if self.deadline_issued_at is not None and (
                type(self.deadline_issued_at) not in (int, float) or
                not math.isfinite(self.deadline_issued_at) or self.deadline_issued_at > self.deadline):
            raise ValueError("invalid_deadline")
        if type(self.deadline) not in (float, int) or not math.isfinite(self.deadline):
            raise ValueError("invalid_deadline")

    def to_mapping(self) -> dict:
        return project(self)


class Action(str, Enum):
    ADVISE = "advise"
    CONTINUE = "continue"
    UPDATE_DEPENDENCIES = "update_dependencies"
    CONTEST_CLAIM = "contest_dependent_claim"
    RANK_CANDIDATES = "rank_candidates"
    SELECT_WINDOWS = "select_windows"
    EVALUATE_RELATION = "evaluate_relation"
    SELECT_TOOLS = "select_tools"
    SELECT_SKILLS = "select_skills"
    CANCEL_CHILD = "cancel_child"
    REPRIORITIZE_CHILD = "reprioritize_child"
    RETAIN_RESULT = "retain_result"
    DELIVER_FINDING = "deliver_finding"
    FINAL_BOUNDED_VIEW = "final_bounded_view"
    DELIVER_FINAL = "deliver_final"
    SUPPRESS_STATUS = "suppress_status"
    PRESENT_STATUS = "present_status"
    CLARIFY_DEFAULT = "clarify_default"
    CLARIFY_RETRIEVE = "clarify_retrieve"
    CLARIFY_ASK = "clarify_ask"
    REUSE_RECEIPT = "reuse_receipt"
    REUSE_CANDIDATE = "reuse_candidate"
    EXPAND_ONE_OWNED_REF = "expand_one_owned_ref"


@dataclass(frozen=True)
class InterventionProposalV1:
    proposal_id: str
    plugin_generation: str
    feature_id: str
    incident_id: str
    expected: Revision
    owner: str
    target_id: str
    action: Action
    evidence_refs: tuple[str, ...]
    expires_at_monotonic: float
    template_id: str | None = None
    template_args: tuple[tuple[str, str], ...] = ()
    authority_class: str = "advisory"
    candidate_ids: tuple[str, ...] = ()
    relation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        object.__setattr__(self, "template_args", tuple(tuple(x) for x in self.template_args))
        if not isinstance(self.expected, Revision):
            raise ValueError("invalid_revision")
        if type(self.expires_at_monotonic) not in (int, float) or not math.isfinite(self.expires_at_monotonic):
            raise ValueError("invalid_deadline")
        for name in ("proposal_id", "plugin_generation", "feature_id", "incident_id", "owner", "target_id"):
            if type(getattr(self, name)) is not str or not 0 < len(getattr(self, name)) <= 256:
                raise ValueError("invalid_identifier")
        if len(self.evidence_refs) > 64 or len(self.candidate_ids) > 32:
            raise ValueError("too_many_refs")
        if any(type(x) is not str or not 0 < len(x) <= 256 for x in (*self.evidence_refs, *self.candidate_ids)):
            raise ValueError("invalid_ref")
        metadata = bounded_metadata(self.metadata)
        for key in ("selected_ids", "candidate_ids"):
            if key in metadata and (not isinstance(metadata[key], tuple) or metadata[key] != self.candidate_ids):
                raise ValueError("metadata_selection_mismatch")
        if "relation" in metadata and metadata["relation"] != self.relation:
            raise ValueError("metadata_relation_mismatch")
        if "feature_action" in metadata and (type(metadata["feature_action"]) is not str or
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", metadata["feature_action"])):
            raise ValueError("invalid_feature_descriptor")
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True)
class SettlementV1:
    proposal_id: str
    status: str
    reason: str
    watermark: int
    receipt_id: str


@dataclass(frozen=True)
class OwnerRequestV1:
    owner: str
    target_id: str
    revision: Revision
    candidates: tuple[Mapping[str, Any], ...]
    deadline: float
    required_ids: tuple[str, ...] = ()
    critical_spans: tuple[str, ...] = ()
    completeness: Completeness = Completeness()
    data_policy: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    event: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    requires_ack: bool = False
    output_contract: str | None = None

    def __post_init__(self):
        if self.output_contract not in (None, "supervision.retrieval-presentation.v1"):
            raise ValueError("invalid_output_contract")
        if not isinstance(self.facts, Mapping):
            raise ValueError("invalid_owner_facts")
        object.__setattr__(self, "facts", freeze(self.facts))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if type(self.event) is not str or len(self.event) > 128:
            raise ValueError("invalid_owner_event")
        if len(self.evidence_refs) > 64 or any(type(x) is not str or not 0 < len(x) <= 256 for x in self.evidence_refs):
            raise ValueError("invalid_owner_refs")
        object.__setattr__(self, "candidates", freeze(self.candidates))
        for name in ("required_ids", "critical_spans", "data_policy", "relations"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        ids = [c.get("id") for c in self.candidates]
        if len(ids) > 32 or any(type(i) is not str or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid_candidates")
        if type(self.deadline) not in (int, float) or not math.isfinite(self.deadline):
            raise ValueError("invalid_deadline")
        if not set(self.required_ids) <= set(ids):
            raise ValueError("missing_required_candidate")
        if any(not isinstance(c.get("excerpt", ""), str) or len(c.get("excerpt", "")) > 2400 for c in self.candidates):
            raise ValueError("invalid_excerpt")
        if any(type(s) is not str or not s or not any(s in c.get("excerpt", "") for c in self.candidates)
               for s in self.critical_spans):
            raise ValueError("unlocated_critical_span")


@dataclass(frozen=True)
class OwnerDecisionV1:
    candidate_ids: tuple[str, ...]
    relation: str | None = None
    applied: bool = False
    receipt_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    selected: bool = False

    def __post_init__(self):
        object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        object.__setattr__(self, "metadata", bounded_metadata(self.metadata))
