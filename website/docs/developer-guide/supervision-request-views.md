# Supervision request and presentation owners

These are opt-in host owners. They do not register tools, grant permissions, read
credentials, invoke a provider, or replace generic request middleware. Hosts without
a negotiated supervisor keep their existing behavior.

## Binding and decision protocol

`runtime_for_agent(create=True)` attaches `NativeViewsBinding` and its actual
`SupervisionViews` when a supervisor is registered for the active profile. Accepted
instruction changes and turn binding reset the revision-bound views; finish,
revocation and plugin unload detach stale presentation closures. The immutable
scope covers profile, lineage, work/run, instruction, requirements, evidence,
catalog and control revisions. No plugin-produced authorization flags or tool
arguments populate authorized host state.

The native binding projects provider-neutral domain facts from accepted user
sources, locally authorized catalogs, persisted results and producer-marked optional
progress. Observations use the existing runtime worker registration, explicit host
egress policy and proposal queue. Execution owners settle synchronous selections;
status proposals only schedule settlement on the existing presentation dispatcher.
Core imports no plugin types. The evidence owner's typed request protocol remains
separate; this adapter handles the existing native `select_tools`, `select_skills`,
`select_windows`, `suppress_status` and `clarify_default` actions.

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

Native `cycle_deadline()` delegates to `SupervisionRuntime.shared_deadline()`:
one absolute monotonic deadline, 150 ms from the original round issuance. Request
views cannot renew it. Output, clarification, status and dependent skill-detail
selection share it with the other runtime owners. Runtime turn/completed-batch
boundaries alone open a new round. The standalone `SupervisionViews` protocol
retains its local-cycle fallback for non-native embedders.

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

Native assembly uses bounded lexical overlap only as an ambiguity prefilter, not
as a semantic decision. Explicit tool names and active required-tool pins bypass
optional shortlisting. Skill candidates come from the native scoped list; a
metadata shortlist must pass a second full-content detail decision under the same
deadline before producing a hint. The facade negotiates
`skill_details="supervision.skill-details.v1"` and provides async
`acquire_skill_details(request)`. It queues at most one <=3-selected-ID acquisition
back to the already-waiting execution owner, compares the exact event, task,
revision, source policy and original issued deadline, and echoes that binding with
authorized complete content. The plugin performs its dependent decision; only
`phase="ready"` over content served to that registration by this owner can become
a hint. Native reads reuse the local skill resolver's collision, quarantine,
platform and disabled-skill gates without invoking `skill_view`, preprocessing,
credential readiness/capture, or credential passthrough registration. Missing,
pruned, oversized or ambiguous bodies stay baseline. Plugin-qualified skills
remain unsupported by this local-only detail reader. Catalogs without separate
exclusion metadata are explicitly labeled unknown, not assigned invented exclusions.

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
spillover path translator **before selection is dispatched**, using a
content-fingerprinted artifact name; persistence failure returns baseline without
inference. Result-reference guardrails retain the full artifact path.
Opaque, multimodal, huge-line and larger/incomplete pools retain existing behavior.

The selection protocol's `source_ref` is an opaque, fresh `result:<uuid>` key, not
that filesystem path. The result owner retains at most 32 in-flight mappings to
immutable records containing the exact original string, full translated archive
path, tool/call identity, scope and request revision. Only this owner can resolve
the key; the native consumer requires that same retained record at observation
and settlement, plus exact proposal evidence and metadata references. Existing
profile/revision, owner/target, registration and deadline fences still apply.
The mapping is removed in `finally` after each decision and cleared on reset or
unload. Old keys cannot be reused even for identical output. A key or checksum
alone grants no authority, and no global filesystem resolver is exposed.

The host's 256-character proposal-reference limit is unchanged. Arbitrarily long
supported archive paths remain intact in `supervision_view.full_output_ref` and
the readback guardrails; they are never truncated or passed as bounded evidence
identifiers. This uses the existing standalone F16 opaque-reference contract and
requires no plugin codec change. It does not extend the spillover store's existing
retention or remote-path verification guarantees.

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
CLI binds its prompt-toolkit application loop, gateway binds its turn loop, and
WebSocket-backed TUI binds its transport loop. Stdio/headless surfaces without an
existing dispatcher retain immediate baseline presentation; no substitute thread,
user turn or wake is created. Reset detaches old closures synchronously and posts
cleanup of that captured set, so delayed teardown cannot erase a new scope's notices.

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

The native accepted-input source currently recognizes only an explicit presentation
contract as the entire accepted user message (not an embedded or quoted example),
never an action/approval/secret default:

````text
```hermes-defaults-v1
{"output_format":"markdown"}
```
````

`markdown`, `plain_text`, and `json` are the finite values for the literal question
`Output format?`. The admitted source ID, exact source text, scope and reversible
presentation alternatives accompany the semantic decision. Other questions retain
the original clarification UI. `{"output_format":"ask"}` explicitly leaves the
choice with the user; a supported `ask_material` action admits only that original
question/UI. `{"output_format_ref":"<accepted-message-id>"}` names exactly one
already accepted, same-work source containing the format contract. The retrieval
owner pins and rechecks its bytes and resolves only the finite format value. It
cannot search files, query history, access another work's source, or authorize a
generated source reference. Source eviction/change or uncertainty keeps the UI.
These contracts must occupy the entire authenticated message; tool arguments and
quoted examples cannot authorize either route.

## Verification boundary

`tests/agent/test_supervision_views.py` exercises native request assembly and real
OpenAI/Responses/Anthropic conversion with cache decoration on/off, canonical tool
settlement, actual scoped deferred discovery/dispatch, delayed uncached decisions,
archive recovery, adversarial candidate mutation and the real inline clarify path.
`tests/agent/test_supervision_status.py` drives the actual three presentation paths,
worker-to-owner dispatch, a real asyncio loop, profile queue capacity, deadline
slips, reset/unload, warning policy and liveness bookkeeping. No live inference,
configuration changes, network credentials or production activation are part of
these tests.

`tests/agent/test_supervision_native_views.py` additionally runs the real standalone
plugin registry and `NativeHostBridge` with a strict fake HTTP transport through
these native consumers, including positive F12/F13/F16/F18/F19 decisions. Supply
`JEV_SUPERVISOR_SOURCE`, or for the clean-environment canonical runner place the
reviewed source checkout path in the disposable test HOME's
`.hermes/jev-supervisor-test-source`. Without that explicit dependency this optional
cross-repository suite is skipped, not counted as integration proof. No runtime
owner is monkeypatched to manufacture a successful proposal. F16 cases cover
ordinary and deliberately long archive roots through sequential execute-code,
deferred tool-call and concurrent search consumers, exact UTF-8 archive readback
before inference and after settlement, critical spans/neighbors and receipt
siblings. Native negative controls submit wrong/stale/overlong references,
metadata mismatches, foreign profile/owner/revision, reset, expiry and unload;
all-block selections keep the ordinary baseline. The path-length controls must
also be run under short and long disposable runner roots, since a test's own
ordinary path can exceed 256 characters under a nested CI workspace.

The native adapter intentionally leaves unsupported domains and uncertain evidence
at baseline. The facade advertises `view_actions_version="supervision.view-actions.v1"`
and exact mappings `present_material_once -> present_status`, `retrieve ->
clarify_retrieve`, and `ask_material -> clarify_ask`. These are dedicated native
Actions with host grants and effect owners, never aliases for generic advice or
selection. The standalone plugin must negotiate these mappings and request their
grants before using them.

Until those three standalone codecs are integrated, the explicitly labeled
`NegotiatedViewCodecBridge` contract tests add only the wire translation; they keep
the real full-registry features, strict fake HTTP, arbiter, proposal contents,
native facade and native owners. That demonstrates host effects, **not** that an
older installed plugin supports those codecs. Other integration tests use the
unaltered standalone `NativeHostBridge`. Missing codecs still retain the original
UI, rather than manufacturing a successful owner receipt.
