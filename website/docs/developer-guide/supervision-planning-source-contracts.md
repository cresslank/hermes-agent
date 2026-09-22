# Delegation and research source-contract gaps

This is a pinned-source gap report, **not** an F05/F08 implementation or acceptance
claim. Host inspected: `b7634ad345b03af934a9b8da466ed8b105020269`; standalone
inspected: `3e0b510f0b9164b7240744610177063ad7ac79d9`. Later integration may supply
the missing owner joins. No production code is changed by this report.

## F05: an authorized launch is not a planner value decision

The ordinary path is `tools/delegate_tool.py::delegate_task` ->
`_normalize_task_list` / `_coerce_task_schemas` -> `_build_children` ->
`owned_delegation.register_launch` ->
`supervision_efficiency.observe_native(..., "delegation_built", ...)` ->
`EfficiencyOwner.delegation_built`, followed by `_run_batch`.

The native call reaches the efficiency owner, but stops at its lookup of
`self.delegations[goal]`. The only supplier of that ledger is
`EfficiencyOwner.admit_delegation`; the pinned tree has no ordinary production
caller. The existing positive test seeds that method before constructing a child.
That test proves adapter/consumer mechanics, not ordinary supplier closure.

The available sources are narrower than the requested facts:

- `delegate_task` supplies goal, context, optional output JSON schema, role and
  actual routing/configuration. A schema is a result-shape constraint, not all
  semantic acceptance criteria; a context string is not a validated dependency
  declaration.
- `owned_delegation.SUPERVISION_SCHEMA` has exactly `obligation`, `consumer_refs`,
  `consumer_set_closed`, `effect_policy_id`. The owner validates consumer and
  effect authority. Optional read-only authority does not certify parent-input
  readiness, a specialist advantage, useful parallelism or favorable overhead.
- Children inherit the parent's tools. A model/provider route identity is not a
  competence claim. Available concurrency and a configured iteration ceiling
  are capacity bounds, not a comparison of parent critical path against launch
  and integration overhead.
- The call site occurs after actual child construction/launch registration. There
  is no retained, unissued planner-candidate selection at this boundary. Merely
  attaching a recommendation to the next parent request would recommend a child
  that has already been submitted. Suppressing or replaying that launch would
  change native execution semantics, not implement an advisory.

**Smallest missing join:** a concrete, still-unissued candidate in the existing
planner owner, bound to a current work-map/todo revision, source-linked acceptance
and input declarations, and the relations owner's dependency/readiness view;
plus a native route-capability and overhead/critical-path policy projection.
Evaluate and consume its recommendation at the next existing parent planning
safe point, before normal `delegate_task` authorization. Do not create or launch
another task. An explicit owner-local proposal format can carry the declarations;
it cannot turn its own `overhead_favorable` / `parallelism_favorable` booleans into
host computations or confer route permission.

`retain_plan` is intentionally **not added**. The normative no-qualifying-candidate
case is A0: keep the parent's plan and dispatch behavior unchanged. There is no
native candidate-plan transition here for an additional applied-effect receipt
to represent. A new ADVISE alias would not make that no-op a real effect.

## F08: candidate receipts are not an evidence-disposition/pass ledger

The ordinary path is the tool executor's `_finalize_tool_batch` ->
`SupervisionRuntime.committed_batch` -> `completed_batch_facts`. It retains exact
serialized tool-result receipts and source-linked finding **candidates**.
`EffectReceiptV1` outcomes/status remain unknown for these retrieval results.
Main-agent verified artifact writes prove committed bytes, not research quality
or acceptance of a cited source.

`EfficiencyOwner.commit_research_pass` and `propose_expansion` have no production
callers at the inspected base. Their existing positive test directly supplies two
empty `ResearchPass` objects and an optional `ResearchGap`. Membership of a ref in
`runtime.evidence` proves a retained receipt exists; it does not prove accepted or
rejected evidence, a complete pass inventory, comparable gap scope, or novelty
relative to a previous pass. Two search calls are not two comparable research
passes. Provider-returned keys named `accepted_evidence_ids`, `mandatory_gap`, or
`ledger_complete` remain untrusted result content.

The pinned work-map parser admits only root keys `version`, `requirements`,
`steps`, `claims`, and step keys `todo_id`, `requirement_indexes`, `target_refs`.
It supplies exact requirement links but no source disposition, pass identity,
optional-gap authority or next-expansion proposal. A singleton requirement list
cannot supply any of those missing associations. No relation/dependency owner
implementation providing those projections exists in this pinned host tree.

**Smallest missing join:** the source/relations owner must expose a bounded,
revision-bound research decision projection with:

1. An actual committed pass ID and enumerated candidate membership, with distinct
   accepted/rejected source receipts (and exact relevant source spans), preserving
   whether the disposition is main-agent acceptance rather than verified truth.
2. The exact unresolved requirement/gap IDs for that pass, gap-state revision,
   mandatory/corroboration obligations and completion criteria. Unknown
   obligation must veto the optional-stop recommendation.
3. A still-unissued optional expansion ID and source/method, owned by the planner,
   with a native budget class and a fresh currentness predicate.

Efficiency can retain the last three such pass projections, compare at least two
for the same gap scope, and consume one ID-bound stop-optional-expansion advisory
at the existing parent safe point. It must neither close requirements nor declare
the task complete. Any actual removal of optional queued work additionally needs
the queue owner's explicit disposition transition; generic ADVISE rendering is
not evidence of stopped execution. Keep the dependency graph and authoritative
dispositions in the relations owner, and durable receipts in the common host
storage owner; do not introduce parallel stores in efficiency.

A separate versioned owner-local proposal block is compatible in principle, and
is **not** rejected merely because work-map-v1 lacks these fields. What is missing
at this base is the owner projection that validates/binds its facts. Shipping a
parser and test-only setter without that join would repeat the existing gap.

## Executed boundary controls

`tests/agent/test_supervision_planning_sources.py` adds nine negative controls
using the installed full Jev registry and strict MockTransport fixture:

- Two public `delegate_task` cases (with/without an output schema) traverse real
  normalization, SQLite launch/control admission and `_build_children`, and reach
  the scheduler boundary unchanged. Child model construction, credentials,
  presentation and scheduler I/O are substituted; no real child execution is
  claimed. Goal/context prose does not produce an F05 value recommendation.
- Two committed result batches for each of `web_search`, `web_extract`,
  `read_file` retain two native receipts without minting accepted research passes,
  even when result content forges disposition/completion fields. No F08 call,
  optional-stop hint, requirement closure or task-complete transition occurs.
- Four actual committed work-map writes reject undeclared acceptance/dependency/
  evidence/expansion fields instead of silently widening work-map-v1.

These controls protect honest abstention. They do not satisfy the requested
ordinary F05/F08 positive feature traces. Those remain blocked on the joins above.
