# Supervision request and presentation owners

These are opt-in host owners. They do not register tools, grant permissions, read
credentials, invoke a provider, or replace generic request middleware. Hosts without
a negotiated supervisor keep their existing behavior.

## Binding and decision protocol

Bind `agent._supervision_views = SupervisionViews(facade_adapter, scope=revision_key)`.
The immutable revision key must cover the owning profile, lineage, work/run,
instruction, requirements, evidence, catalog and control revisions. Call
`owner.reset(new_revision_key)` when that scope changes. Never populate host
owner state from untrusted tool arguments or plugin-produced authorization flags.

The adapter implements exactly:

```python
rank_candidates(request: dict) -> Future | dict | None
select_windows(request: dict) -> Future | dict | None
evaluate_relation(request: dict) -> Future | dict | None
```

Each method **promptly schedules** through the supervision worker and its consent,
egress, plugin-generation and policy gates. It must not perform synchronous network
work. A `concurrent.futures.Future` is the recommended result. The owner waits only
where necessary, within its remaining absolute deadline. Status presentation never
waits. There is no secondary executor or provider client in these owners.

Requests carry `domain`, `scope`, `revision`, `deadline`, and an isolated copy of
`facts`. Responses carry the exact request `revision` plus either `selected_ids`
(canonical IDs only) or `relation` (a finite domain-specific label). Unknown IDs,
stale revisions, changed scope, expiry, exceptions and uncertainty retain baseline.

`cycle_deadline()` allocates one absolute monotonic deadline, 150 ms from the first
eligible owner admission. Retrieval, output selection, clarification and optional
status share it. `request_views()` advances the cycle only after consuming the next
request view; callers must not refresh the token before each result or notice.
This supports first-seen, delayed worker results, rather than a cache-only design.

## Catalog and request assembly

`Catalog.tools(schemas, required_ids=(), rules_revision="")` snapshots already
permission-filtered schemas. `owner.propose_tools(tuple(ids), catalog_revision=...,
scope=..., include_deferred=False)` submits a request-local selection. Unknown or
stale selections restore the original view. Safety/vault/recovery/discovery pins
and host `_supervision_required_tools` remain exposed. Non-deferrable core and
surface tools are also pinned: the discovery bridge cannot invoke them.
`_supervision_rules_revision`
is included in catalog identity. Without discovery escape routes, selection only
reorders; it cannot hide other authorized tools.

For deferred local tools, `authorized_tool_schemas(agent)` uses the native scoped
`get_tool_definitions(..., skip_tool_search_assembly=True)` and `tool_describe`
validator. It never queries remote connector schemas. Construct the catalog from
that return value and submit with `include_deferred=True`. Dispatcher authorization
and registry membership are unchanged. Deferred tools remain callable through the
usual bridge, with its normal permission and schema validation.

The consumer runs in `assemble_api_request`, before provider conversion and cache
decoration. Definitions are selected verbatim, not regenerated from descriptions.
The selected set, schema and catalog revision determine a view identity included
in OpenAI/Responses cache scope. Native Anthropic cache sections are built from the
selected canonical definitions. Canonical `agent.tools` and historical messages
are not edited. This is an explicit opt-in request-view cache boundary, not a
mid-conversation toolset/configuration reload.

Skill metadata uses `SkillCandidate(id, description, required=False)` and:

```python
revision = owner.skills.catalog(tuple(candidates), scope)
owner.skills.rank(tuple(ids), revision=revision, plugin_id=plugin_id, ambiguous=False)
owner.skills.remove_own_hint(plugin_id)
```

Required IDs rank first. Only an explicitly unresolved ambiguous selection creates
one positive request-only hint. Removing a hint or ending its scope does not erase
loaded skill bodies, mandatory rules, safety instructions or transcript history.

## Result and retrieval owners

The canonical result insertion is `tool_executor._commit_tool_result`, shared by
sequential/concurrent dispatch and direct, deferred, inline, execute-code and
connector outer results. `canonical_result_view` runs once after normal spillover
persistence and before subdirectory hints, model wrapping and transcript append.
There are no per-dispatch duplicate transforms.

Supported JSON envelopes have exactly one textual `output`, `stdout` or `content`
field. At most eight complete line-aligned blocks, each at most 1,200 characters,
are selectable. Critical failure/approval/partial/mutation/cleanup/receipt blocks
and neighboring blocks remain mandatory. Every sibling status/receipt field is
retained. The selected view includes exact source offsets, omission metadata and a
full-output reference. The **original** result is persisted with the existing
spillover path translator before inline evidence is removed; persistence failure
returns baseline. Result-reference guardrails retain the full artifact path.
Opaque, multimodal, huge-line and larger/incomplete pools retain existing behavior.

`owner.rank_retrieval(tuple(candidates), required_ids=(), complete=True)` accepts
at most eight immutable owner candidates with `id`, `excerpt` (<=1,200 characters)
and `source_ref`. It reorders the selected original records, retaining the entire
unselected tail, ranks, citations and provenance. It does not search, expand scope
or turn a preference into evidence. External evidence-owner adapters remain
responsible for calling this protocol at their actual retrieval boundary.

## Optional presentation

`_emit_status_kind`, `_emit_notice` and `_emit_wait_notice` accept an optional
`OptionalUpdate` supplied by the producer. Legacy/unclassified output and mandatory
approval, safety, failure, cleanup, correction, commitment and requested-update
classes are never delayed. Existing warning policy wins. Neutral native streaming
and nonstreaming wait producers use `OptionalProgressText`; diagnostic waits clear
pending optional provider progress. `_touch_activity` runs even for hidden waits.

Install the presentation owner with:

```python
cancel = bind_status_owner(agent, owner, profile_key=immutable_profile_key,
                           dispatch=owner_dispatch, call_later=owner_one_shot)
```

Register `cancel` on profile/plugin unload and task reset **before** replacing the
owner or its callbacks. `dispatch(callback)` must post to the existing UI/owner
execution context, never invoke inline on an inference worker.
`call_later(seconds, callback)` returns a cancellation callable.
`asyncio_owner_callbacks(existing_loop)` supplies a thread-safe pair for a gateway
or TUI event loop, with absolute-deadline preservation even when timer arming slips.
CLI adapters supply their existing UI pump dispatcher. Missing dispatch/scheduler
means normal baseline presentation; it must not start a substitute thread or turn.

There are at most 32 pending slots **per profile**, shared across adapter instances,
and one replaceable slot per scope-qualified subject. Exact duplicates are local.
Semantic decisions compare the exact last delivered update, not a fingerprint
alone. A ready validated progress-only/duplicate result may suppress; uncertainty
and expiry run the retained original presentation closure once. Completion handlers
only post to the owner. Network, cancellation and UI callbacks run outside locks.
`clear_optional`, `reset` and `close` cancel outstanding callbacks; current-item,
scope and generation checks reject obsolete completions. This ephemeral queue does
not replace durable completion/finding admission.

## Clarification

Only the trusted action owner may install:

```python
owner.defaults[question] = AuthorizedDefault(
    question, value, scope, evidence_ref, low_stakes=True, authorized=True)
```

The inline clarify executor passes the owner into the actual `clarify_tool` before
UI presentation. Material, unauthorized, secret, approval, stale and multiselect
cases retain existing UI behavior. A batch is suppressed only if every question
independently has an admitted default. The output distinguishes `resolved_value`
with `resolution="authorized_default"` from `user_response`; it never fabricates a
user answer. The model-visible clarify schema cannot create these grants.

## Verification boundary

`tests/agent/test_supervision_views.py` exercises native request assembly and real
OpenAI/Responses/Anthropic conversion with cache decoration on/off, canonical tool
settlement, actual scoped deferred discovery/dispatch, delayed uncached decisions,
archive recovery, adversarial candidate mutation and the real inline clarify path.
`tests/agent/test_supervision_status.py` drives the actual three presentation paths,
worker-to-owner dispatch, a real asyncio loop, profile queue capacity, deadline
slips, reset/unload, warning policy and liveness bookkeeping. No live inference,
configuration changes, network credentials or production activation are part of
these tests. A host must finish binding its negotiated facade and UI lifecycle
callbacks before claiming the optional features are active.
