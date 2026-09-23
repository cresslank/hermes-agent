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
`select_windows` and `suppress_status` actions. Finite output-format defaults
are resolved locally, not dispatched as clarification judgments.

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
owner.skills.remove_own_hint(plugin_id, hint=exact_hint, registration=registration_generation)
```

Required IDs rank first. The native consumer now supplies the selected **complete
skill body**, not a recommendation to load it on another model turn. Metadata-only
hints have no content effect. `supervision_skill_presentation.SkillPresentation`
attaches a snapshot only to a newly assembled trailing user/tool message, never
scans backward to rewrite a prior user row, and preserves that exact suffix on
later request clones while its anchor remains in context. Selection retirement
only ends future eligibility; it cannot erase a previously presented body. System
bytes, role counts and canonical history remain unchanged. Compression may remove
an anchor; snapshots are not durable history and are not restored across restart.

Literal whole-message `Use skill <name>.` / `Load skill <name>.` instructions and
host-catalog `required=True` candidates resolve locally with no remote vote. Local
bodies may contain up to 65,536 characters and are admitted whole or not at all.
If any mandatory body is unavailable, optional selection cannot replace that
route or select only the convenient mandatory subset. The ordinary `skill_view`
route remains available. Plugin-qualified, pruned, disabled, quarantined, dynamic
shell/template and oversized bodies are not automatically supplied; no setup,
credential capture, rendering, or execution follows from content admission.

**Coverage limit:** remote ambiguous selection still requires the existing <=1,200
character full-detail snapshot and original 150ms budget. This does not qualify
semantic selection for ordinary larger skills. Broadening that path needs a
separately bounded provider/host content contract, not truncation or a generic
wire-limit increase. Explicit/mandatory local loading is independent of this cap.

Native assembly uses bounded lexical overlap only as an ambiguity prefilter, not
as a semantic decision. Explicit tool names and active required-tool pins bypass
optional shortlisting. Skill candidates come from the native scoped list; a
metadata shortlist must pass a second full-content detail decision under the same
deadline before selecting content for presentation. The facade negotiates
`skill_details="supervision.skill-details.v1"` and provides async
`acquire_skill_details(request)`. It queues at most one <=3-selected-ID acquisition
back to the already-waiting execution owner, compares the exact event, task,
revision, source policy and original issued deadline, and echoes that binding with
authorized complete content. The plugin performs its dependent decision; only
`phase="ready"` over content served to that registration by this owner can become
a content selection. Native reads reuse the local skill resolver's collision, quarantine,
platform and disabled-skill gates without invoking `skill_view`, preprocessing,
credential readiness/capture, or credential passthrough registration. Missing,
pruned, oversized or ambiguous bodies stay baseline. Plugin-qualified skills
remain unsupported by this local-only detail reader. Catalogs without separate
exclusion metadata are explicitly labeled unknown, not assigned invented exclusions.

### Native optional-hint retirement

A ready hint belongs to the actual plugin **and registration generation**, not the
shared `native_views` adapter. Its fresh hint ID, scope, catalog revision, selected
skill ID and already-qualified <=1,200-character detail snapshot are retained by
the host. Ranking/hint installation occurs under the same settlement fences as
removal, before an applied receipt is emitted.

The ordinary phase producer is a **committed native `todo_list` write**. When a
ready hint is created, it can bind only a nonempty native plan that was itself
committed under the same accepted task/run/instruction scope (<=8 steps and a
complete <=1,200-character serialized snapshot). An old task's retained TodoStore
is not new-task authority. Later, the same plan must move from pending/in-progress
to all completed with every ID, description, parent and order unchanged. Deleted,
cancelled, added, rewritten or still-unfinished steps do not authorize retirement.
This transition describes the enumerated plan only, **not global completion**.

Before the completed batch advances evidence revision, the host emits one scoped
`task_revision` / `stage=unload` observation to the exact owning registration. It
supplies the original task, selected skill snapshot, full completed plan and
explicitly scoped phase facts under the existing task-text and field/source egress
grants. No grant means no remote request. The original next-round shared 150 ms
window covers this decision and all subsequent owners; it is not renewed afterward.
The unchanged feature's `remaining` decision may abstain or retain the hint.

Only `feature_action=remove_own_hint` with that exact hint ID and task reference
can consume the proposal. The host rechecks the live plan, plugin/registration,
revision, catalog, hint object, generation, deadline and mandatory/must-keep/
safety-or-cleanup vetoes under the owner fences. Its sole effect is deleting that
one optional future selection. Other plugins' hints, ranked IDs, presented bodies,
requirements and source history remain unchanged. A new identical-looking hint
is a different object and cannot be removed by an old proposal. Ordinary scope
reset/unload remains lifecycle cleanup, never an applied F12 removal receipt.

Skill eligibility is separate from the per-tool evidence revision. Within the
same accepted profile/lineage/work/run/instruction/requirements scope, ordinary
request assembly preserves the skill view and its last catalog attempt, including
successful retirement. Completing the phase or rewriting identical completed
rows cannot issue another metadata/detail judgment or recreate its suggestion.
A committed changed plan declaration, reopening terminal work, a newly accepted
task/instruction, or a changed native catalog can reopen selection. This is a
bounded current-scope identity, not a global skill blacklist. Unknown/uncommitted
plans cannot reopen selection as a native phase. Registration unload revokes only
that registration's exact owned hint, not another owner's suggestion.

Admission, selection and settlement use the existing SessionDB receipt writer.
If the final receipt cannot persist, the reversible skill-view change is restored;
no new hint or retirement is exposed as a successful effect. No schema or separate
retirement database is introduced.

Empty, unbound, oversized or changed plans retain baseline. This path does not
infer completed work from prose, force skill loading, inspect credential readiness,
create a user turn, or perform an every-turn semantic check. Required focused
skills and historical safety/cleanup instructions are never owned by this feature.

## Result and retrieval owners

The canonical result insertion is `tool_executor._commit_tool_result`, shared by
sequential/concurrent dispatch and direct, deferred, inline, execute-code and
connector outer results. `canonical_result_view` runs once after normal spillover
persistence and before subdirectory hints, model wrapping and transcript append.
There are no per-dispatch duplicate transforms.

Supported JSON envelopes have exactly one textual `output`, `stdout` or `content`
field. At most eight complete line-aligned blocks, each at most 1,200 characters,
are selectable. Critical failure/approval/partial/mutation/cleanup/receipt,
supervisor-control, obligation and provenance blocks and neighbors remain mandatory.
Every sibling field, including the complete native `supervisor_control` envelope, is
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
UI presentation. Unauthorized, secret, approval, stale and multiselect
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
presentation alternatives define a deterministic contract. No remote call,
remaining inference budget or plugin grant is needed to honor an accepted default.
The choices must be exactly the three finite values. Other questions retain the
original clarification UI. `{"output_format":"ask"}` explicitly leaves the
choice with the user and continues into that original question/UI. `{"output_format_ref":"<accepted-message-id>"}` names exactly one
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
these native consumers, including positive F12/F13/F16/F18 decisions. F19 no
longer dispatches remotely. `test_supervision_direct_skills.py` and
`test_supervision_direct_presentation.py` qualify full content, local larger
bodies, mandatory-route preservation, stable request prefixes, supervisor-control
retention and deterministic finite format defaults. Supply
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

The native integration tests use the unaltered standalone `NativeHostBridge`,
including negotiated material presentation. Clarification retrieve/ask now
uses the native finite source contract rather than remote action codecs.
No translation subclass supplies missing behavior. Explicit older-host capability
controls retain the original UI, rather than manufacturing a successful owner
receipt. The standalone installed-entry suite separately qualifies all four
optional view/receipt codecs with real native consumers and mock HTTP. These
proofs do not establish general clarification coverage or live-provider quality.
