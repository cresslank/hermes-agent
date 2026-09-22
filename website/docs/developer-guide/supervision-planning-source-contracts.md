# Source-bound planning proposals

The optional `hermes-planning-proposals-v1` annex supplies **declarations**, not
permissions or semantic truth. It is separate from, and does not extend,
`hermes-work-map-v1`. No new model tool, model turn, background patrol, provider
call, automatic delegation, cancellation, or task-completion action is added.
The supervisor remains optional; missing inputs/grants keep ordinary behavior.

## Ordinary producer and next-request consumer

1. An existing todo or authenticated requirement designates the plan artifact.
2. The ordinary local file writer commits exact main-agent bytes.
   `record_file_owner_commit` / `record_committed_write` ->
   `SupervisionRuntime.record_artifact` -> `DependencyOwner.planning.committed`
   parses the annex in the verified-main-agent branch. Successful reads, child
   copies, provider text and successful unpinned writes cannot enroll a proposal.
3. `SupervisionRuntime.prepare_action` enrolls exact operation fingerprints before
   ordinary dispatch. `_dispatch_authorized_once` wraps the existing execution in
   `run_capture`; the native complete file-read descriptor supplies a source pin.
   `committed_batch` joins it to the full, unchanged actual returned tool row.
4. The main agent's later normal plan write partitions a settled pass's exact
   member set into accepted/rejected **selection decisions** with exact rationales.
5. `turn_request_assembly.assemble_api_request` calls `request_views` before cache
   markers/provider conversion. This is the consumer: batch facts arrive *after*
   individual tool-result drains. One eligible, still-unissued planning node is
   evaluated through the registered decision plugin and revalidated under its
   original shared deadline. A fixed lower-trust, ID-bound advisory is appended
   only to a clone of the latest tool result in this normal request. Canonical
   transcript/tool bytes, system prefix, tool schemas and F12 hint state are not
   edited. There is no synthetic user message or extra API turn.

Legacy `EfficiencyOwner` intent/pass setters cannot authorize these effects.
`delegation_built` is too late to advise about that candidate; it does nothing.
`retain_plan` is not an applied effect. Advice does not certify saved execution.

## Closed annex grammar

One fenced JSON object, exactly `{"version":1,"records":[...]}`. Duplicate/unknown
keys, nonfinite numbers, multiple blocks, duplicate identities, more than 32
records or a block over 16 KiB reject the annex. Each record has a `type` plus
exactly the following keys (no host scope, permission or timing booleans):

| Type | Keys |
|---|---|
| `delegation_candidate` | `local_id`, `todo_id`, `parent_next_todo_id`, `candidate_span`, `acceptance_refs`, `input_refs`, `dependency_todo_ids`, `operation`, `required_resource_ref` |
| `research_pass_open` | `local_id`, `gap_refs`, `completion_criteria_refs`, `source_method_ref`, `operations` |
| `research_pass_close` | `pass_id`, `member_dispositions` |
| `optional_expansion` | `local_id`, `gap_refs`, `completion_criteria_refs`, `source_method_ref`, `operation`, `consumer_refs`, `effect_policy_id`, `obligation_request` |
| `withdraw_expansion` | `expansion_id` |

An exact reference is `{source_ref, source_revision, start, end}`. Revision is the
native `supervision_context.digest` of the complete retained original text;
offsets are Python character offsets, end-exclusive. Windows must be nonempty,
complete and at most 2400 characters. Accepted criteria/gaps must identify a whole
clause enumerated from authenticated input, not a model-created paraphrase or a
substring omitting its qualifications. Candidate/input/method/rationale windows
must resolve against current retained native sources. Use another designated
artifact for candidate prose; do not try to self-hash a JSON block containing its
own hash. Accepted clauses can also be rationales.

An operation is exactly `{tool_name, arguments, route_id}`. Supported routes are
`native:read_file` and `native:delegate_task`, with the corresponding literal tool
name. Fingerprints are canonical sorted JSON over `(tool_name, arguments)`;
arguments are not normalized to manufacture a match. One pass has 1–16 unique
read operations. A close has one `{member_id, source, disposition, rationale_ref}`
per native member; `disposition` is exactly `accepted` or `rejected`. Foreign,
missing, duplicate, changed-pin, undecided or not-yet-settled members reject close.
Repeated source identity remains repeated evidence, even across different reads;
a changed selection judgment is not a newly discovered source.

Already requested `todo_list` results expose `work_map_sources.planning_records`
with bounded IDs, statuses and exact native membership. This is how the main
agent can name a pass for its later ordinary close; there is no hidden test-only
ID supplier. The inventory exposes at most the latest eight records.

## F05: actual separate-conversation resource

The candidate operation is one `delegate_task` task with exactly `goal`, `context`
and `supervision`. Goal equals `candidate_span`; context equals the ordered exact
input windows joined by a newline. No unsupported output/image/role variant is
silently equated. Acceptance has 1–2 full accepted clauses; the resource reference
must be one of them and explicitly contain the literal **separate conversation**.
This narrow resource spelling is not a general prose intent classifier.

Native `OwnedDelegationOwner.planning_preflight` shares `_launch_controls` with
actual launch; it cannot construct children, resolve credentials, reserve slots,
charge spawn budget or manufacture handles. Current profile/session, read-only
policy, closed native consumer controls, exposed read/delegation tools, input
policy, remaining native iteration budget, depth, spawn pause and read-only
one-shot budget checks all apply. Declared dependencies must be completed todos;
unknown/unresolved parent dependencies veto. This is declared readiness, not proof
that no hidden dependency exists.

`tools/delegate_context_recipe.py` supplies the **same** constructor/run recipe to
`_build_child_agent`, `_ChildRun.run_conversation` and preflight. It uses a fresh
conversation with no supplied parent history, `skip_context_files=True`,
`skip_memory=True` and a fresh iteration budget. Nonempty inherited parent prefills
veto the separate-conversation projection. The native child system prompt still
includes normal delegation instructions and may load designated workspace
AGENTS/CLAUDE/cursorrules guidance through `_build_child_system_prompt`; the
constructor flags do **not** mean a clean-room context. The descriptor preserves
that qualifier. This is not independent training, competence, unbiased reasoning
or a filesystem sandbox. Native overhead and
critical-path categories stay `unknown`; favorable flags are false. Only the
independence plus clearly-needed-resource branch can qualify this minimum.

The recommendation binds exact candidate/route IDs. Later ordinary dispatch (and
public `delegate_task` construction admission) marks matching candidates issued
before child construction; the original launch still uses ordinary controls.
No already-issued candidate is recommended or relaunched by the supervisor.

## F08: finite pass and positively optional expansion

Passes progress `open -> settled -> disposition_committed`. Partial, redacted,
truncated, opaque, failed or unchanged-dedup-stub reads are not full source
inventories. Unsupported search/LCM inventories abstain in this implementation;
serialized fields named `ledger_complete` or `accepted_evidence_ids` cannot mint
native membership. Closing must happen after full native batch settlement, not
merely after an individual tool result in the same batch.

The next expansion requires two or three comparable completed passes, exact gap
and completion-criteria refs (at most four), a current source/method ref, native
`within_native_limit` budget, and `obligation_request: "optional"`. Each pass sends
its exact bounded source windows and rationale/selection decisions, not just call
counts or IDs. An oversized complete projection abstains instead of dropping a
member. Dispositions are not truth, corpus completeness, or gap resolution.

A **separate, default-off** profile policy is required:

```yaml
supervision:
  planning:
    allow_discretionary_readonly_labels: true
```

This flag is not a worker cancellation grant. The already installed native owner
must also provide `install_owner(..., consumer_inventory=callback)`, returning a
bounded tuple of the **entire current controlled Consumer set**. The proposed
consumer refs plus the implicit parent must exactly match this positive inventory
and the live resolver. The owner must control all consumers. Missing inventory,
unknown/required obligations, result/effect/cleanup/corroboration requirements,
unaccounted consumers, absent requirement links or a different linked gap scope
veto. `Consumer.requires_corroboration` is explicit and also protects worker
cancellation. Existing installers without the inventory retain baseline behavior.

Advice changes only `proposed -> advised`; the parent still decides. A later exact
ordinary `withdraw_expansion` commit records `withdrawn_by_parent`. Neither advice
nor a low-yield answer marks work skipped/cancelled/completed or closes an F20
requirement. A later actually dispatched expansion is recorded as issued, not
prevented work. No performance savings are inferred from the advisory receipt.

## Canonical owner-record API and bounds

`DependencyOwner.planning` is the graph authority. `supervision_planning_records`
uses `supervision_receipts.writer` and the already initialized SessionDB; no
alternate database, source corpus or plugin-owned state authority exists.

- `save(runtime, rows) -> bool`: atomic, zero-busy-wait, compare-and-swap revisions;
  revision 1 creates, exact replay is idempotent, each update advances by one.
  Owner-thread/worker checks exclude semantic observer callbacks. Persistence
  failure invalidates eligibility rather than claiming a successful effect.
- `load(runtime) -> tuple[dict,...]`: exact current profile/lineage/work records;
  revoked/deleted generations return no records. Graph recovery reconstructs only
  against retained exact sources and current epoch. It never replays a tool or
  carries an old proposal into a new authenticated instruction/run.
- `current(runtime,row) -> bool`: fences cached graphs against reset, deletion,
  retention and competing revision changes at use time.
- `prune(conn, now)`: existing canonical retention transaction; closed historical
  records may expire after seven days, unresolved states are not evicted.

`supervision_owner_records` keys `(profile,lineage,work_id,record_id)` and stores a
CAS `revision`, closed `kind`, `session_id`, `status`, bounded `body_json` and
`updated_at`. Graph rows stamp the native epoch, todo revision, exact plan pin,
declaration, enrollment, members, dispositions and optional obligation snapshot.
Delegation goal/context bytes are **not duplicated** in storage: an operation
fingerprint and supervision controls bind reconstruction from original owners.
There are at most 64 combined planning/dependency nodes, 4096 owner records per
profile, 16 KiB per record, eight pending candidates and three comparable passes.
Missing/evicted dependencies invalidate advice, never imply completeness.
Session deletion/reset triggers and automatic maintenance protection include the
new family. Current immutable declaration identity is never silently rebound.

The DDL reserves `planning`, `research_pass`, `gap_obligation`, `claim_delivery`
and `correction` kinds for the common owner. **This lane writes only the first
three.** The parser API is
`supervision_context.parse_planning_proposals(text, artifact_ref=...,
designated_refs=...)`; it supplies declarations only. F22 literal-source adoption,
claim-use/correction grammar, emission receipts and the parent join are not
implemented here. No feature registry closure or all22 qualification is implied.

## Offline qualification workflow

Use the canonical host `scripts/run_tests.sh --file-retries 0` with the real
standalone plugin entry point and strict MockTransport. The planning contract
suite exercises actual file writers, complete native reads, batch settlement,
SessionDB and normal request assembly, including later public delegation and
parent withdrawal. Child provider construction/scheduler I/O is substituted; a
separate constructor test checks the shared context recipe. No inference provider
or live profile is contacted. Preserve failed-run evidence; fixing source defects
is not permission to retry unchanged timing tests to green.

Negative controls cover source/child copies, old work-map grammar, generic/missing
resources, prefills, dependencies, changed inputs, absent tools/owner/budget,
partial/unenrolled/deduplicated pass membership, bad partitions, obligations,
unknown or extra consumers, default-off policy, response ambiguity/malformed
answers/expiry, stale instructions/control/source/plugin revocation, same-batch
issuance, unavailable canonical writes, deletion/reset/retention and record caps.
The existing request-view suites qualify unchanged F12 behavior and provider/cache
assembly. These are local implementation tests, not measured latency benefits or
production activation approval.
